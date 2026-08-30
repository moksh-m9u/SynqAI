"""Central configuration. Loads .env via python-dotenv; exposes paths and provider keys."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- paths ---
DATA_DIR = Path(os.getenv("SYNQ_DATA_DIR", ROOT / "data"))
ARTIFACTS_DIR = Path(os.getenv("SYNQ_ARTIFACTS_DIR", ROOT / "artifacts"))
OUTPUTS_DIR = Path(os.getenv("SYNQ_OUTPUTS_DIR", ROOT / "outputs"))
AUDIT_DIR = Path(os.getenv("SYNQ_AUDIT_DIR", ROOT / "audit"))

# --- locked stack ---
# LLM (Groq / Qwen)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Charter pins qwen/qwen3-27b; the Groq account exposes qwen/qwen3.8-27b.
# Override via GROQ_MODEL in .env if the pinned id becomes available.
LLM_MODEL_ID = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")

# Embeddings (local HF / sentence-transformers)
EMBEDDING_MODEL_ID = "ibm-granite/granite-embedding-97m-multilingual-r2"

# Vector store (Qdrant Cloud)
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
QDRANT_CLUSTER_ENDPOINT = os.getenv("QDRANT_CLUSTER_ENDPOINT", "")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "synqai_knowledge")

# LangSmith tracing
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY", "")
LANGCHAIN_PROJECT = os.getenv("LANGCHAIN_PROJECT", "SynqAI")
LANGCHAIN_TRACING_V2 = os.getenv("LANGCHAIN_TRACING_V2", "true").strip().lower() in {"1", "true", "yes"}

HUB_COORDS_PATH = ROOT / "resources" / "hub_coords.json"
RULES_PATH = ROOT / "app" / "rules" / "rules.yaml"

# SQLite state (graph checkpointer + exactly-once registry)
STATE_DB = ROOT / "artifacts" / "state.sqlite"

# Business-truth output files (never violate these schema)
WORK_ORDERS_OUT = OUTPUTS_DIR / "work_orders.jsonl"
COMMS_PENDING_OUT = OUTPUTS_DIR / "comms_pending.jsonl"
COMMS_SENT_OUT = OUTPUTS_DIR / "comms_sent.jsonl"
QUARANTINE_OUT = OUTPUTS_DIR / "quarantine.jsonl"
AUDIT_OUT = AUDIT_DIR / "audit.jsonl"


def ensure_dirs() -> None:
    for d in (DATA_DIR, ARTIFACTS_DIR, OUTPUTS_DIR, AUDIT_DIR,
              ARTIFACTS_DIR / "knowledge", ARTIFACTS_DIR / "rules",
              ARTIFACTS_DIR / "entities", ARTIFACTS_DIR / "schema",
              ARTIFACTS_DIR / "retrieval", ARTIFACTS_DIR / "graph",
              ARTIFACTS_DIR / "traces"):
        d.mkdir(parents=True, exist_ok=True)


def require_secrets(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n, "")]
    if missing:
        raise RuntimeError(f"Missing required secrets in .env: {missing}")