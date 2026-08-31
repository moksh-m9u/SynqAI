"""Human Approval Console — approve, reject, edit and compare revisions of every
drafted client communication, with approver + timestamp history."""
from __future__ import annotations

import streamlit as st

from dashboard import actions, base
from dashboard.widgets import data_table, json_viewer, page_header


def render() -> None:
    page_header(
        "Human Approval Console",
        "Every drafted client communication queues here for a human. Approve, reject, or edit "
        "the body — and audit who approved what, when.",
        "The HITL gate is the 'humans stay in the loop' proof. A judge can review the exact "
        "draft + citations, edit it, approve it, and then see the sent message with the "
        "approver identity and timestamp.",
    )
    pend, sent = base.comms()
    sent_ids = {s.get("ticket_id") for s in sent}
    pending = [p for p in pend if p.get("ticket_id") not in sent_ids
               and p.get("status") == "PENDING_APPROVAL"]

    st.subheader("Inbox")
    if not pending:
        st.success("No pending approvals — the queue is drained.")
    else:
        st.caption(f"{len(pending)} drafts awaiting a human decision.")
        data_table(pending, "hitl_inbox",
                   columns=["ticket_id", "recipient", "subject", "message_id", "sent_at"],
                   height=260)
        sel = st.selectbox("Review draft",
                           [f'{p.get("ticket_id")} — {p.get("recipient")}'
                            for p in pending], key="hitl_sel")
        draft = next(p for p in pending
                     if f'{p.get("ticket_id")} — {p.get("recipient")}' == sel)
        c1, c2 = st.columns([1, 1])
        with c1:
            st.markdown("**Original subject:** "+ (draft.get("subject") or "") + "\n\n"
                        "**Recipient:** "+ (draft.get("recipient") or "") + "· "
                        "**status:** "+ (draft.get("status") or ""))
            st.markdown("**Original body**")
            st.write(draft.get("body"))
        with c2:
            st.markdown("**Citations / retrieved context the model relied on**")
            json_viewer(draft.get("citations") or draft.get("context_summary") or {},
                        "hitl_cites", expanded=True)

        edited = st.text_area("Edit body before sending (optional)", value=draft.get("body", ""),
                              height=200, key="hitl_body")
        rev = st.session_state.get("hitl_last_edit", "")
        if rev and rev != draft.get("body", ""):
            st.markdown("**Compared revision** (last edited vs original)")
            c3, c4 = st.columns(2)
            c3.caption("Original")
            c3.write(draft.get("body"))
            c4.caption("Edited")
            c4.write(rev)

        a, b, ccol = st.columns(3)
        if a.button("Approve", type="primary", use_container_width=True, key="hitl_ok"):
            actions.resume(draft.get("ticket_id"), "APPROVE", "Approver(Dashboard)", edited)
            st.session_state["hitl_last_edit"] = edited
            st.success("Approved; thread resumed and message enqueued for sending.")
            st.rerun()
        if b.button("Reject", use_container_width=True, key="hitl_no"):
            actions.resume(draft.get("ticket_id"), "REJECT", "Approver(Dashboard)", draft.get("body"))
            st.session_state["hitl_last_edit"] = draft.get("body")
            st.success("Rejected; no communication will be sent for this ticket.")
            st.rerun()
        if ccol.button("Save edit comparison", use_container_width=True, key="hitl_edit"):
            st.session_state["hitl_last_edit"] = edited
            st.rerun()

    st.subheader("Approval history")
    if sent:
        data_table([{k: s.get(k) for k in ("ticket_id", "message_id", "recipient", "approved_by", "sent_at")}
                    for s in sent], "hitl_hist", height=280)
    else:
        st.caption("Nothing sent yet.")