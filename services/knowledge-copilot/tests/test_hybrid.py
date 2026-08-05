from math import cos, radians, sin

import pytest

from hybrid import bm25_scores, mmr, rank_relevance, rrf, tokenize


def unit(degrees: float) -> list[float]:
    """A unit vector at an angle, so dot products are exactly cos(difference)."""
    return [cos(radians(degrees)), sin(radians(degrees))]


def test_tokenize_keeps_error_codes():
    assert tokenize("Exit code 137 (OOMKilled), x509 error") == [
        "exit",
        "code",
        "137",
        "oomkilled",
        "x509",
        "error",
    ]


def test_bm25_ranks_the_exact_token_chunk_first():
    corpus = [
        tokenize("the pod was terminated with exit code 137"),
        tokenize("the pod restarted after a rolling deploy"),
        tokenize("the pod failed to pull its image"),
    ]
    scores = bm25_scores("exit code 137", corpus)
    assert scores[0] > 0
    assert scores[1] == scores[2] == 0.0


def test_universal_term_scores_far_below_a_discriminating_one():
    corpus = [
        tokenize("pod restarted"),
        tokenize("pod crashed"),
        tokenize("pod evicted"),
    ]
    # `pod` is in every document: df == n, so idf collapses to log(1 + 0.5/3.5).
    everywhere = bm25_scores("pod", corpus)
    assert all(0 < score < 0.5 for score in everywhere)

    # `evicted` is in one: idf = log(1 + 2.5/1.5), roughly 7x larger.
    discriminating = bm25_scores("evicted", corpus)
    assert max(discriminating) > 4 * max(everywhere)


def test_rrf_prefers_the_candidate_both_rankers_like():
    # `b` is second in both rankings; `a` and `c` are each first in only one.
    assert rrf(["a", "b"], ["c", "b"])[0] == "b"


def test_rrf_keeps_ids_found_by_a_single_ranker():
    assert set(rrf(["a"], ["b"])) == {"a", "b"}


def test_rank_relevance_is_linear_from_one():
    # approx, not ==: `1 - 1/3` is not bit-identical to `2/3` in binary floats.
    assert rank_relevance(["a", "b", "c"]) == pytest.approx(
        {"a": 1.0, "b": 2 / 3, "c": 1 / 3}
    )


def test_mmr_at_lambda_one_keeps_the_input_order():
    candidates = [("a", 1.0, unit(0)), ("b", 2 / 3, unit(1)), ("c", 1 / 3, unit(90))]
    assert mmr(candidates, k=2, lam=1.0) == ["a", "b"]


def test_mmr_drops_a_near_duplicate_for_a_more_distant_chunk():
    # `b` is 1 degree from `a` -- the same document, effectively. `c` ranks lower
    # but is orthogonal to `a`, so it actually adds something.
    candidates = [("a", 1.0, unit(0)), ("b", 2 / 3, unit(1)), ("c", 1 / 3, unit(90))]
    assert mmr(candidates, k=2, lam=0.7) == ["a", "c"]
