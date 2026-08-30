"""Live Pipeline — animated node strip + per-node inspector (input/output/prompt/
LLM output/timing) for any thread, all replayed from graph artifacts."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import base
from dashboard.widgets import json_viewer, page_header


def render() -> None:
    page_header(
        "Live Pipeline",
        "Watch the LangGraph execution for any ticket: every node, its timing, the exact "
        "prompt, raw LLM output and parsed result — the closest thing to watching LangSmith "
        "inside the dashboard.",
        "Replaces the old JSON dump with an inspectable execution trace. Click a node to see "
        "what entered it, what the model produced, what was validated, and how long it took.",
    )
    threads = sorted(_thread_ids(), key=str.lower)
    if not threads:
        st.info("No pipeline threads found yet — run something in the Ticket Sandbox or baseline.")
        return
    sel = st.selectbox("Thread / run", [t for t in threads], key="pipe_thread")
    if not sel:
        return
    st.caption(f"Artifacts: `artifacts/graph/{sel}/` — every node file is an immutable replay point.")

    arts = base.node_artifacts(sel)
    status = base.thread_node_status(sel)
    _render_strip(sel, status, arts)

    # populate the "next"state for inspector from node outputs, in execution order
    order = [n for n, _ in base.PIPELINE_NODES if n in arts]
    pick = st.selectbox("Inspect node", ["— pick a node —"] + order, key="pipe_node")
    if pick and pick != "— pick a node —":
        _inspector(pick, arts)

    st.subheader("Raw audit trail")
    rows = [a for a in base.audit() if (a.get("thread_id") or a.get("ticket_id")) == sel]
    if rows:
        _audit_table(rows)
    else:
        st.caption("No audit events for this thread (audit events are keyed by ticket_id).")


def _thread_ids() -> list[str]:
    g = base.ARTIFACTS_DIR / "graph"
    return [d.name for d in sorted(g.glob("*")) if d.is_dir()] if g.exists() else []


def _render_strip(thread: str, status: dict, arts: dict) -> None:
    st.markdown("#### Execution graph")
    pills = []
    for node, label in base.PIPELINE_NODES:
        cls = "node-done"if node in status else "node-wait"
        pills.append(f"<span class='node-pill {cls}'>{label}</span>")
    st.markdown("".join(pills), unsafe_allow_html=True)
    timed = "".join(
        f"<span class='node-pill node-done'>{label} · {(arts.get(node) or {}).get('timing_ms') or 0}ms</span>"
        for node, label in base.PIPELINE_NODES if node in arts)
    if timed:
        st.markdown(timed, unsafe_allow_html=True)


def _inspector(node: str, arts: dict) -> None:
    art = arts.get(node) or {}
    timing = art.get("timing_ms")
    st.markdown(f"#### {node} — {timing}ms"if timing else f"#### {node}")
    has_prompt = False
    payload = {k: v for k, v in art.items() if k not in ("node", "run_id", "thread_id", "at", "timing_ms")}

    tabs = st.tabs(["Output state", "Prompt & LLM", "Timing"])
    with tabs[0]:
        st.markdown("**Node output (parsed + validated)**")
        json_viewer(payload, f"out_{node}")
    with tabs[1]:
        llm_debug = art.get("llm_debug")
        if isinstance(llm_debug, dict):
            if llm_debug.get("prompt") or llm_debug.get("error"):
                c1, c2 = st.columns(2)
                c1.markdown("**Prompt sent to Qwen**")
                c1.write(llm_debug.get("prompt"))
                c2.markdown("**Raw LLM output**")
                c2.write(llm_debug.get("llm_output") or llm_debug.get("raw_text") or llm_debug.get("error"))
            else:
                st.write("No LLM call in this node — it is deterministic Python "
                         "(rules, timings, validation).")
        else:
            st.write("No LLM call in this node — it is deterministic Python "
                     "(rules, timings, validation). The retrieval node writes its own "
                     "query-embedding + score artifact.")
        if node == "retrieve":
            retr = payload.get("retrieval") or {}
            st.markdown("**Retrieved context**")
            data = retr.get("hits") or retr.get("retrieved") or []
            for h in data[:6]:
                st.markdown(f"- `{h.get('score')}` · **{h.get('source')}** — "
                            f"{str(h.get('text'))[:120]}")
    with tabs[2]:
        t1 = art.get("timing_ms")
        st.metric("Node latency", f"{t1} ms"if t1 else "—")
        if node == "retrieve":
            retr = payload.get("retrieval") or {}
            if retr.get("timing_ms"):
                st.metric("Retrieval latency", f"{retr.get('timing_ms')} ms")
                st.caption(f"{len(retr.get('hits') or [])} hits retrieved")
        if isinstance(llm_debug, dict) and llm_debug.get("timing_ms"):
            st.metric("LLM call", f"{llm_debug.get('timing_ms')} ms")
            st.metric("Tokens", llm_debug.get("tokens"))
            st.metric("Model", llm_debug.get("model"))
        st.json({k: v for k, v in art.items() if k in ("at", "timing_ms")})


def _audit_table(rows: list[dict]) -> None:
    df = pd.DataFrame([{k: r.get(k) for k in ("at", "node", "decision", "rule_ids", "ticket_id")} for r in rows])
    st.dataframe(df, use_container_width=True, height=320,
                 column_config={"rule_ids": st.column_config.ListColumn("rule_ids")})