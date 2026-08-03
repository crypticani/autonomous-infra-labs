import hashlib
import sys
from pathlib import Path

import chromadb
import pytest
from chromadb.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chunking import Chunk, Document
from ingest import content_hash, get_collection, ingest, plan_reconcile


def make_chunks(slug: str, count: int, digest: str) -> list[Chunk]:
    return [
        Chunk(
            id=f"{slug}:{i}",
            text=f"chunk {i}",
            metadata={"content_hash": digest, "chunk_index": i},
        )
        for i in range(count)
    ]


def test_new_doc_is_all_adds():
    desired = make_chunks("oom", 3, "h1")
    plan = plan_reconcile(desired, existing={})
    assert [c.id for c in plan.to_add] == ["oom:0", "oom:1", "oom:2"]
    assert plan.to_update == []
    assert plan.to_delete == []
    assert plan.unchanged == []


def test_unchanged_corpus_is_a_noop():
    desired = make_chunks("oom", 3, "h1")
    existing = {"oom:0": "h1", "oom:1": "h1", "oom:2": "h1"}
    plan = plan_reconcile(desired, existing)
    assert plan.to_upsert == []  # nothing embedded
    assert plan.to_delete == []
    assert plan.unchanged == ["oom:0", "oom:1", "oom:2"]


def test_edited_doc_is_updated():
    desired = make_chunks("oom", 3, "h2")  # new hash
    existing = {"oom:0": "h1", "oom:1": "h1", "oom:2": "h1"}
    plan = plan_reconcile(desired, existing)
    assert [c.id for c in plan.to_update] == ["oom:0", "oom:1", "oom:2"]
    assert plan.to_add == []
    assert plan.to_delete == []


def test_shrunk_doc_deletes_orphans():
    desired = make_chunks("oom", 2, "h1")  # was 4 chunks, now 2
    existing = {"oom:0": "h1", "oom:1": "h1", "oom:2": "h1", "oom:3": "h1"}
    plan = plan_reconcile(desired, existing)
    assert plan.unchanged == ["oom:0", "oom:1"]
    assert sorted(plan.to_delete) == ["oom:2", "oom:3"]


def test_removed_doc_deletes_everything():
    desired = make_chunks("oom", 2, "h1")
    existing = {"oom:0": "h1", "oom:1": "h1", "tls:0": "hx", "tls:1": "hx"}
    plan = plan_reconcile(desired, existing)
    assert sorted(plan.to_delete) == ["tls:0", "tls:1"]
    assert plan.unchanged == ["oom:0", "oom:1"]


def test_content_hash_is_stable_and_sensitive():
    a = Document(slug="d", text="body", metadata={"service": "platform"})
    b = Document(slug="d", text="body", metadata={"service": "platform"})
    c = Document(slug="d", text="different body", metadata={"service": "platform"})
    d = Document(
        slug="d", text="body", metadata={"service": "edge"}
    )  # metadata changed
    assert content_hash(a) == content_hash(b)  # same input -> same hash
    assert content_hash(a) != content_hash(c)  # body change flips it
    assert content_hash(a) != content_hash(d)  # front-matter change flips it too


# --- pipeline tests: a fake provider and a throwaway corpus, still offline ---

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
def corpus(tmp_path):
    docs = tmp_path / "corpus"
    docs.mkdir()
    (docs / "probe.md").write_text(DOC, encoding="utf-8")
    return docs


@pytest.fixture
def backend(tmp_path):
    # Not EphemeralClient: repeated calls share one in-process system, so the
    # collection would leak from test to test. A per-test path cannot.
    client = chromadb.PersistentClient(
        path=str(tmp_path / "chroma"),
        settings=Settings(anonymized_telemetry=False),
    )
    return FakeProvider(), client


def test_dry_run_writes_nothing(corpus, backend):
    provider, client = backend
    plan = ingest(dry_run=True, provider=provider, client=client, corpus_dir=corpus)

    assert plan.to_add  # it found work to do...
    assert provider.embed_calls == 0  # ...and did none of it
    assert get_collection(client, provider).count() == 0


def test_reset_is_refused_during_a_dry_run(corpus, backend):
    """--reset --dry-run used to wipe the collection before returning the plan."""
    provider, client = backend
    ingest(provider=provider, client=client, corpus_dir=corpus)
    before = get_collection(client, provider).count()
    assert before > 0

    with pytest.raises(ValueError):
        ingest(
            reset=True,
            dry_run=True,
            provider=provider,
            client=client,
            corpus_dir=corpus,
        )

    assert get_collection(client, provider).count() == before


def test_unchanged_rerun_embeds_nothing(corpus, backend):
    provider, client = backend
    first = ingest(provider=provider, client=client, corpus_dir=corpus)
    after_first = provider.embed_calls
    second = ingest(provider=provider, client=client, corpus_dir=corpus)

    assert len(second.unchanged) == len(first.to_add) > 0
    assert second.to_upsert == []
    assert provider.embed_calls == after_first  # not one vector recomputed


def test_removed_doc_is_deleted_from_the_collection(corpus, backend):
    provider, client = backend
    (corpus / "second.md").write_text(DOC.replace("Probe", "Second"), encoding="utf-8")
    ingest(provider=provider, client=client, corpus_dir=corpus)
    collection = get_collection(client, provider)
    assert collection.count() == 2

    (corpus / "second.md").unlink()
    plan = ingest(provider=provider, client=client, corpus_dir=corpus)

    assert plan.to_delete == ["second:0"]
    assert collection.count() == 1
