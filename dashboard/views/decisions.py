"""Decision Assistant — a live agent you can ask for operational advice.

Two modes:
  · Ticket scenario — paste a breakdown record or pick a sample; the agent walks
    the exact production decision path (understand → resolve → retrieve →
    rules → select → decide) and returns steps, todos and grounded context.
  · Question — ask anything; the agent retrieves real context from the knowledge
    base, scans the dispatch rule catalogue and returns an action plan.

Every decision uses the same code the pipeline runs, so it is reproducible and
auditable. Nothing is written to outputs/ or artifacts/.
"""
from __future__ import annotations

import json

import streamlit as st

from dashboard import base
from dashboard.decisions import TraceLog, answer_question, scenarios, trace_ticket
from dashboard.widgets import data_table, json_viewer, page_header, run_job

COLLECTIONS = ["merged", "baseline", "user_kb"]


def render() -> None:
    page_header(
        "Decision Assistant",
        "Ask the agent for a dispatch decision — on a real ticket or any question — and it "
        "shows you the exact steps it took, the retrieved knowledge it used, and the todo "
        "list it recommends.",
        "This is the 'human-in-the-loop advisor'. A judge can paste a messy breakdown record "
        "and watch the agent reason through it step-by-step, or ask an open question and see "
        "the retrieved context and rule citations behind the answer. Because it reuses the "
        "production rule engine, retriever and entity resolvers, its advice is identical to "
        "what the pipeline would execute — nothing is fabricated.",
    )

    mode = st.radio("I want to…", ["Decide a ticket scenario", "Ask an operational question"],
                    horizontal=True, key="dce_mode")

    col_a, col_b, col_c = st.columns([3, 1, 1])
    with col_a:
        if mode.startswith("Decide"):
            scenario = st.selectbox(
                "Pick a scenario (or paste your own below)", [lbl for lbl, _ in scenarios()],
                key="dce_scenario")
            with st.expander("Paste a custom ticket JSON instead"):
                text = st.text_area("Ticket record (JSON)", height=200, key="dce_paste")
        else:
            question = st.text_area(
                "Your question", height=100, key="dce_question",
                placeholder="e.g. Which vehicle can take a Shakti Cement load out of Delhi this winter?")
    with col_b:
        collection = st.selectbox("Knowledge base", COLLECTIONS, key="dce_coll")
    with col_c:
        top_k = st.slider("Top-k chunks", 3, 12, 6, key="dce_topk")

    run = st.button("Run decision agent", type="primary", use_container_width=True, key="dce_run",
                    disabled=(mode.startswith("Ask") and not (st.session_state.get("dce_question") or "").strip()))

    if run:
        _run(mode, collection, top_k)

    if st.session_state.get("dce_result"):
        _render(st.session_state["dce_result"])


def _run(mode: str, collection: str, top_k: int) -> None:
    scene = st.session_state.get("dce_scenario")
    records = scenarios()
    if mode.startswith("Decide"):
        seed = dict(records).get(scene) if isinstance(dict(records).get(scene), dict) else None
        paste = (st.session_state.get("dce_paste") or "").strip()
        if paste:
            try:
                seed = json.loads(paste)
                if isinstance(seed, list):
                    seed = seed[0]
            except Exception as exc:
                st.error(f"Could not parse pasted JSON: {exc}")
                return
        if seed is None:
            st.error("Choose a scenario or paste ticket JSON.")
            return
        holder = run_job("Decision agent — ticket trace", lambda log: trace_ticket(
            seed, collection=collection, top_k=top_k, log=log))
    else:
        q = st.session_state.get("dce_question", "").strip()
        if not q:
            return
        holder = run_job("Decision agent — question", lambda log: answer_question(
            q, collection=collection, top_k=top_k, log=log))
    if holder["ok"]:
        st.session_state["dce_result"] = holder["result"]


def _render(res: dict) -> None:
    rec = res["recommendation"]
    outcome = rec.get("outcome", "ADVICE")
    _hero(outcome, rec.get("rationale") or rec.get("answer", ""), res.get("wall_ms", 0))

    tab_steps, tab_ctx, tab_tree, tab_todos, tab_json = st.tabs(
        ["Steps", "Retrieved context", "Decision tree", "Todo list", "Trace JSON"])

    with tab_steps:
        for s in res.get("steps", []):
            with st.expander(f"{s['title']} — {s['timing_ms']:.0f} ms · {s['detail']}"):
                c1, c2 = st.columns(2)
                with c1:
                    json_viewer(s.get("input"), f"{s['id']}_in", expanded=False)
                with c2:
                    json_viewer(s.get("output"), f"{s['id']}_out", expanded=False)

    with tab_ctx:
        _retrieved(res.get("retrieved", []))

    with tab_tree:
        _decision_tree(res)

    with tab_todos:
        for i, t in enumerate(res.get("todos", []), 1):
            st.markdown(f"**☐ {i}. {t['do']}**  \n_— {t['why']}_")

    with tab_json:
        json_viewer(res, "dce_trace", expanded=False)


def _hero(outcome: str, rationale: str, wall_ms: int) -> None:
    palette = {"REASSIGN": ("success", "Dispatch a replacement vehicle"),
               "ROADSIDE_ASSIST": ("warning", "Roadside assistance on the broken unit"),
               "QUARANTINE": ("error", "Record quarantined — not processed"),
               "ADVICE": ("info", "Grounded operational advice")}
    tone, label = palette.get(outcome, ("info", outcome))
    getattr(st, tone)(f"**{label}** — decided in {wall_ms} ms")
    st.markdown(rationale)
    st.divider()


def _retrieved(hits: list[dict]) -> None:
    if not hits:
        st.info("No context retrieved (knowledge base empty or unavailable).")
        return
    st.caption(f"{len(hits)} chunks retrieved — the context the decision is grounded in.")
    for h in hits:
        score = max(0.0, min(1.0, float(h.get("score") or 0.0)))
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.markdown(f"**{h.get('source', '?')}** · `{h.get('document_type', '')}` "
                            f"· {h.get('language', '')} · chunk `{h.get('chunk_id', '')[:18]}…`")
            with c2:
                st.download_button("Copy chunk", json.dumps(h, indent=2, default=str),
                                   file_name=f"{h.get('chunk_id', 'chunk')}.json",
                                   mime="application/json", key=f"dce_chunk_{h.get('chunk_id')}")
            st.progress(score, text=f"similarity {float(h.get('score') or 0):.3f}")
            st.markdown((h.get("text") or "")[:600])


def _decision_tree(res: dict) -> None:
    verdicts = res.get("verdicts") or []
    selection = res.get("selection") or {}
    if verdicts:
        st.subheader("Rule evaluation (deterministic engine)")
        rows = [{"rule_id": v["rule_id"], "severity": v.get("severity"),
                 "triggered": "YES" if v.get("triggered") else "no",
                 "detail": v.get("detail", ""), "citation": ", ".join(v.get("citation") or [])}
                for v in verdicts]
        data_table(rows, "dce_rules", columns=["rule_id", "severity", "triggered", "detail", "citation"])
    if selection.get("eliminations"):
        st.subheader("Candidate eliminations (why each vehicle lost)")
        data_table(selection["eliminations"], "dce_eliminations",
                   columns=["candidate", "hub", "rule_id", "reason"])
    if selection.get("notes"):
        st.subheader("Selection notes")
        for n in selection["notes"]:
            st.markdown(f"- {n}")