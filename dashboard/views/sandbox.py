"""Ticket Sandbox — upload / edit / run any ticket through the real LangGraph
pipeline, live, into an isolated workspace (never touches the baseline truth)."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from app.ops import new_run_id, process_records
from dashboard import base
from dashboard.widgets import data_table, json_viewer, page_header, run_job

TICKET_FIELDS = ["ticket_id", "created_at", "vehicle", "driver_id", "origin_hub",
                 "km_from_origin_hub", "destination", "issue", "severity",
                 "client", "status", "resolution_note"]


def render() -> None:
    page_header(
        "Ticket Sandbox",
        "Feed any ticket — raw JSON, CSV batch, or the form below — through the exact "
        "production pipeline and watch it execute live into an isolated workspace.",
        "This is the primary 'does it actually work' demo. A judge uploads their own ticket, "
        "sees the graph nodes run, then inspects the selected vehicle, applied rules, work "
        "order and drafted client email. Baseline outputs/ and artifacts/ are never touched.",
    )

    _input_tabs()
    records = st.session_state.get("sandbox_records", [])
    st.divider()

    if records:
        st.subheader(f"Queue — {len(records)} records")
        data_table(records, "sandbox_queue",
                   columns=["ticket_id", "client", "vehicle", "issue", "origin_hub",
                            "destination", "created_at", "severity"])
        if st.button("Clear queue", key="sbx_clear"):
            st.session_state["sandbox_records"] = []
            st.rerun()
    else:
        st.info("No queue yet — load a ticket above, or hit Run sample ticket.")

    c1, c2, c3 = st.columns(3)
    with c2:
        approve_stop = st.toggle("Pause at human approval (inspect, don't auto-approve)",
                                 value=False, key="sbx_pause")
    with c3:
        confirm = st.checkbox("Confirm run", key="sbx_confirm", help="Explicit guard: live runs reach Groq + Qdrant.")

    if st.button(
        f"Run pipeline on {len(records) or 0} records",
        type="primary", use_container_width=True, key="sbx_run", disabled=not (records and confirm),
    ):
        run_id = new_run_id("sbx")
        approve = "ask"if approve_stop else "auto"
        snapshot = [dict(r) for r in records]  # read outside the worker thread (session_state)
        holder = run_job("Ticket sandbox run", lambda log: process_records(
            snapshot, run_id, kind="sandbox", approve=approve, log=log))
        if holder["ok"]:
            st.session_state["last_sandbox"] = {"run_id": run_id, "results": holder["result"]}

    if "last_sandbox"in st.session_state and st.button("Open latest results"):
        _show_run(st.session_state["last_sandbox"]["run_id"])

    st.subheader("Previous sandbox runs")
    runs = base.sandbox_list()
    if runs:
        by_id = {r["run_id"]: r for r in runs}
        sel = st.selectbox("Workspace run", [f"{r['started'][:19]} — {r['run_id']} "
                                             f"({r['records']} rec, {r['work_orders']} wo, {r['quarantined']} quar)"
                                             for r in runs], key="sbx_prev")
        run_id = sel.split("— ")[1].split("(")[0] if sel else None
        if run_id and st.button(f"Open workspace {run_id}"):
            _show_run(run_id)

    if st.session_state.get("sandbox_action") == "sample":
        st.session_state["sandbox_action"] = None
        st.session_state["sandbox_records"] = base.sample_tickets(1)
        st.rerun()


def _input_tabs() -> None:
    tab_upload, tab_csv, tab_form, tab_raw = st.tabs(
        ["Upload JSON", "Upload CSV (batch)", "Manual form", "Paste raw JSON"])
    with tab_upload:
        f = st.file_uploader("Ticket JSON (single object or array)", type=["json"], key="sbx_fj")
        if f is not None:
            try:
                recs = json.loads(f.getvalue().decode("utf-8"))
                recs = recs if isinstance(recs, list) else [recs]
                if st.button(f"Load {len(recs)} records from {f.name}", key="sbx_loadj"):
                    _set_queue(recs)
            except Exception as exc:
                st.error(f"Invalid JSON: {exc}")
    with tab_csv:
        f = st.file_uploader("CSV with ticket columns", type=["csv"], key="sbx_fc")
        if f is not None:
            try:
                df = pd.read_csv(f)
                if st.button(f"Load {len(df)} rows from {f.name}", key="sbx_loadc"):
                    _set_queue([{k: (None if pd.isna(v) else v) for k, v in r.items()}
                                for r in df.to_dict(orient="records")])
            except Exception as exc:
                st.error(f"Invalid CSV: {exc}")
    with tab_form:
        with st.form("sbx_form"):
            c1, c2 = st.columns(2)
            fid = c1.text_input("ticket_id", "TKT-DEMO-001",
                                help="Unique identifier; existing id + --queue hash guards exactly-once.")
            fdate = c1.text_input("created_at", "2025-03-14T09:30:00Z", help="ISO-8601 timestamp")
            fveh = c2.text_input("vehicle", "CH81AQ4130", help="Registration (aliases are resolved)")
            fdrv = c2.text_input("driver_id", "")
            fcli = st.text_input("client", "ambuja dhaulpur", help="Messy client names resolve against aliases")
            fissue = st.text_area("issue", "Truck breakdown near Gurgaon at 11pm, need replacement before 6pm.",
                                  help="Free-text breakdown report — this drives retrieval + rules")
            c3, c4 = st.columns(2)
            fhub = c3.text_input("origin_hub", "Gurgaon")
            fdest = c4.text_input("destination", "Delhi")
            fkm = st.number_input("km_from_origin_hub", 0.0, 2000.0, 42.0, 1.0)
            fsev = st.selectbox("severity", ["HIGH", "MEDIUM", "LOW", ""])
            if st.form_submit_button("Add to queue"):
                _set_queue([{
                    "ticket_id": fid, "created_at": fdate, "vehicle": fveh, "driver_id": fdrv or None,
                    "client": fcli, "issue": fissue, "origin_hub": fhub, "destination": fdest,
                    "km_from_origin_hub": fkm, "severity": fsev or None, "status": "OPEN",
                }], append=True)
                st.success("Added to queue.")
                st.rerun()
    with tab_raw:
        raw = st.text_area("Editable JSON (single object or array)", height=220,
                           key="sbx_raw",
                           value='{\n  "ticket_id": "TKT-DEMO-002",\n  "created_at": "2025-03-01T05:00:00Z",\n  "vehicle": "mp04hk3427",\n  "issue": "ac failure, coolant leak, replacement needed"\n}')
        if st.button("Load raw JSON", key="sbx_loadraw"):
            try:
                recs = json.loads(raw)
                recs = recs if isinstance(recs, list) else [recs]
                _set_queue(recs)
            except Exception as exc:
                st.error(f"Invalid JSON: {exc}")


def _set_queue(recs: list[dict], append: bool = False) -> None:
    if not recs:
        st.warning("No records to add to the queue.")
        return
    if not append:
        st.session_state["sandbox_records"] = recs
        return
    cur = list(st.session_state.get("sandbox_records") or [])
    existing = [str(r.get("ticket_id") or "") for r in cur]
    for r in recs:
        tid = str(r.get("ticket_id") or "")
        if tid and tid in existing:
            n = 2
            while f"{tid}-{n}" in existing:
                n += 1
            r = dict(r, ticket_id=f"{tid}-{n}")
            existing.append(r["ticket_id"])
        cur.append(r)
    st.session_state["sandbox_records"] = cur


def _show_run(run_id: str) -> None:
    st.subheader(f"Run results — {run_id}")
    ws = __import__("app.ops", fromlist=["workspace_for"]).workspace_for(run_id)
    from app.utils import read_jsonl
    results = read_jsonl(ws["outputs"] / "results.jsonl")
    summary = base.jload(ws["outputs"] / "run_summary.json") or {}
    if not results:
        st.warning("No results in this workspace yet.")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Records", summary.get("records"))
    c2.metric("Work orders", summary.get("work_orders"))
    c3.metric("Quarantined", summary.get("quarantined"))
    c4.metric("Total time", f"{summary.get('total_execution_ms', 0) / 1000:.1f}s")
    st.markdown(f"Workspace: `sandbox/{run_id}/`  —  outputs/, audit/, artifacts/graph all isolated here.")
    for r in results:
        with st.expander(
            f"{r['ticket_id']} — {r['status']}{' · recovered' if r.get('recovered_attempted') else ''} "
            f"· {r.get('execution_ms', 0)}ms",
            expanded=True):
            m = st.columns(3)
            m[0].metric("Execution", f"{r.get('execution_ms', 0)} ms")
            m[1].metric("Selected vehicle", r.get("chosen") or "—")
            m[2].metric("Retrieval", _retrieval_ms(r, ws))
            st.markdown("**Applied rules:** "+ (", ".join(r.get("applied_rules") or []) or "none"))
            st.markdown("**Context citations:** "+ ", ".join(r.get("context_citations") or []))
            if r["status"] == "QUARANTINED":
                json_viewer({"reason": r.get("reason"), "raw": r.get("ticket_id")}, f"q_{r['ticket_id']}")
            wo = r.get("work_order") or {}
            if wo:
                st.markdown("**Work order**")
                json_viewer(wo, f"wo_{r['ticket_id']}")
            draft = r.get("draft")
            if draft:
                st.markdown("**Drafted communication**")
                json_viewer({k: draft.get(k) for k in ("message_id", "recipient", "subject", "body", "status", "citations")},
                            f"draft_{r['ticket_id']}")
            if r.get("comms_sent"):
                st.markdown("**Final communication**")
                json_viewer(r["comms_sent"], f"sent_{r['ticket_id']}")


def _retrieval_ms(r: dict, ws) -> str:
    thread = f"sbx-{ws['root'].name}-{r['ticket_id'].lower()}"
    from dashboard.base import node_artifacts
    arts = node_artifacts(thread)
    retr = arts.get("retrieve") or {}
    ms = (retr.get("retrieval") or {}).get("timing_ms")
    return f"{ms} ms"if ms else "—"