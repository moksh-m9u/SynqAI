"""PII masking.

The hard gate: no raw personal datum (phone, Aadhaar, driving licence) may ever
appear in an outbound action, served response, or evaluator-visible log/artifact.

Masking is applied at ingestion time, BEFORE anything is written to the knowledge
store, artifacts, outputs or audit. Vehicle registration numbers are NOT personal
data and are preserved (they are the operational keys the system runs on).
"""
from __future__ import annotations

import re

_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?91[- ]?|0)?[6-9]\d{4}[- ]?\d{5}(?!\d)"
)
_AADHAAR_RE = re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}\b")
_DL_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}[ -]?\d{3,}[\d](?!\d)")
_DL_NO_SPACE_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{14}(?!\d)")
_LANDLINE_RE = re.compile(r"(?<!\w)(?:\+?91[- ]?)?0\d{2,4}[- ]?\d{6,8}(?!\d)")

MASK = "***MASKED***"


def mask_phone(text: str) -> str:
    return _PHONE_RE.sub(MASK, text or "")


def mask_landline(text: str) -> str:
    return _LANDLINE_RE.sub(MASK, text or "")


def mask_aadhaar(text: str) -> str:
    return _AADHAAR_RE.sub(MASK, text or "")


def mask_dl(text: str) -> str:
    text = _DL_RE.sub(MASK, text or "")
    return _DL_NO_SPACE_RE.sub(MASK, text)


def mask(text: str) -> str:
    """Apply all personal-data masks. Vehicle plates are untouched."""
    if not text:
        return text
    out = mask_phone(text)
    out = mask_landline(out)
    out = mask_aadhaar(out)
    out = mask_dl(out)
    return out


def mask_all(*values: str) -> list[str]:
    return [mask(v) for v in values]