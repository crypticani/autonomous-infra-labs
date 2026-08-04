import logging
import os
from dataclasses import dataclass
from functools import lru_cache

import chromadb
import requests
from chromadb.config import Settings
from dotenv import load_dotenv

from embeddings import BaseEmbeddingProvider, get_embedding_provider
from ingest import CHROMA_PATH, get_collection
from llm import UpstreamError

load_dotenv()

logger = logging.getLogger(__name__)

SIMILARITY_FLOOR = float(os.getenv("SIMILARITY_FLOOR", "0.65"))
DEFAULT_K = 4


class EmptyIndexError(RuntimeError):
    """The collection exists but holds no rows: the index was never built"""


@dataclass(frozen=True)
class Hit:
    text: str
    source: str
    chunk_index: int
    doc_type: str
    score: float


@lru_cache(maxsize=1)
def open_collection():
    provider = get_embedding_provider()
    client = chromadb.PersistentClient(
        path=CHROMA_PATH, settings=Settings(anonymized_telemetry=False)
    )
    return provider, get_collection(client, provider)


def retrieve(
    question: str,
    provider: BaseEmbeddingProvider,
    collection,
    k: int = DEFAULT_K,
    floor: float = SIMILARITY_FLOOR,
) -> list[Hit]:
    if collection.count() == 0:
        raise EmptyIndexError(f"collection {collection.name!r} is empty: run ingest.py")

    try:
        query_vector = provider.embed_query(question)
    except requests.exceptions.Timeout as e:
        raise UpstreamError("embedding the question took too long", 504) from e
    except Exception as e:
        # Every provider raises its own SDK error here; the caller only needs to
        # know the embedding backend failed. `from e` keeps the real traceback.
        raise UpstreamError(f"the embedding backend failed: {e}", 503) from e

    result = collection.query(
        query_embeddings=[query_vector],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )

    hits = [
        Hit(
            text=text,
            source=meta["source"],
            chunk_index=meta["chunk_index"],
            doc_type=meta.get("doc_type", "unknown"),
            score=1 - distance,
        )
        for text, meta, distance in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0]
        )
    ]

    kept = [hit for hit in hits if hit.score >= floor]
    if not kept:
        best = max((hit.score for hit in hits), default=0.0)
        logger.warning(
            f"nothing cleared floor {floor} for {question!r}; best was {best:.3f}"
        )
    return kept
