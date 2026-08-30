"""Write-actions the dashboard can take against the LIVE baseline pipeline:
resume a paused HITL thread (approve/reject/edit) using the real Outbox + graph.
"""
from __future__ import annotations

import json

from app.config import OUTPUTS_DIR, STATE_DB
from app.utils import read_jsonl


def resume(ticket_id: str, decision: str, by: str, body: str | None = None) -> dict:
    from app.ingest.loaders import load_fleet
    from app.knowledge.entities import build_vehicle_registry
    from app.knowledge.vectorstore import KnowledgeStore
    from app.pipeline.enrich import Services
    from app.pipeline.graph import build_checkpointer, make_graph
    from app.pipeline.outbox import Outbox
    from langgraph.types import Command

    if body is not None:
        _rewrite_pending_body(ticket_id, body)

    fleet = build_vehicle_registry(load_fleet())
    svc = Services(fleet=fleet, drivers={}, maint_by_veh={}, trips_by_veh={},
                   assigned=set(), run_id="dashboard-approval")
    svc.outbox = Outbox(STATE_DB)  # type: ignore[attr-defined]
    svc.store = KnowledgeStore()  # type: ignore[attr-defined]
    graph = make_graph(svc, build_checkpointer())
    config = {"configurable": {"thread_id": f"tkt-{ticket_id.lower()}"}}
    state = graph.get_state(config)
    if not state.next:
        return {"status": "already_resolved"}
    graph.invoke(Command(resume={"decision": decision, "by": by}), config=config)
    return {"status": "resumed", "decision": decision, "approver": by}


def _rewrite_pending_body(ticket_id: str, body: str) -> None:
    """Persist an editor's body so the exact revision flows into the sent message
    (the graph reads the draft from the outbox registry on resume)."""
    from pathlib import Path
    path = OUTPUTS_DIR / "comms_pending.jsonl"
    if not path.exists():
        return
    rows = read_jsonl(path)
    changed = False
    for r in rows:
        if r.get("ticket_id") == ticket_id:
            r["body"] = body
            changed = True
    if not changed:
        return
    Path(path.parent).mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")