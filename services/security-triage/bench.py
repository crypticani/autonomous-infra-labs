"""Cost and latency per batch size -- Day 27.

Sets ST_BATCH_SIZE from a measurement instead of Day 23's guess of 5.

**This measures per-call cost across batch sizes, not a matched race between them.** Each
config triages `batch_size * calls` findings off the head of the corpus, so batch 1 and
batch 10 are not judging the same findings, and the wall clocks are not directly
comparable as "time to triage the corpus". That is deliberate, not a shortcut around a
better design: the Ollama budget is 15 minutes for this service and a matched comparison
at batch 1 over ten findings is ten CPU calls, ~20 minutes on its own. What is comparable,
and what actually decides the default, is the per-call token split -- the system prompt is
charged once per call whatever rides along with it, so prompt tokens per finding falls as
the batch grows and the whole-corpus figure follows from that arithmetic rather than from
sitting through it. Wall clock is here to check that model against reality, not to be it.

The quality columns are the reason this is not a throughput benchmark. Day 23 established
that this pipeline can satisfy every guard and still be worthless -- five byte-identical
explanations, needs_human on everything, all counts green. Batch size has the same failure
surface: if 10 findings per call means the model starts dropping fingerprints or declining
more of them, that is the reason not to raise the default, and findings-per-minute alone
would have said the opposite. Cheapest batch and best batch are different questions.

    python bench.py                          # 1,3,5,10 against ST_LLM_PROVIDER
    python bench.py --batches 5 --calls 2    # a second sample at one size
    python bench.py --batches 5 --calls 112  # the full-corpus run (Gemini only)
"""

import argparse
import json
import time

from errors import TriageProviderError
from provider import BaseTriageProvider, get_triage_provider
from scanners import dedupe, parse_envelope
from triage import EXPLANATION_MAX, TriageResult, triage_batch

# Cost is reported in tokens and calls per 1,000 findings, not dollars, and that is the
# honest unit rather than a missing feature. Both backends here are free at the margin:
# Ollama is local, and Gemini runs on the free tier. What is actually scarce is different
# for each -- laptop CPU-minutes for Ollama, and for Gemini the free tier's per-minute and
# per-day *request* ceilings, which charge per call regardless of how many findings rode
# along in it. Requests per 1,000 findings is therefore the Gemini cost model, and it is
# exactly what batch size buys down. A dollar column would read $0.00 for every row here
# and teach nothing; anyone moving this to a paid tier can multiply the token columns by
# their own rate, which will not be the rate that was current today anyway.


def _contradictions(results: list[TriageResult]) -> int:
    """Results whose explanation asserts a severity the same result rated low.

    The Day 26 bug, counted: explanations announcing "the impact is high" on findings the
    model itself had just rated impact low. A model can contradict its own structured
    fields and every guard still passes, because each field is individually valid.

    Negation is handled, because not handling it was worse than the paraphrases this
    misses. The full-corpus run reported 4 contradictions and all 4 were the phrase "not
    easily exploitable" on an `exploitability: low` finding -- which *agrees* with the
    rating. A checker that invents violations is worse than one that misses them: it sends
    you looking for a bug in the model when the bug is in the checker.

    ponytail: still a keyword match, so paraphrases slip through. Upgrade to an LLM-judge
    in eval_triage.py if the count sits at zero while the output still reads wrong.
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

    The Day 27 bug, and the reason it needs a counter rather than an eyeball: `priority`
    used to be the first field in TriageResult, so it was generated before either rating
    existed and came out anti-correlated with them -- `expl=medium imp=high` scoring
    `low`, `expl=low imp=medium` scoring `high`. Reordering the schema is the fix; this is
    how we tell whether it worked.

    `needs_human` is exempt. It is a refusal, not a severity, so it is not required to
    follow from ratings the model has just said it cannot confidently apply.

    ponytail: a coarse band check, deliberately. It flags only the unarguable cases -- a
    combined rating of 5+ called low, or 3 or less called high/critical -- and says nothing
    about the many defensible middle calls. A tighter rule would need a severity matrix
    somebody has to agree with, and this is enough to catch a field that is not reading
    its own inputs.
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
    # Snapshot, run, subtract. The provider's counters are cumulative and shared across
    # configs in this same process, so a delta is the only correct read.
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
        # The number the batching argument rests on: the system prompt is charged once
        # per call, so this should fall as the batch grows. If it does not, batching is
        # buying nothing and the default should stay small.
        "prompt_per_finding": prompt_tokens / len(sent) if sent else 0,
        "findings_per_min": len(results) / elapsed * 60 if elapsed else 0,
        # Cost per 1,000 findings, in the two units that are actually scarce. Tokens
        # extrapolate the whole-corpus figure; calls is what a free-tier request quota
        # charges, and it is 1000/batch_size, so it halves every time the batch doubles.
        "tokens_per_1k": (
            (prompt_tokens + output_tokens) / len(sent) * 1000 if sent else 0
        ),
        "calls_per_1k": 1000 / batch_size,
        "needs_human": sum(1 for r in results if r.priority == "needs_human"),
        "expl_max": max(explanations, default=0),
        # Explanations sitting exactly on the cap, which means the grammar closed the
        # string rather than the model finishing its sentence. The full-corpus run ended
        # three of them mid-clause, trailing comma included -- visible truncation in what
        # goes into a PR comment, so it needs counting and not just a max.
        "truncated": sum(1 for n in explanations if n >= EXPLANATION_MAX),
        "contradictions": _contradictions(results),
        "priority_mismatches": _priority_mismatches(results),
        # The judgments themselves, for --show. Counting them proves the pipeline ran;
        # only reading them tells a real triage apart from five valid-shaped refusals,
        # which is the distinction every count in this dict is blind to.
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
            # A batch size too large to answer inside LLM_TIMEOUT is a *result*, not a
            # crashed benchmark -- it is the measurement that says this size is not
            # available on this hardware. Measured 2026-08-22: batch 10 timed out at 300s
            # while batch 5 finished in 158.7s, because wall clock here is essentially
            # output-bound (~78 output tokens per finding at ~2.5-3 tok/s on CPU) and 10
            # findings' worth of generation does not fit. Letting the traceback through
            # threw away three good rows and the summary below along with them.
            print(f"{batch_size:>5}  -- {e} (status {e.status})")
            continue
        rows.append(row)
        # Printed as each config lands, not collected and printed at the end: a CPU sweep
        # runs for over ten minutes and a run that dies on the last config should still
        # have left its earlier numbers on screen.
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
