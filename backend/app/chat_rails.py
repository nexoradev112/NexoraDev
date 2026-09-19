"""Mandatory deterministic rails for text chat.

The checks in this module run before and after every model call. They do not
depend on a tenant prompt or provider, and they never retain matched values.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_TEXT_CHARS = 16_000

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}(?![\w-])")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)")
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ .-]{0,3}){12,18}\d(?!\d)")
_SECRET_RE = re.compile(
    r"(?:(?<![\w-])sk-[A-Za-z0-9_-]{16,}|\b(?:api[_ -]?key|access[_ -]?token|password)"
    r"\s*[:=]\s*[^\s,;]{6,})",
    re.IGNORECASE,
)
_GOVERNMENT_ID_PATTERNS = (
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    re.compile(r"(?<!\d)784-\d{4}-\d{7}-\d(?!\d)"),
    re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)"),
    re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}[A-Z0-9]{11,30}(?![A-Za-z0-9])", re.IGNORECASE),
)

_JAILBREAK_PATTERNS = (
    re.compile(
        r"\bignore\s+(?:all\s+)?(?:previous|prior|system|developer)\s+"
        r"(?:instructions?|prompts?|messages?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:reveal|print|show|repeat|expose)\s+(?:the\s+)?(?:system|developer)\s+prompt\b", re.I),
    re.compile(r"\b(?:jailbreak|developer\s+mode|do\s+anything\s+now|act\s+as\s+dan)\b", re.I),
    re.compile(r"\b(?:bypass|disable|override)\s+(?:the\s+)?(?:safety|guardrails?|policy|rules?)\b", re.I),
    re.compile(r"تجاهل\s+(?:كل\s+)?(?:التعليمات|الاوامر|الأوامر)", re.I),
    re.compile(r"(?:اكشف|اظهر|أظهر)\s+(?:موجه|تعليمات)\s+النظام", re.I),
    re.compile(r"(?:पिछले|सभी)\s+निर्देशों?\s+को\s+अनदेखा", re.I),
    re.compile(r"सिस्टम\s+(?:प्रॉम्प्ट|प्रॉंप्ट)\s+(?:दिखाओ|बताओ)", re.I),
    re.compile(r"\bpichl[ei]\s+(?:instructions?|nirdesh)\s+ignore\b", re.I),
    re.compile(
        r"\b(?:disregard|forget)\s+(?:all\s+|every\s+)?(?:previous|prior|earlier|above|hidden)?\s*"
        r"(?:instructions?|prompts?|messages?|rules?)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:you\s+are\s+now\s+unrestricted|expose|print|reveal|show)\b.{0,48}"
        r"\b(?:hidden|system|developer)\s+(?:instructions?|prompt|messages?)\b",
        re.I,
    ),
)

_DANGEROUS_PATTERNS = (
    re.compile(
        r"\b(?:make|build|create|assemble)\s+(?:an?\s+)?(?:bomb|explosive|detonator|ied)\b",
        re.I,
    ),
    re.compile(r"\b(?:bomb|explosive|detonator|ied)\s+(?:making|building|instructions?|recipe)\b", re.I),
    re.compile(r"\b(?:how\s+to|ways?\s+to)\s+(?:kill\s+myself|commit\s+suicide|self[- ]?harm)\b", re.I),
    re.compile(r"\b(?:suicide\s+method|self[- ]?harm\s+instructions?|i\s+want\s+to\s+die)\b", re.I),
    re.compile(r"(?:كيف(?:ية)?\s+)?(?:اصنع|صنع|تركيب).{0,20}(?:قنبلة|متفجرات|عبوة\s+ناسفة)", re.I),
    re.compile(r"(?:طريقة\s+الانتحار|كيف\s+انتحر|اقتل\s+نفسي|أقتل\s+نفسي|ايذاء\s+النفس|إيذاء\s+النفس)", re.I),
    re.compile(r"(?:बम|विस्फोटक).{0,24}(?:कैसे\s+बनाएं|कैसे\s+बनाये|बनाना|तैयार|निर्माण)", re.I),
    re.compile(r"(?:आत्महत्या\s+कैसे|खुदकुशी|खुद\s+को\s+(?:मार|नुकसान)|मुझे\s+मरना\s+है)", re.I),
    re.compile(r"\b(?:bomb\s+kaise\s+banaye|suicide\s+kaise\s+kare|khud\s+ko\s+(?:mar|nuksan))\b", re.I),
    re.compile(
        r"\b(?:instructions?|steps?|guide|recipe)\s+(?:for|to\s+make)\s+"
        r"(?:an?\s+)?explosive\s+device\b",
        re.I,
    ),
    re.compile(r"\b(?:how\s+(?:can|do)\s+i\s+)?(?:end\s+my\s+life|painless\s+way\s+to\s+die)\b", re.I),
)

_MEDICAL_CLAIM_PATTERNS = (
    re.compile(
        r"\b(?:you|the patient)\s+(?:definitely\s+)?(?:have|has|are suffering from|is suffering from)\s+"
        r"(?:cancer|diabetes|an?\s+infection|a\s+stroke|a\s+heart attack)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:stop|start|double|reduce|increase)\s+(?:taking\s+)?(?:your\s+)?"
        r"(?:medicine|medication|dose|dosage)\b",
        re.I,
    ),
    re.compile(r"\b(?:take|use)\s+\d+(?:\.\d+)?\s*(?:mg|mcg|ml|tablets?|pills?)\b", re.I),
    re.compile(
        r"\b(?:I diagnose you with|my diagnosis is|you are diagnosed with|"
        r"your symptoms (?:mean|prove|confirm))\b",
        re.I,
    ),
    re.compile(r"\bthis is (?:definitely )?(?:a case of|a sign of|symptoms of)\b", re.I),
    re.compile(r"(?:أنت|المريض)\s+(?:مصاب|تعاني|يعاني)", re.I),
    re.compile(r"(?:أوقف|ابدأ|ضاعف|زد|قلل)\s+(?:الدواء|الجرعة|العلاج)", re.I),
    re.compile(r"(?:आपको|मरीज़\s+को)\s+(?:निश्चित\s+रूप\s+से\s+)?(?:कैंसर|मधुमेह|संक्रमण).{0,8}\s+है", re.I),
    re.compile(r"(?:दवा|खुराक)\s+(?:बंद|शुरू|दोगुनी|कम|बढ़ा)", re.I),
)

_LEGAL_CLAIM_PATTERNS = (
    re.compile(r"\b(?:you are|you're)\s+(?:legally\s+)?(?:entitled|required|liable|obligated)\b", re.I),
    re.compile(
        r"\b(?:you will|you'll)\s+(?:definitely\s+)?(?:win|lose)\s+(?:the\s+)?"
        r"(?:case|lawsuit|appeal)\b",
        re.I,
    ),
    re.compile(r"\b(?:this is|as)\s+(?:formal\s+)?legal advice\b", re.I),
    re.compile(r"\b(?:the law guarantees|guaranteed legal outcome)\b", re.I),
    re.compile(r"\byou (?:must|should) (?:sue|file (?:a claim|a lawsuit)|plead guilty)\b", re.I),
    re.compile(r"(?:أنت|انتم|أنتم)\s+(?:ملزم|مسؤول|مستحق)\s+(?:قانونيا|قانونياً)?", re.I),
    re.compile(r"(?:ستربح|ستخسر)\s+(?:القضية|الدعوى)\s+(?:بالتأكيد)?", re.I),
    re.compile(r"(?:आप|आपको)\s+कानूनी\s+रूप\s+से\s+(?:हकदार|ज़िम्मेदार|बाध्य)", re.I),
    re.compile(r"(?:मुकदमा|केस)\s+(?:निश्चित\s+रूप\s+से\s+)?(?:जीतेंगे|हारेंगे)", re.I),
)

_INVENTED_DATA_PATTERNS = (
    re.compile(r"\b(?:our|the)\s+(?:records?|system)\s+(?:show|shows|confirm|confirms)\b", re.I),
    re.compile(
        r"\byour\s+(?:account balance|credit score|order number|claim number|policy number)"
        r"\s+(?:is|was)\b",
        re.I,
    ),
    re.compile(
        r"\bI(?:'ve| have)\s+"
        r"(?:confirmed|booked|cancelled|canceled|refunded|charged|updated|approved)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:the|your)\s+(?:refund|booking|payment|application|claim)\s+(?:is|has been)\s+"
        r"(?:confirmed|approved|completed|processed)\b",
        re.I,
    ),
    re.compile(r"(?:سجلاتنا|النظام)\s+(?:تظهر|يظهر|تؤكد|يؤكد)", re.I),
    re.compile(r"تم\s+(?:تأكيد|اعتماد|إكمال|معالجة)\s+(?:الحجز|الدفع|الطلب|الاسترداد)", re.I),
    re.compile(r"(?:हमारे\s+रिकॉर्ड|सिस्टम)\s+(?:दिखाते|पुष्टि\s+करते)\s+हैं", re.I),
    re.compile(r"(?:आपका|आपकी)\s+(?:रिफंड|बुकिंग|भुगतान|दावा)\s+(?:स्वीकृत|पूरा|पुष्ट)\s+है", re.I),
)

_UNCERTAINTY_PATTERNS = (
    re.compile(r"\bI(?:'m| am)\s+not\s+(?:sure|certain)\b", re.I),
    re.compile(r"\bI\s+(?:cannot|can't|couldn't)\s+(?:verify|confirm)\b", re.I),
    re.compile(r"\bI\s+(?:do\s+not|don't)\s+know\b", re.I),
    re.compile(r"\bI (?:might|may|could) be wrong\b", re.I),
    re.compile(r"(?:لست|انا\s+غير|أنا\s+غير)\s+(?:متأكد|واثق)", re.I),
    re.compile(r"(?:لا\s+استطيع|لا\s+أستطيع)\s+(?:التحقق|التأكيد)|لا\s+اعرف|لا\s+أعرف", re.I),
    re.compile(r"(?:मुझे\s+यकीन\s+नहीं|मैं\s+निश्चित\s+नहीं|मुझे\s+नहीं\s+पता)", re.I),
    re.compile(r"मैं\s+(?:पुष्टि|सत्यापन)\s+नहीं\s+कर\s+सकता", re.I),
    re.compile(r"\b(?:mujhe\s+nahi\s+pata|verify\s+nahi\s+kar\s+sakta)\b", re.I),
)


@dataclass(frozen=True)
class ChatRailDecision:
    text: str
    blocked: bool = False
    requires_handoff: bool = False
    reason: str = "allowed"
    redactions: tuple[str, ...] = ()


def normalize_for_matching(text: str) -> str:
    """Collapse compatibility forms and discard invisible formatting controls."""

    value = unicodedata.normalize("NFKC", text[:MAX_TEXT_CHARS])
    clean: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if category == "Cf" or character == "ـ":
            continue
        if category in {"Cc", "Cs"}:
            clean.append(" ")
        elif "\u064b" <= character <= "\u065f":
            continue
        else:
            clean.append(character)
    return " ".join("".join(clean).casefold().split())


def check_chat_input(text: str) -> ChatRailDecision:
    """Inspect and sanitize untrusted conversation text before a provider call."""

    bounded = text[:MAX_TEXT_CHARS]
    if any(_looks_like_card(match.group(0)) for match in _CARD_CANDIDATE_RE.finditer(bounded)):
        return ChatRailDecision(
            text="[PAYMENT_CARD_DATA_BLOCKED]",
            blocked=True,
            requires_handoff=True,
            reason="payment_card_data",
            redactions=("payment_card",),
        )

    normalized = normalize_for_matching(bounded)
    if any(pattern.search(normalized) for pattern in _JAILBREAK_PATTERNS):
        return ChatRailDecision(
            text="[PROMPT_OVERRIDE_REQUEST_BLOCKED]",
            blocked=True,
            requires_handoff=True,
            reason="jailbreak_attempt",
        )
    if any(pattern.search(normalized) for pattern in _DANGEROUS_PATTERNS):
        return ChatRailDecision(
            text="[DANGEROUS_REQUEST_BLOCKED]",
            blocked=True,
            requires_handoff=True,
            reason="dangerous_or_self_harm_request",
        )

    redacted, labels = redact_sensitive(bounded)
    return ChatRailDecision(text=redacted, redactions=labels)


def check_model_output(text: str) -> ChatRailDecision:
    """Validate a complete provider response before it is returned to a caller."""

    bounded = text[:MAX_TEXT_CHARS]
    normalized = normalize_for_matching(bounded)
    checks = (
        (_DANGEROUS_PATTERNS, "dangerous_or_self_harm_response"),
        (_MEDICAL_CLAIM_PATTERNS, "medical_claim"),
        (_LEGAL_CLAIM_PATTERNS, "legal_claim"),
        (_INVENTED_DATA_PATTERNS, "unverified_customer_data"),
        (_UNCERTAINTY_PATTERNS, "model_uncertainty"),
    )
    for patterns, reason in checks:
        if any(pattern.search(normalized) for pattern in patterns):
            return ChatRailDecision(
                text="[HUMAN_HANDOFF_REQUIRED]",
                blocked=True,
                requires_handoff=True,
                reason=reason,
            )

    redacted, labels = redact_sensitive(bounded)
    return ChatRailDecision(text=redacted, redactions=labels)


def check_generated_agent(text: str) -> ChatRailDecision:
    """Inspect a generated agent spec. This is not a live customer reply.

    Draft prompts include negative policy such as "never say a booking is
    confirmed" and "if I'm not sure, offer handoff". Those phrases are required
    instructions, not claims made to a caller.
    """

    bounded = text[:MAX_TEXT_CHARS]
    normalized = normalize_for_matching(bounded)
    if any(pattern.search(normalized) for pattern in _DANGEROUS_PATTERNS):
        return ChatRailDecision(
            text="[HUMAN_HANDOFF_REQUIRED]",
            blocked=True,
            requires_handoff=True,
            reason="dangerous_or_self_harm_response",
        )
    redacted, labels = redact_sensitive(bounded)
    return ChatRailDecision(text=redacted, redactions=labels)


def redact_sensitive(text: str) -> tuple[str, tuple[str, ...]]:
    """Redact common identifiers and secrets without retaining matched values."""

    labels: list[str] = []
    value = text[:MAX_TEXT_CHARS]

    if _SECRET_RE.search(value):
        labels.append("secret")
        value = _SECRET_RE.sub("[SECRET_REDACTED]", value)
    for pattern in _GOVERNMENT_ID_PATTERNS:
        if pattern.search(value):
            labels.append("government_id")
            value = pattern.sub("[GOVERNMENT_ID_REDACTED]", value)

    def redact_card(match: re.Match[str]) -> str:
        if not _looks_like_card(match.group(0)):
            return match.group(0)
        labels.append("payment_card")
        return "[PAYMENT_CARD_REDACTED]"

    value = _CARD_CANDIDATE_RE.sub(redact_card, value)
    if _EMAIL_RE.search(value):
        labels.append("email")
        value = _EMAIL_RE.sub("[EMAIL_REDACTED]", value)

    def redact_phone(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if not 8 <= len(digits) <= 15:
            return match.group(0)
        labels.append("phone")
        return "[PHONE_REDACTED]"

    value = _PHONE_RE.sub(redact_phone, value)
    return value, tuple(dict.fromkeys(labels))


def safe_handoff_message(locale: str, reason: str) -> str:
    """Return a fixed safe response; never interpolate rejected content."""

    harmful = reason.startswith("dangerous_or_self_harm")
    if locale == "ar":
        if harmful:
            return (
                "لا أستطيع المساعدة في تعليمات قد تسبب ضرراً. عند وجود خطر فوري اتصل "
                "بالطوارئ المحلية. سأطلب مساعدة بشرية."
            )
        return "لا أستطيع التحقق من ذلك أو الإجابة عنه بأمان. سأحوّل المحادثة إلى مختص بشري."
    if locale in {"hi-IN", "hi-en"}:
        if harmful:
            return (
                "मैं नुकसान पहुँचाने वाले निर्देश नहीं दे सकता। तुरंत खतरा हो तो स्थानीय "
                "आपात सेवा से संपर्क करें। मैं मानव सहायता बुला रहा हूँ।"
            )
        return "मैं इसे सुरक्षित रूप से सत्यापित या उत्तर नहीं कर सकता। मैं इसे मानव विशेषज्ञ को सौंप रहा हूँ।"
    if harmful:
        return (
            "I can't help with instructions that could cause harm. If anyone is in immediate danger, "
            "contact local emergency services now. I'm requesting human help."
        )
    return "I can't safely verify or answer that. I'm handing this conversation to a human specialist."


def _looks_like_card(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        number = int(character)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        checksum += number
    return checksum % 10 == 0
