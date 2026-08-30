"""Shared deterministic helpers: plate canonicalisation, dates, artifact IO."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_REG_CLEAN = re.compile(r"[^A-Z0-9]")
_REG_PATTERN = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{1,2}\d{4}$")
_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def build_llm(max_tokens: int = 512, temperature: float = 0.0) -> Any | None:
    """Shared lazy Qwen-on-Groq handle, or None when the provider is unreachable.
    All Qwen use is pinned to the single model in .env (GROQ_MODEL)."""
    _lock = threading.Lock()
    with _lock:
        try:
            from app.config import LLM_MODEL_ID
            from langchain_groq import ChatGroq
            return ChatGroq(model=LLM_MODEL_ID, temperature=temperature, max_tokens=max_tokens)
        except Exception:
            return None


def canonicalize_reg(value: str) -> str:
    """Normalise an Indian plate to canonical form: UP40IM3144. Non-canonical
    but resovable strings (e.g. 'UP-40-IM-3144', 'up 86 cm 7252') collapse here."""
    s = _REG_CLEAN.sub("", str(value).upper())
    return s


def is_plausible_reg(value: str) -> bool:
    s = canonicalize_reg(value)
    if len(s) < 8:
        return False
    # verify leading state chars are letters, digits follow
    return bool(_REG_PATTERN.match(s)) or bool(re.match(r"^[A-Z]{2}\d{2,}", s))


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s or s.lower() in {"na", "nan", "null", "none", "unknown", "n/a", ""}:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: Any):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    from datetime import date
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def atomic_write_json(path: Path, data: Any) -> None:
    """Write a JSON artifact atomically (tmp file + rename) for crash safety."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, default=str, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def iso(ts) -> str:
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def night_hour(hour: int) -> bool:
    """Night run window: after 18:00 or before 06:00."""
    return hour >= 18 or hour < 6