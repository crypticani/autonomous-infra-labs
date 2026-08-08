import logging
import os
from dataclasses import dataclass
from math import sumprod

import requests
from dotenv import load_dotenv

from embeddings import BaseEmbeddingProvider
from errors import EmbeddingError
from hybrid import bm25_scores, mmr, rank_relevance, rrf, tokenize
from metrics import TOP_SIMILARITY

load_dotenv()

logger = logging.getLogger(__name__)

# Set by eval_retrieval.py --floor-sweep on 2026-08-08 over 11 answerable and 10 absent
# cases: 0.64 is the unique minimum at 1 total error (0 false rejects, 1 false accept),
# against 2 at the old 0.65. The backlog wanted ~0.60 on the strength of one real answer
# scoring 0.659 -- but 0.659 clears 0.64 comfortably, and 0.60 would have admitted 7 of
# the 10 unanswerable questions instead of 1. Measuring the negatives is what caught it.
SIMILARITY_FLOOR = float(os.getenv("SIMILARITY_FLOOR", "0.64"))
DEFAULT_K = 4
CANDIDATE_POOL = 15
MODES = ("dense", "lexical", "hybrid")

# Day 11's eval earned hybrid: hit@1 8/12 -> 9/12, MRR 0.79 -> 0.83, for ~2ms.
# lam stays 1.0 -- MMR is off, because the eval showed it cannot act on this
# embedding space (see eval_retrieval.py and the Readme findings).
RETRIEVAL_MODE = os.getenv("RETRIEVAL_MODE", "hybrid").lower()
DEFAULT_LAM = 1.0

if RETRIEVAL_MODE not in MODES:
    # Checked at import, not per request: a typo in .env would otherwise 500 on
    # every single call with nothing in the response explaining why. This way
    # uvicorn refuses to start and says so.
    raise ValueError(f"RETRIEVAL_MODE={RETRIEVAL_MODE!r} is not one of {MODES}")


class EmptyIndexError(RuntimeError):
    """The index is unusable: no rows at all, or rows without embeddings.

    Both mean the same thing to a caller — re-run ingest.py — and app.py maps
    this to a 503. A bare RuntimeError here would reach the client as a 500 with
    no explanation instead.
    """


@dataclass(frozen=True)
class Hit:
    text: str
    source: str
    chunk_index: int
    doc_type: str
    score: float


@dataclass(frozen=True)
class LexicalIndex:
    """The whole collection in memory: 68 chunks x 768 dims, roughly 400KB.

    Three consumers at once. BM25 needs corpus-wide document frequencies and
    average length, MMR needs candidate vectors to measure redundancy between
    them, and the floor needs an exact cosine for a chunk only BM25 found.
    """

    ids: list[str]
    texts: list[str]
    metadatas: list[dict]
    embeddings: list[list[float]]
    tokens: list[list[str]]


# ponytail: keyed on (name, count), so an in-place content update won't bust it.
# Restart after re-ingest; key on content_hash if that ever bites.
_index_cache: dict[tuple[str, int], LexicalIndex] = {}


def load_index(collection) -> LexicalIndex:
    key = (collection.name, collection.count())
    if key not in _index_cache:
        stored = collection.get(include=["documents", "metadatas", "embeddings"])
        embeddings = stored["embeddings"]
        if embeddings is None or len(embeddings) != len(stored["ids"]):
            # A broken index, not a bad query. Scoring 0 here would blame
            # retrieval quality for an infrastructure fault.
            raise EmptyIndexError(
                f"collection {collection.name!r} returned no embeddings: run ingest.py"
            )
        documents = list(stored["documents"])
        _index_cache[key] = LexicalIndex(
            ids=list(stored["ids"]),
            texts=documents,
            metadatas=[meta or {} for meta in stored["metadatas"]],
            embeddings=[list(vector) for vector in embeddings],
            tokens=[tokenize(text) for text in documents],
        )
    return _index_cache[key]


def matches(metadata: dict, where: dict | None) -> bool:
    # ponytail: equality only, which is all the eval uses. Chroma's own operator
    # syntax ($in, $ne) would need a real predicate walker.
    return all(metadata.get(key) == value for key, value in (where or {}).items())


def _dense(collection, query_vector, pool, where) -> dict[str, tuple]:
    """{id: (text, metadata, cosine)} for the dense candidate pool."""
    result = collection.query(
        query_embeddings=[query_vector],
        n_results=pool,
        where=where or None,
        include=["documents", "metadatas", "distances"],
    )
    return {
        doc_id: (text, meta, 1 - distance)
        for doc_id, text, meta, distance in zip(
            result["ids"][0],
            result["documents"][0],
            result["metadatas"][0],
            result["distances"][0],
        )
    }


def _lexical(index: LexicalIndex, question: str, pool: int, where) -> list[str]:
    scores = bm25_scores(question, index.tokens)
    # score > 0 means the chunk shares at least one query term. Without this the
    # remaining ~60 chunks would still get rank positions, and RRF would hand
    # them credit for containing nothing.
    ranked = sorted(
        (
            i
            for i, score in enumerate(scores)
            if score > 0 and matches(index.metadatas[i], where)
        ),
        key=lambda i: scores[i],
        reverse=True,
    )
    return [index.ids[i] for i in ranked[:pool]]


def retrieve(
    question: str,
    provider: BaseEmbeddingProvider,
    collection,
    k: int = DEFAULT_K,
    floor: float = SIMILARITY_FLOOR,
    mode: str | None = None,
    where: dict[str, str] | None = None,
    lam: float = DEFAULT_LAM,
    pool: int = CANDIDATE_POOL,
) -> list[Hit]:
    mode = mode or RETRIEVAL_MODE
    if mode not in MODES:
        raise ValueError(f"unknown retrieval mode {mode!r}; expected one of {MODES}")

    if collection.count() == 0:
        raise EmptyIndexError(f"collection {collection.name!r} is empty: run ingest.py")

    try:
        query_vector = provider.embed_query(question)
    except requests.exceptions.Timeout as e:
        raise EmbeddingError("embedding the question took too long", 504) from e
    except Exception as e:
        # Every provider raises its own SDK error here; the caller only needs to
        # know the embedding backend failed. `from e` keeps the real traceback.
        raise EmbeddingError(f"the embedding backend failed: {e}", 503) from e

    # Loaded only when something needs it, so dense + lam=1.0 stays exactly the
    # Day 10 path: one Chroma query and nothing else.
    index = load_index(collection) if mode != "dense" or lam < 1.0 else None

    rankings, dense = [], {}
    if mode in ("dense", "hybrid"):
        dense = _dense(collection, query_vector, pool, where)
        rankings.append(list(dense))
    if mode in ("lexical", "hybrid"):
        rankings.append(_lexical(index, question, pool, where))

    # RRF over a single ranking is order-preserving, so dense-only and
    # lexical-only come through untouched and fusion needs no special case.
    fused = rrf(*rankings)
    position = {doc_id: i for i, doc_id in enumerate(index.ids)} if index else {}

    kept, best = [], 0.0
    for doc_id in fused:
        if doc_id in dense:
            text, meta, score = dense[doc_id]
        else:
            # BM25 found this and dense did not -- the `137` rescue. Score it
            # from its stored embedding so a keyword match cannot smuggle a
            # chunk past the floor.
            i = position[doc_id]
            text, meta = index.texts[i], index.metadatas[i]
            score = sumprod(query_vector, index.embeddings[i])
        # Cosine, never the fused score. The floor is Day 10's refusal guard and
        # has to keep meaning similarity to the question; RRF values sit around
        # 0.016-0.033 and are scale-free by design.
        best = max(best, score)
        if score >= floor:
            kept.append((doc_id, text, meta, score))

    # Observed here because this is the only scope that has `best`. Every production
    # question therefore becomes a data point for the same curve --floor-sweep drew
    # offline, which is the part of that measurement that outlives the day it was made.
    TOP_SIMILARITY.observe(best)

    if not kept:
        # The margin matters: 0.64 is a floor that is slightly too high, 0.30 is
        # a question the corpus genuinely cannot answer. Same empty list, very
        # different bug.
        logger.warning(
            f"nothing cleared floor {floor} for {question!r}; best was {best:.3f}"
        )
        return []

    if lam >= 1.0:
        chosen = kept[:k]
    else:
        relevance = rank_relevance([doc_id for doc_id, _, _, _ in kept])
        order = mmr(
            [
                (doc_id, relevance[doc_id], index.embeddings[position[doc_id]])
                for doc_id, _, _, _ in kept
            ],
            k=k,
            lam=lam,
        )
        by_id = {row[0]: row for row in kept}
        chosen = [by_id[doc_id] for doc_id in order]

    return [
        Hit(
            text=text,
            source=meta["source"],
            chunk_index=meta["chunk_index"],
            doc_type=meta.get("doc_type", "unknown"),
            score=score,
        )
        for _, text, meta, score in chosen
    ]
