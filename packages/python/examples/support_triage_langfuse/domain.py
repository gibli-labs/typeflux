from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, tzinfo
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class DomainConfig:
    teams: frozenset[str]
    on_call_primary: str
    on_call_secondary: str
    business_hours_start: time
    business_hours_end: time
    business_hours_tz: tzinfo


def load_config() -> DomainConfig:
    return DomainConfig(
        teams=frozenset({"billing", "technical", "account", "oncall-engineer", "escalation"}),
        on_call_primary="alice-chen",
        on_call_secondary="bob-patel",
        business_hours_start=time.fromisoformat("09:00"),
        business_hours_end=time.fromisoformat("18:00"),
        business_hours_tz=ZoneInfo("America/New_York"),
    )


def is_within_business_hours(now: datetime, cfg: DomainConfig) -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    local = now.astimezone(cfg.business_hours_tz)
    if local.weekday() >= 5:
        return False
    current = local.time()
    return cfg.business_hours_start <= current < cfg.business_hours_end


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)")
_CARD_RE = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_REDACTIONS = (
    (_EMAIL_RE, "[EMAIL]"),
    (_PHONE_RE, "[PHONE]"),
    (_CARD_RE, "[CARD]"),
)


def redact_pii(text: str) -> tuple[str, int]:
    redacted = text
    total = 0
    for pattern, placeholder in _REDACTIONS:
        if pattern is _CARD_RE:
            redacted, count = _redact_card_matches(redacted, placeholder)
        else:
            redacted, count = pattern.subn(placeholder, redacted)
        total += count
    return redacted, total


def _redact_card_matches(text: str, placeholder: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        candidate = match.group(0)
        if not _is_luhn_valid_candidate(candidate):
            return candidate
        count += 1
        return placeholder

    return _CARD_RE.sub(replace, text), count


def _is_luhn_valid_candidate(candidate: str) -> bool:
    digits = candidate.replace(" ", "").replace("-", "")
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    return _luhn_checksum(digits) == 0


def _luhn_checksum(digits: str) -> int:
    total = 0
    for index, char in enumerate(digits[::-1]):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10


COMPLIANCE_FOOTER = (
    "\n\n---\n"
    "This reply was drafted with AI assistance and may be reviewed before sending. "
    "For billing inquiries, you may also contact support@example.com."
)
