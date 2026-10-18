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
from dataclasses import dataclass, field

from agent_layer.config import Limits
from agent_layer.contracts import AgentContext, DependentRequest, Proof
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.guardrails.input import PLATFORM_ANOMALY_SIGNAL, PlatformText, sanitize, wrap_untrusted
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


def _proof_parts(
    proofs: list[Proof], limits: Limits, log: GuardrailLog
) -> tuple[list[dict], list[str], list[str]]:
    images: list[dict] = []
    texts: list[str] = []
    signals: list[str] = []
    for proof in proofs:
        # The declared size is only a hint: the check is on the bytes actually received.
        if proof.size_bytes > limits.max_proof_bytes:
            log.add("proof_skipped", f"{proof.id} too large")
            continue
        if proof.media_type in IMAGE_TYPES and proof.data_b64:
            try:
                actual = len(base64.b64decode(proof.data_b64, validate=True))
            except (binascii.Error, ValueError):
                log.add("proof_skipped", f"{proof.id} invalid base64")
                continue
            if actual > limits.max_proof_bytes or actual != proof.size_bytes:
                log.add("proof_skipped", f"{proof.id} size mismatch (declared {proof.size_bytes}, got {actual})")
                signals.append(PLATFORM_ANOMALY_SIGNAL)
                continue
            images.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": proof.media_type, "data": proof.data_b64},
                }
            )
        elif proof.media_type in TEXT_TYPES and proof.text is not None:
            if len(proof.text.encode()) > limits.max_proof_bytes:
                log.add("proof_skipped", f"{proof.id} too large")
                continue
            s = sanitize(proof.text, max_chars=limits.max_proof_text_chars, log=log, source=f"proof {proof.id}")
            texts.append(s.text)
            signals.extend(s.signals)
        else:
            log.add("proof_skipped", f"{proof.id} unsupported type {proof.media_type}")
    return images, texts, signals


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

    images, texts, proof_signals = _proof_parts(proofs, limits, log)
    signals.extend(proof_signals)
    proof_section = "\n".join(
        wrap_untrusted("untrusted_proof", t, index=str(i)) for i, t in enumerate(texts)
    )
    if images:
        proof_section += f"\n({len(images)} image proof(s) attached above.)"

    text_block = context_block(ctx, limits, pt)
    unique_signals = sorted(set(signals + pt.signals))
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
        + f"{facts}{note}"
    )
    return PreparedInput(
        content=[*images, {"type": "text", "text": text}],
        case_text="\n".join(case_parts),
        proof_texts=texts,
        proof_images=images,
        signals=unique_signals,
    )
