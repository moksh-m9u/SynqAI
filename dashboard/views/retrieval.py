"""Retrieval Playground — full RAG debugger: query embedding, raw cosine scores,
optional lexical rerank, threshold, why-selected/why-rejected, neighbors, and
downloadable retrieved context."""
from __future__ import annotations

import json

import streamlit as st

from dashboard import base
from dashboard.widgets import data_table, json_viewer, page_header


def render() -> None:
    page_header(
        "Retrieval Playground",
        "Ask anything and inspect the exact evidence retrieval would hand the pipeline: "
        "query embedding, cosine scores, lexical overlap, rerank scores and neighbors.",
        "RAG quality is the load-bearing wall behind every grounded claim. A judge can drop a "
        "question, flip threshold/rerank, and see exactly which chunks make it and why — or "
        "retrieve freshly uploaded knowledge within seconds.",
    )
    c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
    query = c1.text_input("Question", "which vehicle can replace a broken truck for shakti cement "
                                     "near delhi tomorrow?", key="retr_q")
    collection = c2.selectbox("Collection", ["synqai_knowledge", "synqai_userkb", "merged"],
                              key="retr_coll",
                              help="synqai_knowledge = baseline KB; synqai_userkb = your uploads; "
                                   "merged = union of both")
    top_k = c3.number_input("Top K", 2, 20, 6, key="retr_k")
    threshold = c4.number_input("Similarity floor", 0.0, 1.0, 0.0, 0.05, key="retr_thr",
                                help="Reject chunks scoring below this cosine similarity")
    rerank = c5.toggle("Lexical rerank", value=False, key="retr_rr",
                       help="Deterministic fusion: 0.7·cosine + 0.3·lexical overlap (no neural reranker)")

    if st.button("Search", type="primary", key="retr_go"):
        try:
            store = base.store()
            res = store.search_rich(query, collection=collection, top_k=int(top_k),
                                    threshold=threshold or None, rerank=rerank,
                                    run_id="dashboard", thread_id="playground")
            st.success(f"{len(res['hits'])} hits · {res['timing_ms']} ms · "
                       f"{res['total_candidates']} candidates fetched")
            _show(res, query)
        except Exception as exc:
            st.error(f"Search failed: {exc}")


def _show(res: dict, query: str) -> None:
    st.subheader("Query embedding")
    st.caption(f"First 8 dims of a {res['dim']}-dim normalized vector — used to cosine-search the collection.")
    st.code(json.dumps(res.get("query_embedding"), indent=1))

    st.subheader("Retrieved chunks")
    rows = []
    for i, h in enumerate(res["hits"]):
        rows.append({
            "rank": i + 1, "chunk_id": h.get("chunk_id"), "source": h.get("source"),
            "cosine": h.get("score"),
            "lexical": h.get("lexical_overlap", "—"),
            "rerank": h.get("rerank_score", "—"),
            "rule_candidate": "yes"if h.get("rule_candidate") else "no",
            "lang": h.get("language") or "—",
            "tokens": h.get("token_count") or len(str(h.get("text", "")).split()),
        })
    data_table(rows, "retrieved", height=320)

    st.subheader("Why selected / why rejected")
    st.markdown(
        f"- **Selected:** top **{len(res['hits'])}** of **{res['total_candidates']}** candidates "
        f"by {'rerank score' if res.get('rerank') else 'cosine similarity'} "
        f"{'(floor ' + str(res.get('threshold')) + ')' if res.get('threshold') else ''}.")
    if res.get("total_candidates", 0) > len(res["hits"]):
        dropped = res["total_candidates"] - len(res["hits"])
        st.markdown(f"- **Dropped:** {dropped} fetched candidates fell outside Top-K"
                    f"{' or below the similarity floor' if res.get('threshold') else ''} "
                    f"— <b>top-K is a ranking cut, not a semantic veto</b>; raise K or lower the "
                    f"floor to see more, the floor is the only true quality gate.",
                    unsafe_allow_html=True)
    else:
        st.markdown("- No candidate was dropped — K or the floor capped the set first.")

    sel = st.selectbox("Inspect a hit", ["— pick a chunk —"] +
                       [f"{i + 1}. {h.get('chunk_id')} ({h.get('source')})"
                        for i, h in enumerate(res["hits"])], key="retr_pick")
    if sel != "— pick a chunk —":
        idx = int(sel.split(".")[0]) - 1
        h = res["hits"][idx]
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Scoring**")
            st.markdown(f"- cosine: `{h.get('score')}`\n- lexical overlap: "
                        f"`{h.get('lexical_overlap', '—')}`\n- rerank: `{h.get('rerank_score', '—')}`\n"
                        f"- rule candidate: {h.get('rule_candidate')}\n- language: "
                        f"{h.get('language') or '—'}\n- tokens: "
                        f"{h.get('token_count') or len(str(h.get('text', '')).split())}")
            st.markdown(f"**Source document:** `{h.get('source')}` ({h.get('document_type')})")
        with c2:
            st.markdown("**Text**")
            st.text(h.get("text") or "")
        st.markdown("**Neighboring chunks** (same source around this hit)")
        neigh = [x for x in base.chunks()
                 if x.get("source") == h.get("source")
                 and x.get("chunk_id") != h.get("chunk_id")][:3]
        if neigh:
            for x in neigh:
                st.markdown(f"- `{x.get('chunk_id')}` — {str(x.get('text'))[:140]}")
        else:
            st.caption("No ordinal neighbors found in the baseline chunk index for this source.")

    st.subheader("Download retrieved context")
    ctx = {
        "query": query, "collection": res.get("collection"),
        "threshold": res.get("threshold"), "rerank": res.get("rerank"),
        "timing_ms": res.get("timing_ms"),
        "hits": [{k: h.get(k) for k in ("chunk_id", "source", "document_type", "score",
                                        "lexical_overlap", "rerank_score", "rule_candidate", "text")}
                 for h in res["hits"]],
    }
    st.download_button("Download retrieved context (JSON)",
                       json.dumps(ctx, indent=2, ensure_ascii=False).encode("utf-8"),
                       file_name="retrieved_context.json", mime="application/json",
                       key="retr_dl")
    st.caption("Every search also writes an inspectable artifact under artifacts/retrieval/ — "
               "the graph nodes read the same retrieval engine you just probed.")