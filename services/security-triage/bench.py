"""Cost and latency per batch size.

**Per-call cost across batch sizes, not a matched race between them.** Each config
triages `batch_size * calls` findings off the head of the corpus, so the wall clocks are
not "time to triage the corpus". What decides the default is the per-call token split:
the system prompt is charged once per call whatever rides along, so prompt tokens per
finding falls as the batch grows.

The quality columns are why this is not a throughput benchmark: the pipeline can satisfy
every guard and still be worthless.

    python bench.py                          # 1,3,5,10 against ST_LLM_PROVIDER
    python bench.py --batches 5 --calls 2    # a second sample at one size
"""

import argparse
import json
import time

from errors import TriageProviderError
from provider import BaseTriageProvider, get_triage_provider
from scanners import dedupe, parse_envelope
from triage import EXPLANATION_MAX, TriageResult, triage_batch

# Tokens and calls per 1,000 findings, not dollars: both backends are free at the margin,
# and what is scarce differs -- CPU-minutes for Ollama, request ceilings for Gemini's free
# tier, which charge per call however many findings rode along. A dollar column would read
# $0.00 for every row. Multiply the token columns by your own rate on a paid tier.


def _contradictions(results: list[TriageResult]) -> int:
    """Results whose explanation asserts a severity the same result rated low.

    A model can contradict its own structured fields with every guard passing, because
    each field is individually valid. Negation is handled: without it, all 4 reported
    contradictions were "not easily exploitable" on an `exploitability: low` finding.

    ponytail: a keyword match, so paraphrases slip through. Upgrade to an LLM judge if the
    count sits at zero while the output still reads wrong.
    """
    count = 0
    for result in results:
        lowered = result.explanation.lower()
        if result.impact == "low" and (
            "impact is high" in lowered or "high impact" in lowered
        ):
            count += 1
        elif result.exploitability == "low" and (
            "easily exploit" in lowered and "not easily exploit" not in lowered
        ):
            count += 1
    return count


_RANK = {"low": 1, "medium": 2, "high": 3}


def _priority_mismatches(results: list[TriageResult]) -> int:
    """Results whose `priority` does not follow from their own exploitability and impact.

    `priority` used to be declared first, so it was generated before either rating existed
    and came out anti-correlated with them. Reordering the schema is the fix.
    `needs_human` is exempt -- a refusal is not a severity.

    ponytail: a coarse band check. It flags only the unarguable cases and says nothing about
    defensible middle calls; a tighter rule needs a severity matrix somebody agrees with.
    """
    count = 0
    for result in results:
        if result.priority == "needs_human":
            continue
        score = _RANK[result.exploitability] + _RANK[result.impact]
        if score >= 5 and result.priority == "low":
            count += 1
        elif score <= 3 and result.priority in ("high", "critical"):
            count += 1
    return count


def run_config(
    provider: BaseTriageProvider, findings: list, batch_size: int, calls: int
) -> dict:
    """One config: `calls` model calls of `batch_size` findings each."""
    sent = findings[: batch_size * calls]
    # The provider's counters are cumulative and shared across configs in this process,
    # so a delta is the only correct read.
    prompt_before, output_before = provider.prompt_tokens, provider.output_tokens

    results: list[TriageResult] = []
    started = time.monotonic()
    for start in range(0, len(sent), batch_size):
        results.extend(triage_batch(provider, sent[start : start + batch_size]))
    elapsed = time.monotonic() - started

    prompt_tokens = provider.prompt_tokens - prompt_before
    output_tokens = provider.output_tokens - output_before
    explanations = [len(r.explanation) for r in results]
    return {
        "batch_size": batch_size,
        "calls": len(range(0, len(sent), batch_size)),
        "sent": len(sent),
        "returned": len(results),
        "wall_s": elapsed,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        # What the batching argument rests on: the system prompt is charged once per
        # call, so this should fall as the batch grows. If it does not, batching buys
        # nothing.
        "prompt_per_finding": prompt_tokens / len(sent) if sent else 0,
        "findings_per_min": len(results) / elapsed * 60 if elapsed else 0,
        # The two scarce units. Tokens extrapolate the whole-corpus figure; calls is
        # 1000/batch_size, so it halves every time the batch doubles.
        "tokens_per_1k": (
            (prompt_tokens + output_tokens) / len(sent) * 1000 if sent else 0
        ),
        "calls_per_1k": 1000 / batch_size,
        "needs_human": sum(1 for r in results if r.priority == "needs_human"),
        "expl_max": max(explanations, default=0),
        # Explanations exactly on the cap: the grammar closed the string rather than the
        # model finishing its sentence, which is visible truncation in a PR comment.
        "truncated": sum(1 for n in explanations if n >= EXPLANATION_MAX),
        "contradictions": _contradictions(results),
        "priority_mismatches": _priority_mismatches(results),
        # For --show. Counting proves the pipeline ran; only reading tells a real triage
        # from five valid-shaped refusals.
        "results": results,
    }


def _fmt(row: dict) -> str:
    return (
        f"{row['batch_size']:>5} {row['calls']:>5} {row['wall_s']:>8.1f} "
        f"{row['prompt_tokens']:>8} {row['output_tokens']:>8} "
        f"{row['prompt_per_finding']:>9.1f} {row['findings_per_min']:>8.2f} "
        f"{row['returned']}/{row['sent']:<8} {row['needs_human']:>6} "
        f"{row['expl_max']:>6} {row['truncated']:>5} {row['contradictions']:>6} "
        f"{row['priority_mismatches']:>7} "
        f"{row['tokens_per_1k']:>10.0f} {row['calls_per_1k']:>8.0f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default="fixtures/this-repo.json")
    parser.add_argument(
        "--batches",
        default="1,3,5,10",
        help="comma-separated batch sizes to measure",
    )
    parser.add_argument(
        "--calls",
        type=int,
        default=1,
        help="model calls per config. 1 fits the Ollama budget; raise it for Gemini.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help=(
            "skip this many findings before slicing. Use it for a second sample: a "
            "repeat at offset 0 re-sends a byte-identical prompt, which Ollama's KV "
            "cache can serve far faster than the first time, so it would measure the "
            "cache rather than the batch size."
        ),
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help=(
            "print every judgment, not just the counts. A row of clean numbers hiding "
            "five identical refusals is the exact failure Day 23 shipped."
        ),
    )
    args = parser.parse_args()
    batch_sizes = [int(b) for b in args.batches.split(",")]

    with open(args.fixture) as f:
        findings = dedupe(parse_envelope(json.load(f)))[args.offset :]

    provider = get_triage_provider()
    print(f"provider: {provider.name} ({provider.model_name})")
    print(
        f"corpus:   {len(findings)} deduped findings from {args.fixture}"
        + (f", from offset {args.offset}" if args.offset else "")
    )
    print("cost in:  tokens and calls per 1,000 findings -- both backends free at the")
    print(
        "          margin, so requests against a quota is the scarce unit, not dollars"
    )
    print(f"explanation cap: {EXPLANATION_MAX} chars")
    print()
    print(
        f"{'batch':>5} {'calls':>5} {'wall_s':>8} {'p_tok':>8} {'o_tok':>8} "
        f"{'p_tok/f':>9} {'find/min':>8} {'ret/sent':<8} {'nhuman':>6} "
        f"{'explmx':>6} {'trunc':>5} {'contra':>6} {'primis':>7} "
        f"{'tok/1k':>10} {'call/1k':>8}"
    )

    rows = []
    for batch_size in batch_sizes:
        try:
            row = run_config(provider, findings, batch_size, args.calls)
        except TriageProviderError as e:
            # A batch too large to answer inside LLM_TIMEOUT is a *result*: this size is
            # not available on this hardware. Letting the traceback through threw away
            # three good rows and the summary along with them.
            print(f"{batch_size:>5}  -- {e} (status {e.status})")
            continue
        rows.append(row)
        # Printed as each config lands: a sweep runs for over ten minutes, and a run
        # that dies on the last config should still leave its earlier numbers on screen.
        print(_fmt(row))
        if args.show:
            for result in row["results"]:
                print(
                    f"      {result.priority:<11} expl={result.exploitability:<6} "
                    f"imp={result.impact:<6} conf {result.confidence:.2f}  "
                    f"[{len(result.explanation)} ch] {result.explanation}"
                )

    dropped = [r for r in rows if r["returned"] != r["sent"]]
    if dropped:
        print()
        for row in dropped:
            print(
                f"  batch {row['batch_size']}: returned "
                f"{row['returned']}/{row['sent']} -- the model dropped findings at this "
                f"size, which is a reason not to raise the default here"
            )


if __name__ == "__main__":
    main()
