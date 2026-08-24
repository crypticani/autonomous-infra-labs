"""Lexical scoring, rank fusion, and diversity reranking.

Pure functions only -- text or vectors in, scores or an ordering out, which is what
makes the tests runnable without an embedding provider.
"""

import re
from collections import Counter
from math import inf, log, sumprod

TOKEN_RE = re.compile(r"[a-z0-9]+")

BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60
MMR_LAMBDA = 0.7


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric runs. `Exit code 137 (OOMKilled)` keeps `137`. No stopword list:
    idf already discounts ubiquitous terms from the data.
    """
    return TOKEN_RE.findall(text.lower())


def bm25_scores(
    query: str,
    documents: list[list[str]],
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> list[float]:
    """Okapi BM25 for one query against a pre-tokenized corpus.

    ponytail: recomputes df/avgdl every call. ~1ms over 68 chunks; precompute into an
    inverted index if the corpus reaches thousands.
    """
    n = len(documents)
    if n == 0:
        return []

    avgdl = sum(len(doc) for doc in documents) / n or 1.0
    df = Counter(term for doc in documents for term in set(doc))
    terms = [t for t in set(tokenize(query)) if t in df]

    scores = []
    for doc in documents:
        tf = Counter(doc)
        dl = len(doc)
        score = 0.0
        for term in terms:
            freq = tf[term]
            if not freq:
                continue
            # log(1 + ...) never goes negative. The classic form does for any term in more
            # than half the corpus -- across 68 runbook chunks, that means `pod`.
            idf = log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * freq * (k1 + 1) / (freq + k1 * (1 - b + b * dl / avgdl))
        scores.append(score)
    return scores


def rrf(*rankings: list[str], k: int = RRF_K) -> list[str]:
    """Reciprocal Rank Fusion: combine rankings by rank, never by score."""
    fused: Counter[str] = Counter()
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            fused[doc_id] += 1 / (k + rank)
    return [doc_id for doc_id, _ in fused.most_common()]


def mmr(
    candidates: list[tuple[str, float, list[float]]],
    k: int,
    lam: float = MMR_LAMBDA,
) -> list[str]:
    """Maximal Marginal Relevance over an already-ranked candidate list.

    `candidates` is (id, relevance, vector), best-first, relevance normally derived from
    rank -- so whatever produced the ranking is what MMR respects. Vectors must be
    L2-normalised, so a dot product is cosine. lam=1.0 returns the input order unchanged.

    ponytail: O(k * candidates), full rescan per pick. Fine at 15.
    """
    remaining = list(candidates)
    picked: list[tuple[str, float, list[float]]] = []

    while remaining and len(picked) < k:
        best_index, best_score = 0, -inf
        for index, (_, relevance, vector) in enumerate(remaining):
            redundancy = max(
                (sumprod(vector, chosen) for _, _, chosen in picked), default=0.0
            )
            score = lam * relevance - (1 - lam) * redundancy
            if score > best_score:
                best_index, best_score = index, score
        picked.append(remaining.pop(best_index))

    return [doc_id for doc_id, _, _ in picked]


def rank_relevance(ordering: list[str]) -> dict[str, float]:
    """Linear 1..0 relevance from a ranking, so MMR's lam trades rank positions against cosine
    redundancy on the same scale. RRF's own scores are not on that scale.
    """
    n = len(ordering) or 1
    return {doc_id: 1 - index / n for index, doc_id in enumerate(ordering)}
