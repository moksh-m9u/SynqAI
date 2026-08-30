"""Local embedding via sentence-transformers (granite multilingual),
cached so repeated pipeline runs never re-download the model."""
from __future__ import annotations

from app.config import EMBEDDING_MODEL_ID

_embedder = None
_dim = 384


def get_embedder():
    global _embedder, _dim
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMBEDDING_MODEL_ID)
        probe = _embedder.encode(["dim-probe"], normalize_embeddings=True,
                                 show_progress_bar=False)
        _dim = len(probe[0])
    return _embedder


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = get_embedder()
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vecs]


def embedding_dim() -> int:
    get_embedder()
    return _dim