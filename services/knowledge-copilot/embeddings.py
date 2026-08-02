import logging
import math
import os
from abc import ABC, abstractmethod

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

logger = logging.getLogger(__name__)

EMBED_DIMENSIONS = 768


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


class BaseEmbeddingProvider(ABC):
    name: str
    model_name: str
    dimensions: int = EMBED_DIMENSIONS

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...


class OllamaEmbeddingProvider(BaseEmbeddingProvider):
    name = "ollama"

    def __init__(self) -> None:
        self.model_name = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        logger.info(
            f"OllamaEmbeddingProvider initialized with model '{self.model_name}' and base URL '{self.base_url}' "
        )

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        response = requests.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model_name, "input": inputs},
            timeout=120,
        )
        response.raise_for_status()
        embeddings = response.json().get("embeddings")
        if not embeddings:
            raise ValueError(f"Ollama returned no embed for {len(inputs)} inputs")
        return [l2_normalize(e) for e in embeddings]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([f"search_document: {t}" for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"search_query: {text}"])[0]


class GeminiEmbeddingProvider(BaseEmbeddingProvider):
    name = "gemini"

    BATCH_SIZE = 100

    def __init__(self) -> None:
        if not os.getenv("GEMINI_API_KEY"):
            logger.error("GEMINI_API_KEY is not set in the environment")
        self.model_name = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
        self.client = genai.Client()
        logger.info(f"GeminiEmbeddingProvider using {self.model_name}")

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH_SIZE):
            response = self.client.models.embed_content(
                model=self.model_name,
                contents=texts[start : start + self.BATCH_SIZE],
                config=types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=self.dimensions,
                ),
            )
            vectors.extend(l2_normalize(e.values) for e in response.embeddings)
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]


def get_embedding_provider() -> BaseEmbeddingProvider:
    provider_type = os.getenv("EMBEDDING_PROVIDER", "ollama").lower()
    if provider_type == "ollama":
        return OllamaEmbeddingProvider()
    if provider_type == "gemini":
        return GeminiEmbeddingProvider()
    raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {provider_type!r}")
