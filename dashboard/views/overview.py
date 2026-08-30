"""Executive Overview + Pipeline Status + Quick Actions."""
from __future__ import annotations

import streamlit as st

from dashboard import base
from dashboard.widgets import data_table, json_viewer, page_header


def render() -> None:
    page_header(
        "Executive Overview",
        "Where the Meridian Freight system stands right now — baseline run status, live "
        "services, and shortcuts to test the pipeline.",
        "This is the judge's front door. It proves the system is live, shows exactly what ran, "
        "and gives one-click entry to every interactive test (upload a ticket, break the "
        "pipeline, probe retrieval) without digging through folders.",
    )

    wos = base.work_orders()
    pending, sent = base.comms()
    quar = base.quarantine()
    summary = base.run_summary()

    try:
        pending_count = sum(1 for p in pending if p.get("status") == "PENDING_APPROVAL")
    except Exception:
        pending_count = len(pending)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Work orders", len(wos))
    c2.metric("Pending approvals", pending_count)
    c3.metric("Comms sent", len(sent))
    c4.metric("Quarantined", len(quar))
    st.caption("Baseline truth is final: work_orders / comms_* / quarantine JSONL under outputs/.")

    st.subheader("Pipeline status")
    vdb = base.vector_db_status()
    status = {
        "last_run_id": (summary or {}).get("run_id", "n/a"),
        "records": (summary or {}).get("records"),
        "work_orders": (summary or {}).get("work_orders"),
        "quarantined": (summary or {}).get("quarantined"),
        "approve_mode": (summary or {}).get("approve_mode"),
        "llm": "qwen/qwen3.8-27b (Groq)",
        "embedding": "granite-embedding-97m-multilingual-r2 (local, 384-dim)",
        "vector_db": ("connected"if vdb.get("ok") else f"unavailable: {vdb.get('error')}"),
        "collections": vdb.get("collections", []),
        "collection_sizes": vdb.get("counts", {}),
        "langsmith_tracing": "enabled (LANGSMITH) on pipeline runs",
        "avg_retrieval_latency_ms": _avg_retrieval_latency(),
        "sandbox_runs": len(base.sandbox_list()),
    }
    json_viewer(status, "pipeline_status", expanded=True)

    st.subheader("Quick actions")
    qa = st.columns(5)
    with qa[0]:
        if st.button("Upload single ticket", use_container_width=True):
            base.goto("sandbox")
            st.rerun()
    with qa[1]:
        if st.button("Upload ticket batch", use_container_width=True):
            base.goto("sandbox")
            st.rerun()
    with qa[2]:
        if st.button("Load sample ticket", use_container_width=True):
            st.session_state["sandbox_action"] = "sample"
            base.goto("sandbox")
            st.rerun()
    with qa[3]:
        if st.button("Chaos test", use_container_width=True):
            base.goto("chaos")
            st.rerun()
    with qa[4]:
        if st.button("Retrieval playground", use_container_width=True):
            base.goto("retrieval")
            st.rerun()

    if quar:
        st.subheader("Quarantine alerts")
        data_table(quar, "quarantine_overview",
                   columns=["ticket_id", "reason", "run_id", "quarantined_at"])

    st.subheader("Recent sandbox / live runs")
    runs = base.sandbox_list()
    if runs:
        data_table(runs[:10], "sandbox_runs",
                   columns=["run_id", "kind", "records", "work_orders", "quarantined", "started", "ok"])
    else:
        st.info("No interactive sandbox runs yet — use the Ticket Sandbox page.")


def _avg_retrieval_latency() -> float | None:
    arts = base.retrieval_artifacts()
    timings = [a.get("timing_ms") for a in arts if a.get("timing_ms")]
    return round(sum(timings) / len(timings), 1) if timings else None