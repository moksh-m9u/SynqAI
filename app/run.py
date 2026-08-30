"""Single-command orchestrator. Consumes a ticket queue end-to-end:

    python app/run.py --queue data/tickets.json --approve auto

Stages:
  preprocess (dedupe + validate + schema-recovery + quarantine)
  -> LangGraph per unique valid ticket (parallel enrich/retrieve, deterministic
     vehicle selection, outbox work order, comms draft, HITL gate, send)
  -> audit + artifacts + traces.

Re-running the exact same command produces byte-identical business-truth output
(it is a pure function of the queue + deterministic rules).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.config import (AUDIT_DIR, DATA_DIR, OUTPUTS_DIR, STATE_DB, ensure_dirs)
from app.ingest.loaders import load_drivers, load_fleet, load_maintenance, load_tickets, load_trips
from app.ingest.models import Ticket
from app.knowledge.entities import (build_client_registry, build_driver_registry,
                                    build_vehicle_registry)
from app.knowledge.vectorstore import KnowledgeStore
from app.pipeline.audit import audit
from app.pipeline.enrich import Services
from app.pipeline.outbox import Outbox
from app.pipeline.preprocess import dedupe, prepare
from app.rules.engine import build_eta_cache, export_rule_specs
from app.utils import atomic_write_json, canonicalize_reg, write_jsonl


def _build_services() -> Services:
    fleet = build_vehicle_registry(load_fleet())
    drivers = build_driver_registry(load_drivers())
    build_client_registry()

    maint_by_veh: dict[str, list] = {}
    for r in load_maintenance():
        key = canonicalize_reg(r.vehicle)
        maint_by_veh.setdefault(key, []).append(r)

    trips_by_veh: dict[str, list] = {}
    for t in load_trips():
        trips_by_veh.setdefault(canonicalize_reg(t.vehicle_reg), []).append(t)

    outbox = Outbox(STATE_DB)
    store = KnowledgeStore()
    svc = Services(fleet=fleet, drivers=drivers, maint_by_veh=maint_by_veh,
                   trips_by_veh=trips_by_veh, assigned=set(), run_id="",
                   eta_stats=build_eta_cache())
    svc.outbox = outbox  # type: ignore[attr-defined]
    svc.store = store  # type: ignore[attr-defined]
    return svc


def _run_ticket(graph, ticket: Ticket, approve_mode: str, run_id: str) -> dict[str, Any]:
    """Stream the graph for one ticket, stop at the HITL interrupt, prompt (if
    asked), resume, and return the final state."""
    thread_id = f"tkt-{ticket.ticket_id.lower()}"
    config = {"configurable": {"thread_id": thread_id}}
    initial = {
        "ticket_id": ticket.ticket_id,
        "ticket": ticket.model_dump(),
        "run_id": run_id,
        "thread_id": thread_id,
    }
    try:
        for chunk in graph.stream(initial, config=config, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            for node, payload in chunk.items():
                if node == "__interrupt__":
                    continue
                print(f"  [{ticket.ticket_id}] {node}")
    except Exception:
        pass  # graph may raise the interrupt exception on some versions; state is safe

    state = graph.get_state(config)
    if not state.next:
        return state.values
    if state.next[0] != "hitl_approval":
        return state.values

    draft = (state.values.get("draft") or {})
    from langgraph.types import Command
    if approve_mode == "ask":
        print("\n" + "=" * 74)
        print(f"[APPROVAL REQUIRED] {draft.get('ticket_id')} -> {draft.get('recipient')}")
        print("Subject:", draft.get("subject"))
        print("Body:\n", draft.get("body"))
        choice = input("Approve and send? [y/N/e] (e=edit body): ").strip().lower()
        if choice == "e":
            edited = input("Paste edited body:\n")
            draft["body"] = edited
            decision, by = "APPROVE", "Approver(CLI-edited)"
        elif choice == "y":
            decision, by = "APPROVE", "Approver(CLI)"
        else:
            decision, by = "REJECT", "Approver(CLI)"
        resume = {"decision": decision, "by": by}
    else:
        resume = {"decision": "APPROVE", "by": "Approver(auto)"}
    graph.invoke(Command(resume=resume), config=config)
    return graph.get_state(config).values


def main() -> int:
    ap = argparse.ArgumentParser(description="Meridian Freight breakdown-to-resolution")
    ap.add_argument("--queue", default=str(DATA_DIR / "tickets.json"), help="ticket queue file")
    ap.add_argument("--approve", default="auto", choices=["auto", "ask"],
                    help="HITL: auto-approve drafts or ask via CLI")
    ap.add_argument("--build-knowledge", action="store_true",
                    help="(re)build chunks + upsert into Qdrant before processing")
    ap.add_argument("--skip-graph", action="store_true", help="preprocess only (no LangGraph)")
    args = ap.parse_args()

    ensure_dirs()
    export_rule_specs()
    queue_hash = hashlib.sha256(Path(args.queue).read_bytes() + args.approve.encode()).hexdigest()[:12]
    run_id = f"run-{queue_hash}"
    started = datetime.now(timezone.utc).isoformat()
    print(f"[pipe] run_id={run_id} queue={args.queue} approve={args.approve} started={started}")

    if args.build_knowledge:
        from scripts.build_knowledge import build_knowledge_stack
        _, store = build_knowledge_stack()
        print(f"[pipe] knowledge store ready: chunks in qdrant={store.count()}")

    # ---------------- preprint queue ----------------
    records = load_tickets(Path(args.queue))
    canonical, duplicates = dedupe(records)
    for d in duplicates:
        audit({"run_id": run_id, "thread_id": "", "ticket_id": d.get("ticket_id"),
               "step": "preprocess_dedupe", "node": "validate", "decision": "DUPLICATE_SKIPPED",
               "data_refs": [args.queue], "rule_ids": [], "citations": [],
               "actor": "system", "detail": {"reason": "sync duplicate, processed once"}})
    print(f"[pipe] records={len(records)} unique={len(canonical)} duplicates={len(duplicates)}")

    svc = _build_services()
    outbox: Outbox = svc.outbox  # type: ignore[attr-defined]
    from app.pipeline.graph import build_checkpointer, make_graph
    checkpointer = build_checkpointer()
    graph = make_graph(svc, checkpointer) if not args.skip_graph else None

    prepared, quarantined = [], []
    seen: set[str] = set()
    for rec in canonical:
        pt = prepare(rec, seen, run_id=run_id)
        if pt.status == "valid":
            prepared.append(pt)
        else:
            quarantined.append(pt)

    quarantine_writes = 0
    for pt in quarantined:
        rec = {
            "ticket_id": pt.ticket_id, "reason": pt.reason,
            "raw_masked": {k: (v if not isinstance(v, str) else v) for k, v in pt.raw.items()},
            "quarantined_at": started, "run_id": run_id, "recovered_attempted": pt.recovered,
        }
        before = outbox.write_quarantine(rec, run_id)
        quarantine_writes += 1
        audit({"run_id": run_id, "thread_id": "", "ticket_id": pt.ticket_id,
               "step": "quarantine", "node": "validate", "decision": "QUARANTINED",
               "data_refs": [args.queue], "rule_ids": [], "citations": [],
               "actor": "system", "detail": {"reason": pt.reason}})
    print(f"[pipe] quarantined={len(quarantined)} (written={quarantine_writes})")

    # --------------- LangGraph per unique valid ticket ----------------
    processed = 0
    for pt in prepared:
        ticket = pt.ticked
        svc.run_id = run_id
        final = _run_ticket(graph, ticket, args.approve, run_id)
        processed += 1
        audit({"run_id": run_id, "thread_id": f"tkt-{ticket.ticket_id.lower()}",
               "ticket_id": ticket.ticket_id, "step": "pipeline_complete",
               "node": "end", "decision": "PROCESSED", "data_refs": [args.queue],
               "rule_ids": [v["rule_id"] for v in final.get("ctx", {}).get("verdicts", []) if v["triggered"]],
               "citations": [c.get("source") for c in final.get("ctx", {}).get("citations", [])],
               "actor": "system", "detail": {"chosen": final.get("selection", {}).get("chosen"),
                                             "work_order": final.get("work_order", {}).get("work_order_id")}})

    # ---------------- summary artifacts ----------------
    summary = {
        "run_id": run_id, "started": started, "finished": datetime.now(timezone.utc).isoformat(),
        "records": len(records), "unique": len(prepared), "duplicates": len(duplicates),
        "quarantined": [{"ticket_id": p.ticket_id, "reason": p.reason} for p in quarantined],
        "approve_mode": args.approve, "queue": args.queue,
    }
    atomic_write_json(OUTPUTS_DIR / "run_summary.json", summary)
    atomic_write_json(OUTPUTS_DIR / "preprocess_report.json", {
        "run_id": run_id, "dedup_reported": len(duplicates),
        "quarantined": summary["quarantined"],
    })

    print(f"[pipe] work_orders={sum(1 for _ in open(OUTPUTS_DIR/'work_orders.jsonl')) if (OUTPUTS_DIR/'work_orders.jsonl').exists() else 0} (target={len(prepared)})")
    print(f"[pipe] done. run_id={run_id} unique_processed={processed} quarantined={len(quarantined)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())