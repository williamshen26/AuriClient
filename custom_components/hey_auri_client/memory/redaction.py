"""Secret detection and redaction for text that may become long-term memory.

Applied before anything is written to the memory store (conversation log,
remembered statements, imports). AuriService applies the same rules again
server-side (AuriService/redaction.py); keep the two copies in step. Phone
numbers, addresses and birthdays are deliberately NOT treated as secrets:
they are useful memories and are marked private instead.
"""
from __future__ import annotations

import re

SECRET_PLACEHOLDER = "[secret omitted]"

# Labels whose value is a credential.
_CREDENTIAL_LABEL = (
    r"(?:wi-?fi\s+)?pass(?:word|code|phrase)"
    r"|pin(?:\s+(?:code|number))?"
    r"|(?:door|gate|garage|alarm|lock|lockbox|safe|security|access|entry|verification"
    r"|confirmation|one[-\s]?time|2fa|mfa|backup)\s+code"
    r"|otp|cvv|cvc|security\s+answer"
    r"|api[-\s]?key|secret\s+key|access\s+token|auth(?:entication)?\s+token|bearer\s+token|private\s+key"
)
# The value never ends in sentence punctuation ("the code is 8812." keeps its full stop).
_LABELED_SECRET = re.compile(
    rf"(?i)\b(?:{_CREDENTIAL_LABEL})\b(?P<sep>\s*(?:is|was|=|:)\s*|\s+)"
    r"(?P<value>\"[^\"\n]{1,80}\"|'[^'\n]{1,80}'|[^\s,;]*[^\s,;.!?)(])"
)
_EXPLICIT_SEPARATOR = re.compile(r"(?i)\b(?:is|was)\b|[=:]")
# Words that follow a credential label without being its value
# ("the Wi-Fi password is on the fridge", "my PIN was reset").
_NOT_A_VALUE = frozenset(
    {
        "a", "an", "the", "on", "in", "at", "under", "behind", "inside", "near", "next", "by",
        "written", "taped", "stuck", "printed", "stored", "kept", "saved", "located", "listed",
        "shown", "posted", "available", "here", "there", "not", "no", "none", "same", "different",
        "changing", "changed", "reset", "expired", "wrong", "correct", "required", "needed",
        "set", "unknown", "secret", "private", "for", "to", "of", "and", "or", "it", "that",
        "this", "what", "which", "please", "again",
    }
)

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
)
# Long random-looking strings (32+ chars mixing upper case, lower case and digits).
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")

_CARD_CANDIDATE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_US_SSN = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
_CA_SIN = re.compile(r"(?<![\d-])\d{3}[ -]\d{3}[ -]\d{3}(?![\d-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_ACCOUNT_NUMBER = re.compile(
    r"(?i)\b(?:bank\s+)?(?:account|acct|routing|transit|institution|card)\s*"
    r"(?:number|no\.?|num|#)?\s*(?:is|was|:|=)?\s*(?P<value>\d[\d -]{4,}\d)"
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    compact = candidate.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        number = int("".join(str(int(char, 36)) for char in rearranged))
    except ValueError:
        return False
    return number % 97 == 1


def find_secret_spans(text: str) -> list[tuple[int, int]]:
    """Return sorted, merged (start, end) spans of secrets found in text."""
    if not text:
        return []

    spans: list[tuple[int, int]] = []

    for match in _LABELED_SECRET.finditer(text):
        value = match.group("value")
        bare = value.strip("\"'.!?)(").lower()
        if not bare or bare in _NOT_A_VALUE:
            continue
        explicit = bool(_EXPLICIT_SEPARATOR.search(match.group("sep")))
        if not explicit and not any(char.isdigit() for char in bare):
            continue
        spans.append((match.start("value"), match.end("value")))

    for pattern in _TOKEN_PATTERNS:
        spans.extend((m.start(), m.end()) for m in pattern.finditer(text))

    for match in _LONG_TOKEN.finditer(text):
        token = match.group(0)
        if (
            any(char.islower() for char in token)
            and any(char.isupper() for char in token)
            and any(char.isdigit() for char in token)
        ):
            spans.append((match.start(), match.end()))

    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            spans.append((match.start(), match.end()))

    spans.extend((m.start(), m.end()) for m in _US_SSN.finditer(text))

    for match in _CA_SIN.finditer(text):
        if _luhn_ok(re.sub(r"\D", "", match.group(0))):
            spans.append((match.start(), match.end()))

    for match in _IBAN.finditer(text):
        if _iban_ok(match.group(0)):
            spans.append((match.start(), match.end()))

    spans.extend((m.start("value"), m.end("value")) for m in _ACCOUNT_NUMBER.finditer(text))

    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def contains_secret(text: str) -> bool:
    """Return True when text contains anything that looks like a secret."""
    return bool(find_secret_spans(text))


def redact(text: str) -> tuple[str, bool]:
    """Replace secrets with SECRET_PLACEHOLDER; returns (text, whether anything was replaced)."""
    spans = find_secret_spans(text)
    if not spans:
        return text, False

    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        parts.append(SECRET_PLACEHOLDER)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), True
