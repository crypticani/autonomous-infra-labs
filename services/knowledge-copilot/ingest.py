import argparse
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

import chromadb
from chromadb.config import Settings
from chromadb.errors import NotFoundError
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from chunking import CORPUS_DIR, Chunk, Document, chunk_corpus, load_corpus
from embeddings import BaseEmbeddingProvider, get_embedding_provider

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "WARNING").upper(),
    format="%(asctime)s - [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

console = Console()

CHROMA_PATH = os.getenv("CHROMA_PATH", str(Path(__file__).parent / "chroma_data"))

SIZE, OVERLAP = 512, 64


def content_hash(doc: Document) -> str:
    payload = doc.text + "\0" + json.dumps(doc.metadata, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def enrich(docs: list[Document]) -> list[Document]:
    today = date.today().isoformat()
    return [
        replace(
            doc,
            metadata={
                **doc.metadata,
                "content_hash": content_hash(doc),
                "indexed_at": today,
            },
        )
        for doc in docs
    ]


@dataclass
class Plan:
    to_add: list[Chunk] = field(default_factory=list)
    to_update: list[Chunk] = field(default_factory=list)
    to_delete: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def to_upsert(self) -> list[Chunk]:
        return self.to_add + self.to_update


def plan_reconcile(desired: list[Chunk], existing: dict[str, str]) -> Plan:
    plan = Plan()
    desired_ids = set()

    for chunk in desired:
        desired_ids.add(chunk.id)
        stored = existing.get(chunk.id)
        if stored is None:
            plan.to_add.append(chunk)
        elif stored != chunk.metadata["content_hash"]:
            plan.to_update.append(chunk)
        else:
            plan.unchanged.append(chunk.id)

    plan.to_delete = [cid for cid in existing if cid not in desired_ids]
    return plan


def collection_name(provider: BaseEmbeddingProvider) -> str:
    return f"knowledge_{provider.name}_{SIZE}_{OVERLAP}"


def get_collection(client: chromadb.ClientAPI, provider: BaseEmbeddingProvider):
    return client.get_or_create_collection(
        name=collection_name(provider),
        configuration={"hnsw": {"space": "cosine"}},
        embedding_function=None,
    )


def existing_hashes(collection) -> dict[str, str]:
    stored = collection.get(include=["metadatas"])
    return {
        cid: (meta or {}).get("content_hash", "")
        for cid, meta in zip(stored["ids"], stored["metadatas"])
    }


def ingest(
    reset: bool = False,
    dry_run: bool = False,
    provider: BaseEmbeddingProvider | None = None,
    client: chromadb.ClientAPI | None = None,
    corpus_dir: Path = CORPUS_DIR,
) -> Plan:

    if reset and dry_run:
        raise ValueError("reset drops the collection; it is never a dry run")

    provider = provider or get_embedding_provider()
    client = client or chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False),
    )

    if reset:
        try:
            client.delete_collection(collection_name(provider))
        except NotFoundError:
            pass

    collection = get_collection(client, provider)

    docs = enrich(load_corpus(corpus_dir))
    desired = chunk_corpus(docs, size=SIZE, overlap=OVERLAP)
    plan = plan_reconcile(desired, existing_hashes(collection))

    if dry_run:
        return plan

    if plan.to_upsert:
        upsert = plan.to_upsert
        collection.upsert(
            ids=[c.id for c in upsert],
            documents=[c.text for c in upsert],
            embeddings=provider.embed_documents([c.text for c in upsert]),
            metadatas=[c.metadata for c in upsert],
        )
    if plan.to_delete:
        collection.delete(ids=plan.to_delete)

    return plan


def search(collection, provider, question, k=3, where=None) -> list[dict]:
    qvec = provider.embed_query(question)
    result = collection.query(
        query_embeddings=[qvec],
        n_results=k,
        where=where,
        include=["metadatas", "distances"],
    )
    return [
        {
            "source": meta["source"],
            "chunk": meta["chunk_index"],
            "doc_type": meta["doc_type"],
            "score": 1 - distance,
        }
        for meta, distance in zip(result["metadatas"][0], result["distances"][0])
    ]


def parse_where(pairs: list[str] | None) -> dict | None:
    if not pairs:
        return None
    filters: dict[str, str | int] = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        value = raw.strip()
        filters[key.strip()] = int(value) if value.lstrip("-").isdigit() else value
    if len(filters) == 1:
        return filters
    return {"$and": [{k: v} for k, v in filters.items()]}


def print_summary(plan: Plan, dry_run: bool) -> None:
    verb = "would" if dry_run else "did"
    table = Table(title=f"Ingestion plan ({'dry run' if dry_run else 'applied'})")
    table.add_column("action")
    table.add_column("count", justify="right")
    table.add_row(f"add ({verb})", str(len(plan.to_add)))
    table.add_row(f"update ({verb})", str(len(plan.to_update)))
    table.add_row(f"delete ({verb})", str(len(plan.to_delete)))
    table.add_row("unchanged (skipped)", str(len(plan.unchanged)))
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Day 9: idempotent corpus ingestion")
    parser.add_argument(
        "--reset", action="store_true", help="drop the collection and rebuild"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan, change nothing"
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=CORPUS_DIR,
        help="directory of markdown docs to ingest (default: ./corpus)",
    )
    parser.add_argument("--query", help="run a similarity search after ingesting")
    parser.add_argument(
        "--where",
        action="append",
        metavar="key=value",
        help="metadata filter for --query (repeatable), e.g. --where doc_type=runbook",
    )
    args = parser.parse_args()

    if args.reset and args.dry_run:
        parser.error("--reset drops the collection; it cannot be a --dry-run")

    provider = get_embedding_provider()

    client = chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    plan = ingest(
        reset=args.reset,
        dry_run=args.dry_run,
        provider=provider,
        client=client,
        corpus_dir=args.corpus,
    )
    print_summary(plan, args.dry_run)

    if not args.query:
        return

    collection = get_collection(client, provider)
    where = parse_where(args.where)
    console.rule(f'[bold]"{args.query}" where={where}')
    for hit in search(collection, provider, args.query, where=where):
        console.print(
            f' {hit["source"]}#{hit["chunk"]} '
            f'[dim]({hit["doc_type"]})[/dim] '
            f'[bold]{hit["score"]:.3f}[/bold]'
        )


if __name__ == "__main__":
    main()
