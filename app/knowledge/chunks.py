"""Knowledge chunking. Every ingestible non-operational source becomes an
inspectable, citeable chunk; nothing is lost inside a black box.

Sources chunked:
- dispatcher_interview.txt  -> section chunks (rule_candidate=True)
- emails/*.txt              -> one chunk per thread (PII-masked body)
- maintenance_log.xlsx      -> one chunk per record (PII-masked notes)

Chunk payloads are written to artifacts/knowledge/ as JSON for the dashboard."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import ARTIFACTS_DIR, DATA_DIR
from app.ingest.models import EmailRecord, MaintenanceRecord
from app.utils import atomic_write_json, write_jsonl

SECTION_RE = re.compile(r"(^|\n)((?:INTERVIEWER|RAJENDER):)", re.M)


class Chunk:
    def __init__(self, chunk_id: str, source: str, text: str,
                 doc_type: str, metadata: dict[str, Any],
                 rule_candidate: bool = False, entity_refs: list[str] | None = None):
        self.chunk_id = chunk_id
        self.source = source
        self.text = text
        self.doc_type = doc_type
        self.metadata = metadata
        self.rule_candidate = rule_candidate
        self.entity_refs = entity_refs or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "source": self.source,
            "document_type": self.doc_type,
            "text": self.text,
            "metadata": self.metadata,
            "rule_candidate": self.rule_candidate,
            "entity_refs": self.entity_refs,
        }


def _chunk_interview(text: str) -> list[Chunk]:
    parts = re.split(r"(?=INTERVIEWER:)", text)
    chunks = []
    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        topic = ""
        m = re.search(r"INTERVIEWER:\s*(.+)", part)
        if m:
            topic = m.group(1).strip()[:80]
        chunks.append(Chunk(
            chunk_id=f"dispatcher_{i:03d}",
            source="dispatcher_interview.txt",
            text=part,
            doc_type="transcript",
            metadata={"section_index": i, "topic_hint": topic},
            rule_candidate=True,
        ))
    return chunks


def _chunk_emails(emails: list[EmailRecord]) -> list[Chunk]:
    chunks = []
    for e in emails:
        body = e.body_masked or e.body
        text = f"Thread: {e.thread_id}\nSubject: {e.subject}\nDate: {e.date}\n{body}"
        refs = []
        for token in re.findall(r"[A-Z]{2}-?\d{2}-?[A-Z]{1,2}?-?\d{4}|\b[A-Z]{2}\d{2}[A-Z]?\d{4}\b", body.upper()):
            if token not in refs:
                refs.append(token)
        chunks.append(Chunk(
            chunk_id=f"email_{e.thread_id}",
            source=f"emails/{e.source_file}",
            text=text,
            doc_type="email_thread",
            metadata={"subject": e.subject, "date": e.date.isoformat(),
                      "from_addr": e.from_addr, "to_addr": e.to_addr},
            rule_candidate=bool(refs),
            entity_refs=refs[:20],
        ))
    return chunks


def _chunk_maintenance(records: list[MaintenanceRecord]) -> list[Chunk]:
    chunks = []
    for r in records:
        chunks.append(Chunk(
            chunk_id=f"maint_{r.entry_id}",
            source=f"maintenance_log.xlsx",
            text=f"{r.date} | {r.vehicle} | odo={r.odometer_km} | mechanic={r.mechanic} | {r.notes_masked}",
            doc_type="maintenance_record",
            metadata={"date": r.date.isoformat(), "vehicle": r.vehicle,
                      "odometer_km": r.odometer_km, "mechanic": r.mechanic},
        ))
    return chunks


def build_knowledge_base(interview_text: str,
                         emails: list[EmailRecord],
                         maintenance: list[MaintenanceRecord]) -> list[Chunk]:
    chunks = _chunk_interview(interview_text) + _chunk_emails(emails) + _chunk_maintenance(maintenance)
    records = [c.to_dict() for c in chunks]
    write_jsonl(ARTIFACTS_DIR / "knowledge" / "chunks.jsonl", records)
    atomic_write_json(ARTIFACTS_DIR / "knowledge" / "index.json", {
        "chunk_count": len(records),
        "sources": sorted({c["source"] for c in records}),
        "rule_candidates": [c["chunk_id"] for c in records if c["rule_candidate"]],
    })
    return chunks