"""Languages: English, Simplified Chinese and Traditional Chinese.

The model writes its own replies in whatever language it is told to use. This module
covers what code decides: which language each person gets, and every fixed text code
sends to a person (fallback replies, warnings, guardian questions, summaries).

Which language:

* The dependent: the language of their latest message, when it is clear; otherwise
  their profile locale from the platform; otherwise English.
* The guardian: their profile locale; otherwise the language their guidance is
  written in; otherwise English.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agent_layer.contracts import AgentContext, DependentRequest

Locale = Literal["en", "zh-Hans", "zh-Hant"]
DEFAULT_LOCALE: Locale = "en"

LANGUAGE_NAMES: dict[Locale, str] = {
    "en": "English",
    "zh-Hans": "Simplified Chinese (简体中文)",
    "zh-Hant": "Traditional Chinese (繁體中文)",
}

_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")
# Common characters that differ between the two scripts, aligned pair by pair.
_SIMPLIFIED_ONLY = "们这钱买给说学书妈爷为么时过还没让发对会车来吗个东习费题问门气电话读应该礼机网级课业实现点张长开关银账欢乐见难谢请帮医紧"
_TRADITIONAL_ONLY = "們這錢買給說學書媽爺為麼時過還沒讓發對會車來嗎個東習費題問門氣電話讀應該禮機網級課業實現點張長開關銀賬歡樂見難謝請幫醫緊"
assert len(_SIMPLIFIED_ONLY) == len(_TRADITIONAL_ONLY)


def normalize_locale(tag: str | None) -> Locale | None:
    """Map a BCP 47 tag from the platform (``zh-TW``, ``en-GB``, ``zh-Hans-CN`` …) to a
    supported locale, or None when it is missing or unsupported."""
    if not tag:
        return None
    parts = tag.replace("_", "-").lower().split("-")
    if parts[0] == "en":
        return "en"
    if parts[0] == "zh":
        rest = set(parts[1:])
        if rest & {"hant", "tw", "hk", "mo"}:
            return "zh-Hant"
        return "zh-Hans"
    return None


def detect_locale(text: str) -> Locale | None:
    """The language a text is written in, or None when it is too short to tell.

    A Han character carries about as much as a short English word, so a message counts
    as Chinese when it has at least 2 Han characters and at least one per 4 Latin
    letters (``我要50 for books`` is Chinese).
    """
    han = len(_HAN.findall(text))
    latin = len(_LATIN.findall(text))
    if han >= 2 and han * 4 >= latin:
        simplified = sum(text.count(c) for c in _SIMPLIFIED_ONLY)
        traditional = sum(text.count(c) for c in _TRADITIONAL_ONLY)
        return "zh-Hant" if traditional > simplified else "zh-Hans"
    if latin >= 2:
        return "en"
    return None


def _latest_dependent_text(request: DependentRequest) -> str:
    for msg in reversed(request.thread):
        if msg.role == "dependent":
            return msg.text
    return request.message


def dependent_locale(ctx: AgentContext, request: DependentRequest | None = None) -> Locale:
    detected = detect_locale(_latest_dependent_text(request)) if request is not None else None
    return detected or normalize_locale(ctx.dependent_locale) or DEFAULT_LOCALE


def guardian_locale(ctx: AgentContext) -> Locale:
    return normalize_locale(ctx.guardian_locale) or detect_locale(ctx.policy.guidance) or DEFAULT_LOCALE


def language_instruction(dependent: Locale, guardian: Locale) -> str:
    """The language rule for the model, stated per run."""
    return (
        f"Languages: write reply_to_dependent in {LANGUAGE_NAMES[dependent]}. Write "
        f"summary_for_guardian, guardian questions and guardian notes in {LANGUAGE_NAMES[guardian]}. "
        "Keep amounts as plain numbers (for example 12.50)."
    )


# ------------------------------------------------------------------- fixed texts

_TEXTS: dict[str, dict[Locale, str]] = {
    # Replies to the dependent when the model's reply can't be used.
    "reply.sent_full": {
        "en": "Done! I've sent {amount}.",
        "zh-Hans": "好了！已经给你转了 {amount}。",
        "zh-Hant": "好了！已經轉給你 {amount}。",
    },
    "reply.sent_partial": {
        "en": "I've sent {amount} for now.",
        "zh-Hans": "先给你转了 {amount}。",
        "zh-Hant": "先轉給你 {amount}。",
    },
    "reply.counter_offer": {
        "en": "I can't do the full amount right now, but let's find something that works.",
        "zh-Hans": "现在没办法给全额，我们一起想想别的办法吧。",
        "zh-Hant": "現在沒辦法給全額，我們一起想想別的辦法吧。",
    },
    "reply.asked_proof": {
        "en": "Could you share a receipt or quote for this?",
        "zh-Hans": "可以发一下这笔费用的收据或报价吗？",
        "zh-Hant": "可以傳一下這筆費用的收據或報價嗎？",
    },
    "reply.asked_guardian": {
        "en": "I've asked your guardian about this and will get back to you.",
        "zh-Hans": "我已经问了你的家长，有结果会告诉你。",
        "zh-Hant": "我已經問了你的家長，有結果會告訴你。",
    },
    "reply.auditor_flagged": {
        "en": "I've passed this to your guardian to decide. You'll hear back soon.",
        "zh-Hans": "这件事已经交给你的家长决定，很快会有回复。",
        "zh-Hant": "這件事已經交給你的家長決定，很快會有回覆。",
    },
    "reply.asked_dependent": {
        "en": "Could you tell me what it's for and how much you need?",
        "zh-Hans": "可以告诉我这笔钱是做什么用的、需要多少吗？",
        "zh-Hant": "可以告訴我這筆錢是做什麼用的、需要多少嗎？",
    },
    "reply.out_of_scope": {
        "en": "I can only help with your allowance. Ask me when you need money for something.",
        "zh-Hans": "我只能帮你处理零花钱的事。需要用钱的时候再来找我吧。",
        "zh-Hant": "我只能幫你處理零用錢的事。需要用錢的時候再來找我吧。",
    },
    "reply.declined": {
        "en": "I can't send this one. Feel free to ask again with more details.",
        "zh-Hans": "这笔暂时不能给你。可以补充更多细节再问一次。",
        "zh-Hant": "這筆暫時不能給你。可以補充更多細節再問一次。",
    },
    "reply.blocked": {
        "en": "I can't send this because it's outside the limits your guardian set.",
        "zh-Hans": "这笔超出了你家长设定的限额，所以不能转。",
        "zh-Hant": "這筆超出了你家長設定的限額，所以不能轉。",
    },
    "reply.safe_fallback": {
        "en": "I couldn't finish looking at this, so I've passed it to your guardian.",
        "zh-Hans": "我没能处理完这个请求，已经转给你的家长了。",
        "zh-Hant": "我沒能處理完這個請求，已經轉給你的家長了。",
    },
    # To the dependent.
    "pace.warning": {
        "en": "Heads up: you've used {pct}% of this week's budget, and there are {days} days to go. {left} is left.",
        "zh-Hans": "提醒一下：本周预算你已经用了 {pct}%，还剩 {days} 天。剩余 {left}。",
        "zh-Hant": "提醒一下：本週預算你已經用了 {pct}%，還剩 {days} 天。剩餘 {left}。",
    },
    "pace.warning.one": {
        "en": "Heads up: you've used {pct}% of this week's budget, and there is 1 day to go. {left} is left.",
        "zh-Hans": "提醒一下：本周预算你已经用了 {pct}%，还剩 1 天。剩余 {left}。",
        "zh-Hant": "提醒一下：本週預算你已經用了 {pct}%，還剩 1 天。剩餘 {left}。",
    },
    # To the guardian.
    "guardian.flagged": {
        "en": "Send {amount} to {name} for {purpose}? The agent said yes; the Auditor disagreed.",
        "zh-Hans": "要给{name}转 {amount}，用于“{purpose}”吗？助手同意，审核员不同意。",
        "zh-Hant": "要給{name}轉 {amount}，用於「{purpose}」嗎？助手同意，審核員不同意。",
    },
    "guardian.fallback": {
        "en": "I couldn't finish reviewing a request from {name}. Please decide.",
        "zh-Hans": "我没能处理完{name}的一个请求，请你来决定。",
        "zh-Hant": "我沒能處理完{name}的一個請求，請你來決定。",
    },
    "guardian.fallback_view": {
        "en": "The agent hit a safety stop before reaching a decision.",
        "zh-Hans": "助手在做出决定前触发了安全停止。",
        "zh-Hant": "助手在做出決定前觸發了安全停止。",
    },
    "guardian.stopped_summary": {
        "en": "The agent stopped safely before deciding.",
        "zh-Hans": "助手在做出决定前安全停止了。",
        "zh-Hant": "助手在做出決定前安全停止了。",
    },
    "reservoir.question": {
        "en": (
            "The allowance reservoir has {left} left, about {weeks} weeks at the current pace. "
            "Top up by {top_up} to cover {target} weeks? (Sign a new approve in Phantom.)"
        ),
        "zh-Hans": "零花钱余额还剩 {left}，按目前的速度大约够用 {weeks} 周。要补充 {top_up}，凑够 {target} 周吗？（请在 Phantom 里签署新的 approve。）",
        "zh-Hant": "零用錢餘額還剩 {left}，按目前的速度大約夠用 {weeks} 週。要補充 {top_up}，湊夠 {target} 週嗎？（請在 Phantom 裡簽署新的 approve。）",
    },
    "reservoir.view": {
        "en": "Forecast weekly spend {burn} across {n} dependent(s).",
        "zh-Hans": "预计每周支出 {burn}（共 {n} 人）。",
        "zh-Hant": "預計每週支出 {burn}（共 {n} 人）。",
    },
    "flags.category": {
        "en": "{n} requests in the '{category}' category this week",
        "zh-Hans": "本周有 {n} 笔“{category}”类请求",
        "zh-Hant": "本週有 {n} 筆「{category}」類請求",
    },
    "flags.day": {
        "en": "{n} requests on {day}",
        "zh-Hans": "{day}有 {n} 笔请求",
        "zh-Hant": "{day}有 {n} 筆請求",
    },
    "flags.out_of_scope": {
        "en": "{n} messages that weren't about the allowance",
        "zh-Hans": "{n} 条与零花钱无关的消息",
        "zh-Hant": "{n} 則與零用錢無關的訊息",
    },
    "flags.refused": {
        "en": "{n} blocked or flagged attempts",
        "zh-Hans": "{n} 次请求被拦截或被标记",
        "zh-Hant": "{n} 次請求被攔截或被標記",
    },
    "summary.fallback": {
        "en": "{name} received {spent} of {cap} this week across {n} transfer(s).",
        "zh-Hans": "{name}本周共收到 {spent}（上限 {cap}），共 {n} 笔转账。",
        "zh-Hant": "{name}本週共收到 {spent}（上限 {cap}），共 {n} 筆轉帳。",
    },
    "summary.worth_a_look": {
        "en": "Worth a look:",
        "zh-Hans": "值得留意：",
        "zh-Hant": "值得留意：",
    },
    "learn.change": {
        "en": "Stop asking you about '{category}' requests up to {top}.",
        "zh-Hans": "以后 {top} 以内的“{category}”类请求不再询问你。",
        "zh-Hant": "以後 {top} 以內的「{category}」類請求不再詢問你。",
    },
    "learn.evidence": {
        "en": "You approved all {n} '{category}' requests I asked about (largest {top}).",
        "zh-Hans": "我问过你的 {n} 笔“{category}”类请求，你全部批准了（最大一笔 {top}）。",
        "zh-Hant": "我問過你的 {n} 筆「{category}」類請求，你全部批准了（最大一筆 {top}）。",
    },
}

_CATEGORIES: dict[str, dict[Locale, str]] = {
    "education": {"en": "education", "zh-Hans": "教育", "zh-Hant": "教育"},
    "food": {"en": "food", "zh-Hans": "餐饮", "zh-Hant": "餐飲"},
    "transport": {"en": "transport", "zh-Hans": "交通", "zh-Hant": "交通"},
    "health": {"en": "health", "zh-Hans": "健康", "zh-Hant": "健康"},
    "clothing": {"en": "clothing", "zh-Hans": "服装", "zh-Hant": "服裝"},
    "entertainment": {"en": "entertainment", "zh-Hans": "娱乐", "zh-Hant": "娛樂"},
    "gaming": {"en": "gaming", "zh-Hans": "游戏", "zh-Hant": "遊戲"},
    "savings": {"en": "savings", "zh-Hans": "储蓄", "zh-Hant": "儲蓄"},
    "allowance": {"en": "allowance", "zh-Hans": "零花钱", "zh-Hant": "零用錢"},
    "other": {"en": "other", "zh-Hans": "其他", "zh-Hant": "其他"},
}

_WEEKDAYS: dict[Locale, tuple[str, ...]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "zh-Hans": ("周一", "周二", "周三", "周四", "周五", "周六", "周日"),
    "zh-Hant": ("週一", "週二", "週三", "週四", "週五", "週六", "週日"),
}


def t(key: str, locale: Locale, *, count: int | None = None, **values: object) -> str:
    """A fixed text in ``locale``. With ``count == 1``, a ``<key>.one`` variant wins."""
    if count == 1 and f"{key}.one" in _TEXTS:
        key = f"{key}.one"
    return _TEXTS[key][locale].format(**values)


def category_label(category: str, locale: Locale) -> str:
    return _CATEGORIES.get(str(category), {}).get(locale, str(category))


def weekday(index: int, locale: Locale) -> str:
    return _WEEKDAYS[locale][index]
