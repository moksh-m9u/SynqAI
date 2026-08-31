"""Knowledge Upload — upload files (TXT/PDF/DOCX/XLSX/CSV/JSON), preview chunked
text, edit chunks, then index into the isolated synqai_userkb Qdrant collection.
The baseline knowledge collection is never modified.
"""
from __future__ import annotations

import streamlit as st

from app.knowledge.upload import (chunk_text, delete_chunk, estimate_cost, extract_text,
                                  index_upload, merge_chunks, reindex_chunks,
                                  split_chunk, userkb_count)
from dashboard import base
from dashboard.widgets import data_table, json_viewer, page_header, run_job

STEP = ["Upload & extract", "Preview chunks", "Index into Qdrant"]


def render() -> None:
    page_header(
        "Upload your knowledge base",
        "Make the pipeline genuinely reusable: drop in interview transcripts, email threads, "
        "maintenance logs or driver rosters. Inspect exactly what will be indexed, edit any "
        "chunk, then embed into the isolated `synqai_userkb` collection.",
        "Proves the system isn't hard-wired to Meridian Freight. A judge can add their own "
        "knowledge and immediately see it retrieved by the Retrieval Playground — no code, no "
        "rebuild. The production collection stays untouched.",
    )
    st.caption("Current user-KB size: "
               f"{_safe_count()} indexed chunks "
               "(collection `synqai_userkb`).")
    m1, m2, m3 = st.columns(3)
    with m1:
        f = st.file_uploader("Upload a document", type=["txt", "md", "log", "pdf", "docx", "xlsx", "csv", "json"])
    with m2:
        source = st.text_input("Source name (shown in citations + chunk IDs)", "",
                               help="Leave empty to use the filename")
    with m3:
        st.caption("Supported: TXT · PDF · DOCX · XLSX · CSV · JSON. Uploading does NOT touch "
                   "the baseline `synqai_knowledge` collection.")

    if f is not None and st.button("Extract & chunk", type="primary", key="upl_extract"):
        try:
            ext = extract_text(f.getvalue(), f.name)
            st.session_state["upl_extracted"] = {"file_name": ext.file_name,
                                                 "document_type": ext.document_type,
                                                 "language": ext.language,
                                                 "text": ext.text, "rows": ext.rows,
                                                 "note": ext.note}
            src = source.strip() or f.name
            chunks = chunk_text(ext.text, src, ext.document_type, ext.language)
            st.session_state["upl_chunks"] = chunks
            st.session_state["upl_src"] = src
            st.session_state["upl_step"] = "chunks"
            st.rerun()
        except Exception as exc:
            st.error(f"Extraction failed: {exc}")

    if "upl_extracted"in st.session_state:
        ext = st.session_state["upl_extracted"]
        st.subheader("Preview — extracted document")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Document type", ext["document_type"])
        c2.metric("Detected language", ext["language"])
        c3.metric("Notes", ext.get("note") or "—")
        c4.metric("Bytes", len(ext["text"]))
        with st.expander("Extracted text", expanded=False):
            st.write(ext["text"])

    if st.session_state.get("upl_step") == "chunks":
        _chunk_editor()
        _index_flow()


def _chunk_editor() -> None:
    chunks = st.session_state["upl_chunks"]
    st.subheader(f"Chunk preview — {len(chunks)} chunks before indexing")
    st.caption("Deterministic chunking (900 chars, 60 overlap). 'Rule candidate' is a keyword "
               "heuristic with confidence — the same signal the rule engine would surface.")
    st.markdown("**Cost estimate** "+ _cost_line(chunks))
    for i, ch in enumerate(chunks):
        with st.expander(
            f"{i:02d} · {ch.get('chunk_id')} · {ch.get('token_count')} tokens · "
            f"rule={'yes' if ch.get('rule_candidate') else 'no'} · conf={ch.get('confidence')}",
            expanded=i < 2):
            st.write(ch["text"])
            bcol = st.columns(4)
            if bcol[0].button("Edit", key=f"up_e_{i}"):
                st.session_state[f"up_edit_{i}"] = not st.session_state.get(f"up_edit_{i}", False)
            if bcol[1].button("Delete", key=f"up_d_{i}"):
                st.session_state["upl_chunks"] = delete_chunk(chunks, i)
                st.rerun()
            if bcol[2].button("Merge \u2193", key=f"up_m_{i}", disabled=i >= len(chunks) - 1):
                st.session_state["upl_chunks"] = merge_chunks(chunks, i)
                st.rerun()
            if bcol[3].button("Split", key=f"up_spb_{i}", disabled=i >= len(chunks) - 1):
                at = int(st.session_state.get(f"up_sp_{i}", len(ch["text"]) // 2))
                st.session_state["upl_chunks"] = split_chunk(chunks, i, at)
                st.rerun()
            if st.session_state.get(f"up_edit_{i}"):
                new_text = st.text_area("Chunk text", value=ch["text"], key=f"up_ta_{i}",
                                        height=120, label_visibility="collapsed")
                col = st.columns(3)
                if col[0].button("Save edit", key=f"up_save_{i}"):
                    st.session_state["upl_chunks"] = [dict(c, text=new_text) if j == i else c
                                                      for j, c in enumerate(st.session_state["upl_chunks"])]
                    _retag()
                    st.rerun()
                if i < len(chunks) - 1:
                    col[1].number_input(
                        "Split at character", 0, max(len(ch["text"]), 1), len(ch["text"]) // 2,
                        key=f"up_sp_{i}", label_visibility="visible")
                    col[2].caption(f"Text length: {len(ch['text'])} chars")


def _retag() -> None:
    from app.knowledge.upload import tag_chunk
    st.session_state["upl_chunks"] = [
        tag_chunk(c, i) for i, c in enumerate(st.session_state["upl_chunks"])]


def _cost_line(chunks) -> str:
    est = estimate_cost(chunks)
    return f"{est['chunks']} chunks · {est['tokens']} tokens · ~{est['vector_size_mb']} MB · "\
           f"est. embed cost ${est['estimated_embed_cost_usd']:.6f}"


def _index_flow() -> None:
    st.divider()
    st.subheader("Index into Qdrant")
    st.caption("Embeds with `granite-embedding-97m-multilingual-r2` (local, 384-dim); "
               "deterministic chunk_id → idempotent upsert into `synqai_userkb`.")
    if st.button(f"Index {len(st.session_state['upl_chunks'])} chunks — "
                 f"${estimate_cost(st.session_state['upl_chunks'])['estimated_embed_cost_usd']:.6f} estimated",
                 type="primary", key="upl_index"):
        holder = run_job("Indexing into Qdrant",
                         lambda log: _index_fn(log=log))
        if holder["ok"]:
            st.session_state.pop("upl_chunks", None)
            st.session_state.pop("upl_step", None)
            st.success(f"Indexed {holder['result']} chunks into `synqai_userkb`.")
            if st.button("Ask it in Retrieval Playground "):
                base.goto("retrieval")
                st.rerun()


def _index_fn(log=None) -> int:
    chunks = st.session_state.get("upl_chunks", [])
    if log:
        log(f"embedding {len(chunks)} chunks…")
    n = index_upload(chunks)
    if log:
        log(f"indexed {n} chunks into synqai_userkb")
    return n


def _safe_count() -> int:
    try:
        return userkb_count()
    except Exception:
        return 0