import pytest
import requests

from ingest import get_collection, ingest
from llm import UpstreamError
from retrieval import EmptyIndexError, retrieve


class StubCollection:
    """Stands in for a Chroma collection so distances are exact, not hash-derived."""

    name = "stub"

    def __init__(self, rows, count=None):
        self.rows = rows
        self._count = len(rows) if count is None else count
        self.query_kwargs = {}

    def count(self) -> int:
        return self._count

    def query(self, **kwargs):
        self.query_kwargs = kwargs
        rows = self.rows[: kwargs.get("n_results", len(self.rows))]
        return {
            "documents": [[text for text, _, _ in rows]],
            "metadatas": [[meta for _, meta, _ in rows]],
            "distances": [[distance for _, _, distance in rows]],
        }


def meta(source: str, index: int = 0) -> dict:
    return {"source": source, "chunk_index": index, "doc_type": "runbook"}


QUESTION = "why does the ingress return a certificate error"


def test_hits_below_the_floor_are_dropped(provider):
    collection = StubCollection(
        [
            ("tls text", meta("tls-cert-expiry.md"), 0.24),  # score 0.76, a bullseye
            ("pool text", meta("postgres-conn-pool-exhaustion.md"), 0.41),  # 0.59, miss
        ]
    )
    hits = retrieve(QUESTION, provider=provider, collection=collection)

    assert [hit.source for hit in hits] == ["tls-cert-expiry.md"]
    assert hits[0].score == pytest.approx(0.76)
    assert hits[0].text == "tls text"  # the chunk body, not just its metadata


def test_nothing_above_the_floor_is_an_empty_list_not_an_error(provider):
    collection = StubCollection([("x", meta("x.md"), 0.60)])  # score 0.40
    assert retrieve(QUESTION, provider=provider, collection=collection) == []


def test_an_empty_collection_raises(provider):
    with pytest.raises(EmptyIndexError):
        retrieve(QUESTION, provider=provider, collection=StubCollection([], count=0))


def test_k_reaches_the_collection(provider):
    rows = [(f"text {i}", meta(f"doc{i}.md"), 0.10) for i in range(5)]
    collection = StubCollection(rows)

    hits = retrieve(QUESTION, provider=provider, collection=collection, k=2)

    assert collection.query_kwargs["n_results"] == 2
    assert collection.query_kwargs["include"] == [
        "documents",
        "metadatas",
        "distances",
    ]
    assert len(hits) == 2


def test_floor_is_overridable(provider):
    collection = StubCollection([("x", meta("x.md"), 0.50)])  # score 0.50
    assert retrieve(QUESTION, provider=provider, collection=collection) == []
    lower = retrieve(QUESTION, provider=provider, collection=collection, floor=0.4)
    assert len(lower) == 1


@pytest.mark.parametrize(
    "error, status",
    [
        (requests.exceptions.Timeout("slow"), 504),
        (RuntimeError("the gemini sdk blew up"), 503),
    ],
)
def test_embedding_failures_become_upstream_errors(error, status):
    class Failing:
        name = "failing"

        def embed_query(self, text):
            raise error

    collection = StubCollection([("x", meta("x.md"), 0.1)])
    with pytest.raises(UpstreamError) as caught:
        retrieve(QUESTION, provider=Failing(), collection=collection)

    # A Gemini embedding error used to escape app.py entirely as a 500.
    assert caught.value.status == status


def test_retrieve_reads_documents_from_a_real_collection(corpus, backend):
    provider, client = backend
    ingest(provider=provider, client=client, corpus_dir=corpus)
    collection = get_collection(client, provider)

    hits = retrieve(
        "body text for the probe document",
        provider=provider,
        collection=collection,
        floor=0.0,  # FakeProvider scores are hash noise; the point is the payload
    )

    assert hits and hits[0].source == "probe.md"
    assert "probe document" in hits[0].text
