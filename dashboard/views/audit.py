"""Audit Explorer — timeline view of every decision with filters for ticket,
node, severity and run. Click a decision to open its evidence, artifacts and
LangSmith trace reference."""
from __future__ import annotations

import streamlit as st

from dashboard import base
from dashboard.widgets import data_table, json_viewer, page_header

SEVERITY = {"HIGH", "MEDIUM", "LOW"}


def render() -> None:
    page_header(
        "Audit Explorer",
        "Every decision the system made, in order, with the data it used, the rules it cited "
        "and a pointer to the LangSmith trace — replayable from a single JSONL file.",
        "Immutability + accountability: any decision can be traced to its inputs and its model "
        "trace. This page is the compliance view — prove what happened, when, and why.",
    )
    lines = base.audit()
    if not lines:
        st.info("No audit events yet.")
        return

    c1, c2, c3, c4 = st.columns(4)
    nodes = sorted({a.get("node") for a in lines})
    f_ticket = c1.text_input("ticket_id")
    f_node = c2.selectbox("Node", ["all"] + nodes)
    f_sev = c3.multiselect("Severity", sorted(SEVERITY))
    f_run = c4.selectbox("Run", ["all"] + sorted({a.get("run_id") for a in lines}))

    out = lines
    if f_ticket:
        out = [a for a in out if f_ticket.lower() in (a.get("ticket_id") or "").lower()]
    if f_node != "all":
        out = [a for a in out if a.get("node") == f_node]
    if f_sev:
        out = [a for a in out if _row_sev(a) in f_sev]
    if f_run != "all":
        out = [a for a in out if a.get("run_id") == f_run]

    st.caption(f"{len(out)} / {len(lines)} events")
    data_table(out, "audit_events",
               columns=["at", "run_id", "ticket_id", "node", "decision", "rule_ids", "actor"],
               height=360)

    if not out:
        return
    pick = st.selectbox("Inspect event", ["— pick —"] + [
        f"{a.get('ticket_id','?')} · {a.get('node')} · {a.get('decision','')[:60]}"
        for a in out], key="aud_pick")
    if pick == "— pick —"or not pick:
        return
    event = next(a for a in out if
                 f"{a.get('ticket_id','?')} · {a.get('node')} · {a.get('decision','')[:60]}"== pick)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**State / decision payload**")
        json_viewer({k: event.get(k) for k in ("decision", "detail", "data_refs", "citations")},
                    "aud_state", expanded=True)
    with c2:
        st.markdown("**Rule citations**")
        if event.get("rule_ids"):
            for rid in event.get("rule_ids") or []:
                spec = next((r for r in base.rules() if r.get("id") == rid), None)
                if spec:
                    st.markdown(f"- **{rid}** — {spec.get('condition')}\n"
                                f"citation: `{spec.get('citation')}`")
                else:
                    st.markdown(f"- {rid}")
        else:
            st.caption("No rules cited for this step.")
        st.markdown("**LangSmith / artifacts**")
        st.write("trace pointer available in the Retrieval node artifact "
                 "(`langsmith_trace`) — open the Live Pipeline page for a full trace view.")

    thread = event.get("thread_id") or f"tkt-{(event.get('ticket_id') or '').lower()}"
    arts = base.node_artifacts(thread)
    if arts and st.button("Open graph artifacts for this thread "):
        base.goto("pipeline")
        st.session_state["pipe_thread"] = thread
        st.rerun()


def _row_sev(a: dict) -> str:
    det = a.get("detail") or {}
    return (det.get("severity") or "MEDIUM").upper() if isinstance(det, dict) else "MEDIUM"