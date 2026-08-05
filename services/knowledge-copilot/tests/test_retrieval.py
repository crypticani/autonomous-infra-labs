import pytest
import requests

from ingest import get_collection, ingest
from llm import UpstreamError
from retrieval import CANDIDATE_POOL, EmptyIndexError, retrieve


class StubCollection:
    """Stands in for a Chroma collection so distances are exact, not hash-derived."""

    name = "stub"

    def __init__(self, rows, count=None, embeddings=None):
        self.rows = rows
        self._count = len(rows) if count is None else count
        # Any non-dense mode asks for the whole collection. Default to vectors
        # that make every chunk equally similar, so a test that cares about exact
        # scores still gets them from query()'s distances rather than from here.
        self.embeddings = embeddings or [[1.0] * 4 for _ in rows]
        self.query_kwargs = {}

    def count(self) -> int:
        return self._count

    def get(self, include=None):
        # The whole collection, unlike query() -- this is what feeds BM25 and the
        # cosine of a chunk only BM25 found.
        return {
            "ids": [f"doc:{i}" for i in range(len(self.rows))],
            "documents": [text for text, _, _ in self.rows],
            "metadatas": [meta for _, meta, _ in self.rows],
            "embeddings": self.embeddings,
        }

    def query(self, **kwargs):
        self.query_kwargs = kwargs
        rows = self.rows[: kwargs.get("n_results", len(self.rows))]
        return {
            "ids": [[f"doc:{i}" for i in range(len(rows))]],
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


def test_k_trims_a_larger_candidate_pool(provider):
    rows = [(f"text {i}", meta(f"doc{i}.md"), 0.10) for i in range(5)]
    collection = StubCollection(rows)

    hits = retrieve(QUESTION, provider=provider, collection=collection, k=2)

    # Chroma is asked for the pool, not k: MMR needs candidates to choose among.
    assert collection.query_kwargs["n_results"] == CANDIDATE_POOL
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


def test_bm25_rescues_a_chunk_the_dense_pool_missed(provider):
    rows = [
        ("a pod restarted", meta("a.md"), 0.20),
        ("another pod restarted", meta("b.md"), 0.30),
        ("terminated with exit code 137", meta("c.md"), 0.35),
    ]
    # pool=1 means the dense side only ever sees a.md, so BM25 is the only route
    # by which c.md can surface -- the `137` case hybrid search exists for.
    collection = StubCollection(rows, embeddings=[[1.0] * 4] * 3)
    hits = retrieve(
        "exit code 137",
        provider=provider,
        collection=collection,
        mode="hybrid",
        pool=1,
        floor=0.0,
    )

    assert "c.md" in [hit.source for hit in hits]
    # And it carries a real cosine from its stored embedding, not a sentinel and
    # not a BM25 score, so the floor can still judge it on the same terms.
    rescued = next(hit for hit in hits if hit.source == "c.md")
    assert rescued.score == pytest.approx(sum(provider.embed_query("exit code 137")))


def test_a_rescued_chunk_still_has_to_clear_the_floor(provider):
    rows = [("terminated with exit code 137", meta("c.md"), 0.35)]
    collection = StubCollection(rows, embeddings=[[0.0] * 4])  # cosine 0.0

    hits = retrieve(
        "exit code 137", provider=provider, collection=collection, mode="lexical"
    )

    # A keyword match must not smuggle a chunk past Day 10's refusal guard.
    assert hits == []


def test_metadata_filter_excludes_by_doc_type(provider):
    rows = [
        (
            "runbook text",
            {"source": "r.md", "chunk_index": 0, "doc_type": "runbook"},
            0.2,
        ),
        (
            "postmortem text",
            {"source": "p.md", "chunk_index": 0, "doc_type": "postmortem"},
            0.25,
        ),
    ]
    collection = StubCollection(rows, embeddings=[[1.0] * 4] * 2)

    # lexical mode, so this exercises retrieval.matches() rather than Chroma's
    # own `where` -- the stub does not implement server-side filtering.
    hits = retrieve(
        "text",
        provider=provider,
        collection=collection,
        mode="lexical",
        where={"doc_type": "postmortem"},
        floor=0.0,
    )

    assert [hit.source for hit in hits] == ["p.md"]


def test_an_unknown_mode_is_rejected(provider):
    collection = StubCollection([("x", meta("x.md"), 0.1)])
    with pytest.raises(ValueError, match="unknown retrieval mode"):
        retrieve(QUESTION, provider=provider, collection=collection, mode="magic")


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
