"""Ticket Explorer — replay any ticket's timeline with a scrubber, then compare
two tickets side by side. Everything is rebuilt from artifacts (no hidden state)."""
from __future__ import annotations

import streamlit as st

from dashboard import base
from dashboard.widgets import json_viewer, page_header

_STEP_KEYS = {
    "create_work_order": "work order created",
    "draft_communication": "communication drafted",
    "hitl_approval": "human approval recorded",
    "send_communication": "communication sent",
}


def render() -> None:
    page_header(
        "Ticket Explorer",
        "Replay the exact lifecycle of any ticket: scrub the timeline and watch the work order, "
        "drafted email and final message appear at the moment they happened. Then compare any "
        "two tickets.",
        "The evaluator's best friend: prove a ticket's full path from intake to sent message in "
        "one page, then A/B two tickets to show the pipeline differs correctly (e.g. a "
        "2020+ requirement or a rejected draft).",
    )
    tickets = _all_tickets()
    if not tickets:
        st.info("No tickets processed yet.")
        return

    mode = st.radio("View", ["Replay timeline", "Compare two tickets"], horizontal=True, key="tkt_mode")
    if mode == "Replay timeline":
        _replay(tickets)
    else:
        _compare(tickets)


def _all_tickets() -> list[str]:
    ids = set()
    for r in (*base.work_orders(), *base.quarantine()):
        if r.get("ticket_id"):
            ids.add(r["ticket_id"])
    for p, s in zip(*base.comms()):
        if p.get("ticket_id"):
            ids.add(p["ticket_id"])
        if s.get("ticket_id"):
            ids.add(s["ticket_id"])
    return sorted(ids, key=str.lower)


def _load(ticket: str):
    wo = next((x for x in base.work_orders() if x.get("ticket_id") == ticket), None)
    quar = next((x for x in base.quarantine() if x.get("ticket_id") == ticket), None)
    draft = next((x for x in base.comms()[0] if x.get("ticket_id") == ticket), None)
    sent = next((x for x in base.comms()[1] if x.get("ticket_id") == ticket), None)
    audit = [a for a in base.audit() if a.get("ticket_id") == ticket]
    arts = base.node_artifacts(f"tkt-{ticket.lower()}")
    return wo, quar, draft, sent, audit, arts


def _replay(tickets: list[str]) -> None:
    sel = st.selectbox("Ticket", ["— pick —"] + tickets, key="tkt_sel")
    if sel == "— pick —"or not sel:
        return
    wo, quar, draft, sent, audit, arts = _load(sel)
    steps = [a for a in audit if a.get("node")]
    if not steps:
        st.info("No audited lifecycle events for this ticket.")
        return
    labels = []
    for a in steps:
        key = a.get("node")
        labels.append(f"{len(labels) + 1:02d} {_pretty(key)} — {a.get('decision') or ''} [{a.get('at') or ''}]".strip())
    step = st.slider("Timeline scrubber", 0, len(steps) - 1, len(steps) - 1,
                     format="%d", key="tkt_step",
                     help="Drag back to the moment each artifact first existed.")
    st.caption("→ "+ "→ ".join(labels))
    st.markdown(f"**Viewpoint: {labels[step]}**")
    _state_at(step, steps, wo, quar, draft, sent, arts)


def _pretty(node: str) -> str:
    return {
        "enrich": "Enrich", "retrieve": "Retrieve", "select_vehicle": "Select vehicle",
        "create_work_order": "Create work order", "draft_communication": "Draft comms",
        "hitl_approval": "Approval", "send_communication": "Send comms",
    }.get(node, node)


def _state_at(step: int, steps: list[dict], wo, quar, draft, sent, arts) -> None:
    seen_nodes = {s.get("node") for s in steps[: step + 1]}
    col = st.columns(3)
    col[0].metric("Work order", "created"if wo and "create_work_order"in seen_nodes else "pending")
    col[1].metric("Drafted comms",
                  "drafted"if draft and "draft_communication"in seen_nodes else "pending")
    col[2].metric("Sent", "sent"if sent and "send_communication"in seen_nodes else "not yet")

    with st.expander("Work order", expanded="create_work_order"in seen_nodes and bool(wo)):
        if wo:
            json_viewer(wo, f"wo_{wo.get('ticket_id')}", expanded=True)
        else:
            st.caption("No work order yet at this viewpoint")
    with st.expander("Drafted communication"):
        if draft and "draft_communication"in seen_nodes:
            json_viewer({k: draft.get(k) for k in ("message_id", "recipient", "subject", "body", "status")},
                        f"dr_{draft.get('ticket_id', 'x')}", expanded=True)
        else:
            st.caption("No draft yet at this viewpoint")
    with st.expander("Final communication sent"):
        if sent and "send_communication"in seen_nodes:
            json_viewer(sent, f"sn_{sent.get('ticket_id', 'x')}", expanded=True)
        else:
            st.caption("Not sent at this viewpoint")
    if quar:
        with st.expander("Quarantine event"):
            json_viewer({"reason": quar.get("reason"), "run_id": quar.get("run_id")},
                        f"q_{quar.get('ticket_id', 'x')}", expanded=True)
    if arts.get("retrieve"):
        st.subheader("Retrieved context (retrieval node)")
        json_viewer(arts["retrieve"], "rt_"+ sel_key(arts), expanded=False)


def sel_key(arts) -> str:
    return str(arts.get("retrieve", {}).get("ticket_id", "x"))


def _compare(tickets: list[str]) -> None:
    c1, c2 = st.columns(2)
    a = c1.selectbox("Ticket A", tickets, key="cmp_a")
    b = c2.selectbox("Ticket B", [t for t in tickets if t != a], key="cmp_b")
    for label, ticket in (("A", a), ("B", b)):
        wo, quar, draft, sent, audit, arts = _load(ticket)
        if label == "A":
            col = st.columns(2)[0]
        else:
            col = st.columns(2)[1]
        with col:
            st.markdown(f"#### {label} · {ticket}")
            sel_art = arts.get("select_vehicle") or {}
            sel = sel_art.get("selection") or {}
            st.metric("Selected vehicle", sel.get("chosen") or "—")
            st.caption(f"pool={sel.get('pool_size')} eliminations="
                       f"{len((sel.get('eliminations') or []))}")
            if wo:
                st.markdown(f"**WO** `{wo.get('work_order_id')}` "+ ",".join(
                    f"{c.get('source', '')}"for c in wo.get("citations") or []))
            if quar:
                st.error(f"Quarantined: {quar.get('reason')}")
            if draft:
                st.markdown("**Draft**")
                st.write(draft.get("body"))
            if sent:
                st.markdown(f"**Sent** by {sent.get('approved_by')} — "
                            f"`{sent.get('message_id')}`")
            if not (wo or quar or draft):
                st.caption("No lifecycle artifacts yet.")
    st.subheader("Why differ")
    st.write("The rule engine decides eligibility by client contract (BS stage / vehicle year / "
             "engine heater), origin distance, season and vehicle rotation — compare the "
             "Select Vehicle node artifacts above for eliminations and rule citations.")