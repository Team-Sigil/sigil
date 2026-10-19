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
# Common characters written differently in the two scripts, as (Simplified, Traditional)
# pairs. Only characters that exist in one script alone are listed (e.g. not 后/後, since 后
# is also Traditional), so one hit is real evidence.
_SCRIPT_PAIRS = (
    "们們 这這 钱錢 买買 卖賣 给給 说說 学學 书書 妈媽 爷爺 为為 么麼 时時 过過 还還 没沒 让讓 发發 "
    "对對 会會 车車 来來 吗嗎 个個 东東 习習 费費 题題 问問 门門 气氣 电電 话話 读讀 应應 该該 礼禮 "
    "机機 网網 级級 课課 业業 实實 现現 点點 张張 长長 开開 关關 银銀 账賬 欢歡 乐樂 见見 难難 谢謝 "
    "请請 帮幫 医醫 紧緊 经經 转轉 块塊 笔筆 记記 节節 饭飯 馆館 补補 钟鐘 头頭 爱愛 处處 动動 报報 "
    "认認 识識 写寫 边邊 远遠 进進 运運 岁歲 儿兒 亲親 带帶 换換 种種 样樣 数數 单單 价價 务務 产產 "
    "资資 设設 计計 视視 观觀 历歷 万萬 华華 国國 语語 词詞 页頁 场場 广廣 楼樓 线線 结結 统統 规規 "
    "则則 简簡 体體 确確 认認 额額 余餘 剩賸 收收 据據 票票 购購 物物 买買 周週 预預 算算 零零 用用"
).split()
_SCRIPT_PAIRS = tuple(dict.fromkeys(p for p in _SCRIPT_PAIRS if p[0] != p[1]))  # drop identical pairs
_SIMPLIFIED_ONLY = frozenset(p[0] for p in _SCRIPT_PAIRS)
_TRADITIONAL_ONLY = frozenset(p[1] for p in _SCRIPT_PAIRS)
assert all(len(p) == 2 for p in _SCRIPT_PAIRS) and not _SIMPLIFIED_ONLY & _TRADITIONAL_ONLY


MIN_LATIN_FOR_MISMATCH = 8


def chinese_script(text: str) -> Locale | None:
    """Which Chinese script a text uses, or None when it has no characters that tell
    (many common characters are the same in both)."""
    simplified = sum(1 for c in text if c in _SIMPLIFIED_ONLY)
    traditional = sum(1 for c in text if c in _TRADITIONAL_ONLY)
    if simplified == traditional:
        return None
    return "zh-Hant" if traditional > simplified else "zh-Hans"


def language_mismatch(text: str, expected: Locale) -> bool:
    """True when a text is clearly not in the expected language or Chinese script. Unclear
    texts (too short, or no characters that tell the scripts apart) are not mismatches."""
    found = detect_locale(text)
    if found is None:
        return False
    if found == "en" and len(_LATIN.findall(text)) < MIN_LATIN_FOR_MISMATCH:
        return False  # "OK", "Hi", a name: too little to call it the wrong language
    if expected == "en" or found == "en":
        return found != expected
    script = chinese_script(text)
    return script is not None and script != expected


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
        return chinese_script(text) or "zh-Hans"  # no telling characters: the more common script
    if latin >= 2:
        return "en"
    return None


def _latest_dependent_text(request: DependentRequest) -> str:
    for msg in reversed(request.thread):
        if msg.role == "dependent":
            return msg.text
    return request.message


def dependent_locale(ctx: AgentContext, request: DependentRequest | None = None) -> Locale:
    profile = normalize_locale(ctx.dependent_locale)
    if request is None:
        return profile or DEFAULT_LOCALE
    text = _latest_dependent_text(request)
    detected = detect_locale(text)
    if detected in ("zh-Hans", "zh-Hant") and chinese_script(text) is None and profile in ("zh-Hans", "zh-Hant"):
        return profile  # Chinese, but the message doesn't tell which script: the profile does
    return detected or profile or DEFAULT_LOCALE


def guardian_locale(ctx: AgentContext) -> Locale:
    return normalize_locale(ctx.guardian_locale) or detect_locale(ctx.policy.guidance) or DEFAULT_LOCALE


_SCRIPT_HINT: dict[Locale, str] = {
    "en": "",
    "zh-Hans": " (Simplified characters such as 这、给、说、钱, never Traditional)",
    "zh-Hant": " (Traditional characters such as 這、給、說、錢, never Simplified)",
}


def language_instruction(dependent: Locale, guardian: Locale) -> str:
    """The language rule for the model, stated per run. Code checks the reply afterwards
    (``language_mismatch``) and replaces one in the wrong language or script."""
    return (
        f"Languages: write reply_to_dependent in {LANGUAGE_NAMES[dependent]}{_SCRIPT_HINT[dependent]}. "
        f"Write summary_for_guardian, guardian questions and guardian notes in "
        f"{LANGUAGE_NAMES[guardian]}{_SCRIPT_HINT[guardian]}. Keep amounts as plain numbers (for example 12.50)."
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
    "reply.transfer_pending": {
        "en": "Your {amount} is on its way. It isn't confirmed yet, so it may take a little while.",
        "zh-Hans": "你的 {amount} 正在转账中，还没有确认，可能需要等一会儿。",
        "zh-Hant": "你的 {amount} 正在轉帳中，還沒有確認，可能需要等一會兒。",
    },
    "reply.safe_fallback": {
        "en": "I couldn't finish looking at this, so I've passed it to your guardian.",
        "zh-Hans": "我没能处理完这个请求，已经转给你的家长了。",
        "zh-Hant": "我沒能處理完這個請求，已經轉給你的家長了。",
    },
    "reply.receipt_accepted": {
        "en": "Thanks! Your receipt for {amount} is checked, so you're all set.",
        "zh-Hans": "谢谢！你 {amount} 的收据已经核对好了。",
        "zh-Hant": "謝謝！你 {amount} 的收據已經核對好了。",
    },
    "reply.receipt_rejected": {
        "en": "I couldn't accept that receipt. Please send a clear photo of the receipt for {amount}.",
        "zh-Hans": "这张收据没能通过。请发一张 {amount} 那笔消费的清晰收据照片。",
        "zh-Hant": "這張收據沒能通過。請傳一張 {amount} 那筆消費的清晰收據照片。",
    },
    "reply.receipt_unchecked": {
        "en": "I couldn't check your receipt just now. Please send it again in a little while.",
        "zh-Hans": "现在没能核对你的收据，请稍后再发一次。",
        "zh-Hant": "現在沒能核對你的收據，請稍後再傳一次。",
    },
    "reply.receipt_owed": {
        "en": "Please send a photo of the receipt for {amount} here once you've bought it.",
        "zh-Hans": "买好之后，请在这里发一张 {amount} 的收据照片。",
        "zh-Hant": "買好之後，請在這裡傳一張 {amount} 的收據照片。",
    },
    # Why the platform refused a receipt (its reason, in the dependent's language).
    "receipt.reason.proof does not cover the amount": {
        "en": "It doesn't show the purchase or the full amount.",
        "zh-Hans": "收据上看不到这笔消费或完整金额。",
        "zh-Hant": "收據上看不到這筆消費或完整金額。",
    },
    "receipt.reason.proof already used": {
        "en": "That receipt was already used for another payment.",
        "zh-Hans": "这张收据已经用于另一笔付款了。",
        "zh-Hant": "這張收據已經用於另一筆付款了。",
    },
    "receipt.reason.not usable as proof": {
        "en": "It can't be used as proof; take the photo with the app's camera.",
        "zh-Hans": "它不能作为凭证，请用应用内的相机拍照。",
        "zh-Hant": "它不能作為憑證，請用應用程式內的相機拍照。",
    },
    "receipt.reason.proof not for this request": {
        "en": "It was sent for a different request.",
        "zh-Hans": "它是为另一个请求发送的。",
        "zh-Hant": "它是為另一個請求傳送的。",
    },
    "guardian.receipt_accepted": {
        "en": "{name} sent the receipt for {amount} ({purpose}); it checked out.",
        "zh-Hans": "{name}发来了 {amount}（{purpose}）的收据，核对无误。",
        "zh-Hant": "{name}傳來了 {amount}（{purpose}）的收據，核對無誤。",
    },
    "guardian.receipt_rejected": {
        "en": "{name} sent a receipt for {amount} ({purpose}), but it was refused: {reason}",
        "zh-Hans": "{name}发来了 {amount}（{purpose}）的收据，但未通过：{reason}",
        "zh-Hant": "{name}傳來了 {amount}（{purpose}）的收據，但未通過：{reason}",
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
    # Policy interview (flows/policy_interview.py).
    "interview.opening": {
        "en": "Let's set up the allowance. Who is it for, and roughly how much should they get each week?",
        "zh-Hans": "我们来设置零花钱吧。是给谁的？每周大概给多少？",
        "zh-Hant": "我們來設定零用錢吧。是給誰的？每週大概給多少？",
    },
    "interview.ask_missing": {
        "en": "A few more things before I can draft this: {items}. What would you like?",
        "zh-Hans": "在起草之前还需要了解几件事：{items}。你希望怎么设置？",
        "zh-Hant": "在起草之前還需要了解幾件事：{items}。你希望怎麼設定？",
    },
    "interview.confirm": {
        "en": "I filled in a few things myself. Are these right?\n{items}",
        "zh-Hans": "有几项是我自己补上的，这样可以吗？\n{items}",
        "zh-Hant": "有幾項是我自己補上的，這樣可以嗎？\n{items}",
    },
    "interview.review": {
        "en": "Here's how I would handle a few requests under this policy:\n{scenarios}\nAnything you'd change? If not, you can review and sign it.",
        "zh-Hans": "按照这个规则，我会这样处理几个请求：\n{scenarios}\n有需要修改的吗？没有的话就可以查看并签署了。",
        "zh-Hant": "按照這個規則，我會這樣處理幾個請求：\n{scenarios}\n有需要修改的嗎？沒有的話就可以檢視並簽署了。",
    },
    "interview.conflict": {
        "en": "Some of these limits don't fit together: {errors}. Which would you like to change?",
        "zh-Hans": "有几个限额互相冲突：{errors}。你想改哪一个？",
        "zh-Hant": "有幾個限額互相衝突：{errors}。你想改哪一個？",
    },
    "interview.label.weekly_cap": {"en": "weekly budget", "zh-Hans": "每周预算", "zh-Hant": "每週預算"},
    "interview.label.max_per_request": {"en": "most for one request", "zh-Hans": "单次最多", "zh-Hant": "單次最多"},
    "interview.label.proof_threshold": {"en": "receipt needed from", "zh-Hans": "多少钱起需要收据", "zh-Hant": "多少錢起需要收據"},
    "interview.label.blocked_categories": {"en": "never allowed", "zh-Hans": "绝对不允许的类别", "zh-Hant": "絕對不允許的類別"},
    "interview.label.allowance": {"en": "regular allowance", "zh-Hans": "固定零花钱", "zh-Hant": "固定零用錢"},
    "interview.label.review_threshold": {"en": "second check from", "zh-Hans": "多少钱起需要二次审核", "zh-Hant": "多少錢起需要二次審核"},
    "interview.label.always_fine": {"en": "always fine", "zh-Hans": "总是可以的", "zh-Hant": "總是可以的"},
    "interview.label.ask_first": {"en": "ask me first about", "zh-Hans": "需要先问我的", "zh-Hant": "需要先問我的"},
    "interview.label.dependent_context": {"en": "about the dependent", "zh-Hans": "关于被照顾的人", "zh-Hant": "關於被照顧的人"},
    "interview.label.emergencies": {"en": "emergencies", "zh-Hans": "紧急情况", "zh-Hant": "緊急情況"},
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


def has_text(key: str) -> bool:
    return key in _TEXTS


def category_label(category: str, locale: Locale) -> str:
    return _CATEGORIES.get(str(category), {}).get(locale, str(category))


def weekday(index: int, locale: Locale) -> str:
    return _WEEKDAYS[locale][index]
