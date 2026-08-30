"""Meridian Freight — operations dashboard. Reads artifacts from disk only
(no hidden in-memory state). Evaluator can reconstruct any ticket in under a
minute from here."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Meridian Freight Ops", layout="wide")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ARTIFACTS_DIR, AUDIT_DIR, OUTPUTS_DIR, DATA_DIR  # noqa: E402
from app.utils import read_jsonl  # noqa: E402

PAGES = {
    "Executive Overview": "overview",
    "Live Pipeline": "pipeline",
    "Ticket Explorer": "ticket",
    "Knowledge Explorer": "knowledge",
    "Rule Explorer": "rules",
    "Entity Explorer": "entities",
    "Retrieval Inspector": "retrieval",
    "HITL Console": "hitl",
    "Audit Explorer": "audit",
}


def _j(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _work_orders():
    return read_jsonl(OUTPUTS_DIR / "work_orders.jsonl")


def _comms():
    return read_jsonl(OUTPUTS_DIR / "comms_pending.jsonl"), read_jsonl(OUTPUTS_DIR / "comms_sent.jsonl")


def _quarantine():
    return read_jsonl(OUTPUTS_DIR / "quarantine.jsonl")


def _audit():
    return read_jsonl(AUDIT_DIR / "audit.jsonl")


def _chunks():
    return read_jsonl(ARTIFACTS_DIR / "knowledge" / "chunks.jsonl")


# ================================================================ pages
def page_overview():
    st.title("Executive Overview")
    wos, pend, sent = _work_orders(), *_comms()
    q = _quarantine()
    records = {}
    if (OUTPUTS_DIR / "run_summary.json").exists():
        records = json.load(open(OUTPUTS_DIR / "run_summary.json", encoding="utf-8"))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Work orders", len(wos))
    c2.metric("Pending approvals", sum(1 for p in pend if p.get("status") == "PENDING_APPROVAL"))
    c3.metric("Comms sent", len(sent))
    c4.metric("Quarantined", len(q))
    st.subheader("Pipeline health")
    st.json({
        "run_id": records.get("run_id", "n/a"),
        "records": records.get("records"), "unique": records.get("unique"),
        "duplicates": records.get("duplicates"), "quarantined": records.get("quarantined"),
        "approve_mode": records.get("approve_mode"),
        "work_orders": len(wos), "comms_pending": len(pend), "comms_sent": len(sent),
        "quarantine_records": len(q),
    })
    if q:
        st.subheader("Quarantine alerts")
        st.table([{k: x.get(k) for k in ("ticket_id", "reason")} for x in q])


def page_pipeline():
    st.title("Live Pipeline")
    audit_lines = _audit()
    by_ticket = {}
    for a in audit_lines:
        by_ticket.setdefault(a.get("thread_id") or a.get("ticket_id"), []).append(a)
    st.caption("History is read from audit.jsonl + artifacts/graph/<thread_id>/*.json — nodes are replayable.")
    if not by_ticket:
        st.info("No pipeline runs yet.")
        return
    sel = st.selectbox("Thread / ticket", sorted(by_ticket.keys()))
    if sel:
        steps = by_ticket[sel]
        node_files = sorted((ARTIFACTS_DIR / "graph" / sel).glob("*.json")) if (ARTIFACTS_DIR / "graph" / sel).exists() else []
        cols = [f.stem for f in node_files]
        st.markdown("**Node trace:** " + " → ".join(cols) if cols else "No node artifacts yet")
        for s in steps:
            st.json({k: s.get(k) for k in ("step", "decision", "node", "rule_ids", "at")})


def page_ticket():
    st.title("Ticket Explorer")
    wos = _work_orders()
    pend, sent = _comms()
    q = _quarantine()
    ticket_ids = sorted({x.get("ticket_id") for x in wos} | {x.get("ticket_id") for x in q}) or []
    sel = st.selectbox("Ticket", ticket_ids) if ticket_ids else None
    if not sel:
        st.info("No tickets processed yet.")
        return
    wo = next((x for x in wos if x.get("ticket_id") == sel), None)
    draft = next((x for x in pend if x.get("ticket_id") == sel), None)
    msg = next((x for x in sent if x.get("ticket_id") == sel), None)
    quar = next((x for x in q if x.get("ticket_id") == sel), None)
    st.subheader("Timeline")
    for a in [x for x in _audit() if x.get("ticket_id") == sel]:
        st.write(f"- **{a.get('step')}** — {a.get('decision')}  `{a.get('at','')}`")
    if wo:
        st.subheader("Work order")
        st.json(wo)
    if quar:
        st.subheader("Quarantine")
        st.json({"reason": quar.get("reason"), "raw_masked": quar.get("raw_masked")})
    if draft:
        st.subheader("Drafted communication (approver view)")
        st.json({"message_id": draft.get("message_id"), "recipient": draft.get("recipient"),
                 "subject": draft.get("subject"), "body": draft.get("body"),
                 "citations": draft.get("citations"),
                 "context_summary": draft.get("context_summary")})
    if msg:
        st.subheader("Sent communication")
        st.json(msg)
    thread_dir = ARTIFACTS_DIR / "graph" / f"tkt-{sel.lower()}"
    if thread_dir.exists():
        st.subheader("Retrieved context / applied rules / eliminated vehicles")
        for f in ("retrieve", "select_vehicle", "enrich"):
            if (thread_dir / f"{f}.json").exists():
                st.markdown(f"**{f}**")
                st.json(_j(thread_dir / f"{f}.json"))


def page_knowledge():
    st.title("Knowledge Explorer")
    chunks = _chunks()
    st.caption(f"{len(chunks)} chunks")
    search = st.text_input("Filter by source / document_type")
    if search:
        chunks = [c for c in chunks if search.lower() in (c.get("source") or "").lower()
                  or search.lower() in (c.get("document_type") or "").lower()]
    if chunks:
        sel = st.selectbox("Chunk", [f'{c.get("chunk_id")}  [{c.get("source")}]' for c in chunks])
        idx = next(i for i, c in enumerate(chunks) if f'{c.get("chunk_id")}  [{c.get("source")}]' == sel)
        st.json(chunks[idx])


def page_rules():
    st.title("Rule Explorer")
    from app.rules.engine import all_rule_specs
    rules = all_rule_specs()
    gcd = lambda r, k: r.get(k) or "any"
    sev = st.multiselect("Severity", sorted({r.get("severity") for r in rules}), default=None)
    cli = st.multiselect("Client", sorted({gcd(r, "client") for r in rules}), default=None)
    sea = st.multiselect("Season", sorted({gcd(r, "season") for r in rules}), default=None)
    roa = st.multiselect("Route", sorted({gcd(r, "route") for r in rules}), default=None)
    for r in rules:
        if sev and r.get("severity") not in sev:
            continue
        if cli and gcd(r, "client") not in cli:
            continue
        if sea and gcd(r, "season") not in sea:
            continue
        if roa and gcd(r, "route") not in roa:
            continue
        with st.expander(f"{r.get('id')} — {r.get('severity')} [client={gcd(r,'client')} season={gcd(r,'season')} route={gcd(r,'route')}]"):
            st.write(r.get("condition"))
            st.write("**Action:** ", r.get("action"))
            st.write("**Citation:** ", r.get("citation"))
            if r.get("note"):
                st.caption("Note: " + r.get("note"))


def page_entities():
    st.title("Entity Explorer")
    veh = _j(ARTIFACTS_DIR / "entities" / "vehicles.json") or {}
    drivers = _j(ARTIFACTS_DIR / "entities" / "drivers.json") or []
    clients = _j(ARTIFACTS_DIR / "entities" / "clients.json") or []
    tab1, tab2, tab3, tab4 = st.tabs(["Vehicles", "Drivers", "Clients", "Conflicts"])
    with tab1:
        st.write(f"{len(veh.get('vehicles', []))} canonical vehicles")
        sel = st.selectbox("Vehicle", [f'{v.get("canonical_reg")} ({v.get("vehicle_id")})' for v in veh.get("vehicles", [])])
        v = next(x for x in veh.get("vehicles", []) if f'{x.get("canonical_reg")} ({x.get("vehicle_id")})' == sel)
        col1, col2 = st.columns(2)
        col1.json({k: v.get(k) for k in ("canonical_reg", "vehicle_id", "model", "year", "bs_stage", "engine_heater", "home_hub", "capacity_tonnes", "status")})
        col2.write("**Aliases**"); col2.json(v.get("aliases"))
        st.write("**Provenance**"); st.json(v.get("provenance"))
    with tab2:
        st.write(f"{len(drivers)} drivers (PII masked)")
        st.table([{k: d.get(k) for k in ("driver_id", "home_hub", "joining_date")} for d in drivers[:50]])
    with tab3:
        st.json(clients)
    with tab4:
        st.json(veh.get("conflicts_resolved", []))


def page_retrieval():
    st.title("Retrieval Inspector")
    query = st.text_input("query")
    if st.button("Search") and query:
        try:
            from app.knowledge.vectorstore import KnowledgeStore
            store = KnowledgeStore()
            hits = store.search(query, top_k=6, run_id="dashboard", thread_id="dashboard")
            if not hits:
                st.warning("insufficient information — no grounded chunks")
            for h in hits:
                with st.expander(f"{h['score']:.3f} — {h['source']}"):
                    st.write(h["text"])
        except Exception as exc:
            st.error(f"store unavailable: {exc}")


def page_hitl():
    st.title("HITL Console")
    pend, sent = _comms()
    sent_ids = {s.get("ticket_id") for s in sent}
    pending = [p for p in pend if p.get("ticket_id") not in sent_ids
               and p.get("status") == "PENDING_APPROVAL"]
    if not pending:
        st.info("No pending approvals")
        return
    sel = st.selectbox("Queue", [f'{p.get("ticket_id")} — {p.get("recipient")}' for p in pending])
    draft = next(p for p in pending if f'{p.get("ticket_id")} — {p.get("recipient")}' == sel)
    st.write("**Subject:**", draft.get("subject"))
    st.write("**Body:**")
    st.write(draft.get("body"))
    st.write("**Citations:**"); st.json(draft.get("citations"))
    edited = st.text_area("Edit body (optional)", value=draft.get("body", ""), height=200)
    c1, c2 = st.columns(2)
    do_approve = c1.button("Approve and send")
    do_reject = c2.button("Reject")
    if do_approve or do_reject:
        decision, by = ("APPROVE", "Approver(Dashboard)") if do_approve else ("REJECT", "Approver(Dashboard)")
        if do_approve and edited != draft.get("body"):
            by = "Approver(Dashboard-edited)"
        try:
            _resume(draft.get("ticket_id"), decision, by, edited if do_approve else draft.get("body"))
            st.success(f"{decision} recorded; thread resumed.")
            st.rerun()
        except Exception as exc:
            st.error(f"resume failed: {exc}")


def _resume(ticket_id: str, decision: str, by: str, body: str | None = None):
    from app.config import STATE_DB
    from app.ingest.loaders import load_fleet
    from app.knowledge.entities import build_vehicle_registry
    from app.knowledge.vectorstore import KnowledgeStore
    from app.pipeline.enrich import Services
    from app.pipeline.graph import build_checkpointer, make_graph
    from app.pipeline.outbox import Outbox
    from langgraph.types import Command
    fleet = build_vehicle_registry(load_fleet())
    svc = Services(fleet=fleet, drivers={}, maint_by_veh={}, trips_by_veh={},
                   assigned=set(), run_id="dashboard-approval")
    svc.outbox = Outbox(STATE_DB)
    svc.store = KnowledgeStore()
    graph = make_graph(svc, build_checkpointer())
    config = {"configurable": {"thread_id": f"tkt-{ticket_id.lower()}"}}
    state = graph.get_state(config)
    if not state.next:
        return {"status": "already_resolved"}
    # allow an edited body to flow into what is sent
    if body is not None:
        from app.utils import append_jsonl
        pend_path = OUTPUTS_DIR / "comms_pending.jsonl"
        rows = read_jsonl(pend_path)
        for r in rows:
            if r.get("ticket_id") == ticket_id:
                r["body"] = body
        from pathlib import Path
        Path(pend_path.parent).mkdir(parents=True, exist_ok=True)
        import json as _json
        with open(pend_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(_json.dumps(r, ensure_ascii=False, default=str) + "\n")
    graph.invoke(Command(resume={"decision": decision, "by": by}), config=config)
    return {"status": "resumed"}


def page_audit():
    st.title("Audit Explorer")
    lines = _audit()
    tid = st.text_input("ticket_id")
    node = st.selectbox("node", [""] + sorted({a.get("node") for a in lines}))
    rule = st.text_input("rule_id")
    out = lines
    if tid:
        out = [a for a in out if a.get("ticket_id") == tid]
    if node:
        out = [a for a in out if a.get("node") == node]
    if rule:
        out = [a for a in out if rule in (a.get("rule_ids") or [])]
    st.write(f"{len(out)} lines")
    st.json(out[-200:])


def main():
    st.sidebar.title("Meridian Freight")
    choice = st.sidebar.radio("Navigate", list(PAGES.keys()))
    globals()[f"page_{PAGES[choice]}"]()


if __name__ == "__main__":
    main()