"""Input guardrails: everything a dependent writes, and every proof, is untrusted data.

Untrusted text is normalised, length-capped, stripped of hidden characters and
escaped so it cannot close the delimiter tags it is wrapped in. Injection *signals*
are detected and reported; they never block a request (false positives would punish
honest dependents) but they force an Auditor review and are shown to the model.

Text from the platform is not trusted either, even when it is signed policy or written
by the engine (zero trust: a compromised or buggy platform must not be able to steer
the model). ``PlatformText`` gives it the same treatment, and reports anything that
needed fixing as an anomaly, which forces an Auditor review.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

from agent_layer.guardrails.events import GuardrailLog

# Zero-width, bidi-override and other format characters used to hide text.
_HIDDEN = re.compile(r"[​-‏‪-‮⁠-⁤⁦-⁩﻿­]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_SIGNALS: dict[str, re.Pattern[str]] = {
    "instruction_override": re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|rule|prompt|polic|limit|guidance)",
        re.I | re.S,
    ),
    "role_play": re.compile(r"\b(you are now|act as|pretend (to be|you)|developer mode|jailbreak)\b", re.I),
    "system_prompt_probe": re.compile(r"\b(system prompt|your (instructions|rules|guidance)|reveal|print the)\b", re.I),
    "claimed_guardian_approval": re.compile(
        r"\b(mom|mum|dad|mother|father|parent|guardian|grandma|grandpa)\b.{0,40}"
        r"\b(said|says|approved|agreed|allowed|ok(ay)?|fine|let me)\b",
        re.I | re.S,
    ),
    "authority_claim": re.compile(r"\b(admin|administrator|developer|anthropic|system)\s*(:|says|here|message)", re.I),
    "urgency_pressure": re.compile(r"\b(emergency|urgent(ly)?|right now|immediately|asap|hospital|police|stranded)\b", re.I),
    "address_like": re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"),
}

EMERGENCY_SIGNAL = "urgency_pressure"
PLATFORM_ANOMALY_SIGNAL = "platform_text_anomaly"

# Something shaped like a markup tag, e.g. ``</guardian_guidance>``.
_MARKUP = re.compile(r"</?\s*[A-Za-z_][\w-]*[^<>]*>")


@dataclass
class SanitizedText:
    text: str
    signals: list[str] = field(default_factory=list)
    truncated: bool = False


def sanitize(raw: str, *, max_chars: int, log: GuardrailLog | None = None, source: str = "input") -> SanitizedText:
    text = unicodedata.normalize("NFKC", raw)
    signals: list[str] = []
    if _HIDDEN.search(text):
        signals.append("hidden_characters")
        text = _HIDDEN.sub("", text)
    text = _CONTROL.sub("", text)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
        if log:
            log.add("input_truncated", f"{source} > {max_chars} chars")
    signals.extend(name for name, pat in _SIGNALS.items() if pat.search(text))
    if log and signals:
        log.add("injection_signals", f"{source}: {','.join(signals)}")
    return SanitizedText(text=text, signals=signals, truncated=truncated)


def wrap_untrusted(tag: str, text: str, **attrs: str) -> str:
    """Wrap untrusted text in a delimiter it cannot escape.

    ``<``, ``>`` and ``&`` are HTML-escaped, so the text cannot contain a closing tag.
    Attribute values are escaped too.
    """
    attr_str = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in attrs.items())
    return f"<{tag}{attr_str}>\n{html.escape(text, quote=False)}\n</{tag}>"


class PlatformText:
    """Renders platform-sourced text into a prompt: normalised, stripped of hidden and
    control characters, capped and escaped, like untrusted text.

    ``block`` wraps text in a tag it cannot close; ``inline`` returns a single escaped
    line, so a field such as a history purpose cannot fake extra lines. Anything that
    had to be changed is logged and recorded in ``anomalies``; ``signals`` turns them
    into the review-forcing signal.
    """

    def __init__(self, log: GuardrailLog | None = None):
        self._log = log
        self.anomalies: list[str] = []

    @property
    def signals(self) -> list[str]:
        return [PLATFORM_ANOMALY_SIGNAL] if self.anomalies else []

    def _clean(self, raw: str, max_chars: int, source: str) -> str:
        # Injection-pattern signals are not used here: guidance and guardian answers
        # legitimately say things like "Mom says it's fine".
        s = sanitize(raw, max_chars=max_chars)
        problems = []
        if "hidden_characters" in s.signals or _CONTROL.search(unicodedata.normalize("NFKC", raw)):
            problems.append("hidden_or_control_characters")
        if s.truncated:
            problems.append("oversized")
        if _MARKUP.search(s.text):
            problems.append("markup")
        if problems:
            self.anomalies.append(f"{source}: {','.join(problems)}")
            if self._log:
                self._log.add(PLATFORM_ANOMALY_SIGNAL, f"{source}: {','.join(problems)}")
        return s.text

    def block(self, tag: str, raw: str, *, max_chars: int, source: str, **attrs: str) -> str:
        return wrap_untrusted(tag, self._clean(raw, max_chars, source), **attrs)

    def inline(self, raw: str, *, max_chars: int, source: str) -> str:
        text = " ".join(self._clean(raw, max_chars, source).split())
        return html.escape(text, quote=False)
