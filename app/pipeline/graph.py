"""LangGraph pipeline: breakdown -> validate -> enrich/retrieve (parallel) ->
select -> work order -> draft comms -> HITL interrupt() -> send -> audit.

Every node writes a replay artifact under artifacts/graph/<thread_id>/ and tags
each with the LangSmith run_id + graph thread_id so any decision is traceable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langchain_core.runnables import RunnableConfig

from app.config import STATE_DB
from app.pipeline.audit import audit
from app.pipeline.comms import draft_communication, NO_COMMS_COMMENT
from app.pipeline.enrich import build_context
from app.pipeline.outbox import Outbox
from app.pipeline.retrieve import retrieve_context
from app.pipeline.select_vehicle import select_vehicle
from app.utils import atomic_write_json


class PipelineState(TypedDict, total=False):
    run_id: str
    thread_id: str
    ticket_id: str
    ticket: dict[str, Any]
    ctx: dict[str, Any]
    retrieval: dict[str, Any]
    selection: dict[str, Any]
    work_order: dict[str, Any]
    draft: dict[str, Any] | None
    approval: Any
    comms_sent: dict[str, Any] | None
    graph_notes: list[str]


def _artifact_dir(thread_id: str):
    from app.config import ARTIFACTS_DIR
    d = ARTIFACTS_DIR / "graph" / thread_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trace_ids(config: dict[str, Any]) -> str:
    """Volatile LangSmith trace run id — kept for artifact provenance only, never
    in business-truth output (which must stay byte-identical across replays)."""
    try:
        from langsmith.run_helpers import get_current_run_tree
        tree = get_current_run_tree()
        if tree is not None and getattr(tree, "id", None):
            return str(tree.id)
    except Exception:
        pass
    return ""


def _node_artifact(thread_id: str, node: str, run_id: str, payload: dict[str, Any]) -> None:
    atomic_write_json(_artifact_dir(thread_id) / f"{node}.json",
                      {"node": node, "run_id": run_id, "thread_id": thread_id, **payload})


def ticket_created_for_send(state) -> str:
    """Canonical event time for comms_sent: the ticket's creation time, kept for
    byte-stable replays (the physical wall-clock is preserved in the audit line)."""
    t = state.get("ticket") or {}
    return t.get("created_at", datetime.now(timezone.utc).isoformat())


def _audit_node(state, node: str, decision: str, data_refs, rule_ids, citations, detail, actor="system"):
    audit({
        "run_id": state.get("run_id", ""), "thread_id": state.get("thread_id", ""),
        "ticket_id": state.get("ticket_id", ""), "step": node, "node": node,
        "decision": decision, "data_refs": data_refs, "rule_ids": rule_ids,
        "citations": citations, "actor": actor, "detail": detail,
    })


def make_graph(services, checkpointer) -> Any:
    outbox: Outbox = services.outbox
    store = services.store
    svc = services

    # ---------------------------------------------------------------- nodes
    def enrich_node(state: PipelineState, config: RunnableConfig) -> dict[str, Any]:
        run_id = state.get("run_id", "")
        trace_id = _trace_ids(config)
        thread_id = (config.get("configurable") or {}).get("thread_id", "thread-x")
        ticket = state["ticket"]
        from app.ingest.models import Ticket
        ctx, citations = build_context(Ticket.model_validate(ticket), svc)
        ctx["citations"] = citations
        ctx["ticket"] = ticket
        ctx["ticket_id"] = ticket.get("ticket_id")
        ctx["run_id"], ctx["thread_id"] = run_id, thread_id
        svc.run_id = run_id
        s = {**state, "thread_id": thread_id, "ctx": {**ctx, "ticket": ticket}}
        _node_artifact(thread_id, "enrich", run_id, {
            "ticket_id": state["ticket_id"], "context": ctx, "langsmith_trace": trace_id,
        })
        return {"ctx": s["ctx"], "thread_id": thread_id}

    def retrieve_node(state: PipelineState, config: RunnableConfig) -> dict[str, Any]:
        run_id = state.get("run_id", "")
        trace_id = _trace_ids(config)
        thread_id = (config.get("configurable") or {}).get("thread_id", "thread-x")
        ticket = state["ticket"]
        issues = [ticket.get("issue", ""), ticket.get("client", "")]
        query = f"{ticket.get('client','')} {ticket.get('issue','')} breakdown replacement {ticket.get('vehicle','')}"
        retr = retrieve_context(query, store, run_id=run_id, thread_id=thread_id)
        if issues and issues[1]:
            retr2 = retrieve_context(f"{ticket.get('client','')} client dispatch rules", store,
                                     run_id=run_id, thread_id=thread_id, top_k=4)
            retr["hits"] = retr2["hits"] + retr["hits"]
            retr["top_refs"] = sorted({h["source"] for h in retr["hits"]})
        retr["langsmith_trace"] = trace_id
        _node_artifact(thread_id, "retrieve", run_id, {"ticket_id": state["ticket_id"], "retrieval": retr})
        return {"retrieval": retr}

    def merge_node(state: PipelineState) -> dict[str, Any]:
        retr = state.get("retrieval", {})
        ctx = dict(state.get("ctx", {}))
        hits = retr.get("hits", [])
        for h in hits[:6]:
            ctx.setdefault("citations", [])
            if not any(c.get("ref") == h.get("chunk_id") for c in ctx["citations"]):
                ctx["citations"].append({"source": h.get("source", ""), "ref": h.get("chunk_id"),
                                         "document_type": h.get("document_type", "knowledge_chunk"),
                                         "score": h.get("score")})
            ctx.setdefault("retrieved_chunks", []).append(h)
        return {"ctx": ctx}

    def select_node(state: PipelineState, config: RunnableConfig) -> dict[str, Any]:
        run_id = state.get("run_id", "")
        trace_id = _trace_ids(config)
        thread_id = (config.get("configurable") or {}).get("thread_id", "thread-x")
        ticket, ctx = state["ticket"], state["ctx"]
        from app.ingest.models import Ticket
        sel = select_vehicle(Ticket.model_validate(ticket), svc, ctx)
        sel_dict = sel.model_dump()
        # remember chosen metadata for comms
        chosen_reg = sel_dict.get("chosen")
        chosen_meta = svc.fleet.get(chosen_reg) if chosen_reg else None
        sel_dict["candidate_hub"] = (chosen_meta.home_hub if chosen_meta else
                                     (ctx.get("vehicle") or {}).get("home_hub", ""))
        sel_dict["chosen_year"] = chosen_meta.year if chosen_meta else None
        _audit_node(state, "select_vehicle",
                    f"selected={sel_dict.get('chosen') or 'NONE'}",
                    ["fleet_master.csv", "maintenance_log.xlsx"],
                    [v["rule_id"] for v in ctx.get("verdicts", []) if v["triggered"]],
                    [c["source"] for c in sel_dict.get("citations", [])],
                    detail={"eliminations": sel_dict.get("eliminations")[:40],
                            "notes": sel_dict.get("notes"), "pool_size": sel_dict.get("pool_size")})
        _node_artifact(thread_id, "select_vehicle", run_id,
                       {"ticket_id": state["ticket_id"], "selection": sel_dict, "langsmith_trace": trace_id})
        # mark the chosen vehicle as assigned for this queue run
        if chosen_reg:
            svc.assigned.add(chosen_reg)
        return {"selection": sel_dict}

    def work_order_node(state: PipelineState) -> dict[str, Any]:
        ticket, sel = state["ticket"], state["selection"]
        wo = {
            "work_order_id": f"WO-{ticket['ticket_id']}",
            "ticket_id": ticket["ticket_id"],
            "vehicle_reg": sel.get("chosen") or ticket["vehicle"],
            "created_at": ticket["created_at"],
            "citations": [{"source": c.get("source"), "ref": c.get("ref")}
                          for c in sel.get("citations", [])][:8],
        }
        wo = outbox.write_work_order(wo, state.get("run_id", "")) or wo
        _audit_node(state, "create_work_order", f"work_order {wo['work_order_id']} vehicle={wo['vehicle_reg']}",
                    ["outbox", "work_orders.jsonl"], state.get("ctx", {}).get("applied_rule_ids", []),
                    [c["source"] for c in wo.get("citations", [])],
                    detail={"vehicle_reg": wo["vehicle_reg"], "idempotent": wo["ticket_id"] == ticket["ticket_id"]})
        s = {**state, "work_order": wo}
        _node_artifact(state.get("thread_id", state["ticket_id"]), "create_work_order",
                       state.get("run_id", ""), {"work_order": wo})
        return {"work_order": wo}

    def draft_node(state: PipelineState) -> dict[str, Any]:
        ctx, sel = state["ctx"], state["selection"]
        draft = draft_communication(ctx, sel, run_id=state.get("run_id", ""),
                                    thread_id=state.get("thread_id", ""))
        if draft is None:
            draft = {"message_id": None, "ticket_id": state["ticket_id"], "status": "NO_COMMS",
                     "context_summary": {"note": NO_COMMS_COMMENT}}
            decision = "no_client_communication_needed"
        else:
            draft = outbox.write_comms_draft(draft, state.get("run_id", "")) or draft
            decision = f"draft queued for approval ({draft['message_id']})"
        _audit_node(state, "draft_communication", decision,
                    ["comms_pending.jsonl"], state.get("ctx", {}).get("applied_rule_ids", []),
                    (draft.get("citations") or []) and [c["source"] for c in draft["citations"]],
                    detail={"recipient": draft.get("recipient"), "status": draft.get("status")})
        _node_artifact(state.get("thread_id", state["ticket_id"]), "draft_communication",
                       state.get("run_id", ""), {"draft": draft})
        return {"draft": draft}

    def hitl_node(state: PipelineState) -> dict[str, Any]:
        payload = {
            "gate": "send_client_communication",
            "message": state.get("draft"),
            "ticket_id": state["ticket_id"],
            "work_order": state.get("work_order"),
            "approve_required": state.get("draft", {}).get("status") == "PENDING_APPROVAL",
        }
        approval = interrupt(payload)
        _audit_node(state, "hitl_approval",
                    f"approval_received={approval}",
                    ["comms_pending.jsonl"], [], [], detail={"approval": approval}, actor="human")
        return {"approval": approval}

    def send_node(state: PipelineState) -> dict[str, Any]:
        draft = state.get("draft") or {}
        approval = state.get("approval")
        if not draft.get("status") == "PENDING_APPROVAL":
            _audit_node(state, "send_communication", "no_send_skipped_no_draft",
                        [], [], [], detail={"note": NO_COMMS_COMMENT})
            return {"comms_sent": None}
        decision_ok = isinstance(approval, dict) and approval.get("decision", "").upper() in ("APPROVE", "SEND", "YES")
        if not decision_ok:
            _audit_node(state, "send_communication", "rejected_or_edited_not_sent",
                        [], [], [], detail={"approval": approval}, actor="human")
            return {"comms_sent": None}
        by = approval.get("by") if isinstance(approval, dict) else "Approver"
        msg = {
            "message_id": draft.get("message_id"),
            "ticket_id": draft.get("ticket_id"),
            "recipient": draft.get("recipient"),
            "body": draft.get("body"),
            "approved_by": by,
            "sent_at": ticket_created_for_send(state),  # canonical event time -> byte-stable replays
        }
        msg = outbox.write_comms_sent(msg, state.get("run_id", "")) or msg
        _audit_node(state, "send_communication",
                    f"sent {msg.get('message_id')} by {by}", ["comms_sent.jsonl"], [],
                    [c["source"] for c in (draft.get("citations") or [])],
                    detail={"recipient": msg.get("recipient")}, actor="human")
        s = {**state, "comms_sent": msg}
        _node_artifact(state.get("thread_id", state["ticket_id"]), "send_communication",
                       state.get("run_id", ""), {"comms_sent": msg})
        return {"comms_sent": msg}

    # ---------------------------------------------------------------- graph
    g = StateGraph(PipelineState)
    g.add_node("enrich", enrich_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("merge", merge_node)
    g.add_node("select_vehicle", select_node)
    g.add_node("create_work_order", work_order_node)
    g.add_node("draft_communication", draft_node)
    g.add_node("hitl_approval", hitl_node)
    g.add_node("send_communication", send_node)

    g.add_edge(START, "enrich")
    g.add_edge(START, "retrieve")
    g.add_edge("enrich", "merge")
    g.add_edge("retrieve", "merge")
    g.add_edge("merge", "select_vehicle")
    g.add_edge("select_vehicle", "create_work_order")
    g.add_edge("create_work_order", "draft_communication")
    g.add_edge("draft_communication", "hitl_approval")
    g.add_edge("hitl_approval", "send_communication")
    g.add_edge("send_communication", END)

    return g.compile(checkpointer=checkpointer)


def build_checkpointer(db_path=STATE_DB):
    from langgraph.checkpoint.sqlite import SqliteSaver
    import sqlite3
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return SqliteSaver(conn)