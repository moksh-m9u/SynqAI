"""FastAPI service for the Meridian Freight breakdown-to-resolution system.

Read-only introspection of the live pipeline artifacts plus a thin approval and
processing surface. Every response is PII-masked by construction: drivers are
served as ids only, ticket bodies are masked at ingestion, and retrieval answers
are snippets of indexed team knowledge (not raw queues).

Endpoints are grouped by tags: ``system``, ``tickets``, ``knowledge``,
``audit``, and ``approvals``. All reads come straight from the on-disk JSONL
artifacts produced by LangGraph runs, so a caller can reconstruct any number.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

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


OPENAPI_TAGS = [
    {
        "name": "system",
        "description": "Service health and capability check. Proves the pipeline is live and shows baseline artifact counts.",
    },
    {
        "name": "tickets",
        "description": "Inspect the ingested ticket queue and follow one ticket from breakdown to resolution via its timeline (retrieval → rules → work order → draft → sent message) and run one ticket through the live LangGraph pipeline.",
    },
    {
        "name": "knowledge",
        "description": "Dispatch rule catalogue extracted from the retiring dispatcher, and live RAG retrieval over the indexed knowledge base (Qdrant).",
    },
    {
        "name": "audit",
        "description": "The full audit trail of every pipeline decision: node, rule citations, outcome, and timing.",
    },
    {
        "name": "approvals",
        "description": "The human-in-the-loop surface: list drafts awaiting a decision and approve or reject them, resuming the exact LangGraph thread.",
    },
]

APP = FastAPI(
    title="Meridian Freight — Breakdown-to-Resolution API",
    version="1.0.0",
    summary="Inspect and drive the AI dispatch pipeline (Milestone demo API).",
    description=(
        "System that takes a truck **breakdown ticket** and drives it to **resolution**: "
        "deduplicate → schema-recover → enrich → RAG-retrieve → apply dispatch rules → "
        "select replacement vehicle → create work order → draft & send client communication, "
        "with a human approval gate and a full audit trail.\n\n"
        "### Quick start\n"
        "1. `GET /health` — is the pipeline live?\n"
        "2. `GET /tickets` — browse the ingested queue.\n"
        "3. `GET /tickets/{id}/timeline` — replay one ticket node by node.\n"
        "4. `GET /retrieval?query=...` — probe the RAG store with any question.\n"
        "5. `POST /tickets/{id}/process` — run any queued ticket live through LangGraph.\n"
        "6. `GET /approvals` + `POST /approvals/{id}` — the HITL gate.\n\n"
        "### Guarantees\n"
        "- **Exactly-once**: processing is idempotent (content-addressed run ids, dedupe guard).\n"
        "- **No PII**: responses are masked by construction; no raw driver/owner data is served.\n"
        "- **Reproducible**: every read is backed by immutable JSONL artifacts under `outputs/`, `audit/`, `artifacts/`.\n"
    ),
    openapi_tags=OPENAPI_TAGS,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app = APP  # decorators use the conventional name; uvicorn imports APP


# ---------------------------------------------------------- response schemas
class Artifact(BaseModel):
    """Free-form artifact row (work order, draft, audit event, retrieval hit …)."""
    model_config = ConfigDict(extra="allow")


class HealthResponse(BaseModel):
    status: str = Field(..., description="Always \"ok\" when the service and its artifacts are reachable.")
    work_orders: int = Field(..., description="Work orders emitted across live runs.")
    comms_pending: int = Field(..., description="Client communications currently awaiting human approval.")
    comms_sent: int = Field(..., description="Client communications already sent.")
    quarantined: int = Field(..., description="Records quarantined (bad input) rather than processed.")


class TicketSummary(BaseModel):
    ticket_id: str = Field(..., description="Unique queue identifier.")
    vehicle: str | None = Field(None, description="Reported vehicle registration (may be an alias).")
    origin_hub: str | None = Field(None, description="Breakdown origin hub.")
    destination: str | None = Field(None, description="Route destination.")
    client: str | None = Field(None, description="Reported client name (may be an alias).")
    severity: str | None = Field(None, description="LOW | MEDIUM | HIGH.")
    status: str | None = Field(None, description="Ticket lifecycle status (OPEN, …).")


class TimelineResponse(BaseModel):
    ticket_id: str = Field(..., description="Resolved ticket id.")
    work_order: Artifact | None = Field(None, description="Created work order, if any.")
    draft: Artifact | None = Field(None, description="Drafted client communication, if still pending.")
    sent: Artifact | None = Field(None, description="Sent communication, if any.")
    quarantine: Artifact | None = Field(None, description="Quarantine record, if the ticket was rejected.")
    audit_steps: list[Artifact] = Field(default_factory=list, description="Audit events for this ticket (newest last).")
    graph_nodes: dict[str, Artifact] = Field(default_factory=dict,
                                             description="Per-node LangGraph artifacts keyed by node id.")


class RuleSpec(BaseModel):
    """One dispatch rule extracted from the dispatcher's knowledge."""
    model_config = ConfigDict(extra="allow")


class Citation(BaseModel):
    source: str = Field(..., description="Source file of the retrieved chunk.")
    chunk_id: str = Field(..., description="Chunk identifier in the knowledge base.")
    score: float = Field(..., description="Cosine similarity (0–1) between query and chunk embedding.")


class RetrievalHit(BaseModel):
    """A single ranked retrieval result."""
    model_config = ConfigDict(extra="allow")


class RetrievalResponse(BaseModel):
    query: str = Field(..., description="The question asked.")
    answer: str = Field(..., description="The top chunk's text (≤900 chars). \"insufficient information\" when nothing is strong enough.")
    grounded: bool | None = Field(None, description="True when the best hit scores ≥ 0.45.")
    citations: list[Citation] | None = Field(None, description="Ranked source citations for the answer.")
    hits: list[RetrievalHit] = Field(..., description="Full ranked hit set as returned by the store.")


class AuditEvent(BaseModel):
    """One audit event. Shape varies by node; extra keys preserved."""
    model_config = ConfigDict(extra="allow")


class ApprovalDraft(BaseModel):
    """A drafted communication waiting for a human decision."""
    model_config = ConfigDict(extra="allow")


class ApprovalDecision(BaseModel):
    """Request body for approving / rejecting a drafted communication."""
    decision: str = Field(..., description="APPROVE or REJECT.", examples=["APPROVE"])
    by: str = Field("Approver(API)", description="Identity attached to the audit record for this decision.")


class ApprovalResult(BaseModel):
    ticket_id: str = Field(..., description="The ticket being decided.")
    status: str = Field(..., description="RESOLVED when approved (message enqueued for send), REJECTED otherwise, or already_sent.")
    comms_sent: Artifact | None = Field(None, description="The sent communication, when available.")


class ProcessResult(BaseModel):
    ticket_id: str = Field(..., description="The ticket that was run.")
    status: str = Field(..., description="processed, quarantined or not_found.")
    reason: str | None = Field(None, description="Quarantine reason when status == \"quarantined\".")
    work_order: Artifact | None = Field(None, description="Work order created by the live run.")
    comms_sent: Artifact | None = Field(None, description="Communication sent by the live run (auto-approve).")


# ---------------------------------------------------------- reads
def _load_artifacts():
    from app.config import OUTPUTS_DIR, AUDIT_DIR
    wos = read_jsonl(OUTPUTS_DIR / "work_orders.jsonl")
    pend = [r for r in read_jsonl(OUTPUTS_DIR / "comms_pending.jsonl") if r.get("status") == "PENDING_APPROVAL"]
    sent = read_jsonl(OUTPUTS_DIR / "comms_sent.jsonl")
    q = read_jsonl(OUTPUTS_DIR / "quarantine.jsonl")
    audit_lines = read_jsonl(AUDIT_DIR / "audit.jsonl")
    return wos, pend, sent, q, audit_lines


@app.get("/health",
         tags=["system"],
         response_model=HealthResponse,
         summary="Live check + baseline artifact counts",
         description="Health probe. Returns the current on-disk artifact counts so a caller can "
                     "confirm the pipeline has run and how much truth lives in the system.")
def health():
    wos, pend, sent, q, _ = _load_artifacts()
    return {"status": "ok", "work_orders": len(wos), "comms_pending": len(pend),
            "comms_sent": len(sent), "quarantined": len(q)}


@app.get("/tickets",
         tags=["tickets"],
         response_model=list[TicketSummary],
         summary="List the ingested ticket queue",
         description="Returns a PII-safe summary of every ticket in the queue used to seed the baseline run. "
                     "Bodies and driver details are intentionally omitted.")
def tickets():
    records = load_tickets()
    return [{"ticket_id": r.get("ticket_id"), "vehicle": r.get("vehicle"),
             "origin_hub": r.get("origin_hub"), "destination": r.get("destination"),
             "client": r.get("client"), "severity": r.get("severity"), "status": r.get("status")}
            for r in records]


@app.get("/tickets/{ticket_id}/timeline",
         tags=["tickets"],
         response_model=TimelineResponse,
         summary="Replay one ticket through every pipeline stage",
         description="Follows a single ticket from the queue to its outcome: the work order, the parsed "
                     "and (later) sent communication, quarantine state, audit events, and the raw per-node "
                     "LangGraph artifacts that captured each step's decision.")
def timeline(
    ticket_id: str = Path(..., description="Queue ticket id, e.g. \"TKT-0027\"", examples=["TKT-0027"]),
):
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


@app.get("/rules",
         tags=["knowledge"],
         response_model=list[RuleSpec],
         summary="List the extracted dispatch rule catalogue",
         description="Every rule the AI surfaced from the retiring dispatcher's knowledge — hard "
                     "constraints, soft constraints and heuristics — with the conditions that trigger "
                     "each rule and the knowledge citation behind it. Optionally filter by constraint class.")
def rules(
    severity: str | None = Query(default=None,
                                 description="Filter by constraint class: hard_constraint, soft_constraint or heuristic.",
                                 examples=["hard_constraint"]),
):
    out = all_rule_specs()
    if severity:
        out = [r for r in out if r.get("severity") == severity]
    return out


@app.get("/retrieval",
         tags=["knowledge"],
         response_model=RetrievalResponse,
         summary="Ask the RAG knowledge base anything",
         description="Embeds the query, retrieves the top-k similar chunks from Qdrant, and returns a "
                     "grounded answer when the best hit clears a 0.45 similarity threshold — otherwise "
                     "\"insufficient information\". Use this to verify that uploaded knowledge is retrievable.",
         responses={503: {"description": "Knowledge store unavailable (Qdrant / embedding model failed to load)."}})
def retrieval(
    query: str = Query(..., min_length=3, description="Free-text question, e.g. \"shakti cement replacement near Delhi\"",
                       examples=["shakti cement breakdown spare tyre policy"]),
    top_k: int = Query(default=6, ge=1, le=25, description="How many chunks to retrieve and rank."),
):
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


@app.get("/audit",
         tags=["audit"],
         response_model=list[AuditEvent],
         summary="Search the audit trail",
         description="Filtered view of every pipeline event: node executed, rule citations, decision, "
                     "and artifact linkage. Supports optional filters by ticket, node, and rule id.")
def audit_search(
    ticket_id: str | None = Query(default=None, description="Only events for this ticket.", examples=["TKT-0027"]),
    node: str | None = Query(default=None, description="Only events from this node (e.g. select_vehicle).",
                             examples=["select_vehicle"]),
    rule: str | None = Query(default=None, description="Only events that cited this rule id (e.g. R_ORIGIN_50KM).",
                             examples=["R_ORIGIN_50KM"]),
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum number of events to return."),
):
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


@app.get("/approvals",
         tags=["approvals"],
         response_model=list[ApprovalDraft],
         summary="List drafts awaiting a human decision",
         description="Every client communication that paused at the HITL gate and has not yet been sent.")
def approvals():
    _, pend, sent, _, _ = _load_artifacts()
    sent_ids = {s.get("ticket_id") for s in sent}
    return [d for d in pend if d.get("ticket_id") not in sent_ids]


@app.post("/approvals/{ticket_id}",
          tags=["approvals"],
          response_model=ApprovalResult,
          summary="Approve or reject a drafted communication",
          description="Resumes the exact paused LangGraph thread for this ticket: an APPROVE sends the "
                      "message (recorded with the approver identity), a REJECT kills the draft so nothing "
                      "is sent. Already-resolved tickets return the sent message instead.",
          responses={
              400: {"description": "decision must be APPROVE or REJECT."},
              404: {"description": "No pending approval exists for this ticket."},
          })
def approve(
    ticket_id: str = Path(..., description="Ticket with a pending draft, e.g. \"TKT-0027\"", examples=["TKT-0027"]),
    body: ApprovalDecision = ...,
):
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


@app.post("/tickets/{ticket_id}/process",
          tags=["tickets"],
          response_model=ProcessResult,
          summary="Run one queued ticket live through the full pipeline",
          description="Takes a ticket from the seed queue through schema recovery, enrichment, RAG "
                      "retrieval, rules, vehicle selection, work-order creation, draft and (auto-approve) "
                      "send — using the production graph and services. Quarantinable tickets return the "
                      "reason instead of running.",
          responses={404: {"description": "ticket is not in the queue."}})
def process_ticket(
    ticket_id: str = Path(..., description="Queue ticket id to run, e.g. \"TKT-0009\"", examples=["TKT-0009"]),
):
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