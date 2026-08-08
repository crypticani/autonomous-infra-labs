from pathlib import Path

import store


def test_retrieval_does_not_import_ingest():
    """The query path must not depend on the write path.

    retrieval.py imported CHROMA_PATH and get_collection from ingest.py only because
    that is where the plumbing was written first. This asserts the seam rather than the
    behaviour, because the behaviour was already correct -- the coupling was the defect,
    and only a source-level assertion can catch it coming back.
    """
    source = (Path(__file__).resolve().parents[1] / "retrieval.py").read_text(
        encoding="utf-8"
    )
    assert "from ingest import" not in source
    assert "import ingest" not in source


def test_collection_name_encodes_provider_and_chunking(provider):
    assert store.collection_name(provider) == "knowledge_fake_512_64"


def test_get_collection_is_idempotent(backend):
    provider, client = backend
    first = store.get_collection(client, provider)
    first.add(ids=["a"], documents=["hello"], embeddings=[[1.0, 0.0, 0.0, 0.0]])
    second = store.get_collection(client, provider)
    assert second.name == first.name
    assert second.count() == 1
