import hashlib
import sys
from pathlib import Path

import chromadb
import pytest
from chromadb.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DOC = """---
title: Probe runbook
service: probe
doc_type: runbook
last_reviewed: 2026-08-03
---

Body text for the probe document.
"""


class FakeProvider:
    """Deterministic stand-in for an embedding backend. Counts what it embeds."""

    name = "fake"
    dimensions = 4

    def __init__(self) -> None:
        self.embed_calls = 0

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255 for b in digest[: self.dimensions]]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += len(texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def corpus(tmp_path):
    docs = tmp_path / "corpus"
    docs.mkdir()
    (docs / "probe.md").write_text(DOC, encoding="utf-8")
    return docs


@pytest.fixture
def backend(tmp_path, provider):
    # Not EphemeralClient: repeated calls share one in-process system, so the
    # collection would leak from test to test. A per-test path cannot.
    client = chromadb.PersistentClient(
        path=str(tmp_path / "chroma"),
        settings=Settings(anonymized_telemetry=False),
    )
    return provider, client
