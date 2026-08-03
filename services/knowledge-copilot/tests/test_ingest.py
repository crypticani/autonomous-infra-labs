import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chunking import Chunk, Document
from ingest import content_hash, plan_reconcile


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
    assert plan.to_upsert == []          # nothing embedded
    assert plan.to_delete == []
    assert plan.unchanged == ["oom:0", "oom:1", "oom:2"]


def test_edited_doc_is_updated():
    desired = make_chunks("oom", 3, "h2")           # new hash
    existing = {"oom:0": "h1", "oom:1": "h1", "oom:2": "h1"}
    plan = plan_reconcile(desired, existing)
    assert [c.id for c in plan.to_update] == ["oom:0", "oom:1", "oom:2"]
    assert plan.to_add == []
    assert plan.to_delete == []


def test_shrunk_doc_deletes_orphans():
    desired = make_chunks("oom", 2, "h1")           # was 4 chunks, now 2
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
    d = Document(slug="d", text="body", metadata={"service": "edge"})  # metadata changed
    assert content_hash(a) == content_hash(b)     # same input -> same hash
    assert content_hash(a) != content_hash(c)     # body change flips it
    assert content_hash(a) != content_hash(d)     # front-matter change flips it too