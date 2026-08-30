"""FastAPI service for the Meridian automation. Read-only introspection plus a
thin approval surface. Every response is PII-masked by construction (drivers are
served as ids only; bodies are masked at ingestion)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app.config import ensure_dirs
from app.ingest.loaders import load_fleet, load_tickets
from app.knowledge.entities import build_vehicle_registry
from app.rules.engine import all_rule_specs
from app.utils import read_jsonl

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    from app.config import OUTPUTS_DIR, AUDIT_DIR
    app.state.outputs = OUTPUTS_DIR
    app.state.audit = AUDIT_DIR
    yield


APP = FastAPI(title="Meridian Freight — Breakdown-to-Resolution",
              version="1.0.0", lifespan=lifespan)
app = APP  # decorators below use the conventional name; uvicorn imports APP


class ApprovalIn(BaseModel):
    decision: str  # APPROVE | REJECT
    by: str = "Approver(API)"


# ---------------------------------------------------------- reads
def _load_artifacts():
    from app.config import OUTPUTS_DIR, AUDIT_DIR
    wos = read_jsonl(OUTPUTS_DIR / "work_orders.jsonl")
    pend = [r for r in read_jsonl(OUTPUTS_DIR / "comms_pending.jsonl") if r.get("status") == "PENDING_APPROVAL"]
    sent = read_jsonl(OUTPUTS_DIR / "comms_sent.jsonl")
    q = read_jsonl(OUTPUTS_DIR / "quarantine.jsonl")
    audit_lines = read_jsonl(AUDIT_DIR / "audit.jsonl")
    return wos, pend, sent, q, audit_lines


@app.get("/health")
def health():
    wos, pend, sent, q, _ = _load_artifacts()
    return {"status": "ok", "work_orders": len(wos), "comms_pending": len(pend),
            "comms_sent": len(sent), "quarantined": len(q)}


@app.get("/tickets")
def tickets():
    records = load_tickets()
    return [{"ticket_id": r.get("ticket_id"), "vehicle": r.get("vehicle"),
             "origin_hub": r.get("origin_hub"), "destination": r.get("destination"),
             "client": r.get("client"), "severity": r.get("severity"), "status": r.get("status")}
            for r in records]


@app.get("/tickets/{ticket_id}/timeline")
def timeline(ticket_id: str):
    from pathlib import Path
    from app.config import ARTIFACTS_DIR, AUDIT_DIR, OUTPUTS_DIR
    wos, pend, sent, q, audit_lines = _load_artifacts()
    wo = next((w for w in wos if w.get("ticket_id") == ticket_id), None)
    draft = next((d for d in pend if d.get("ticket_id") == ticket_id), None)
    sent_msg = next((s for s in sent if s.get("ticket_id") == ticket_id), None)
    qrec = next((x for x in q if x.get("ticket_id") == ticket_id), None)
    steps = [a for a in audit_lines if a.get("ticket_id") == ticket_id]
    graph_dir = ARTIFACTS_DIR / "graph" / f"tkt-{ticket_id.lower()}"
    nodes = {}
    if graph_dir.exists():
        for f in sorted(graph_dir.glob("*.json")):
            import json
            nodes[f.stem] = json.loads(f.read_text())
    return {"ticket_id": ticket_id, "work_order": wo, "draft": draft,
            "sent": sent_msg, "quarantine": qrec, "audit_steps": steps[-40:], "graph_nodes": nodes}


@app.get("/rules")
def rules(severity: str | None = None):
    out = all_rule_specs()
    if severity:
        out = [r for r in out if r.get("severity") == severity]
    return out


@app.get("/retrieval")
def retrieval(query: str = Query(..., min_length=3), top_k: int = 6):
    from app.knowledge.vectorstore import KnowledgeStore
    try:
        store = KnowledgeStore()
    except Exception as exc:
        raise HTTPException(503, f"knowledge store unavailable: {exc}")
    hits = store.search(query, top_k=top_k, run_id="api", thread_id="api")
    if not hits:
        return {"query": query, "answer": "insufficient information", "hits": []}
    top = hits[0]
    if top["score"] < 0.45:
        return {"query": query, "answer": "insufficient information",
                "grounded": False, "hits": hits}
    return {"query": query, "answer": top["text"][:900], "grounded": True,
            "citations": [{"source": h["source"], "chunk_id": h["chunk_id"], "score": h["score"]} for h in hits],
            "hits": hits}


@app.get("/audit")
def audit_search(ticket_id: str | None = None, node: str | None = None,
                 rule: str | None = None, limit: int = 100):
    from app.config import AUDIT_DIR
    lines = read_jsonl(AUDIT_DIR / "audit.jsonl")
    out = []
    for a in lines:
        if ticket_id and a.get("ticket_id") != ticket_id:
            continue
        if node and a.get("node") != node:
            continue
        if rule and rule not in a.get("rule_ids", []):
            continue
        out.append(a)
    return out[-limit:]


@app.get("/approvals")
def approvals():
    _, pend, sent, _, _ = _load_artifacts()
    sent_ids = {s.get("ticket_id") for s in sent}
    return [d for d in pend if d.get("ticket_id") not in sent_ids]


@app.post("/approvals/{ticket_id}")
def approve(ticket_id: str, body: ApprovalIn):
    decision, by = body.decision.upper(), body.by
    if decision not in ("APPROVE", "REJECT"):
        raise HTTPException(400, "decision must be APPROVE or REJECT")
    thread_id = f"tkt-{ticket_id.lower()}"
    from app.pipeline.graph import build_checkpointer, make_graph
    from app.pipeline.enrich import Services
    from app.knowledge.entities import build_vehicle_registry
    from app.pipeline.outbox import Outbox
    from app.config import STATE_DB
    fleet = build_vehicle_registry(load_fleet())
    svc = Services(fleet=fleet, drivers={}, maint_by_veh={}, trips_by_veh={},
                   assigned=set(), run_id="api-approval")
    from app.knowledge.vectorstore import KnowledgeStore
    svc.outbox = Outbox(STATE_DB)
    svc.store = KnowledgeStore()
    graph = make_graph(svc, build_checkpointer())
    config = {"configurable": {"thread_id": thread_id}}
    state = graph.get_state(config)
    if not state.next:
        msg = next((s for s in read_jsonl(app.state.outputs / "comms_sent.jsonl")
                    if s.get("ticket_id") == ticket_id), None)
        if msg:
            return {"ticket_id": ticket_id, "status": "already_sent", "sent": msg}
        raise HTTPException(404, "no pending approval for this ticket")
    from langgraph.types import Command
    graph.invoke(Command(resume={"decision": decision, "by": by}), config=config)
    final = graph.get_state(config).values
    return {"ticket_id": ticket_id, "status": "RESOLVED" if decision == "APPROVE" else "REJECTED",
            "comms_sent": final.get("comms_sent")}


@app.post("/tickets/{ticket_id}/process")
def process_ticket(ticket_id: str):
    records = load_tickets()
    rec = next((r for r in records if r.get("ticket_id") == ticket_id), None)
    if not rec:
        raise HTTPException(404, "ticket not in queue")
    from app.pipeline.preprocess import prepare
    from app.ingest.models import Ticket
    from app.pipeline.graph import build_checkpointer, make_graph
    from app.pipeline.enrich import Services
    from app.pipeline.outbox import Outbox
    from app.config import STATE_DB
    import uuid
    fleet = build_vehicle_registry(load_fleet())
    svc = Services(fleet=fleet, drivers={}, maint_by_veh={}, trips_by_veh={},
                   assigned=set(), run_id="api-run")
    from app.knowledge.vectorstore import KnowledgeStore
    svc.outbox = Outbox(STATE_DB)
    svc.store = KnowledgeStore()
    graph = make_graph(svc, build_checkpointer())
    talent = prepare(rec, {rec.get("ticket_id")}, run_id="api-run")
    if talent.status != "valid":
        return {"ticket_id": ticket_id, "status": "quarantined", "reason": talent.reason}
    from app.run import _run_ticket
    final = _run_ticket(graph, talent.ticked, "auto", f"api-{uuid.uuid4().hex[:8]}")
    return {"ticket_id": ticket_id, "status": "processed",
            "work_order": final.get("work_order"), "comms_sent": final.get("comms_sent")}