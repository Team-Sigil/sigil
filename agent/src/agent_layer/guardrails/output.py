"""Output guardrails: what the agent says, and whether it matches what it did.

* ``check_reply`` – the dependent must not see the guardian's private guidance, wallet
  addresses or anything secret-looking; long replies are capped. A failing reply is
  replaced by a safe template rather than edited.
* ``reconcile_outcome`` – the model's claimed outcome is replaced by the truth when
  they disagree (e.g. it says "sent" but the engine blocked the transfer).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_layer.contracts import Outcome
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.guardrails.input import ADDRESS_LIKE
from agent_layer.i18n import DEFAULT_LOCALE, Locale, t
from agent_layer.money import to_display

_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bseed phrase\b|助记词|助記詞)", re.I)
_WORD = re.compile(r"[a-z0-9']+")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# A run of this many consecutive guidance words appearing in a reply counts as a leak.
LEAK_NGRAM = 7
# For Chinese, a run of this many consecutive Han characters (punctuation ignored).
LEAK_HAN_NGRAM = 10
# Where a capped reply may be cut, so it ends at a word or clause.
_BREAKS = " 。，！？；、"


def safe_reply(outcome: Outcome, amount: int = 0, locale: Locale = DEFAULT_LOCALE) -> str:
    return t(f"reply.{outcome.value}", locale, amount=to_display(amount))


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _han_ngrams(text: str, n: int) -> set[str]:
    han = "".join(_HAN.findall(text))
    return {han[i : i + n] for i in range(len(han) - n + 1)}


def _leaks(text: str, guidance: str) -> bool:
    return bool(
        _ngrams(text, LEAK_NGRAM) & _ngrams(guidance, LEAK_NGRAM)
        or _han_ngrams(text, LEAK_HAN_NGRAM) & _han_ngrams(guidance, LEAK_HAN_NGRAM)
    )


def screen_text(text: str, *, guidance: str, max_chars: int, log: GuardrailLog) -> str | None:
    """Return the text (capped to ``max_chars``), or None if it must not be shown."""
    problems: list[str] = []
    if ADDRESS_LIKE.search(text):
        problems.append("address")
    if _SECRET.search(text):
        problems.append("secret")
    if guidance and _leaks(text, guidance):
        problems.append("guidance_leak")
    if problems:
        log.add("reply_replaced", ",".join(problems))
        return None
    if len(text) > max_chars:
        log.add("reply_truncated", str(len(text)))
        cut = text[:max_chars]
        at = max(cut.rfind(c) for c in _BREAKS)
        if at >= max_chars * 0.7:  # don't throw away a long tail to find a break
            cut = cut[:at]
        return cut.rstrip(" ,;:，；：、") + "…"
    return text


def check_reply(
    reply: str,
    *,
    outcome: Outcome,
    amount_sent: int,
    guidance: str,
    max_chars: int,
    log: GuardrailLog,
    locale: Locale = DEFAULT_LOCALE,
) -> str:
    screened = screen_text(reply, guidance=guidance, max_chars=max_chars, log=log)
    return screened if screened is not None else safe_reply(outcome, amount_sent, locale)


@dataclass(frozen=True)
class RunFacts:
    """What actually happened during the run, as reported by the platform API."""

    amount_sent: int
    transfer_blocked: bool
    auditor_flagged: bool
    proof_requested: bool
    guardian_asked: bool
    requested_amount: int | None


def reconcile_outcome(claimed: Outcome, facts: RunFacts, log: GuardrailLog) -> Outcome:
    """Return the outcome that matches the facts; log any correction."""
    if facts.auditor_flagged:
        truth = Outcome.AUDITOR_FLAGGED
    elif facts.amount_sent > 0:
        full = facts.requested_amount is None or facts.amount_sent >= facts.requested_amount
        if claimed in (Outcome.SENT_PARTIAL, Outcome.COUNTER_OFFER) and not full:
            truth = claimed
        else:
            truth = Outcome.SENT_FULL if full else Outcome.SENT_PARTIAL
    elif facts.transfer_blocked and claimed in (Outcome.SENT_FULL, Outcome.SENT_PARTIAL):
        truth = Outcome.BLOCKED
    elif claimed in (Outcome.SENT_FULL, Outcome.SENT_PARTIAL):
        truth = Outcome.DECLINED  # claimed money moved, but none did
    elif claimed is Outcome.ASKED_PROOF and not facts.proof_requested:
        truth = Outcome.DECLINED if not facts.guardian_asked else Outcome.ASKED_GUARDIAN
    elif claimed is Outcome.ASKED_GUARDIAN and not facts.guardian_asked:
        truth = Outcome.ASKED_PROOF if facts.proof_requested else Outcome.DECLINED
    else:
        truth = claimed
    if truth is not claimed:
        log.add("outcome_corrected", f"{claimed} -> {truth}")
    return truth
