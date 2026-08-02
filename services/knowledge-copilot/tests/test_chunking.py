import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chunking import Document, chunk_document, chunk_text, parse_front_matter

SAMPLE = " ".join(f"word{i}" for i in range(400))


def test_no_chunk_exceeds_requested_size():
    for chunk in chunk_text(SAMPLE, size=256, overlap=32):
        assert len(chunk) <= 256


def test_chunks_actually_overlap():
    chunks = chunk_text(SAMPLE, size=256, overlap=64)
    assert len(chunks) > 1
    for previous, following in zip(chunks, chunks[1:]):
        tail = previous[-40:]
        assert tail in following, "overlap window did not carry text forward"


def test_every_word_survives_chunking():
    chunks = chunk_text(SAMPLE, size=256, overlap=32)
    seen = {word for chunk in chunks for word in chunk.split()}
    assert seen == set(SAMPLE.split())


def test_chunk_ids_are_stable_across_runs():
    doc = Document(slug="oomkilled-pod", text=SAMPLE, metadata={"source": "x.md"})
    first = [c.id for c in chunk_document(doc, size=256, overlap=32)]
    second = [c.id for c in chunk_document(doc, size=256, overlap=32)]
    assert first == second
    assert first[0] == "oomkilled-pod:0"


def test_overlap_must_be_smaller_than_size():
    with pytest.raises(ValueError):
        chunk_text(SAMPLE, size=128, overlap=128)


def test_short_text_yields_one_chunk():
    assert chunk_text("pod restarted", size=256, overlap=32) == ["pod restarted"]


def test_fron_matter_is_parsed_and_stripped():
    raw = "---\ntitle: Pod OOMKilled\nservice: platform\n---\n\n## Symptom\n\nBody."
    metadata, body = parse_front_matter(raw)
    assert metadata == {"title": "Pod OOMKilled", "service": "platform"}
    assert body.strip().startswith("## Symptom")


def test_missing_front_matter_is_not_an_error():
    metadata, body = parse_front_matter("## Symptom\n\nBody.")
    assert metadata == {}
    assert body.startswith("## Symptom")
