"""Schema recovery: unknown formats never crash the system.

Native loader -> Pydantic validation -> on failure -> LLM-aided field mapping
-> Python extraction -> re-validation -> continue.

Qwen only INFERS the field mapping. Python performs the actual extraction and
writes production records. Qwen can never inject a value directly.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.config import ARTIFACTS_DIR
from app.ingest.models import Ticket
from app.utils import atomic_write_json, build_llm, parse_datetime

TICKET_SCHEMA = {
    "ticket_id": "unique ticket identifier, e.g. TKT-0001",
    "created_at": "ISO-8601 datetime of the breakdown",
    "vehicle": "vehicle registration number",
    "driver_id": "driver identifier like DRV-001 (may be null/empty)",
    "origin_hub": "origin hub city/name",
    "km_from_origin_hub": "float distance in km from origin hub",
    "destination": "destination city/name",
    "issue": "breakdown issue text",
    "severity": "HIGH|MEDIUM|LOW or empty",
    "client": "client/customer name",
    "status": "OPEN|CLOSED or other workflow status",
    "resolution_note": "resolution note (may be empty)",
}

_HEURISTIC_KEY_MAP = {
    "ticketid": "ticket_id", "tktid": "ticket_id", "tktno": "ticket_id", "ticketno": "ticket_id",
    "id": "ticket_id", "ref": "ticket_id", "reference": "ticket_id",
    "date": "created_at", "time": "created_at", "timestamp": "created_at",
    "breakdown_date": "created_at", "reported_at": "created_at", "datetime": "created_at",
    "breakdownwhen": "created_at", "breakdowntime": "created_at", "reportedwhen": "created_at",
    "vehiclereg": "vehicle", "vehicle_reg": "vehicle", "reg": "vehicle",
    "vehicleregistration": "vehicle", "vehicle_no": "vehicle", "plate": "vehicle",
    "truck": "vehicle", "truckno": "vehicle", "vehicleid": "vehicle",
    "driver": "driver_id", "driverid": "driver_id", "driverno": "driver_id",
    "drivercode": "driver_id", "drv": "driver_id",
    "hub": "origin_hub", "origin": "origin_hub", "from": "origin_hub", "source_hub": "origin_hub",
    "kmfromhub": "km_from_origin_hub", "km": "km_from_origin_hub", "distancefromorigin": "km_from_origin_hub",
    "distance": "km_from_origin_hub", "dist": "km_from_origin_hub", "farfrombasekm": "km_from_origin_hub",
    "dest": "destination", "to": "destination", "delivery_to": "destination", "destination_city": "destination",
    "problem": "issue", "breakdown": "issue", "fault": "issue", "complaint": "issue", "reason": "issue",
    "symptom": "issue",
    "prio": "severity", "priority": "severity",
    "customer": "client", "customer_name": "client", "account": "client", "company": "client",
    "state": "status", "ticket_status": "status",
    "notes": "resolution_note", "resolution": "resolution_note", "note": "resolution_note",
}


def _llm_map_keys(raw: dict, groq_llm=None) -> dict[str, str]:
    """Ask Qwen for a JSON {raw_key -> canonical_key} mapping. Pure inference."""
    if groq_llm is None:
        return {}
    prompt = (
        "You map keys of a messy breakdown-ticket record to a canonical schema. "
        "Return ONLY JSON, no prose: an object mapping each input key to one canonical key.\n"
        f"Canonical schema keys: {json.dumps(TICKET_SCHEMA)}\n"
        f"Record: {json.dumps(raw)}\n"
        "Rules: unknown/irrelevant keys map to '__skip__'. Values are NOT transformed here."
    )
    try:
        resp = groq_llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        mapping = json.loads(m.group(0))
        return {k: v for k, v in mapping.items() if v != "__skip__"}
    except Exception:
        return {}


def recover_record(raw: dict[str, Any], run_id: str = "", thread_id: str = "",
                   groq_llm=None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return (normalized_record or None if unrecoverable, report)."""
    report: dict[str, Any] = {"original": raw, "method": None, "actions": []}

    # Qwen fires when a purely heuristic mapping cannot cover every key — i.e.
    # exactly the 'brand new file format' surprise. Python still performs the
    # extraction below; Qwen only ever suggests the key-to-key mapping.
    heuristic_map = {}
    for k in raw:
        lk = re.sub(r"[^a-z]", "", str(k).lower())
        if lk in _HEURISTIC_KEY_MAP:
            heuristic_map[k] = _HEURISTIC_KEY_MAP[lk]
    unknowns = [k for k in raw if not heuristic_map.get(k) and k not in TICKET_SCHEMA]

    llm_map: dict[str, str] = {}
    if unknowns or groq_llm is not None:
        llm = groq_llm or build_llm()
        if llm is not None:
            llm_map = _llm_map_keys(raw, llm)
            report["actions"].append(f"llm key mapping attempted for unknowns={unknowns}")

    # merge: LLM wins on ambiguity, heuristic fills gaps; mapping never guesses values
    merged: dict[str, str] = {}
    for k in raw:
        merged[k] = llm_map.get(k) or heuristic_map.get(k)
        if merged[k] is None:
            merged[k] = k if k in TICKET_SCHEMA else "_unknown"
    report["mapping"] = merged

    norm: dict[str, Any] = {}
    for k, canon in merged.items():
        if canon == "_unknown":
            continue
        norm.setdefault(canon, raw[k])

    # Python performs value coercion
    for canon, value in list(norm.items()):
        if canon == "created_at" and isinstance(value, str):
            parsed = parse_datetime(value)
            norm[canon] = parsed.isoformat() if parsed else value
        if canon == "km_from_origin_hub" and isinstance(value, str):
            try:
                norm[canon] = float(value)
            except ValueError:
                norm[canon] = None if value.strip() in {"", "null", "none"} else value
        if canon == "driver_id" and value in (None, "", "null"):
            norm[canon] = None

    report["normalized"] = norm
    report["method"] = "llm_mapping" if llm_map else ("heuristic_mapping" if heuristic_map else "naive")

    # store schema-recovery artifact
    atomic_write_json(ARTIFACTS_DIR / "schema" / f"recovery_{re.sub(r'[^A-Za-z0-9]', '_', str(raw.get('ticket_id') or raw.get('id') or 'rec'))}_{run_id or 'x'}.json",
                      {"run_id": run_id, "thread_id": thread_id, **report})

    try:
        Ticket.model_validate(norm)
        return norm, report
    except Exception as exc:
        report["validation_error"] = str(exc)
        return None, report