"""Queue preprocessing: dedupe, schema recovery, quarantine, canonical tickets.

Native loader -> Pydantic validation -> failure -> schema recovery -> validation.
Duplicates are processed exactly once; broken records are quarantined WITH a
reason and an alert, never silently dropped, never a crash.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ingest.models import Ticket
from app.ingest.schema_recovery import recover_record
from app.utils import canonicalize_reg, parse_datetime

REQUIRED = ["ticket_id", "created_at", "vehicle", "origin_hub", "destination", "client"]


@dataclass
class PreparedTicket:
    status: str                     # valid | quarantined
    reason: str = ""
    ticket_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    normalized: dict[str, Any] = field(default_factory=dict)   # fields for the graph
    ticked: Ticket | None = None
    duplicate_of: str = ""
    recovered: bool = False
    report: dict[str, Any] = field(default_factory=dict)


def dedupe(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """First occurrence wins; exact repeats and '(sync copy)' variants collapse.

    Records with a missing ticket_id are NOT dropped here: they may be rescued by
    schema recovery (third-party uploads rename keys). If they stay broken,
    prepare() quarantines them with a reason."""
    seen: set[str] = set()
    canonical: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for r in records:
        tid = str(r.get("ticket_id") or "").strip()
        if not tid or tid in seen:
            if tid:
                duplicates.append(r)
            else:
                canonical.append(r)  # maybe recoverable
            continue
        seen.add(tid)
        canonical.append(r)
    return canonical, duplicates


def prepare(record: dict[str, Any], seen: set[str], run_id: str = "",
            thread_id: str = "", groq_llm=None) -> PreparedTicket:
    """Native validation -> schema recovery -> quarantine (never a crash).

    A missing/renamed ticket_id is NOT enough to give up: schema recovery runs
    first, because third-party uploads rename keys. Only a record that is still
    broken after the rescue attempt is quarantined."""
    tid = str(record.get("ticket_id") or "").strip()
    if tid in seen:
        return PreparedTicket(status="quarantined", ticket_id=tid or "(missing_id)", raw=record,
                              duplicate_of=tid, reason=f"duplicate of {tid}")
    seen.add(tid or "(pending_recovery)")

    normalized: dict[str, Any] | None = None
    report: dict[str, Any] = {}
    recovered = False

    try:
        ticket = Ticket.model_validate(record)
    except Exception as exc:
        # --- schema recovery attempt (Qwen suggests mapping, Python extracts) ---
        normalized, report = recover_record(record, run_id=run_id, thread_id=thread_id,
                                            groq_llm=groq_llm)
        recovered = normalized is not None
        if not recovered:
            reasons = _broken_reasons(record)
            return PreparedTicket(status="quarantined", ticket_id=tid or "(missing_id)",
                                  raw=record,
                                  reason="; ".join(reasons) or f"validation failed: {exc}",
                                  report=report, recovered=False)
        try:
            ticket = Ticket.model_validate(normalized)
        except Exception as vexc:
            return PreparedTicket(status="quarantined", ticket_id=tid or "(missing_id)", raw=record,
                                  reason=f"schema recovery produced invalid record: {vexc}",
                                  report=report, recovered=True)
        resolved_tid = tid or str(normalized.get("ticket_id") or "(missing_id)")
        seen.discard("(pending_recovery)")
        if resolved_tid in seen:
            return PreparedTicket(status="quarantined", ticket_id=resolved_tid, raw=record,
                                  duplicate_of=resolved_tid,
                                  reason=f"duplicate of {resolved_tid} (after schema recovery)",
                                  report=report, recovered=True)
        seen.add(resolved_tid)
        return PreparedTicket(status="valid", ticket_id=resolved_tid, raw=record,
                              normalized=ticket.model_dump(), ticked=ticket,
                              recovered=True, report=report)

    # native validation passed — check semantic completeness of critical fields
    problems = _broken_reasons(record)
    if problems:
        return PreparedTicket(status="quarantined", ticket_id=tid, raw=record,
                              reason="; ".join(problems), recovered=recovered)
    return PreparedTicket(status="valid", ticket_id=tid, raw=record,
                          normalized=ticket.model_dump(), ticked=ticket,
                          recovered=recovered)


def _broken_reasons(record: dict[str, Any]) -> list[str]:
    """Critical-field completeness rules that separate quarantine from rescue."""
    reasons: list[str] = []
    tid = str(record.get("ticket_id") or "")
    ts = parse_datetime(record.get("created_at"))
    if ts is None:
        reasons.append("created_at not a parseable date")
    reg_raw = record.get("vehicle")
    reg = canonicalize_reg(reg_raw) if reg_raw else ""
    if not reg or len(reg) < 8 or "??" in str(reg_raw).lower():
        reasons.append(f"vehicle registration invalid/unknown: {reg_raw!r}")
    if not str(record.get("origin_hub") or "").strip():
        reasons.append("origin_hub empty")
    if not str(record.get("destination") or "").strip():
        reasons.append("destination empty")
    if not str(record.get("issue") or "").strip():
        reasons.append("issue empty")
    return reasons