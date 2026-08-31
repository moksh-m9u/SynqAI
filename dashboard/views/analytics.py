"""Analytics — interactive Plotly charts over the real artifacts."""
from __future__ import annotations

from collections import Counter

import streamlit as st

from dashboard import base
from dashboard.widgets import page_header


def render() -> None:
    page_header(
        "Analytics Dashboard",
        "Live charts computed from the actual work orders, audit trail, quarantine file and "
        "retrieval artifacts — nothing synthetic.",
        "The operational picture: which clients consume capacity, where quarantine bites, "
        "which rules fire most, and how fast retrieval really is.",
    )
    import plotly.express as px

    wos = base.work_orders()
    audit_lines = base.audit()
    quar = base.quarantine()
    retr = base.retrieval_artifacts()
    summary = base.run_summary()

    r1, r2 = st.columns(2)

    with r1:
        st.subheader("Rule usage frequency")
        rule_counts = Counter(rid for a in audit_lines for rid in (a.get("rule_ids") or []))
        if rule_counts:
            df = _pd({"rule": list(rule_counts), "times": list(rule_counts.values())})
            st.plotly_chart(px.bar(df.sort_values("times", ascending=True), x="times", y="rule",
                                   orientation="h", color="times", color_continuous_scale="Blues",
                                   title="Times each rule fired in the processed queue"),
                            use_container_width=True)
        else:
            st.caption("No rule citations in audit yet.")

    with r2:
        st.subheader("Work orders by client")
        ctr = Counter()
        for thread in _thread_dirs():
            arts = base.node_artifacts(thread)
            enrich = arts.get("enrich") or {}
            ticket = (enrich.get("context") or {}).get("ticket") or {}
            if ticket.get("client"):
                ctr[ticket["client"]] += 1
        if ctr:
            df = _pd({"client": list(ctr), "work_orders": list(ctr.values())})
            st.plotly_chart(px.bar(df, x="client", y="work_orders", color="client",
                                   title="Work orders per client"), use_container_width=True)
        else:
            st.caption("No work orders with client context found.")

    r3, r4 = st.columns(2)
    with r3:
        st.subheader("Quarantine reasons")
        if quar:
            qc = Counter(q.get("reason", "")[:70] for q in quar)
            df = _pd({"reason_tail": list(qc), "count": list(qc.values())})
            st.plotly_chart(px.bar(df, y="reason_tail", x="count", orientation="h",
                                   title="Quarantine reasons"), use_container_width=True)
        else:
            st.caption("No quarantined records.")

    with r4:
        st.subheader("Retrieval latency")
        if retr:
            df = _pd({"query": [f"{a['query'][:28]}…"for a in retr],
                      "ms": [a.get("timing_ms") or 0 for a in retr]})
            st.plotly_chart(px.bar(df, x="query", y="ms", title="Retrieval latency per search (ms)"),
                            use_container_width=True)
        else:
            st.caption("No retrieval artifacts yet — run a search in the playground.")

    r5, r6 = st.columns(2)
    with r5:
        st.subheader("Processing time distribution")
        pts = _processing_times(audit_lines)
        if pts:
            df = _pd({"ticket": list(pts), "seconds": list(pts.values())})
            st.plotly_chart(px.box(df, y="seconds", title="Pipeline duration per ticket (s)"),
                            use_container_width=True)
        else:
            st.caption("No timing data yet.")

    with r6:
        st.subheader("Duplicate rate")
        dups = (summary or {}).get("duplicates", 0)
        recs = (summary or {}).get("records", 0)
        df = _pd({"": ["unique", "duplicates"], "count": [max(int(recs or 0) - int(dups), 0), int(dups)]})
        st.plotly_chart(px.bar(df, x="", y="count", color="",
                               color_discrete_map={"unique": "#2ea043", "duplicates": "#f85149"},
                               title=f"Unique vs duplicates in last baseline run (dups={dups})"),
                        use_container_width=True)

    st.subheader("Embedding collection growth")
    vdb = base.vector_db_status()
    counts = vdb.get("counts", {}) if vdb.get("ok") else {}
    if counts:
        df = _pd({"collection": list(counts), "chunks": [c or 0 for c in counts.values()]})
        st.plotly_chart(px.bar(df, x="collection", y="chunks", color="collection",
                               title="Qdrant collection sizes (chunks indexed)"),
                        use_container_width=True)


def _thread_dirs() -> list[str]:
    g = base.ARTIFACTS_DIR / "graph"
    return [d.name for d in g.glob("tkt-*")] if g.exists() else []


def _processing_times(audit_lines: list[dict]) -> dict[str, float]:
    import datetime as dt
    pts: dict[str, list] = {}
    for a in audit_lines:
        tid = a.get("ticket_id")
        if not tid:
            continue
        try:
            t = dt.datetime.fromisoformat(a.get("at"))
        except Exception:
            continue
        pts.setdefault(tid, []).append(t)
    out = {}
    for tid, times in pts.items():
        if len(times) >= 2:
            out[tid] = round((max(times) - min(times)).total_seconds(), 1)
    return out


def _pd(d: dict):
    import pandas as pd
    return pd.DataFrame(d)