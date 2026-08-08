import re
from dataclasses import dataclass
from pathlib import Path

CORPUS_DIR = Path(__file__).parent / "corpus"

# Chunking parameters, and therefore part of a collection's identity: store.py builds
# the collection name from them, so a re-chunk at different values lands in a different
# collection rather than silently mixing two geometries.
SIZE, OVERLAP = 512, 64

FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


@dataclass(frozen=True)
class Document:
    slug: str
    text: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    metadata: dict[str, str | int]


def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    match = FRONT_MATTER_RE.match(raw)
    if not match:
        return {}, raw

    metadata = {}
    for line in match.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        metadata[key.strip()] = value.strip()

    return metadata, raw[match.end() :]


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> list[Document]:
    docs = []
    for path in sorted(corpus_dir.glob("*.md")):
        metadata, body = parse_front_matter(path.read_text(encoding="utf-8"))
        metadata["source"] = path.name
        docs.append(Document(slug=path.stem, text=body.strip(), metadata=metadata))

    if not docs:
        raise FileNotFoundError(f"No markdown files found in {corpus_dir}")
    return docs


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    if overlap >= size:
        raise ValueError(
            f"Overlap ({overlap}) must be smaller than chunk size ({size})"
        )

    text = text.strip()
    if not text:
        return []

    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        if end < len(text):
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        next_start = end - overlap
        boundary = text.rfind(" ", start, next_start)
        if boundary != -1:
            next_start = boundary + 1
        start = max(next_start, start + 1)

    return chunks


def chunk_document(doc: Document, size: int, overlap: int) -> list[Chunk]:
    return [
        Chunk(
            id=f"{doc.slug}:{index}",
            text=text,
            metadata={**doc.metadata, "chunk_index": index},
        )
        for index, text in enumerate(chunk_text(doc.text, size, overlap))
    ]


def chunk_corpus(docs: list[Document], size: int, overlap: int) -> list[Chunk]:
    return [chunk for doc in docs for chunk in chunk_document(doc, size, overlap)]
