import hashlib
import os
import sys
from pathlib import Path

import chromadb
import pytest
from chromadb.config import Settings

# Before app is imported: importing it would otherwise start a real poll loop against
# whatever ALERTMANAGER_URL happens to be set to, on every test run.
os.environ.setdefault("ALERT_SYNC_ENABLED", "false")

# Before app is imported, and for the same reason: once real SLACK_* keys land in .env,
# load_dotenv() would make the route genuinely active and the "not configured" test
# would get a 401 instead of a 404. setdefault is enough -- load_dotenv does not
# override an existing environment variable.
os.environ.setdefault("SLACK_ENABLED", "false")

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


@pytest.fixture(autouse=True)
def clear_index_cache():
    # retrieval's index cache is keyed on (collection name, row count), and every
    # StubCollection is named "stub" -- so two same-sized stubs collide and the
    # second test reads the first one's documents. Same leak the PersistentClient
    # comment below is about: in-process state has to be reset per test.
    from retrieval import _index_cache

    _index_cache.clear()
    yield
    _index_cache.clear()


@pytest.fixture(autouse=True)
def clear_slack_state():
    # Same leak as _index_cache above: sessions and the dedupe set are module-level
    # dicts, so one test's thread history or accepted event_id would otherwise be
    # visible to the next.
    import sessions
    from slack_events import _seen_events

    sessions._sessions.clear()
    _seen_events.clear()
    yield
    sessions._sessions.clear()
    _seen_events.clear()


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
