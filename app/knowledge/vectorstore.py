"""Qdrant Cloud: single collection storing every knowledge chunk with rich
metadata. The store is for grounded retrieval & citations ONLY — rule execution
never depends on semantic search (rules are compiled YAML, run deterministically).

Every retrieval call writes an inspectable artifact under artifacts/retrieval/.
"""
from __future__ import annotations

import json
import uuid as uuidlib
from typing import Any

from qdrant_client import QdrantClient, models

from app.config import (ARTIFACTS_DIR, QDRANT_CLUSTER_ENDPOINT, QDRANT_API_KEY,
                        QDRANT_COLLECTION)
from app.knowledge.chunks import Chunk
from app.knowledge.embeddings import embed_texts
from app.utils import atomic_write_json

PAYLOAD_ALLOWED = {"source", "entity", "document_type", "rule_candidate",
                   "chunk_id", "text", "subject", "date", "mechanic"}


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

    def search(self, query: str, top_k: int = 6, filters: dict[str, Any] | None = None,
               run_id: str = "", thread_id: str = "") -> list[dict[str, Any]]:
        qvec = embed_texts([query])[0]
        qf = models.Filter(
            must=[models.FieldCondition(key=k, match=models.MatchValue(value=v))
                  for k, v in (filters or {}).items()]
        ) if filters else None
        resp = self.client.query_points(
            collection_name=self.collection,
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
            })

        # retrieval artifact: query, retrieved chunks, similarity scores, trace ids
        artifact = {
            "query": query,
            "filters": filters or {},
            "retrieved": results,
            "run_id": run_id,
            "thread_id": thread_id,
        }
        fname = f"retr_{uuidlib.uuid4().hex[:8]}_{run_id or 'x'}.json"
        atomic_write_json(ARTIFACTS_DIR / "retrieval" / fname, artifact)
        return results

    def count(self) -> int:
        return self.client.count(self.collection).count