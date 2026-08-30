"""Exactly-once business-truth writers (outbox pattern).

- work_orders.jsonl  : {work_order_id, ticket_id, vehicle_reg, created_at, citations}
- comms_pending.jsonl: drafts awaiting HITL, full context + citations for the approver
- comms_sent.jsonl    : {message_id, ticket_id, recipient, body, approved_by, sent_at}
- quarantine.jsonl   : broken records with a reason

Idempotency: a ticket action is written at most once, guarded by a SQLite registry
AND a scan of the authoritative JSONL file on startup. Re-running the pipeline can
therefore never double anything and never lose anything.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import (ARTIFACTS_DIR, COMMS_PENDING_OUT, COMMS_SENT_OUT,
                        QUARANTINE_OUT, WORK_ORDERS_OUT)
from app.utils import read_jsonl


class Outbox:
    def __init__(self, state_db: Path):
        self.db = sqlite3.connect(state_db)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS outbox_registry (
                action TEXT NOT NULL,
                ticket_id TEXT NOT NULL,
                run_id TEXT,
                at TEXT,
                PRIMARY KEY (action, ticket_id)
            )""")
        self.db.commit()
        # rebuild registry from authoritative files (business truth wins)
        self._load_existing()

    def _load_existing(self) -> None:
        for action, path in (("work_order", WORK_ORDERS_OUT), ("comms_draft", COMMS_PENDING_OUT),
                             ("comms_sent", COMMS_SENT_OUT), ("quarantine", QUARANTINE_OUT)):
            for rec in read_jsonl(path):
                key = rec.get("ticket_id")
                if key:
                    self.db.execute("INSERT OR IGNORE INTO outbox_registry (action, ticket_id, at) VALUES (?, ?, ?)",
                                    (action, str(key), rec.get("created_at") or rec.get("quarantined_at") or rec.get("sent_at")))
        self.db.commit()

    def exists(self, action: str, ticket_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM outbox_registry WHERE action=? AND ticket_id=?",
                              (action, str(ticket_id))).fetchone()
        return row is not None

    def mark(self, action: str, ticket_id: str, run_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO outbox_registry (action, ticket_id, run_id, at) VALUES (?,?,?,?)",
                        (action, str(ticket_id), run_id, datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    # ---------------------------------------------------------------- work orders
    def write_work_order(self, wo: dict[str, Any], run_id: str) -> dict[str, Any] | None:
        """Write one work order per unique valid ticket. Returns the record or None if duplicate."""
        if self.exists("work_order", wo["ticket_id"]):
            existing = [r for r in read_jsonl(WORK_ORDERS_OUT) if r.get("ticket_id") == wo["ticket_id"]]
            return existing[0] if existing else wo
        need = {"work_order_id", "ticket_id", "vehicle_reg", "created_at", "citations"}
        assert need.issubset(wo), f"work order schema violated: missing {need - set(wo)}"
        _append(WORK_ORDERS_OUT, wo)
        self.mark("work_order", wo["ticket_id"], run_id)
        return wo

    # ---------------------------------------------------------------- comms draft
    def write_comms_draft(self, draft: dict[str, Any], run_id: str) -> dict[str, Any] | None:
        if self.exists("comms_draft", draft["ticket_id"]):
            existing = [r for r in read_jsonl(COMMS_PENDING_OUT) if r.get("ticket_id") == draft["ticket_id"]]
            return existing[0] if existing else draft
        _append(COMMS_PENDING_OUT, draft)
        self.mark("comms_draft", draft["ticket_id"], run_id)
        return draft

    # ---------------------------------------------------------------- comms sent
    def write_comms_sent(self, msg: dict[str, Any], run_id: str) -> dict[str, Any] | None:
        if self.exists("comms_sent", msg["ticket_id"]):
            existing = [r for r in read_jsonl(COMMS_SENT_OUT) if r.get("ticket_id") == msg["ticket_id"]]
            return existing[0] if existing else msg
        need = {"message_id", "ticket_id", "recipient", "body", "approved_by", "sent_at"}
        assert need.issubset(msg), f"comms sent schema violated: missing {need - set(msg)}"
        _append(COMMS_SENT_OUT, msg)
        self.mark("comms_sent", msg["ticket_id"], run_id)
        return msg

    # ---------------------------------------------------------------- quarantine
    def write_quarantine(self, rec: dict[str, Any], run_id: str) -> dict[str, Any] | None:
        if self.exists("quarantine", rec["ticket_id"]):
            existing = [r for r in read_jsonl(QUARANTINE_OUT) if r.get("ticket_id") == rec["ticket_id"]]
            return existing[0] if existing else rec
        _append(QUARANTINE_OUT, rec)
        self.mark("quarantine", rec["ticket_id"], run_id)
        return rec

    def close(self) -> None:
        self.db.commit()
        self.db.close()


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        import json
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")