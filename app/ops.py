"""Interactive operations for the dashboard: run arbitrary tickets, run the
sample queue, and inject chaos — all through the *unchanged* production pipeline.

Every run is isolated in sandbox/<run_id>/ (own outputs/, audit/, artifacts/ and
Qdrant-independent SQLite checkpointer), so the demo baseline in outputs/ and
artifacts/ stays byte-identical and every interactive run is independently
replayable from its own JSONL + graph artifacts.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app.config import ARTIFACTS_DIR, AUDIT_DIR, DATA_DIR, OUTPUTS_DIR, ROOT
from app.ingest.masking import _AADHAAR_RE, _DL_RE, _PHONE_RE  # reuse exact gate
from app.utils import atomic_write_json, read_jsonl, write_jsonl

SANDBOX_ROOT = ROOT / "sandbox"

_NODES = ["validate", "deduplicate", "enrich", "retrieve", "rule_engine",
          "select_vehicle", "draft_communication", "hitl", "send", "complete"]


def workspace_for(run_id: str) -> dict[str, Path]:
    """Create the isolated workspace layout for one interactive run."""
    base = SANDBOX_ROOT / run_id
    ws = {
        "root": base,
        "outputs": base / "outputs",
        "audit": base / "audit",
        "artifacts": base / "artifacts",
        "graph": base / "artifacts" / "graph",
        "state_db": base / "state.sqlite",
        "audit_file": base / "audit" / "audit.jsonl",
    }
    for p in ws.values():
        if p.suffix in {".sqlite", ".jsonl"}:
            p.parent.mkdir(parents=True, exist_ok=True)
        else:
            p.mkdir(parents=True, exist_ok=True)
    return ws


def sandbox_list() -> list[dict[str, Any]]:
    runs = []
    if not SANDBOX_ROOT.exists():
        return runs
    for d in sorted(SANDBOX_ROOT.iterdir()):
        if not d.is_dir():
            continue
        summary = Path(d / "outputs" / "run_summary.json")
        s = json.loads(summary.read_text()) if summary.exists() else {}
        runs.append({
            "run_id": d.name,
            "started": s.get("started", ""),
            "records": s.get("records", 0),
            "work_orders": s.get("work_orders", 0),
            "quarantined": s.get("quarantined", 0),
            "kind": s.get("kind", "sandbox"),
            "ok": bool(s),
        })
    return sorted(runs, key=lambda r: r["started"], reverse=True)


# ---------------------------------------------------------------- single record flow
def process_records(records: list[dict[str, Any]], run_id: str, *,
                    kind: str = "sandbox", approve: str = "auto",
                    log: Callable[[str], None] | None = None,
                    base_store_collection: str | None = None) -> list[dict[str, Any]]:
    """Run records through preprocess -> LangGraph into an isolated workspace.

    Returns one result per record; results reference workspace paths so a page
    can read the produced artifacts directly."""
    ws = workspace_for(run_id)
    from app.pipeline.outbox import Outbox
    outbox = Outbox(ws["state_db"], outputs_dir=ws["outputs"])

    from app.ingest.loaders import load_drivers, load_fleet, load_maintenance, load_trips
    from app.ingest.models import Ticket
    from app.knowledge.entities import (build_client_registry, build_driver_registry,
                                        build_vehicle_registry)
    from app.pipeline.enrich import Services
    from app.pipeline.graph import build_checkpointer, make_graph
    from app.pipeline.preprocess import prepare
    from app.rules.engine import build_eta_cache
    from app.utils import canonicalize_reg

    fleet = build_vehicle_registry(load_fleet())
    drivers = build_driver_registry(load_drivers())
    build_client_registry()

    maint_by_veh: dict[str, list] = {}
    for r in load_maintenance():
        maint_by_veh.setdefault(canonicalize_reg(r.vehicle), []).append(r)
    trips_by_veh: dict[str, list] = {}
    for t in load_trips():
        trips_by_veh.setdefault(canonicalize_reg(t.vehicle_reg), []).append(t)

    from app.knowledge.vectorstore import KnowledgeStore
    store = KnowledgeStore(collection=base_store_collection) if base_store_collection else KnowledgeStore()
    svc = Services(fleet=fleet, drivers=drivers, maint_by_veh=maint_by_veh,
                   trips_by_veh=trips_by_veh, assigned=set(), run_id=run_id,
                   eta_stats=build_eta_cache())
    svc.outbox = outbox  # type: ignore[attr-defined]
    svc.store = store  # type: ignore[attr-defined]
    svc.run_id = run_id

    graph = make_graph(svc, build_checkpointer(ws["state_db"]))
    started = datetime.now(timezone.utc).isoformat()

    def emit(line: str):
        if log:
            log(line)

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    for rec in records:
        r0 = time.perf_counter()
        emit(f"[{rec.get('ticket_id', '?')}] prepare")
        pt = prepare(rec, seen, run_id=run_id)
        if pt.status != "valid":
            quar = {
                "ticket_id": pt.ticket_id, "reason": pt.reason,
                "raw_masked": {k: (v if not isinstance(v, str) else v) for k, v in pt.raw.items()},
                "quarantined_at": started, "run_id": run_id,
                "recovered_attempted": pt.recovered,
            }
            outbox.write_quarantine(quar, run_id)
            results.append({
                "ticket_id": pt.ticket_id, "status": "QUARANTINED", "reason": pt.reason,
                "recovered_attempted": pt.recovered, "work_order": None, "draft": None,
                "comms_sent": None, "execution_ms": round((time.perf_counter() - r0) * 1000),
            })
            emit(f"[{pt.ticket_id}] QUARANTINED -> {pt.reason}")
            continue

        ticket = pt.ticked
        thread_id = f"sbx-{run_id}-{ticket.ticket_id.lower()}"
        emitted_node: set[str] = set()

        def node_cb(chunk):
            for node, payload in chunk.items():
                if node == "__interrupt__":
                    continue
                if node not in emitted_node:
                    emitted_node.add(node)
                    emit(f"[{ticket.ticket_id}] {node}")

        from app.run import _run_ticket
        final = _run_ticket(graph, ticket, approve, run_id, thread_id=thread_id,
                            node_cb=node_cb)
        ms = round((time.perf_counter() - r0) * 1000)
        results.append({
            "ticket_id": ticket.ticket_id,
            "status": "PROCESSED",
            "reason": "",
            "recovered_attempted": pt.recovered,
            "work_order": final.get("work_order"),
            "draft": final.get("draft"),
            "comms_sent": final.get("comms_sent"),
            "chosen": (final.get("selection") or {}).get("chosen"),
            "applied_rules": [v["rule_id"] for v in
                              (final.get("ctx") or {}).get("verdicts", []) if v.get("triggered")],
            "context_citations": [c.get("source") for c in (final.get("ctx") or {}).get("citations", [])],
            "execution_ms": ms,
        })
        emit(f"[{ticket.ticket_id}] done in {ms}ms")

    summary = {
        "run_id": run_id, "kind": kind, "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "records": len(records),
        "work_orders": sum(1 for r in results if r["status"] == "PROCESSED"),
        "quarantined": sum(1 for r in results if r["status"] == "QUARANTINED"),
        "quarantine_reasons": [r["reason"] for r in results if r["status"] == "QUARANTINED"],
        "total_execution_ms": round((time.perf_counter() - t0) * 1000),
        "approve": approve,
    }
    atomic_write_json(ws["outputs"] / "run_summary.json", summary)
    write_jsonl(ws["outputs"] / "results.jsonl", results)
    return results


# ---------------------------------------------------------------- sample queue
def sample_records(n: int = 3) -> list[dict[str, Any]]:
    """Pick a small deterministic sample from the challenge queue for live runs."""
    records = json.loads((DATA_DIR / "tickets.json").read_text(encoding="utf-8"))
    if isinstance(records, dict):
        records = records.get("tickets", records.get("records", []))
    seen, out = set(), []
    for r in records:
        tid = r.get("ticket_id", "")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(r)
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- chaos
CHAOS_FLAGS = ["duplicate", "missing_vehicle", "invalid_date", "missing_hub",
               "broken_json", "wrong_schema", "conflicting_maintenance",
               "duplicate_comms"]


def chaos_plan(flags: list[str]) -> list[dict[str, Any]]:
    """Build an intentional-failure queue from the sample. Each record keeps a
    `_chaos` tag documenting the synthetic defect for the report."""
    records = sample_records(4)
    plan: list[dict[str, Any]] = []
    if "duplicate" in flags and records:
        a = {**records[0], "_chaos": {"defect": "duplicate"}}
        b = {**records[0], "_chaos": {"defect": "duplicate", "of": records[0]["ticket_id"]}}
        plan.append(a)
        plan.append(b)
    if "missing_vehicle" in flags and records:
        plan.append({**records[1], "vehicle": "", "driver": "", "_chaos": {"defect": "missing_vehicle"}})
    if "invalid_date" in flags and len(records) > 2:
        plan.append({**records[2], "created_at": "not-a-date",
                     "_chaos": {"defect": "invalid_date"}})
    if "missing_hub" in flags and records:
        plan.append({**records[0], "origin_hub": "", "destination": "",
                     "_chaos": {"defect": "missing_hub"}})
    if "wrong_schema" in flags and records:
        plan.append({
            "TicketRef": records[1]["ticket_id"], "VehicleTruck": records[1]["vehicle"],
            "Fault": records[1]["issue"], "Where": records[1]["origin_hub"], "When": records[1]["created_at"],
            "_chaos": {"defect": "wrong_schema"},
        })
    if "conflicting_maintenance" in flags and records:
        plan.append({**records[2], "capacity_tonnes": None, "issue": "engine overheated & coolant leak",
                     "_chaos": {"defect": "conflicting_maintenance",
                                "note": "no maintenance conflict actually possible in ticket data; engine evidence lives in maintenance_log"}})
    return plan


def chaos_report(results: list[dict[str, Any]], ws: dict[str, Path]) -> dict[str, Any]:
    """Score a chaos run against the rubric: exactly-once, quarantine, recovery,
    safe degradation, PII leak."""
    wos = read_jsonl(ws["outputs"] / "work_orders.jsonl")
    quar = read_jsonl(ws["outputs"] / "quarantine.jsonl")
    sent = read_jsonl(ws["outputs"] / "comms_sent.jsonl")
    draft = read_jsonl(ws["outputs"] / "comms_pending.jsonl")

    by_ticket: dict[str, list[dict]] = {}
    for w in wos:
        by_ticket.setdefault(w["ticket_id"], []).append(w)

    exactly_once = all(len(v) == 1 for v in by_ticket.values())
    processed = [r for r in results if r["status"] == "PROCESSED"]
    quarantined = [r for r in results if r["status"] == "QUARANTINED"]
    recovered = [r for r in results if r["status"] == "PROCESSED" and r["recovered_attempted"]]
    crashed = [r for r in results if r["status"] not in ("PROCESSED", "QUARANTINED")]

    leaked = pii_scan([*wos, *quar, *sent, *draft])
    return {
        "exactly_once": exactly_once,
        "exactly_once_detail": {k: len(v) for k, v in by_ticket.items()},
        "quarantined": len(quarantined),
        "quarantine_reasons": [r["reason"] for r in quarantined],
        "recovered": len(recovered),
        "processed": len(processed),
        "safe_degradation": len(crashed) == 0,
        "crashed": [r["ticket_id"] for r in crashed],
        "pii_leaked": len(leaked) > 0,
        "pii_matches": leaked[:10],
        "warnings": [r for r in results if r["status"] == "QUARANTINED"],
    }


def pii_scan(records: Iterable[dict[str, Any]]) -> list[str]:
    """Deterministic leak detector over every string value in the records."""
    matches: list[str] = []
    for rec in records:
        def walk(v):
            if isinstance(v, str):
                for pat, label in ((_PHONE_RE, "phone"), (_AADHAAR_RE, "aadhaar"), (_DL_RE, "dl")):
                    if pat.search(v) and not v.strip().endswith("MASKED"):
                        matches.append(f"{label}:{v[:22]}...")
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)
        walk(rec)
    return matches


def run_id_for(payload: bytes, kind: str) -> str:
    return f"{kind}-{hashlib.sha256(payload).hexdigest()[:12]}"


def new_run_id(kind: str = "sbx") -> str:
    """Fresh unique run id for interactive runs (each sandbox/chaos execution gets
    its own workspace; content-addressed ids would silently reuse a prior one)."""
    import uuid
    return f"{kind}-{uuid.uuid4().hex[:12]}"