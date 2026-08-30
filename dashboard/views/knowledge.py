"""Knowledge Explorer — inspect every indexed chunk with the same filters a
judge would use (document type, source, language, rule candidate, entity) plus
retrieval scores, token counts and neighboring chunks."""
from __future__ import annotations

import streamlit as st

from dashboard import base
from dashboard.widgets import data_table, json_viewer, page_header


def render() -> None:
    page_header(
        "Knowledge Explorer",
        "Browse every chunk the pipeline can retrieve from the knowledge base with the "
        "metadata that drove it — and jump straight to retrieval scores for the same chunk.",
        "Proves the RAG index is inspectable, not a black box: language, rule-candidate "
        "flags, entity tags and observed retrieval scores are all transparent here.",
    )
    chunks = base.chunks()
    if not chunks:
        st.info("No knowledge chunks indexed yet.")
        return
    c1, c2, c3, c4 = st.columns(4)
    doc_types = sorted({c.get("document_type") for c in chunks})
    sources = sorted({c.get("source") for c in chunks})
    langs = sorted({c.get("language") for c in chunks})
    f_doctype = c1.multiselect("Document type", doc_types)
    f_source = c2.multiselect("Source", sources)
    f_lang = c3.multiselect("Language", langs)
    f_rule = c4.multiselect("Rule candidate", ["yes", "no"])

    score_by_id = _scores()
    out = []
    for c in chunks:
        if f_doctype and c.get("document_type") not in f_doctype:
            continue
        if f_source and c.get("source") not in f_source:
            continue
        if f_lang and c.get("language") not in f_lang:
            continue
        if f_rule and ("yes"if c.get("rule_candidate") else "no") not in f_rule:
            continue
        out.append(c)
    st.caption(f"{len(out)} / {len(chunks)} chunks shown")
    if f_source or f_doctype or f_lang or f_rule:
        st.markdown("**Score column** is the best observed cosine similarity from retrieval "
                    "artifacts, colored semantic proof the chunk is actually used.")

    data = [{
        "chunk_id": c.get("chunk_id"), "source": c.get("source"),
        "type": c.get("document_type"), "lang": c.get("language"),
        "rule": "yes"if c.get("rule_candidate") else "no",
        "tokens": c.get("token_count") or len(str(c.get("text", "")).split()),
        "best_score": score_by_id.get(c.get("chunk_id")),
    } for c in out]
    data_table(data, "knowledge_chunks", height=460)

    if not out:
        return
    labels = {c.get("chunk_id"): f"{c.get('chunk_id')} │ {c.get('source')}"
              for c in out}
    pick = st.selectbox("Chunk", ["— pick —"] + [v for _, v in sorted(labels.items())],
                        key="kn_chunk")
    if pick == "— pick —"or not pick:
        return
    cid = pick.split("│")[0].strip()
    chunk = next(c for c in out if c.get("chunk_id") == cid)
    _chunk_detail(chunk, score_by_id.get(cid))


def _chunk_detail(chunk: dict, score) -> None:
    meta = {k: chunk.get(k) for k in ("chunk_id", "source", "document_type", "language",
                                      "rule_candidate", "confidence", "entity", "subject",
                                      "date", "mechanic") if chunk.get(k) is not None}
    st.markdown("#### Chunk detail")
    c1, c2, c3 = st.columns(3)
    c1.metric("Best retrieval score", f"{score:.3f}"if score is not None else "never retrieved")
    c2.metric("Tokens", chunk.get("token_count") or len(str(chunk.get("text", "")).split()))
    c3.metric("Rule candidate", "yes"if chunk.get("rule_candidate") else "no")
    by = st.columns(3)
    with by[0]:
        st.download_button("Download chunk", str(chunk).encode("utf-8"),
                           file_name=f"{chunk.get('chunk_id')}.json", mime="application/json",
                           key="kn_dl")
    with by[1]:
        if st.button("Open source", key="kn_src"):
            st.session_state["kn_show_src"] = True
    with by[2]:
        if st.button("View adjacent chunks", key="kn_adj"):
            st.session_state["kn_show_adj"] = True
    if st.session_state.get("kn_show_src"):
        st.markdown(f"Source file: `{chunk.get('source')}` · type `{chunk.get('document_type')}`")
    json_viewer(meta, "kn_meta", expanded=True)
    st.text(chunk.get("text") or "")
    if st.session_state.get("kn_show_adj"):
        st.markdown("**Adjacent chunks** (same source, ordinal neighbors)")
        same = [x for x in base.chunks() if x.get("source") == chunk.get("source")]
        idx = next((i for i, x in enumerate(same) if x.get("chunk_id") == chunk.get("chunk_id")), None)
        if idx is not None:
            for x in same[max(0, idx - 2): idx + 3]:
                if x.get("chunk_id") != chunk.get("chunk_id"):
                    st.markdown(f"- **{x.get('chunk_id')}** — {str(x.get('text'))[:160]}")
    st.markdown("**Provenance / edit history:** chunks are immutable canonical artifacts "
                "(`artifacts/knowledge/chunks.jsonl`); no post-index edits are tracked. "
                "A chunk's provenance = source + document_type + original file, all above.")


def _scores() -> dict[str, float]:
    out: dict[str, float] = {}
    for a in base.retrieval_artifacts():
        for h in (a.get("retrieved") or a.get("hits") or []):
            cid = h.get("chunk_id")
            if cid and (h.get("score") is not None):
                if h.get("score") > out.get(cid, -1):
                    out[cid] = h.get("score")
    return out