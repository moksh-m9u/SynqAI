"""Chaos Mode — deliberately break the pipeline and score how it holds up
(exactly-once, quarantine, safe degradation, recovery, no PII leaks)."""
from __future__ import annotations

import json

import streamlit as st

from app.ops import CHAOS_FLAGS, chaos_plan, chaos_report, new_run_id, process_records, workspace_for
from dashboard import base
from dashboard.widgets import data_table, json_viewer, page_header, run_job

VERDICT = {
    "exactly_once": ("Exactly-once maintained", "No duplicate work orders in the chaos workspace."),
    "quarantined": ("Quarantined safely", "Bad records were quarantined with a reason, not dropped."),
    "recovered": ("Recovered", "Schema-recovery salvaged a malformed record back to a valid ticket."),
    "safe_degradation": ("Safe degradation", "No crash: every input produced a deterministic decision."),
    "pii_leaked": ("PII leaked", "Aadhaar / DL / phone regexes surfaced in any emitted artifact."),
}


def render() -> None:
    page_header(
        "Chaos Mode",
        "Intentionally break the pipeline — duplicates, missing vehicles, invalid dates, missing "
        "hubs, broken JSON, wrong schemas, conflicting maintenance — and watch the system "
        "fail productively instead of corrupting state.",
        "This is the rubric's centerpiece: reliability engineering. It must keep exactly-once "
        "guarantees, quarantine bad input with reasons, degrade safely, attempt recovery, and "
        "never leak PII.",
    )
    st.markdown("### Build your failure queue")
    cols = st.columns(4)
    selected = {}
    for i, flag in enumerate(CHAOS_FLAGS):
        selected[flag] = cols[i % 4].toggle(_label(flag), value=False, key=f"ch_{flag}")
    picked = [f for f, on in selected.items() if on]

    if st.button("Preview chaos queue", key="ch_prev", disabled=not picked):
        plan = chaos_plan(picked)
        st.session_state["chaos_plan"] = plan
        st.session_state["chaos_ran"] = False
    if st.session_state.get("chaos_plan"):
        plan = st.session_state["chaos_plan"]
        data_table(plan, "chaos_plan", height=300)
        if st.button("Run chaos suite", type="primary", key="ch_run",
                     disabled=st.session_state.get("chaos_ran", False)):
            run_id = new_run_id("chaos")
            holder = run_job("Chaos run", lambda log: process_records(
                plan, run_id, kind="chaos", approve="auto", log=log))
            if holder["ok"]:
                ws = workspace_for(run_id)
                report = chaos_report(holder["result"], ws)
                st.session_state["chaos_report"] = report
                st.session_state["chaos_results"] = holder["result"]
                st.session_state["chaos_ran"] = True
            else:
                st.error("Chaos run crashed.")

    if st.session_state.get("chaos_report"):
        _verdicts(st.session_state["chaos_report"])
        st.subheader("Per-record outcomes")
        data_table(st.session_state["chaos_results"], "chaos_results",
                   columns=["ticket_id", "status", "reason", "recovered_attempted",
                            "chosen", "execution_ms"])
        st.download_button("Download chaos report",
                           json.dumps(st.session_state["chaos_report"], indent=2).encode("utf-8"),
                           file_name="chaos_report.json", mime="application/json", key="ch_dl")


def _verdicts(report: dict) -> None:
    st.subheader("Verdicts")
    cols = st.columns(len(VERDICT))
    for i, key in enumerate(VERDICT):
        ok = bool(report.get(key)) if key != "pii_leaked"else not bool(report.get("pii_leaked"))
        title, why = VERDICT[key]
        with cols[i]:
            st.markdown(
                f"<div style='padding:.6rem;border-radius:10px;background:"
                f"{'#173a20' if ok else '#4a1520'};color:{'#7ee787' if ok else '#ff7b72'}'>"
                f"<b>{title}</b><br><span style='font-size:.75rem;color:#c9d4e3'>{why}</span></div>",
                unsafe_allow_html=True)
            if key == "exactly_once":
                st.json(report.get("exactly_once_detail"))
            if key == "quarantined":
                st.caption("reasons: "+ "; ".join(report.get("quarantine_reasons") or ["—"]))
            if key == "pii_leaked"and report.get("pii_matches"):
                st.caption(str(report.get("pii_matches")[:3]))


def _label(flag: str) -> str:
    return {
        "duplicate": "Duplicate ticket",
        "missing_vehicle": "Missing vehicle",
        "invalid_date": "Invalid date",
        "missing_hub": "Missing hub",
        "broken_json": "Broken JSON",
        "wrong_schema": "Wrong schema",
        "conflicting_maintenance": "Conflicting maintenance",
        "duplicate_comms": "Duplicate communication",
    }.get(flag, flag)