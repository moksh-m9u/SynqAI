"""Qdrant Cloud: single collection storing every knowledge chunk with rich
metadata. The store is for grounded retrieval & citations ONLY — rule execution
never depends on semantic search (rules are compiled YAML, run deterministically).

Every retrieval call writes an inspectable artifact under artifacts/retrieval/.
Interactive runs may search an additional user-upload collection (synqai_userkb)
and apply a deterministic lexical rerank — never a neural reranker.
"""
from __future__ import annotations

import json
import time
import uuid as uuidlib
from typing import Any

from qdrant_client import QdrantClient, models

from app.config import (ARTIFACTS_DIR, QDRANT_CLUSTER_ENDPOINT, QDRANT_API_KEY,
                        QDRANT_COLLECTION)
from app.knowledge.chunks import Chunk
from app.knowledge.embeddings import embed_texts
from app.utils import atomic_write_json

USER_KB_COLLECTION = "synqai_userkb"

PAYLOAD_ALLOWED = {"source", "entity", "document_type", "rule_candidate",
                   "chunk_id", "text", "subject", "date", "mechanic",
                   "language", "token_count"}


def lexical_overlap(query: str, text: str) -> float:
    """Deterministic lexical-fusion signal (query-token presence in chunk text).
    Used only for explainable re-ranking in the playground; rule execution and
    production retrieval never depend on it."""
    q = {t for t in query.lower().split() if len(t) > 1}
    if not q:
        return 0.0
    body = text.lower()
    hits = sum(1 for t in q if t in body)
    return round(hits / len(q), 4)


def fused_score(cosine: float, lex: float, alpha: float = 0.7) -> float:
    return round(alpha * cosine + (1 - alpha) * lex, 4)


class KnowledgeStore:
    def __init__(self, collection: str = QDRANT_COLLECTION):
        self.collection = collection
        self.client = QdrantClient(
            url=QDRANT_CLUSTER_ENDPOINT,
            api_key=QDRANT_API_KEY,
        )
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self.client.get_collections().collections]
        if self.collection not in existing:
            from app.knowledge.embeddings import embedding_dim
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=embedding_dim(), distance=models.Distance.COSINE),
            )

    def list_collections(self) -> list[str]:
        try:
            return [c.name for c in self.client.get_collections().collections]
        except Exception:
            return [self.collection]

    def upsert_chunks(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        vectors = embed_texts([c.text for c in chunks])
        points = [
            models.PointStruct(
                id=str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, c.chunk_id)),
                vector=vectors[i],
                payload={
                    "chunk_id": c.chunk_id,
                    "source": c.source,
                    "document_type": c.doc_type,
                    "rule_candidate": c.rule_candidate,
                    "text": c.text,
                    **{k: v for k, v in c.metadata.items() if k in PAYLOAD_ALLOWED},
                },
            )
            for i, c in enumerate(chunks)
        ]
        self.client.upsert(self.collection, points=points)
        return len(points)

    def upsert_chunk_dicts(self, chunks: list[dict[str, Any]], collection: str | None = None) -> int:
        """Upsert dict-shaped chunks (knowledge upload preview) into a collection."""
        if not chunks:
            return 0
        coll = collection or self.collection
        if coll != self.collection:
            self.collection = coll
            self._ensure_collection()
        vectors = embed_texts([c["text"] for c in chunks])
        points = [
            models.PointStruct(
                id=str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, c["chunk_id"])),
                vector=vectors[i],
                payload={
                    "chunk_id": c["chunk_id"],
                    "source": c.get("source", "user-upload"),
                    "document_type": c.get("document_type", "text"),
                    "rule_candidate": bool(c.get("rule_candidate")),
                    "text": c["text"],
                    "language": c.get("language", ""),
                    "token_count": int(c.get("token_count", 0)),
                },
            )
            for i, c in enumerate(chunks)
        ]
        store = QdrantClient(url=QDRANT_CLUSTER_ENDPOINT, api_key=QDRANT_API_KEY)
        store.upsert(coll, points=points)
        return len(points)

    def _query(self, collection: str, query: str, top_k: int = 6,
               filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        qvec = embed_texts([query])[0]
        qf = models.Filter(
            must=[models.FieldCondition(key=k, match=models.MatchValue(value=v))
                  for k, v in (filters or {}).items()]
        ) if filters else None
        resp = self.client.query_points(
            collection_name=collection,
            query=qvec,
            limit=top_k,
            query_filter=qf,
            with_payload=True,
        )
        results = []
        for h in resp.points:
            payload = h.payload or {}
            results.append({
                "chunk_id": payload.get("chunk_id"),
                "source": payload.get("source"),
                "document_type": payload.get("document_type"),
                "text": payload.get("text"),
                "score": round(h.score, 4),
                "rule_candidate": payload.get("rule_candidate", False),
                "language": payload.get("language", ""),
                "token_count": payload.get("token_count"),
            })
        return results

    def search(self, query: str, top_k: int = 6, filters: dict[str, Any] | None = None,
               run_id: str = "", thread_id: str = "") -> list[dict[str, Any]]:
        return self.search_rich(query, top_k=top_k, filters=filters,
                                run_id=run_id, thread_id=thread_id)["hits"]

    def search_rich(self, query: str, collection: str | None = None, top_k: int = 6,
                    filters: dict[str, Any] | None = None, threshold: float | None = None,
                    rerank: bool = False, alpha: float = 0.7,
                    run_id: str = "", thread_id: str = "") -> dict[str, Any]:
        """Full RAG-debug search: chosen collection (or merged default+user KB),
        optional similarity floor, optional lexical rerank, latency + query
        embedding sample for the playground."""
        coll = collection or QDRANT_COLLECTION
        t0 = time.perf_counter()
        candidate_k = top_k * 3 if rerank else top_k
        if coll == "merged":
            hits = (self._query(QDRANT_COLLECTION, query, candidate_k, filters) +
                    self._query(USER_KB_COLLECTION, query, candidate_k, filters))
            seen: dict[str, dict[str, Any]] = {}
            for h in sorted(hits, key=lambda h: h["score"], reverse=True):
                seen.setdefault(h["chunk_id"], h)
            hits = sorted(seen.values(), key=lambda h: h["score"], reverse=True)
        else:
            if coll != self.collection:
                self.collection = coll
                self._ensure_collection()
            hits = self._query(coll, query, candidate_k, filters)
        hits = [h for h in hits if threshold is None or h["score"] >= threshold]
        if rerank:
            for h in hits:
                h["lexical_overlap"] = lexical_overlap(query, h.get("text", ""))
                h["rerank_score"] = fused_score(h["score"], h["lexical_overlap"], alpha)
            hits.sort(key=lambda h: h["rerank_score"], reverse=True)
        timing_ms = round((time.perf_counter() - t0) * 1000, 1)

        qvec = embed_texts([query])[0]
        artifact = {
            "query": query,
            "collection": coll,
            "filters": filters or {},
            "threshold": threshold,
            "rerank": rerank,
            "retrieved": hits[:top_k],
            "timing_ms": timing_ms,
            "run_id": run_id,
            "thread_id": thread_id,
        }
        fname = f"retr_{uuidlib.uuid4().hex[:8]}_{run_id or 'x'}.json"
        atomic_write_json(ARTIFACTS_DIR / "retrieval" / fname, artifact)
        return {
            "query": query,
            "collection": coll,
            "hits": hits[:top_k],
            "timing_ms": timing_ms,
            "query_embedding": [round(float(x), 4) for x in qvec[:8]],
            "dim": len(qvec),
            "threshold": threshold,
            "rerank": rerank,
            "total_candidates": len(hits),
        }

    def count(self, collection: str | None = None) -> int:
        return self.client.count(collection or self.collection).count