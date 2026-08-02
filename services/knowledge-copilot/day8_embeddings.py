import argparse
import json
import logging
import os
from pathlib import Path

import chromadb
from chromadb.config import Settings
from chromadb.errors import NotFoundError
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from chunking import chunk_corpus, load_corpus
from embeddings import BaseEmbeddingProvider, get_embedding_provider

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "WARNING").upper(),
    format="%(asctime)s - [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

console = Console()

CHROMA_PATH = os.getenv("CHROMA_PATH", str(Path(__file__).parent / "chroma_data"))
QUERIES_PATH = Path(__file__).parent / "queries.json"

CONFIGS = [(256, 32), (512, 64), (1024, 128)]
TOP_K = 3

RELATED_PAIR = (
    "The pod was OOMKilled after exceeding its memory limit.",
    "The container got terminated because it ran out of memory",
)
UNRELATED_PAIR = (
    "The pod was OOMKilled after exceeding its memory limit.",
    "The TLS certificate for the ingress expired last night.",
)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def show_vector_intuition(provider: BaseEmbeddingProvider) -> None:
    console.rule("[bold]1. What an embedding actually is")

    sample = RELATED_PAIR[0]
    vector = provider.embed_documents([sample])[0]

    console.print(f"[dim]text  :[/dim] {sample}")
    console.print(f"[dim]model :[/dim] {provider.name}/{provider.model_name}")
    console.print(f"[dim]dims  :[/dim] {len(vector)}")
    console.print(
        "[dim]first5:[/dim] [" + ", ".join(f"{v:+.4f}" for v in vector[:5]) + ", ...]\n"
    )

    for label, (left, right) in (
        ("related", RELATED_PAIR),
        ("unrelated", UNRELATED_PAIR),
    ):
        a, b = provider.embed_documents([left, right])
        console.print(f"  cos({label}) = [bold]{cosine_similarity(a, b):.4f}[/bold]")
        console.print(f"    [dim]A: {left}[/dim]")
        console.print(f"    [dim]B: {right}[/dim]")
    console.print()


def build_collection(client, provider, docs, size, overlap):
    name = f"runbooks_{provider.name}_{size}_{overlap}"
    chunks = chunk_corpus(docs, size=size, overlap=overlap)

    collection = client.get_or_create_collection(
        name=name,
        configuration={"hnsw": {"space": "cosine"}},
        embedding_function=None,
    )

    collection.upsert(
        ids=[c.id for c in chunks],
        documents=[c.text for c in chunks],
        embeddings=provider.embed_documents([c.text for c in chunks]),
        metadatas=[c.metadata for c in chunks],
    )

    console.print(
        f"  [green]indexed[/green] {len(chunks):>3} chunks"
        f"(size={size}, overlap={overlap}) -> {name}"
    )
    return collection


def search(collection, query_vector, k: int = TOP_K) -> list[dict]:
    result = collection.query(
        query_embeddings=[query_vector],
        n_results=k,
        include=["metadatas", "distances"],
    )
    return [
        {
            "source": meta["source"],
            "chunk": meta["chunk_index"],
            "score": 1 - distance,
        }
        for meta, distance in zip(result["metadatas"][0], result["distances"][0])
    ]


def compare_configs(collections, provider, queries) -> dict:
    console.rule("[bold]3. Same query, three chunk sizes")
    hits = {label: {"top1": 0, "top3": 0} for label in collections}

    for query in queries:
        expected = query["expected_source"]
        query_vector = provider.embed_query(query["question"])

        results_by_config = {}
        for label, collection in collections.items():
            results = search(collection, query_vector)
            results_by_config[label] = results

            sources = [r["source"] for r in results]
            if sources and sources[0] == expected:
                hits[label]["top1"] += 1
            if expected in sources:
                hits[label]["top3"] += 1

        table = Table(
            title=f'[bold]"{query["question"]}"[/bold]\n[dim]expected: {expected}[/dim]',
            title_justify="left",
        )
        table.add_column("#", width=2)
        for label in collections:
            table.add_column(label, overflow="fold")

        for rank in range(TOP_K):
            row = [str(rank + 1)]
            for label in collections:
                results = results_by_config[label]
                if rank >= len(results):
                    row.append("-")
                    continue
                hit = results[rank]
                marker = (
                    "[green]OK[/green]" if hit["source"] == expected else "[red]X[/red]"
                )
                row.append(
                    f'{marker} {hit["source"]}#{hit["chunk"]} '
                    f'[bold]{hit["score"]:.3f}[/bold]'
                )
            table.add_row(*row)

        console.print(table)
        console.print()

    return hits


def show_summary(hits: dict, total: int) -> None:
    console.rule("[bold]4. Which chunk size retrieved best")

    table = Table()
    table.add_column("config")
    table.add_column("hit@1", justify="right")
    table.add_column("hit@3", justify="right")
    for label, counts in hits.items():
        table.add_row(label, f'{counts["top1"]}/{total}', f'{counts["top3"]}/{total}')
    console.print(table)

    console.print(
        f"[yellow]Signal, not a benchmark[/yellow] - n={total}. One query moving "
        "swings this by 20%. Day 11 builds the eval set that can actually settle it."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Day 8: embeddings and vector search")
    parser.add_argument(
        "--reset", action="store_true", help="drop existing collections before indexing"
    )
    args = parser.parse_args()

    provider = get_embedding_provider()
    docs = load_corpus()
    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

    show_vector_intuition(provider)

    console.rule("[bold]2. Indexing the corpus at three chunk sizes")
    console.print(f"  {len(docs)} runbooks from corpus/, persisting to {CHROMA_PATH}\n")

    client = chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False),
    )

    collections = {}
    for size, overlap in CONFIGS:
        if args.reset:
            try:
                client.delete_collection(f"runbooks_{provider.name}_{size}_{overlap}")
            except NotFoundError:
                pass
        collections[f"{size}/{overlap}"] = build_collection(
            client, provider, docs, size, overlap
        )
    console.print()

    show_summary(compare_configs(collections, provider, queries), len(queries))


if __name__ == "__main__":
    main()
