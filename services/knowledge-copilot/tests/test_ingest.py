import pytest
from conftest import DOC

from chunking import Chunk, Document
from connectors.alertmanager import AlertmanagerError
from ingest import (
    content_hash,
    existing_hashes,
    get_collection,
    ingest,
    plan_reconcile,
    sync_alerts,
)


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


# --- Day 12: a second source ------------------------------------------------

ALERT = {
    "fingerprint": "aaa",
    "startsAt": "2026-08-06T14:22:11.000Z",
    # Far future, because these tests do not pin sync_alerts' `now`. An active alert's
    # endsAt is startsAt + resolve_timeout and therefore always ahead of the clock; a
    # fixed past value made live_status read "resolved" from the day after it was
    # written, and the "still firing" assertion below began failing on its own.
    # Resolution is tested by absence (fetch=lambda: []), never through endsAt.
    "endsAt": "2099-12-31T23:59:59.000Z",
    "status": {"state": "active"},
    "labels": {"alertname": "Probe", "severity": "warning"},
    "annotations": {"summary": "probe is unhappy"},
}
OTHER_ALERT = {**ALERT, "fingerprint": "bbb", "labels": {"alertname": "Other"}}


def alert_doc(fingerprint: str, status: str = "firing") -> Document:
    return Document(
        slug=f"alert-{fingerprint}",
        text=f"Alert: Probe\nStatus: {status}\nStarted: 2026-08-06T14:22:11.000Z",
        metadata={
            "doc_type": "alert",
            "source": "Probe",
            "fingerprint": fingerprint,
            "status": status,
            "started_at": "2026-08-06T14:22:11.000Z",
        },
    )


def test_an_alert_sync_does_not_delete_the_corpus(corpus, backend):
    """The bug this scoping exists for. Unscoped, plan_reconcile deletes every
    runbook chunk on the first alert poll, because no runbook is in the alert
    source's desired set."""
    provider, client = backend
    ingest(provider=provider, client=client, corpus_dir=corpus)
    collection = get_collection(client, provider)
    corpus_chunks = collection.count()
    assert corpus_chunks > 0

    plan = ingest(
        docs=[alert_doc("aaa")],
        where={"doc_type": "alert"},
        provider=provider,
        client=client,
    )

    assert plan.to_delete == []
    assert collection.count() == corpus_chunks + 1


def test_a_corpus_ingest_does_not_delete_alerts(corpus, backend):
    """The mirror case: re-ingesting the corpus must not evict live alerts."""
    provider, client = backend
    ingest(
        docs=[alert_doc("aaa")],
        where={"doc_type": "alert"},
        provider=provider,
        client=client,
    )
    collection = get_collection(client, provider)

    plan = ingest(
        provider=provider,
        client=client,
        corpus_dir=corpus,
        where={"doc_type": "runbook"},
    )

    assert plan.to_delete == []
    assert collection.get(where={"doc_type": "alert"})["ids"] == ["alert-aaa:0"]


def test_a_resolved_alert_leaving_the_desired_set_is_deleted(corpus, backend):
    provider, client = backend
    ingest(provider=provider, client=client, corpus_dir=corpus)
    collection = get_collection(client, provider)
    ingest(
        docs=[alert_doc("aaa"), alert_doc("bbb")],
        where={"doc_type": "alert"},
        provider=provider,
        client=client,
    )
    before = collection.count()

    plan = ingest(
        docs=[alert_doc("aaa")],
        where={"doc_type": "alert"},
        provider=provider,
        client=client,
    )

    assert plan.to_delete == ["alert-bbb:0"]
    assert collection.count() == before - 1


def test_existing_hashes_scopes_to_the_filter(corpus, backend):
    provider, client = backend
    ingest(provider=provider, client=client, corpus_dir=corpus)
    ingest(
        docs=[alert_doc("aaa")],
        where={"doc_type": "alert"},
        provider=provider,
        client=client,
    )
    collection = get_collection(client, provider)

    assert set(existing_hashes(collection, {"doc_type": "alert"})) == {"alert-aaa:0"}
    assert len(existing_hashes(collection)) > 1  # unfiltered still sees everything


def test_an_unchanged_alert_embeds_nothing(backend):
    provider, client = backend
    ingest(
        docs=[alert_doc("aaa")],
        where={"doc_type": "alert"},
        provider=provider,
        client=client,
    )
    after_first = provider.embed_calls

    plan = ingest(
        docs=[alert_doc("aaa")],
        where={"doc_type": "alert"},
        provider=provider,
        client=client,
    )

    assert plan.to_upsert == []
    assert provider.embed_calls == after_first  # not one vector recomputed


def test_sync_writes_live_alerts(corpus, backend):
    provider, client = backend
    ingest(provider=provider, client=client, corpus_dir=corpus)
    collection = get_collection(client, provider)
    before = collection.count()

    plan = sync_alerts(provider=provider, client=client, fetch=lambda: [ALERT])

    assert [c.id for c in plan.to_add] == ["alert-aaa:0"]
    assert collection.count() == before + 1


def test_sync_marks_a_vanished_alert_resolved_and_keeps_it(backend):
    provider, client = backend
    sync_alerts(provider=provider, client=client, fetch=lambda: [ALERT, OTHER_ALERT])
    collection = get_collection(client, provider)

    sync_alerts(provider=provider, client=client, fetch=lambda: [ALERT])

    stored = collection.get(ids=["alert-bbb:0"], include=["metadatas", "documents"])
    assert stored["metadatas"][0]["status"] == "resolved"
    assert "Status: resolved" in stored["documents"][0]


def test_a_failed_fetch_leaves_the_collection_untouched(corpus, backend):
    """The data-loss path. A raising fetch must not be reconciled against, or every
    indexed alert is marked resolved because the response 'contained' none of them."""
    provider, client = backend
    ingest(provider=provider, client=client, corpus_dir=corpus)
    sync_alerts(provider=provider, client=client, fetch=lambda: [ALERT])
    collection = get_collection(client, provider)
    before = collection.count()

    def boom():
        raise AlertmanagerError("appsrv is down")

    with pytest.raises(AlertmanagerError):
        sync_alerts(provider=provider, client=client, fetch=boom)

    assert collection.count() == before
    assert collection.get(ids=["alert-aaa:0"])["metadatas"][0]["status"] == "firing"


def test_a_quiet_cluster_is_not_a_failure(backend):
    """A successful [] is trusted: everything really did resolve."""
    provider, client = backend
    sync_alerts(provider=provider, client=client, fetch=lambda: [ALERT])
    collection = get_collection(client, provider)

    sync_alerts(provider=provider, client=client, fetch=lambda: [])

    assert collection.get(ids=["alert-aaa:0"])["metadatas"][0]["status"] == "resolved"


def test_a_resynced_resolved_alert_embeds_nothing(backend):
    """The churn guard, end to end through Chroma: a resolved alert re-rendered from
    stored metadata must hash identically or it re-embeds on every poll."""
    provider, client = backend
    sync_alerts(provider=provider, client=client, fetch=lambda: [ALERT])
    sync_alerts(provider=provider, client=client, fetch=lambda: [])
    after_resolution = provider.embed_calls

    plan = sync_alerts(provider=provider, client=client, fetch=lambda: [])

    assert plan.to_upsert == []
    assert provider.embed_calls == after_resolution
