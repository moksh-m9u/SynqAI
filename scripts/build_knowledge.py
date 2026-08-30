"""Knowledge stack build: chunks -> local embeddings -> Qdrant Cloud upsert.
Idempotent: point ids are uuid5(chunk_id), re-running never duplicates vectors."""
from __future__ import annotations

from app.ingest.loaders import load_emails, load_interview, load_maintenance
from app.knowledge.chunks import build_knowledge_base
from app.knowledge.vectorstore import KnowledgeStore
from app.rules.engine import export_rule_specs


def build_knowledge_stack() -> tuple[list, KnowledgeStore]:
    interview = load_interview()
    emails = load_emails()
    maintenance = load_maintenance()
    chunks = build_knowledge_base(interview, emails, maintenance)
    store = KnowledgeStore()
    n = store.upsert_chunks(chunks)
    export_rule_specs()
    return chunks, store


if __name__ == "__main__":
    chunks, store = build_knowledge_stack()
    print(f"upserted chunks={len(chunks)} collection_count={store.count()}")