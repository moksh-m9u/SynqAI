"""Grounded knowledge retrieval. Queries Qdrant with ticket context; every call
writes an artifact under artifacts/retrieval with query + chunks + similarity
scores + run/thread ids. Used for citations in enrichment and comms, never for
rule execution or eligibility (those are deterministic)."""
from __future__ import annotations

from typing import Any

from app.knowledge.vectorstore import KnowledgeStore


def retrieve_context(query: str, store: KnowledgeStore, run_id: str = "",
                     thread_id: str = "", top_k: int = 6) -> dict[str, Any]:
    hits = store.search(query, top_k=top_k, run_id=run_id, thread_id=thread_id)
    return {
        "query": query,
        "hits": hits,
        "top_refs": [h["source"] for h in hits[:5]],
        "grounded": bool(hits),
    }