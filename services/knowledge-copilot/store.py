"""Opening a Chroma collection, in one place.

retrieval.py used to import CHROMA_PATH and get_collection from ingest.py: the query
path depending on the write path, purely because that is where the plumbing was written
first. Both modules also built their own PersistentClient with the same settings. This
module is the seam. It is a leaf -- it imports chunking and embeddings and nothing else
from this service -- so neither direction of that old dependency can grow back without
a circular import making it obvious.
"""

import os
from functools import lru_cache
from pathlib import Path

import chromadb
from chromadb.config import Settings
from dotenv import load_dotenv

from chunking import OVERLAP, SIZE
from embeddings import BaseEmbeddingProvider, get_embedding_provider

load_dotenv()

CHROMA_PATH = os.getenv("CHROMA_PATH", str(Path(__file__).parent / "chroma_data"))


def default_client() -> chromadb.ClientAPI:
    """The persistent client for CHROMA_PATH.

    Named `default_` because ingest() and sync_alerts() accept an injected client --
    that is how the tests point them at a tmp_path -- and a bare `client` here would
    shadow that parameter inside get_collection.
    """
    return chromadb.PersistentClient(
        path=CHROMA_PATH, settings=Settings(anonymized_telemetry=False)
    )


def collection_name(provider: BaseEmbeddingProvider) -> str:
    return f"knowledge_{provider.name}_{SIZE}_{OVERLAP}"


def get_collection(client: chromadb.ClientAPI, provider: BaseEmbeddingProvider):
    return client.get_or_create_collection(
        name=collection_name(provider),
        configuration={"hnsw": {"space": "cosine"}},
        embedding_function=None,
    )


@lru_cache(maxsize=1)
def open_collection():
    """The process-wide collection handle. Cached: opening it is not free, and every
    request would otherwise pay for a fresh client."""
    provider = get_embedding_provider()
    return provider, get_collection(default_client(), provider)
