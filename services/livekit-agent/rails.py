"""Non-optional safety controls for the realtime voice worker.

These rails run inside the platform-owned Python process.  They are deliberately
independent from agent prompts and provider credentials, so a tenant workflow or
BYOK model cannot turn them off.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

DEFAULT_MAX_CALL_SECONDS = 1_800
DEFAULT_PLATFORM_MAX_CALL_SECONDS = 3_600
MAX_TEXT_CHARS = 16_000

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}(?![\w-])")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)")
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ .-]{0,3}){12,18}\d(?!\d)")
_GOVERNMENT_ID_PATTERNS = (
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    re.compile(r"(?<!\d)784-\d{4}-\d{7}-\d(?!\d)"),
    re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)"),
    re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}[A-Z0-9]{11,30}(?![A-Za-z0-9])", re.IGNORECASE),
)
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)

# Credential material is sensitive even when it does not look like conventional
# PII.  Keep the labels visible for conversational context, but never pass the
# value to a tenant model, TTS provider, transcript, or tool boundary.
_LABELED_CREDENTIAL_RE = re.compile(
    r"""(?ix)
    \b(?:
        api[\s_-]?keys?|access[\s_-]?tokens?|refresh[\s_-]?tokens?|
        auth(?:entication|orization)?[\s_-]?tokens?|client[\s_-]?secrets?|
        secret[\s_-]?keys?|tokens?|secrets?|passwords?|passwds?|passcodes?|pwd
    )\b
    \s*(?:is|=|:)\s*
    (?:
        bearer\s+[A-Za-z0-9._~+/=-]{8,512}|
        \"(?:[^\"\\\r\n]|\\.){4,512}\"|
        '(?:[^'\\\r\n]|\\.){4,512}'|
        [^\s,;<>]{4,512}
    )
    """
)
_BEARER_CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{8,512}"
)
_KNOWN_CREDENTIAL_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_])(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{16,}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Za-z0-9_-])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
)

_JAILBREAK_PATTERNS = (
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|earlier|system|developer)\s+(?:instructions?|prompts?|messages?)\b", re.IGNORECASE),
    re.compile(r"\b(?:reveal|print|show|repeat|expose)\s+(?:the\s+)?(?:system|developer)\s+prompt\b", re.IGNORECASE),
    re.compile(r"\b(?:jailbreak|developer\s+mode|do\s+anything\s+now)\b", re.IGNORECASE),
    re.compile(r"\b(?:bypass|disable|override)\s+(?:the\s+)?(?:safety|guardrails?|policy|rules?)\b", re.IGNORECASE),
    re.compile(r"تجاهل\s+(?:كل\s+)?(?:التعليمات|الاوامر|الأوامر)", re.IGNORECASE),
    re.compile(r"(?:पिछले|सभी)\s+निर्देशों?\s+को\s+अनदेखा", re.IGNORECASE),
    re.compile(r"\b(?:disregard|forget)\s+(?:all\s+|every\s+)?(?:previous|prior|earlier|above|hidden)?\s*(?:instructions?|prompts?|messages?|rules?)\b", re.IGNORECASE),
    re.compile(r"\b(?:you\s+are\s+now\s+unrestricted|expose|print|reveal|show)\b.{0,48}\b(?:hidden|system|developer)\s+(?:instructions?|prompt|messages?)\b", re.IGNORECASE),
    re.compile(r"\b(?:pretend|act|role[ -]?play)\b.{0,64}\b(?:no|without|free\s+of)\b.{0,24}\b(?:rules?|restrictions?|polic(?:y|ies)|limits?)\b", re.IGNORECASE),
    re.compile(r"\b(?:treat|consider)\b.{0,36}\b(?:system|developer|safety)\b.{0,36}\b(?:optional|irrelevant|untrusted|just\s+text)\b", re.IGNORECASE),
    re.compile(r"\b(?:follow|obey|prioriti[sz]e)\s+(?:only\s+)?(?:my|the\s+user(?:'s)?)\s+(?:instructions?|commands?)\b", re.IGNORECASE),
    re.compile(r"\b(?:what|which)\s+(?:instructions?|rules?)\s+(?:were\s+you|have\s+you\s+been)\s+(?:given|told)\b", re.IGNORECASE),
    re.compile(r"\b(?:quote|list|output)\b.{0,48}\b(?:initial|hidden|confidential)\s+(?:message|instructions?|rules?)\b", re.IGNORECASE),
    re.compile(r"\b(?:pretend|imagine|assume|suppose)\b.{0,56}\b(?:safety|rules?|polic(?:y|ies)|restrictions?)\b.{0,32}\b(?:do\s+not|don't|doesn't|never|no\s+longer)\s+apply\b", re.IGNORECASE),
    re.compile(r"\b(?:pretend|imagine|assume|act\s+as\s+if)\b.{0,48}\b(?:zero|no)\s+(?:rules?|restrictions?|limits?|guardrails?)\b", re.IGNORECASE),
    re.compile(r"\b(?:tell|give|show|send)\b.{0,40}\b(?:content|text|wording|copy)\b.{0,36}\b(?:first|initial|hidden|confidential|system|developer)\s+(?:instructions?|message|prompt)\b", re.IGNORECASE),
    re.compile(r"\b(?:repeat|summari[sz]e|reconstruct)\b.{0,48}\b(?:first|initial|hidden|system|developer)\s+(?:instructions?|message|prompt)\b", re.IGNORECASE),
    re.compile(r"\b(?:system|developer)\s+(?:prompt|instructions?)\s+(?:batao|dikhao|bolo|share\s+karo)\b", re.IGNORECASE),
    re.compile(r"(?:اعرض|اظهر|أظهر|اكشف).{0,20}(?:تعليمات|أوامر).{0,12}(?:النظام|المطور)", re.IGNORECASE),
    re.compile(r"(?:تصرف|تظاهر).{0,32}(?:بدون|بلا).{0,20}(?:قيود|قواعد|سياسات)", re.IGNORECASE),
    re.compile(r"(?:اتبع|نفذ).{0,20}(?:تعليماتي|أوامري).{0,24}(?:بدلا|بدلاً).{0,20}(?:النظام|تعليماتك)", re.IGNORECASE),
    re.compile(r"(?:बिना|बगैर).{0,20}(?:नियम|पाबंदी|सुरक्षा).{0,24}(?:काम|जवाब|अभिनय)", re.IGNORECASE),
    re.compile(r"\b(?:mere|meri)\s+(?:instructions?|baat)\s+(?:hi\s+)?(?:follow|maano)\b.{0,32}\b(?:rules?|policy|nirdesh)\b", re.IGNORECASE),
)

_DANGEROUS_PATTERNS = (
    re.compile(r"\b(?:make|build|create|assemble)\s+(?:an?\s+)?(?:bomb|explosive|detonator|ied|explosive\s+device)\b", re.IGNORECASE),
    re.compile(r"\b(?:bomb|explosive|detonator|ied)\s+(?:making|building|instructions?|recipe)\b", re.IGNORECASE),
    re.compile(r"\b(?:how\s+to|ways?\s+to)\s+(?:kill\s+myself|commit\s+suicide|self[- ]?harm)\b", re.IGNORECASE),
    re.compile(r"\b(?:suicide\s+method|self[- ]?harm\s+instructions?|i\s+want\s+to\s+die)\b", re.IGNORECASE),
    re.compile(r"(?:كيف(?:ية)?\s+)?(?:اصنع|صنع|تركيب).{0,20}(?:قنبلة|متفجرات|عبوة\s+ناسفة)", re.IGNORECASE),
    re.compile(r"(?:طريقة\s+الانتحار|كيف\s+انتحر|اقتل\s+نفسي|أقتل\s+نفسي|ايذاء\s+النفس|إيذاء\s+النفس)", re.IGNORECASE),
    re.compile(r"(?:बम|विस्फोटक).{0,24}(?:कैसे\s+बनाएं|कैसे\s+बनाये|बनाना|तैयार|निर्माण)", re.IGNORECASE),
    re.compile(r"(?:आत्महत्या\s+कैसे|खुदकुशी|खुद\s+को\s+(?:मार|नुकसान)|मुझे\s+मरना\s+है)", re.IGNORECASE),
    re.compile(r"\b(?:bomb\s+kaise\s+banaye|suicide\s+kaise\s+kare|khud\s+ko\s+(?:mar|nuksan))\b", re.IGNORECASE),
    re.compile(r"\b(?:instructions?|steps?|guide|recipe)\s+(?:for|to\s+make)\s+(?:an?\s+)?explosive\s+device\b", re.IGNORECASE),
    re.compile(r"\b(?:how\s+(?:can|do)\s+i\s+)?(?:end\s+my\s+life|painless\s+way\s+to\s+die)\b", re.IGNORECASE),
    re.compile(r"\b(?:walk\s+me\s+through|teach\s+me|give\s+me\s+(?:steps|a\s+recipe))\b.{0,56}\b(?:poison|weapon|ransomware|malware|phishing)\b", re.IGNORECASE),
    re.compile(r"\b(?:hurt|poison|kill)\s+(?:a\s+person|someone|them)\b.{0,48}\b(?:without\s+getting\s+caught|undetected|secretly)\b", re.IGNORECASE),
    re.compile(r"\b(?:make|build|mix|create)\b.{0,36}\b(?:something|a\s+device|chemicals?)\b.{0,36}\b(?:explode|explodes|lethal|poisonous)\b", re.IGNORECASE),
    re.compile(r"\b(?:steal|harvest|capture|obtain)\b.{0,32}\b(?:passwords?|credentials?|login\s+tokens?|session\s+cookies?)\b", re.IGNORECASE),
    re.compile(r"\b(?:write|build|create)\b.{0,24}\b(?:ransomware|credential\s+stealer|phishing\s+kit|malware)\b", re.IGNORECASE),
    re.compile(r"\b(?:write|build|create|give)\b.{0,32}\b(?:code|script|tool|extension)\b.{0,48}\b(?:steals?|exfiltrates?|captures?|harvests?|grabs?)\b.{0,24}\b(?:cookies?|passwords?|credentials?|tokens?|api\s+keys?)\b", re.IGNORECASE),
    re.compile(r"\b(?:steal|exfiltrate|grab|harvest|capture)\b.{0,36}\b(?:browser\s+cookies?|session\s+tokens?|api\s+keys?|login\s+sessions?)\b", re.IGNORECASE),
    re.compile(r"\b(?:how|can\s+you|could\s+you|explain|teach|show)\b.{0,36}\b(?:make|prepare|mix|use)\b.{0,40}\b(?:poison|toxin|lethal\s+substance)\b", re.IGNORECASE),
    re.compile(r"\b(?:go\s+to\s+sleep\s+and\s+never\s+wake\s+up|make\s+sure\s+i\s+do\s+not\s+wake\s+up|end\s+it\s+all)\b", re.IGNORECASE),
    re.compile(r"(?:علمني|اعطني\s+خطوات|أعطني\s+خطوات).{0,40}(?:سم|تسميم|سلاح|برمجية\s+خبيثة|تصيد)", re.IGNORECASE),
    re.compile(r"(?:बिना\s+पकड़े|चुपके\s+से).{0,32}(?:ज़हर|जहर|मार|नुकसान)", re.IGNORECASE),
    re.compile(r"\b(?:password|credential)\s+(?:chori|kaise\s+churaye)|bina\s+pakde.{0,24}(?:zeher|maar)\b", re.IGNORECASE),
)

_MEDICAL_CLAIM_PATTERNS = (
    re.compile(r"\b(?:you|the patient)\s+(?:definitely\s+)?(?:have|has|are suffering from|is suffering from)\s+(?:cancer|diabetes|an?\s+infection|a\s+stroke|a\s+heart attack)\b", re.IGNORECASE),
    re.compile(r"\b(?:stop|start|double|reduce|increase)\s+(?:taking\s+)?(?:your\s+)?(?:medicine|medication|dose|dosage)\b", re.IGNORECASE),
    re.compile(r"\b(?:take|use)\s+\d+(?:\.\d+)?\s*(?:mg|mcg|ml|tablets?|pills?)\b", re.IGNORECASE),
    re.compile(r"\b(?:this is|my diagnosis is|you are diagnosed with)\b", re.IGNORECASE),
    re.compile(r"(?:أنت|المريض)\s+(?:مصاب|تعاني|يعاني)\s+ب?", re.IGNORECASE),
    re.compile(r"(?:أوقف|ابدأ|ضاعف|زد|قلل)\s+(?:الدواء|الجرعة|العلاج)", re.IGNORECASE),
    re.compile(r"(?:आपको|मरीज़ को)\s+(?:निश्चित रूप से\s+)?(?:कैंसर|मधुमेह|संक्रमण|दिल का दौरा)\s+है", re.IGNORECASE),
    re.compile(r"(?:दवा|खुराक)\s+(?:बंद|शुरू|दोगुनी|कम|बढ़ा)\s+", re.IGNORECASE),
    re.compile(r"\b(?:this|that)\s+(?:medicine|medication|drug|treatment)\s+(?:will|is\s+guaranteed\s+to)\s+(?:cure|heal|fix)\s+(?:you|your\s+condition|the\s+condition)\b", re.IGNORECASE),
    re.compile(r"\b(?:take|use)\s+(?:one|two|three|four|\d+)\s+(?:aspirin|ibuprofen|paracetamol|acetaminophen|antibiotic|tablets?|pills?)\b.{0,24}\b(?:daily|every\s+day|per\s+day)\b", re.IGNORECASE),
    re.compile(r"(?:هذا|ذلك)\s+(?:الدواء|العلاج).{0,20}(?:سيشفي|يعالج\s+تماما|يعالج\s+تماماً).{0,20}(?:حالتك|مرضك)?", re.IGNORECASE),
    re.compile(r"\b(?:yeh?|yah)\s+(?:dawa|medicine)\s+(?:tumhe|aapko).{0,20}(?:theek|thik|cure)\s+(?:kar\s+degi|karegi|karega)\b", re.IGNORECASE),
)
_LEGAL_CLAIM_PATTERNS = (
    re.compile(r"\b(?:you are|you're)\s+(?:legally\s+)?(?:entitled|required|liable|obligated)\b", re.IGNORECASE),
    re.compile(r"\b(?:you will|you'll)\s+(?:definitely\s+)?(?:win|lose)\s+(?:the\s+)?(?:case|lawsuit|appeal)\b", re.IGNORECASE),
    re.compile(r"\b(?:this is|as)\s+(?:formal\s+)?legal advice\b", re.IGNORECASE),
    re.compile(r"\b(?:the law guarantees|guaranteed legal outcome)\b", re.IGNORECASE),
    re.compile(r"(?:أنت|انتم)\s+(?:ملزم|مسؤول|مستحق)\s+(?:قانونياً|قانونا)?", re.IGNORECASE),
    re.compile(r"(?:ستربح|ستخسر)\s+(?:القضية|الدعوى)\s+(?:بالتأكيد)?", re.IGNORECASE),
    re.compile(r"(?:आप|आपको)\s+कानूनी रूप से\s+(?:हकदार|ज़िम्मेदार|बाध्य)", re.IGNORECASE),
    re.compile(r"(?:मुकदमा|केस)\s+(?:निश्चित रूप से\s+)?(?:जीतेंगे|हारेंगे)", re.IGNORECASE),
    re.compile(r"\b(?:the\s+)?(?:law|statute|regulation)\s+(?:guarantees?|assures?)\s+(?:you\s+)?(?:compensation|damages|a\s+payout|success)\b", re.IGNORECASE),
)
_INVENTED_DATA_PATTERNS = (
    re.compile(r"\b(?:our|the)\s+(?:records?|system)\s+(?:show|shows|confirm|confirms)\b", re.IGNORECASE),
    re.compile(r"\byour\s+(?:account balance|credit score|order number|order status|claim number|policy number)\s+(?:is|was)\b", re.IGNORECASE),
    re.compile(r"\bI(?:'ve| have)\s+(?:confirmed|booked|cancelled|canceled|refunded|charged|updated|submitted|approved)\b", re.IGNORECASE),
    re.compile(r"\b(?:the|your)\s+(?:refund|appointment|booking|payment|application|claim)\s+(?:is|has been)\s+(?:confirmed|approved|completed|processed|cancelled|canceled)\b", re.IGNORECASE),
    re.compile(r"\byour\s+(?:order|package|parcel)\s+(?:was|has\s+been|is)\s+shipped\b.{0,48}\b(?:arrives?|will\s+arrive|delivery)\b", re.IGNORECASE),
    re.compile(r"(?:طلبك|شحنتك).{0,16}(?:تم\s+شحنه|تم\s+شحنها|شحن).{0,28}(?:سيصل|ستصل|غدا|غداً)", re.IGNORECASE),
    re.compile(r"\b(?:aapka|tumhara)\s+(?:order|parcel)\s+ship(?:ped)?\s+ho\s+gaya\b", re.IGNORECASE),
)
_UNCERTAINTY_PATTERNS = (
    re.compile(r"\bI(?:'m| am)\s+not\s+(?:sure|certain)\b", re.IGNORECASE),
    re.compile(r"\bI\s+(?:think|guess|suppose)\b", re.IGNORECASE),
    re.compile(r"\b(?:probably|maybe|perhaps)\b", re.IGNORECASE),
    re.compile(r"\bI\s+(?:cannot|can't)\s+(?:verify|confirm)\b", re.IGNORECASE),
    re.compile(r"(?:لست|أنا غير)\s+(?:متأكد|واثق)", re.IGNORECASE),
    re.compile(r"(?:ربما|على الأرجح)", re.IGNORECASE),
    re.compile(r"(?:मुझे यकीन नहीं|मैं निश्चित नहीं|शायद)", re.IGNORECASE),
)

_PLATFORM_BLOCKED_TOPICS = frozenset(
    {
        "build a bomb",
        "make a bomb",
        "steal credentials",
        "buy stolen cards",
        "self harm instructions",
        "suicide method",
    }
)
_INTERNAL_TOOLS = frozenset({"play_workflow_audio", "request_human_handoff"})


class SafetyViolation(RuntimeError):
    """Fail-closed runtime-policy rejection without echoing unsafe input."""

    def __init__(self, reason: str, safe_message: str, *, handoff: bool = False) -> None:
        super().__init__(safe_message)
        self.reason = reason
        self.safe_message = safe_message
        self.handoff = handoff


@dataclass(frozen=True)
class RailDecision:
    text: str
    blocked: bool = False
    requires_handoff: bool = False
    reason: str = "allowed"
    redactions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RailPolicy:
    """Server-derived policy. Empty external allowlists mean deny all."""

    max_call_seconds: int = DEFAULT_MAX_CALL_SECONDS
    tool_allowlist: frozenset[str] = field(default_factory=frozenset)
    webhook_allowlist: frozenset[str] = field(default_factory=frozenset)
    blocked_topics: frozenset[str] = field(default_factory=lambda: _PLATFORM_BLOCKED_TOPICS)

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, object],
        environ: Mapping[str, str] | None = None,
    ) -> RailPolicy:
        env = os.environ if environ is None else environ
        runtime_policy = config.get("runtimePolicy")
        if not isinstance(runtime_policy, Mapping):
            runtime_policy = {}

        hard_max = _positive_int(env.get("PLATFORM_MAX_CALL_SECONDS"), DEFAULT_PLATFORM_MAX_CALL_SECONDS)
        hard_max = min(14_400, max(60, hard_max))
        requested_max = _positive_int(config.get("maxCallSeconds"), DEFAULT_MAX_CALL_SECONDS)
        max_call_seconds = min(requested_max, hard_max)

        tools = _string_set(runtime_policy.get("toolAllowlist", config.get("toolAllowlist")))
        webhooks = frozenset(
            value
            for raw in _string_set(runtime_policy.get("webhookAllowlist", config.get("webhookAllowlist")))
            if (value := canonical_webhook(raw)) is not None
        )
        tenant_topics = _string_set(runtime_policy.get("blockedTopics", config.get("blockedTopics")))
        topics = _PLATFORM_BLOCKED_TOPICS | frozenset(_normalise(value) for value in tenant_topics)
        return cls(
            max_call_seconds=max_call_seconds,
            tool_allowlist=frozenset(_normalise_tool(value) for value in tools),
            webhook_allowlist=webhooks,
            blocked_topics=topics,
        )

    def tool_allowed(self, identifier: str) -> bool:
        normalised = _normalise_tool(identifier)
        return normalised in _INTERNAL_TOOLS or normalised in self.tool_allowlist

    def webhook_allowed(self, target: str) -> bool:
        canonical = canonical_webhook(target)
        if canonical is None:
            return False
        if canonical in self.webhook_allowlist:
            return True
        for allowed in self.webhook_allowlist:
            if not allowed.endswith("/*"):
                continue
            prefix = allowed[:-1]
            if canonical.startswith(prefix):
                return True
        return False


def check_user_input(text: str, policy: RailPolicy) -> RailDecision:
    """Inspect caller text before it is sent to any tenant-selected model."""

    bounded = _bounded_text(text)
    card_matches = [match.group(0) for match in _CARD_CANDIDATE_RE.finditer(bounded) if _looks_like_card(match.group(0))]
    if card_matches:
        return RailDecision(
            text="[PAYMENT_CARD_DATA_BLOCKED]",
            blocked=True,
            requires_handoff=True,
            reason="payment_card_data",
            redactions=("payment_card",),
        )

    normalised = _normalise_for_safety(bounded)
    if any(pattern.search(normalised) for pattern in _JAILBREAK_PATTERNS):
        return RailDecision(
            text="[PROMPT_OVERRIDE_REQUEST_BLOCKED]",
            blocked=True,
            reason="jailbreak_attempt",
        )
    if any(pattern.search(normalised) for pattern in _DANGEROUS_PATTERNS):
        return RailDecision(
            text="[DANGEROUS_REQUEST_BLOCKED]",
            blocked=True,
            requires_handoff=True,
            reason="dangerous_or_self_harm_request",
        )
    if any(topic and topic in normalised for topic in policy.blocked_topics):
        return RailDecision(
            text="[BLOCKED_TOPIC_REQUEST]",
            blocked=True,
            requires_handoff=True,
            reason="blocked_topic",
        )

    redacted, labels = redact_pii(bounded)
    return RailDecision(text=redacted, redactions=labels)


def check_model_output(text: str, *, verified_facts: Sequence[str] = ()) -> RailDecision:
    """Validate a complete model response before any audio reaches TTS."""

    bounded = _bounded_text(text)
    normalised = _normalise(bounded)
    safety_text = _normalise_for_safety(bounded)
    if any(pattern.search(safety_text) for pattern in _DANGEROUS_PATTERNS):
        return _handoff_decision(
            "dangerous_or_self_harm_response",
            "I can’t provide instructions that could cause harm. I’m requesting human help.",
        )
    if any(pattern.search(safety_text) for pattern in _MEDICAL_CLAIM_PATTERNS):
        return _handoff_decision(
            "medical_claim",
            "I can’t provide a medical conclusion or treatment instruction. I’m requesting a human specialist to help you safely.",
        )
    if any(pattern.search(safety_text) for pattern in _LEGAL_CLAIM_PATTERNS):
        return _handoff_decision(
            "legal_claim",
            "I can’t provide a legal conclusion or promise an outcome. I’m requesting a qualified human to help you.",
        )
    if any(pattern.search(safety_text) for pattern in _INVENTED_DATA_PATTERNS) and not _supported_by_verified_fact(normalised, verified_facts):
        return _handoff_decision(
            "unverified_customer_data",
            "I can’t verify that customer or account result, so I won’t present it as fact. I’m requesting a human review.",
        )
    if any(pattern.search(safety_text) for pattern in _UNCERTAINTY_PATTERNS):
        return _handoff_decision(
            "model_uncertainty",
            "I can’t verify that confidently. I’m requesting a human teammate instead of guessing.",
        )

    redacted, labels = redact_pii(bounded)
    return RailDecision(text=redacted, redactions=labels)


def redact_pii(text: str) -> tuple[str, tuple[str, ...]]:
    """Redact common PII without returning matched values to logs or callers."""

    labels: list[str] = []
    value = text

    if _LABELED_CREDENTIAL_RE.search(value):
        labels.append("credential")
        value = _LABELED_CREDENTIAL_RE.sub("[CREDENTIAL_REDACTED]", value)
    if _BEARER_CREDENTIAL_RE.search(value):
        labels.append("credential")
        value = _BEARER_CREDENTIAL_RE.sub("[CREDENTIAL_REDACTED]", value)
    for pattern in _KNOWN_CREDENTIAL_PATTERNS:
        if pattern.search(value):
            labels.append("credential")
            value = pattern.sub("[CREDENTIAL_REDACTED]", value)

    for pattern in _GOVERNMENT_ID_PATTERNS:
        if pattern.search(value):
            labels.append("government_id")
            value = pattern.sub("[GOVERNMENT_ID_REDACTED]", value)

    def redact_cards(match: re.Match[str]) -> str:
        if not _looks_like_card(match.group(0)):
            return match.group(0)
        labels.append("payment_card")
        return "[PAYMENT_CARD_REDACTED]"

    value = _CARD_CANDIDATE_RE.sub(redact_cards, value)
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


def sanitize_workflow_prompt(text: object, policy: RailPolicy) -> str:
    """Bound node prompts and remove PII or non-allowlisted external targets."""

    value, _labels = redact_pii(str(text or "")[:2_000])

    def replace_url(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(".,);]")
        suffix = match.group(0)[len(url) :]
        return (url if policy.webhook_allowed(url) else "[EXTERNAL_URL_BLOCKED]") + suffix

    return _URL_RE.sub(replace_url, value)


def assert_outbound_consent(room_name: str, config: Mapping[str, object]) -> None:
    """Fail closed before joining an outbound room without server-verified consent."""

    call = config.get("call")
    if not isinstance(call, Mapping):
        call = {}
    direction = str(call.get("direction") or config.get("direction") or "").strip().lower()
    is_outbound = room_name.startswith("call-") or direction == "outbound"
    if not is_outbound:
        return

    consent_verified = call.get("consentVerified", config.get("consentVerified"))
    if consent_verified is not True:
        raise SafetyViolation(
            "outbound_consent_missing",
            "Outbound calling consent has not been verified.",
        )

    recording_enabled = call.get("recordingEnabled", config.get("recordingEnabled", False)) is True
    recording_consent = call.get("recordingConsentVerified", config.get("recordingConsentVerified"))
    if recording_enabled and recording_consent is not True:
        raise SafetyViolation(
            "recording_consent_missing",
            "Recording consent has not been verified.",
        )


def assert_workflow_action_allowed(policy: RailPolicy, kind: str, identifier: str) -> None:
    """Enforce deny-by-default action allowlists outside the model prompt."""

    normalised_kind = kind.strip().lower()
    allowed = policy.webhook_allowed(identifier) if normalised_kind == "webhook" else policy.tool_allowed(identifier)
    if not allowed:
        raise SafetyViolation(
            f"{normalised_kind or 'action'}_not_allowlisted",
            "The requested external action is not approved for this agent.",
            handoff=True,
        )


def guard_tool_text(
    policy: RailPolicy,
    tool_name: str,
    value: object,
    *,
    is_result: bool = False,
    verified_facts: Sequence[str] = (),
) -> str:
    """Check textual tool arguments/results at the Python execution boundary."""

    assert_workflow_action_allowed(policy, "tool", tool_name)
    if is_result:
        decision = check_model_output(str(value or ""), verified_facts=verified_facts)
    else:
        decision = check_user_input(str(value or ""), policy)
    if decision.blocked:
        raise SafetyViolation(decision.reason, decision.text, handoff=decision.requires_handoff)
    return decision.text


def validate_control_plane_url(raw_url: str, environ: Mapping[str, str] | None = None) -> str:
    """Accept HTTPS origins, or explicitly enabled private HTTP origins only."""

    env = os.environ if environ is None else environ
    value = raw_url.strip().rstrip("/")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise RuntimeError("Agent control plane URL is invalid") from error
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.scheme not in {"http", "https"}
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise RuntimeError("Agent control plane URL is invalid")
    if parsed.scheme == "https":
        return value

    if env.get("ALLOW_PRIVATE_APP_URL", "").strip().lower() != "true":
        raise RuntimeError("Agent control plane must use HTTPS")
    configured = {
        item.strip().rstrip("/")
        for item in env.get("PRIVATE_APP_ORIGINS", "").split(",")
        if item.strip()
    }
    if value not in configured:
        raise RuntimeError("Private HTTP control plane host is not allowed")
    return value


def canonical_webhook(raw_url: str) -> str | None:
    value = raw_url.strip()
    wildcard = value.endswith("/*")
    candidate = value[:-2] if wildcard else value
    parsed = urlparse(candidate)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in {None, 443}
    ):
        return None
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    path = parsed.path or "/"
    canonical = urlunparse(("https", host, path.rstrip("/") or "/", "", parsed.query, ""))
    return f"{canonical.rstrip('/')}/*" if wildcard else canonical


def _handoff_decision(reason: str, message: str) -> RailDecision:
    return RailDecision(text=message, blocked=True, requires_handoff=True, reason=reason)


def _supported_by_verified_fact(output: str, facts: Sequence[str]) -> bool:
    # Verified values must come from a server-side tool result/config envelope.  A
    # tenant prompt cannot add them.  Exact bounded inclusion avoids fuzzy guesses.
    for fact in facts[:100]:
        normalised_fact = _normalise(str(fact))[:500]
        if len(normalised_fact) >= 8 and normalised_fact in output:
            return True
    return False


def _bounded_text(value: object) -> str:
    text = str(value or "")
    return text[:MAX_TEXT_CHARS]


def _normalise(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    clean: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category == "Cf" or character == "ـ" or "\u064b" <= character <= "\u065f":
            continue
        clean.append(" " if category in {"Cc", "Cs"} else character)
    return " ".join("".join(clean).casefold().split())


def _normalise_for_safety(value: str) -> str:
    normalised = _normalise(value)

    def join_spaced_letters(match: re.Match[str]) -> str:
        return re.sub(r"\s+", "", match.group(0))

    # Collapse deliberate single-letter spacing ("i g n o r e") after Unicode
    # format controls were removed by _normalise. Ordinary multiword prose is
    # unaffected because every joined token must be exactly one Latin letter.
    return re.sub(
        r"(?<![a-z])(?:[a-z]\s+){3,}[a-z](?![a-z])",
        join_spaced_letters,
        normalised,
        flags=re.IGNORECASE,
    )


def _normalise_tool(value: str) -> str:
    return re.sub(r"[^a-z0-9_.:-]", "", _normalise(value))[:128]


def _string_set(value: object) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(item).strip()[:500] for item in value if isinstance(item, str) and item.strip())


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _looks_like_card(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        checksum += value
    return checksum % 10 == 0
