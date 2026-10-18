"""Builds the model's view of a run from platform data.

Anything a dependent wrote, or that came from an uploaded file, is sanitised and
wrapped in an ``<untrusted_...>`` delimiter it cannot escape.

Numbers and enums from the platform are stated plainly. Its free text (guidance,
names, history purposes, verified guardian answers, events) is zero-trust too: capped
and escaped through ``PlatformText``, and anything that needed fixing forces a review.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field

from agent_layer.config import Limits
from agent_layer.contracts import (
    PROOF_FLAGS,
    PROOF_FLAGS_CONCERNS,
    PROOF_FLAGS_DISQUALIFYING,
    AgentContext,
    DependentRequest,
    Proof,
)
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.guardrails.input import PLATFORM_ANOMALY_SIGNAL, PlatformText, sanitize, wrap_untrusted
from agent_layer.i18n import Locale, dependent_locale, guardian_locale, language_instruction
from agent_layer.money import to_display

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
TEXT_TYPES = {"text/plain"}


@dataclass
class PreparedInput:
    content: list[dict]  # the first user turn for the maker
    case_text: str  # the untrusted request text, for the Auditor
    proof_texts: list[str] = field(default_factory=list)
    proof_images: list[dict] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    proof_ids: list[str] = field(default_factory=list)  # proofs that count as proof
    proof_concerns: list[str] = field(default_factory=list)  # integrity concerns: force a review
    dependent_locale: Locale = "en"
    guardian_locale: Locale = "en"


def dependent_name(ctx: AgentContext, limits: Limits, pt: PlatformText) -> str:
    return pt.inline(ctx.dependent_name, max_chars=limits.max_name_chars, source="dependent_name")


def guidance_block(ctx: AgentContext, limits: Limits, pt: PlatformText) -> str:
    return pt.block(
        "guardian_guidance",
        ctx.policy.guidance or "(none given)",
        max_chars=limits.max_guidance_chars,
        source="guidance",
    )


def history_lines(ctx: AgentContext, limits: Limits, pt: PlatformText) -> str:
    return "\n".join(
        f"- {h.at:%a %d %b}: {h.kind} {to_display(h.amount)} ({h.category}) "
        + pt.inline(h.purpose, max_chars=limits.max_purpose_chars, source="history_purpose")
        for h in ctx.history[-limits.max_history_items :]
    ) or "- (none)"


def context_block(ctx: AgentContext, limits: Limits, pt: PlatformText) -> str:
    p = ctx.policy
    blocked = ", ".join(c.value for c in p.blocked_categories) or "none"
    allowance = (
        f"{to_display(p.allowance.amount)} weekly" if p.allowance else "none"
    )
    return (
        f"Dependent: {dependent_name(ctx, limits, pt)}\n"
        f"{guidance_block(ctx, limits, pt)}\n"
        f"Outer limits (enforced by code): weekly cap {to_display(p.weekly_cap)}, "
        f"max per request {to_display(p.max_per_request)}, proof needed from "
        f"{to_display(p.proof_threshold)}, blocked categories: {blocked}, "
        f"regular allowance: {allowance}.\n"
        f"This week: spent {to_display(ctx.budget.spent_this_week)}, "
        f"left {to_display(ctx.budget.remaining_this_week)}. "
        f"Reservoir: {to_display(ctx.delegated_amount)}."
        + (" TRANSFERS ARE FROZEN BY THE GUARDIAN." if ctx.frozen else "")
        + f"\nRecent history:\n{history_lines(ctx, limits, pt)}"
    )


def _taken_in_app(proof: Proof, is_image: bool, by_id: dict[str, Proof], request: DependentRequest) -> bool:
    """A verified in-app capture, or text the platform read from one on this request."""
    if is_image:
        return proof.source == "photo"
    origin = by_id.get(proof.derived_from or "")
    return (
        proof.source == "ocr" and origin is not None and origin.source == "photo"
        and origin.request_id == request.id and not set(origin.flags) & PROOF_FLAGS_DISQUALIFYING
    )


@dataclass
class ProofView:
    images: list[dict] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)  # sanitised, one per shown text proof
    notes: list[str] = field(default_factory=list)  # code-written notes about the proofs
    signals: list[str] = field(default_factory=list)  # injection or tampering signals
    concerns: list[str] = field(default_factory=list)  # integrity concerns: force a review
    qualifying_ids: list[str] = field(default_factory=list)  # proofs that count as proof


def _proof_parts(proofs: list[Proof], request: DependentRequest, limits: Limits, log: GuardrailLog) -> ProofView:
    """Check each proof and decide whether it is shown, and whether it counts as proof.

    It counts only if it belongs to this request, its bytes match the platform's hash,
    it is an image or text the platform extracted from one (never typed), and it is not
    a duplicate or AI-generated. With ``camera_only_proofs``, it must also be a verified
    in-app capture (or text read from one). Everything else is shown as context at most.
    """
    view = ProofView()
    by_id = {p.id: p for p in proofs}
    for proof in proofs:
        label = f"proof {proof.id}"
        # Pairing: the platform must say which request the proof was uploaded for.
        if (proof.request_id, proof.dependent_id) == (None, None):
            log.add("proof_unpaired", proof.id)
            paired = False
        elif proof.request_id != request.id or proof.dependent_id != request.dependent_id:
            log.add("proof_skipped", f"{proof.id} belongs to another request")
            view.signals.append(PLATFORM_ANOMALY_SIGNAL)
            continue
        else:
            paired = True

        # The declared size is only a hint: the check is on the bytes actually received.
        if proof.size_bytes > limits.max_proof_bytes:
            log.add("proof_skipped", f"{proof.id} too large")
            continue
        if proof.media_type in IMAGE_TYPES and proof.data_b64:
            try:
                raw = base64.b64decode(proof.data_b64, validate=True)
            except (binascii.Error, ValueError):
                log.add("proof_skipped", f"{proof.id} invalid base64")
                continue
            if len(raw) > limits.max_proof_bytes or len(raw) != proof.size_bytes:
                log.add("proof_skipped", f"{proof.id} size mismatch (declared {proof.size_bytes}, got {len(raw)})")
                view.signals.append(PLATFORM_ANOMALY_SIGNAL)
                continue
            is_image = True
        elif proof.media_type in TEXT_TYPES and proof.text is not None:
            raw = proof.text.encode()
            if len(raw) > limits.max_proof_bytes:
                log.add("proof_skipped", f"{proof.id} too large")
                continue
            is_image = False
        else:
            log.add("proof_skipped", f"{proof.id} unsupported type {proof.media_type}")
            continue
        if proof.sha256 is not None and hashlib.sha256(raw).hexdigest() != proof.sha256:
            log.add("proof_skipped", f"{proof.id} hash mismatch")
            view.signals.append(PLATFORM_ANOMALY_SIGNAL)
            continue

        index = str(len(view.texts) if not is_image else len(view.images))
        if is_image:
            view.images.append(
                {"type": "image", "source": {"type": "base64", "media_type": proof.media_type, "data": proof.data_b64}}
            )
        else:
            s = sanitize(proof.text, max_chars=limits.max_proof_text_chars, log=log, source=label)
            view.texts.append(s.text)
            view.signals.extend(s.signals)

        flags = sorted(f for f in proof.flags if f in PROOF_FLAGS)
        if len(flags) < len(proof.flags):
            log.add("proof_flags_ignored", proof.id)
        kind = "image" if is_image else "text"
        concerns = [f for f in flags if f in PROOF_FLAGS_CONCERNS]
        if concerns:
            view.concerns.extend(f"proof_{f}" for f in concerns)
            log.add("proof_flagged", f"{proof.id}: {','.join(concerns)}")
            view.notes.append(f"The platform flagged {kind} proof {index}: {', '.join(concerns)}.")
        if "camera_signed" in flags:
            view.notes.append(
                f"Image proof {index} carries a valid camera signature: it shows where the photo was "
                "taken, not that the receipt is genuine."
            )
        typed = proof.source == "typed" or (not is_image and proof.source != "ocr")
        camera_ok = _taken_in_app(proof, is_image, by_id, request) if limits.camera_only_proofs else True
        if typed:
            view.notes.append(
                f"Text proof {index} was typed by the dependent, not extracted from a document: "
                "treat it as their claim, not as evidence."
            )
        elif not paired:
            view.notes.append(f"The platform didn't confirm {kind} proof {index} was uploaded for this request.")
        elif not camera_ok:
            log.add("proof_not_camera", proof.id)
            view.notes.append(
                f"{kind.capitalize()} proof {index} wasn't taken with the app's camera, so it can't be used as evidence."
            )
        elif is_image and proof.source == "photo":
            view.notes.append(f"Image proof {index} was taken with the app's camera and verified.")
        if paired and not typed and camera_ok and not set(flags) & PROOF_FLAGS_DISQUALIFYING:
            view.qualifying_ids.append(proof.id)
    return view


def prepare_request_input(
    ctx: AgentContext,
    request: DependentRequest,
    proofs: list[Proof],
    limits: Limits,
    log: GuardrailLog,
) -> PreparedInput:
    pt = PlatformText(log)
    first = sanitize(request.message, max_chars=limits.max_message_chars, log=log, source="request")
    signals = list(first.signals)
    case_parts = [wrap_untrusted("untrusted_dependent_message", first.text)]

    thread_lines: list[str] = []
    thread = request.thread[-limits.max_thread_messages :]
    if len(request.thread) > len(thread):
        log.add("thread_truncated", str(len(request.thread)))
    for msg in thread:
        if msg.role == "dependent":
            s = sanitize(msg.text, max_chars=limits.max_message_chars, log=log, source="thread")
            signals.extend(s.signals)
            wrapped = wrap_untrusted("untrusted_dependent_message", s.text)
            thread_lines.append(wrapped)
            case_parts.append(wrapped)
        elif msg.role == "guardian_answer":
            thread_lines.append(pt.block(
                "verified_guardian_answer", msg.text,
                max_chars=limits.max_guardian_answer_chars, source="guardian_answer",
            ))
        elif msg.role == "agent":
            thread_lines.append(pt.block(
                "your_earlier_reply", msg.text, max_chars=limits.max_reply_chars * 2, source="agent_reply",
            ))
        else:
            thread_lines.append(pt.block(
                "system_event", msg.text, max_chars=limits.max_event_chars, source="system_event",
            ))

    pv = _proof_parts(proofs, request, limits, log)
    images, texts = pv.images, pv.texts
    signals.extend(pv.signals)
    proof_section = "\n".join(
        wrap_untrusted("untrusted_proof", t, index=str(i)) for i, t in enumerate(texts)
    )
    if images:
        proof_section += f"\n({len(images)} image proof(s) attached above.)"
    if pv.notes:
        proof_section += "\n" + "\n".join(pv.notes)
        case_parts.append("Notes on the proof: " + " ".join(pv.notes))
    if proofs and not pv.qualifying_ids:
        proof_section += "\nNone of the proofs can be used as evidence, so no amount that needs proof can be sent."

    text_block = context_block(ctx, limits, pt)
    unique_signals = sorted(set(signals + pt.signals))
    dep_locale, guard_locale = dependent_locale(ctx, request), guardian_locale(ctx)
    facts = (
        f"Amount requested: {to_display(request.amount_requested) if request.amount_requested else 'not stated'}. "
        f"Already sent for this request: {to_display(request.already_sent)}. "
        f"Emergency flagged by the app: {'yes' if request.emergency_claimed else 'no'}."
    )
    note = (
        f"\nNote from the app's input filter: possible manipulation signals ({', '.join(unique_signals)}). "
        "Weigh the request on its merits; these signals mean extra care, not automatic refusal."
        if unique_signals
        else ""
    )
    text = (
        f"{text_block}\n\n"
        f"New money request.\n"
        f"{wrap_untrusted('untrusted_dependent_message', first.text)}\n"
        + (f"Conversation since then:\n" + "\n".join(thread_lines) + "\n" if thread_lines else "")
        + (f"Proof:\n{proof_section}\n" if proof_section else "")
        + f"{facts}{note}\n{language_instruction(dep_locale, guard_locale)}"
    )
    return PreparedInput(
        content=[*images, {"type": "text", "text": text}],
        case_text="\n".join(case_parts),
        proof_texts=texts,
        proof_images=images,
        signals=unique_signals,
        proof_ids=pv.qualifying_ids,
        proof_concerns=sorted(set(pv.concerns)),
        dependent_locale=dep_locale,
        guardian_locale=guard_locale,
    )
