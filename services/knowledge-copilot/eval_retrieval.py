"""Retrieval quality sweep. Never calls the generator, so the whole grid runs in seconds
rather than 195s per answer.
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from hybrid import MMR_LAMBDA
from retrieval import (
    CANDIDATE_POOL,
    DEFAULT_K,
    DEFAULT_LAM,
    MODES,
    RETRIEVAL_MODE,
    retrieve,
)
from store import open_collection

EVAL_SET = Path(__file__).parent / "eval_set.json"
console = Console()


class CachingProvider:
    """The sweep asks the same 12 questions of every configuration. Embedding each once turns
    ~72 network calls into 12, and takes a constant hop out of the measured latency where it
    would otherwise mask the ranking differences.
    """

    def __init__(self, inner):
        self.inner = inner
        self.name = inner.name
        self.dimensions = inner.dimensions
        self._cache: dict[str, list[float]] = {}

    def embed_query(self, text: str) -> list[float]:
        if text not in self._cache:
            self._cache[text] = self.inner.embed_query(text)
        return self._cache[text]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed_documents(texts)


@dataclass
class Outcome:
    kind: str
    hit1: bool
    soft_hit1: bool
    recall: float
    rr: float
    chunk_hit: bool | None
    refused: bool
    seconds: float
    error: str | None = None


def score(case: dict, hits: list, seconds: float) -> Outcome:
    sources = [hit.source for hit in hits]
    primary = case["primary"]

    if primary is None:
        # No answer exists in the corpus. The only correct behaviour is silence,
        # so every metric collapses onto "did it refuse".
        refused = not hits
        return Outcome(
            case["kind"],
            refused,
            refused,
            float(refused),
            float(refused),
            None,
            refused,
            seconds,
        )

    relevant = set(case.get("acceptable", [])) | {primary}
    rr = next((1 / rank for rank, src in enumerate(sources, 1) if src in relevant), 0.0)
    needle = case.get("must_contain")

    return Outcome(
        kind=case["kind"],
        hit1=bool(sources) and sources[0] == primary,
        soft_hit1=bool(sources) and sources[0] in relevant,
        recall=len(relevant & set(sources)) / len(relevant),
        rr=rr,
        chunk_hit=None if not needle else any(needle in hit.text for hit in hits),
        refused=not hits,
        seconds=seconds,
    )


def run(cases, provider, collection, mode, lam, k, use_filter) -> list[Outcome]:
    outcomes = []
    for case in cases:
        where = case.get("where") if use_filter else None
        start = time.perf_counter()
        try:
            hits = retrieve(
                case["question"],
                provider,
                collection,
                k=k,
                mode=mode,
                where=where,
                lam=lam,
            )
        except Exception as e:
            # One bad query must not destroy eleven good measurements.
            outcomes.append(
                Outcome(
                    case["kind"],
                    False,
                    False,
                    0.0,
                    0.0,
                    None,
                    False,
                    time.perf_counter() - start,
                    f"{type(e).__name__}: {e}",
                )
            )
            continue
        outcomes.append(score(case, hits, time.perf_counter() - start))
    return outcomes


def percentile(values: list[float], q: float) -> float:
    # ponytail: nearest-rank on 12 samples. Interpolation would be false
    # precision at this sample size.
    ordered = sorted(values)
    return ordered[min(int(q * len(ordered)), len(ordered) - 1)]


def summary_table(results: list[tuple[str, list[Outcome]]]) -> Table:
    table = Table(title="Retrieval quality by configuration")
    columns = (
        "config",
        "hit@1",
        "soft@1",
        "recall@k",
        "MRR",
        "chunk",
        "refused",
        "p50",
        "p95",
        "err",
    )
    for column in columns:
        table.add_column(column, justify="left" if column == "config" else "right")

    for label, outcomes in results:
        n = len(outcomes)
        chunks = [o.chunk_hit for o in outcomes if o.chunk_hit is not None]
        latencies = [o.seconds for o in outcomes]
        table.add_row(
            label,
            f"{sum(o.hit1 for o in outcomes)}/{n}",
            f"{sum(o.soft_hit1 for o in outcomes)}/{n}",
            f"{sum(o.recall for o in outcomes) / n:.2f}",
            f"{sum(o.rr for o in outcomes) / n:.2f}",
            f"{sum(chunks)}/{len(chunks)}",
            str(sum(o.refused for o in outcomes)),
            f"{percentile(latencies, 0.5) * 1000:.0f}ms",
            f"{percentile(latencies, 0.95) * 1000:.0f}ms",
            str(sum(o.error is not None for o in outcomes)),
        )
    return table


def by_kind_table(results: list[tuple[str, list[Outcome]]]) -> Table:
    """soft_hit@1 split by query kind -- where the prediction actually lives. Hybrid is
    supposed to win on exact_token and give up nothing on paraphrase.
    """
    kinds = sorted({o.kind for _, outcomes in results for o in outcomes})
    table = Table(title="soft hit@1 by query kind")
    table.add_column("config")
    for kind in kinds:
        table.add_column(kind, justify="right")

    for label, outcomes in results:
        row = [label]
        for kind in kinds:
            group = [o for o in outcomes if o.kind == kind]
            row.append(f"{sum(o.soft_hit1 for o in group)}/{len(group)}")
        table.add_row(*row)
    return table


def best_similarity(question: str, provider, collection) -> float:
    """The highest cosine any chunk scored for this question."""
    hits = retrieve(question, provider, collection, k=2 * CANDIDATE_POOL, floor=0.0)
    return max((hit.score for hit in hits), default=0.0)


def sweep_counts(
    answerable: list[float], absent: list[float], floor: float
) -> tuple[int, int]:
    """(false rejections, false acceptances) at one candidate floor. Pure arithmetic over
    numbers gathered once, which is what makes sweeping sixteen floors cost one pass.
    """
    false_rejects = sum(1 for best in answerable if best < floor)
    false_accepts = sum(1 for best in absent if best >= floor)
    return false_rejects, false_accepts


def floor_sweep_table(answerable: list[float], absent: list[float]) -> Table:
    title = f"floor sweep -- {len(answerable)} answerable, {len(absent)} absent"
    table = Table(title=title)
    table.add_column("floor")
    table.add_column("false rejects", justify="right")
    table.add_column("false accepts", justify="right")
    table.add_column("total errors", justify="right")

    for step in range(55, 71):
        floor = step / 100
        rejects, accepts = sweep_counts(answerable, absent, floor)
        table.add_row(
            f"{floor:.2f}", str(rejects), str(accepts), str(rejects + accepts)
        )
    return table


def main() -> int | None:
    parser = argparse.ArgumentParser(description="Day 11: retrieval quality sweep")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--modes", nargs="+", default=list(MODES), choices=MODES)
    parser.add_argument(
        "--lam",
        type=float,
        nargs="+",
        default=[1.0, MMR_LAMBDA],
        help="MMR lambda; 1.0 is the no-rerank control",
    )
    parser.add_argument(
        "--filters",
        action="store_true",
        help="also run each config with each query's `where` applied",
    )
    parser.add_argument(
        "--floor-sweep",
        action="store_true",
        help="measure where the similarity floor should sit, then exit",
    )
    parser.add_argument(
        "--floor",
        type=int,
        help="Day 28: fail (exit 1) if the shipped config scores fewer than this many "
        "soft hit@1 out of the eval set. No default on purpose -- a regression bar "
        "picked before the baseline was measured is just a number chosen to pass.",
    )
    args = parser.parse_args()

    cases = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    inner, collection = open_collection()
    provider = CachingProvider(inner)

    # Warm every question before timing: otherwise whichever config runs first pays all 12
    # cache misses and looks 20x slower, which is sweep order rather than a property.
    for case in cases:
        provider.embed_query(case["question"])

    if args.floor_sweep:
        # One retrieval pass per case, then sixteen floors swept over the numbers it produced --
        # the floor only filters what retrieval already scored.
        answerable, absent = [], []
        for case in cases:
            best = best_similarity(case["question"], provider, collection)
            (absent if case["primary"] is None else answerable).append(best)
        console.print(floor_sweep_table(answerable, absent))
        return

    results = []
    for mode in args.modes:
        for lam in args.lam:
            for use_filter in ([False, True] if args.filters else [False]):
                label = f"{mode} lam={lam}" + (" +filter" if use_filter else "")
                results.append(
                    (
                        label,
                        run(cases, provider, collection, mode, lam, args.k, use_filter),
                    )
                )

    console.print(summary_table(results))
    console.print(by_kind_table(results))

    for label, outcomes in results:
        for case, outcome in zip(cases, outcomes):
            if outcome.error:
                console.print(
                    f"[red]{label}[/] {case['question'][:40]!r}: {outcome.error}"
                )

    return report_shipped_config(results, args.floor)


def report_shipped_config(results, floor: int | None) -> int:
    """One machine-readable line, so the repo-root runner can put this service in the same
    table as the other three.
    """
    shipped = f"{RETRIEVAL_MODE} lam={DEFAULT_LAM}"
    match = next((r for r in results if r[0] == shipped), None)
    if match is None:
        # Reachable when --modes or --lam excluded the shipped config. Say so rather than
        # reporting the first row, which would silently be a control.
        console.print(
            f"[yellow]{shipped!r} was not in this sweep -- no EVAL_RESULT emitted.[/]"
        )
        return 0

    _, outcomes = match
    passed, total = sum(o.soft_hit1 for o in outcomes), len(outcomes)
    console.print(f"\n[bold]Shipped config ({shipped}): {passed}/{total} soft hit@1[/]")
    print("EVAL_RESULT " + json.dumps({"passed": passed, "total": total}))

    if floor is not None and passed < floor:
        console.print(f"[red]below the --floor of {floor}[/]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
