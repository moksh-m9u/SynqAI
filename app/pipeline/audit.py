"""Audit trail: one JSONL line per step per ticket. Every decision records what
was decided, on what data, under which rule, by whom or what, plus the LangSmith
run/thread ids so any decision is replayable.

The audit file is per-run: rerunning the pipeline atomically rewrites it, so the
file on disk always reflects the *latest* run and replays byte-for-byte."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import AUDIT_DIR, AUDIT_OUT

_OPENED = False


def _rotate() -> None:
    global _OPENED
    if not _OPENED:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        AUDIT_OUT.write_text("", encoding="utf-8")
        _OPENED = True


def audit(row: dict[str, Any]) -> None:
    line = {
        "at": datetime.now(timezone.utc).isoformat(),
        "run_id": row.get("run_id", ""),
        "thread_id": row.get("thread_id", ""),
        "ticket_id": row.get("ticket_id", ""),
        "step": row.get("step", ""),
        "node": row.get("node", ""),
        "decision": row.get("decision", ""),
        "data_refs": row.get("data_refs", []),
        "rule_ids": row.get("rule_ids", []),
        "citations": row.get("citations", []),
        "actor": row.get("actor", "system"),
        "detail": row.get("detail", {}),
    }
    _rotate()
    import json
    with open(AUDIT_OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")