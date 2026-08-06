import argparse
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path

import chromadb
from chromadb.config import Settings
from chromadb.errors import NotFoundError
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from chunking import CORPUS_DIR, Chunk, Document, chunk_corpus, load_corpus
from connectors.alertmanager import (
    ALERT_DOC_TYPE,
    AlertmanagerError,
    fetch_alerts,
    merge,
)
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


def existing_hashes(collection, where: dict | None = None) -> dict[str, str]:
    """Stored content hashes, optionally scoped to one source's documents.

    Unscoped, plan_reconcile deletes everything not in `desired` -- correct while the
    corpus was the only source, and catastrophic once a second one exists: an alert
    sync's desired set contains no runbooks, so it would delete all of them on the
    first poll. Scoping is the root-cause fix. A guard inside the alert path would
    leave the next source (K8s events, Jenkins builds) to rediscover the same bug.
    """
    stored = collection.get(include=["metadatas"], where=where or None)
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
    docs: list[Document] | None = None,
    where: dict | None = None,
) -> Plan:
    """Reconcile `desired` against what is stored, then write the difference.

    `docs` makes this source-agnostic: given documents, those are the desired set and
    corpus_dir is ignored. `where` scopes both the read-back and therefore the
    deletes, so each source only ever removes documents it owns.
    """

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

    docs = enrich(load_corpus(corpus_dir) if docs is None else docs)
    desired = chunk_corpus(docs, size=SIZE, overlap=OVERLAP)
    plan = plan_reconcile(desired, existing_hashes(collection, where))

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


ALERT_WHERE = {"doc_type": ALERT_DOC_TYPE}


def indexed_alerts(collection) -> dict[str, dict]:
    """Fingerprint -> stored metadata, for the alerts already in the collection.

    This is how merge() learns what to compare a fetch against, and therefore how
    resolution-by-absence works at all.
    """
    stored = collection.get(include=["metadatas"], where=ALERT_WHERE)
    return {
        meta["fingerprint"]: meta
        for meta in stored["metadatas"]
        if meta and "fingerprint" in meta
    }


def sync_alerts(
    provider: BaseEmbeddingProvider | None = None,
    client: chromadb.ClientAPI | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    fetch=fetch_alerts,
) -> Plan:
    """One poll: fetch, merge with what is indexed, reconcile.

    `fetch` raises on any failure and this function deliberately does not catch it,
    so a failed poll reaches the caller having read and written nothing. Callers that
    poll on a timer log it and try again -- they must never treat it as an empty
    alert set, because merge() would then resolve every alert in the index at once.
    """
    provider = provider or get_embedding_provider()
    client = client or chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    collection = get_collection(client, provider)

    live = fetch()  # raises before anything is read or written

    docs = merge(
        live, indexed_alerts(collection), now=now or datetime.now(timezone.utc)
    )
    return ingest(
        dry_run=dry_run,
        docs=docs,
        where=ALERT_WHERE,
        provider=provider,
        client=client,
    )


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
    parser.add_argument(
        "--source",
        choices=("corpus", "alerts"),
        default="corpus",
        help="what to ingest (default: corpus)",
    )
    args = parser.parse_args()

    if args.reset and args.dry_run:
        parser.error("--reset drops the collection; it cannot be a --dry-run")

    if args.reset and args.source == "alerts":
        # --reset drops the whole collection, runbooks included, which is never what
        # a caller syncing alerts meant. Alerts are removed by retention, not reset.
        parser.error("--reset applies to the corpus; alerts expire via retention")

    provider = get_embedding_provider()

    client = chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    if args.source == "alerts":
        try:
            plan = sync_alerts(provider=provider, client=client, dry_run=args.dry_run)
        except AlertmanagerError as e:
            # "the server is down" is an operational condition, not a bug. A stack
            # trace here buries the one line that says which URL failed.
            console.print(f"[red]{e}[/red]")
            raise SystemExit(1) from None
    else:
        plan = ingest(
            reset=args.reset,
            dry_run=args.dry_run,
            provider=provider,
            client=client,
            corpus_dir=args.corpus,
        )
    print_summary(plan, args.dry_run)


if __name__ == "__main__":
    main()
