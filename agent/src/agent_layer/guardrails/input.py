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

# A base58 run the length of a Solana address. Lookarounds instead of \b, because \b
# sees no boundary between a Han character and a letter ("发到9xQe…").
ADDRESS_LIKE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")

# English and Chinese (Simplified and Traditional) patterns per signal. Chinese text is
# matched after NFKC, so full-width ":" and digits are already ASCII.
_SIGNALS: dict[str, list[re.Pattern[str]]] = {
    "instruction_override": [
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|rule|prompt|polic|limit|guidance)",
            re.I | re.S,
        ),
        re.compile(
            r"(忽略|忽視|无视|無視|忘记|忘記|不要理|别管|別管|跳过|跳過|绕过|繞過).{0,20}"
            r"(指令|指示|规则|規則|提示|设定|設定|限制|限额|限額|政策|要求|规定|規定)",
            re.S,
        ),
    ],
    "role_play": [
        re.compile(r"\b(you are now|act as|pretend (to be|you)|developer mode|jailbreak)\b", re.I),
        re.compile(r"(你现在是|你現在是|你扮演|扮演一个|扮演一個|假装你|假裝你|开发者模式|開發者模式|越狱|越獄)"),
    ],
    "system_prompt_probe": [
        re.compile(r"\b(system prompt|your (instructions|rules|guidance)|reveal|print the)\b", re.I),
        re.compile(r"(系统提示|系統提示|提示词|提示詞|你的(指令|指示|规则|規則|设定|設定)|透露|泄露|洩露)"),
    ],
    "claimed_guardian_approval": [
        re.compile(
            r"\b(mom|mum|dad|mother|father|parent|guardian|grandma|grandpa)\b.{0,40}"
            r"\b(said|says|approved|agreed|allowed|ok(ay)?|fine|let me)\b",
            re.I | re.S,
        ),
        re.compile(
            r"(妈妈|媽媽|我妈|我媽|爸爸|我爸|父母|家长|家長|监护人|監護人|奶奶|爷爷|爺爺|外婆|外公|阿嬷|阿嬤).{0,20}"
            r"(说|說|同意|批准|答应|答應|允许|允許|可以|没问题|沒問題|准了|准许|准許|行了)",
            re.S,
        ),
    ],
    "authority_claim": [
        re.compile(r"\b(admin|administrator|developer|anthropic|system)\s*(:|says|here|message)", re.I),
        re.compile(r"(管理员|管理員|开发者|開發者|开发人员|開發人員|系统|系統|官方)\s*(:|说|說|消息|訊息|通知)"),
    ],
    "urgency_pressure": [
        re.compile(r"\b(emergency|urgent(ly)?|right now|immediately|asap|hospital|police|stranded)\b", re.I),
        re.compile(r"(紧急|緊急|急需|急用|立刻|立即|赶快|趕快|医院|醫院|警察|被困|出事)"),
    ],
    "address_like": [ADDRESS_LIKE],
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
    signals.extend(name for name, pats in _SIGNALS.items() if any(p.search(text) for p in pats))
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
