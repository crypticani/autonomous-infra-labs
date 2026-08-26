"""Golden-route regression eval, and the measurement that picks the router's model.

Two degenerate routers exist and both are easy to ship by accident: one that names a
service for everything, and one that declines everything. The eval set is built so each
fails about half of it. A case with a named `expect` is one this repo says is definitely
that service's, so declining it is a miss; a case with `expect: null` is one nothing should
be confident about, so naming a service is a miss. A score is only meaningful because both
mistakes cost the same here.

Some cases accept a list, which is the same concession `eval_triage.py` makes with bands:
local Ollama is not reproducible even at `temperature: 0`, and a question that genuinely
fits two services should not flap the score. A list including `null` means declining is
also defensible.

    python eval_router.py                                  # the shipped provider and model
    python eval_router.py --model qwen2.5-coder:1.5b        # the cheap candidate
    python eval_router.py --provider gemini
    python eval_router.py --limit 5                         # smoke test before the full run

The two numbers under the table are the ones that decide anything: how many definite cases
the router declined, and how many vague ones it answered anyway.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from statistics import median

from rich.console import Console
from rich.table import Table

EVAL_SET = Path(__file__).parent / "eval_set.json"

console = Console()


def load_cases(path: Path) -> list[dict]:
    """The eval set, checked against the service names that actually exist.

    A case naming a retired service would otherwise fail forever for a reason that looks
    like a model regression.
    """
    import backends

    cases = json.loads(path.read_text(encoding="utf-8"))
    known = set(backends.NAMES) | {None}

    for case in cases:
        expected = case["expect"]
        allowed = expected if isinstance(expected, list) else [expected]
        unknown = [name for name in allowed if name not in known]
        if unknown:
            console.print(
                f"[red]case {case['label']!r} expects {unknown}, which is not a "
                f"service in backends.py[/red]"
            )
            sys.exit(2)
    return cases


def grade(case: dict, routed: str | None) -> tuple[bool, str]:
    """`routed` is what the gateway would act on -- after the confidence floor, not before.

    Grading the model's raw pick would score a router the gateway then overrides, which is
    not the thing anybody uses.
    """
    expected = case["expect"]
    allowed = expected if isinstance(expected, list) else [expected]

    if routed in allowed:
        return True, ""
    if expected is None:
        return False, f"answered {routed} where nothing was confident enough"
    if routed is None:
        return False, f"declined a definite {expected} question"
    return False, f"wanted {expected}"


def _words(row: dict) -> int:
    return len((row["reason"] or "").split())


def _name(expected) -> str:
    """`null` printed as the decline it means, rather than as Python's None."""
    if isinstance(expected, list):
        return " | ".join(_name(e) for e in expected)
    return "(decline)" if expected is None else expected


def report(rows: list[dict], elapsed: float, tokens: tuple[int, int]) -> bool:
    table = Table(title="Router eval")
    table.add_column("case", style="cyan", max_width=40)
    table.add_column("expected")
    table.add_column("routed")
    table.add_column("conf", justify="right", style="dim")
    table.add_column("s", justify="right", style="dim")
    # Clipped rather than wrapped: an unreachable backend puts a whole connection-pool
    # traceback in this cell, and wrapping it turns one row into fifteen. The full text
    # still prints under the table.
    table.add_column(
        "why", max_width=30, style="dim", no_wrap=True, overflow="ellipsis"
    )
    table.add_column("", justify="center")

    for row in rows:
        confidence = row["confidence"]
        table.add_row(
            row["case"]["label"],
            _name(row["case"]["expect"]),
            "[red](error)[/red]" if row["error"] else _name(row["routed"]),
            f"{confidence:.2f}" if confidence is not None else "-",
            f"{row['seconds']:.1f}",
            row["why"] or "",
            "[green]ok[/green]" if row["passed"] else "[red]miss[/red]",
        )
    console.print(table)

    passed = sum(r["passed"] for r in rows)
    errored = [r for r in rows if r["error"]]

    # The two numbers that actually decide anything. A router is useful only if both stay
    # low: one counts the questions it should have placed and refused to, the other counts
    # the ones it had no business being sure about. Optimising either alone is trivial and
    # produces a router nobody would ship.
    #
    # Errored cases are excluded from both. A case the model never answered is not evidence
    # about how the model answers, and counting it as a decline is how an outage gets
    # written down as a finding.
    graded = [r for r in rows if not r["error"]]
    definite = [r for r in graded if r["case"]["expect"] is not None]
    vague = [r for r in graded if r["case"]["expect"] is None]
    over_declined = sum(1 for r in definite if r["routed"] is None)
    over_confident = sum(1 for r in vague if r["routed"] is not None)

    console.print(
        f"\n[bold]{passed}/{len(rows)} routed as expected[/bold]  "
        f"declined a definite question: {over_declined}/{len(definite)}  "
        f"answered a vague one: {over_confident}/{len(vague)}"
    )
    if errored:
        console.print(
            f"[red]{len(errored)} case(s) never reached the model[/red] -- the score "
            f"below is not a measurement of routing: {errored[0]['why']}"
        )

    # Latency per route is the number a human feels, and the mean is the honest form of it
    # here: every case is one call on the same prompt, so there is no long tail to hide.
    seconds = [r["seconds"] for r in rows]
    console.print(
        f"{elapsed:.0f}s total, {sum(seconds) / len(seconds):.1f}s per route, "
        f"{tokens[0]} prompt + {tokens[1]} output tokens "
        f"({sum(tokens) / len(rows):.0f} per route)"
    )

    # `reason` is generated before `service` so the classification is conditioned on
    # stated reasoning rather than justifying a pick already made. That only works if the
    # reason is actually reasoning -- and the first measured run had five of six misses
    # explained by three words or fewer ("OOMKilled", "scaling question", "operational
    # question"). Whether the passes were any better was unanswerable, because this report
    # printed reasons for misses only. Hence both numbers, and every reason below.
    if graded:
        hit_words = [_words(r) for r in graded if r["passed"]]
        miss_words = [_words(r) for r in graded if not r["passed"]]
        console.print(
            f"median reason: {median(hit_words) if hit_words else 0:.0f} words when "
            f"right, {median(miss_words) if miss_words else 0:.0f} when wrong"
        )

    # A router that picked wrong for a stated reason is a prompt problem; one that picked
    # wrong for an unrelated reason is a model problem. Only the sentence tells them apart,
    # and only having the right ones to compare against makes it readable.
    for row in rows:
        if row["reason"]:
            mark = "[green]ok  [/green]" if row["passed"] else "[red]miss[/red]"
            console.print(
                f"  {mark} [dim]{row['case']['label']}:[/dim] {row['reason']}"
            )

    return passed == len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Day 30: grade the intent router")
    parser.add_argument("--provider", help="override GW_LLM_PROVIDER for this run")
    parser.add_argument("--model", help="override the provider's model for this run")
    parser.add_argument(
        "--limit", type=int, help="run only the first N cases, for a smoke test"
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="skip the throwaway first call; the cold model load then lands on case one",
    )
    args = parser.parse_args()

    # Set before importing provider: both are read in a constructor, and
    # get_router_provider is lru_cached so there is no second chance.
    if args.provider:
        os.environ["GW_LLM_PROVIDER"] = args.provider
    if args.model:
        provider_type = os.getenv("GW_LLM_PROVIDER", "ollama").lower()
        os.environ[
            "GW_GEMINI_MODEL" if provider_type == "gemini" else "GW_OLLAMA_MODEL"
        ] = args.model

    import router
    from provider import get_router_provider

    cases = load_cases(EVAL_SET)
    if args.limit:
        cases = cases[: args.limit]

    provider = get_router_provider()
    console.print(
        f"{len(cases)} cases against [bold]{provider.name}[/bold] "
        f"({provider.model_name}), acting on {router.ROUTE_ON} confidence and up"
    )

    # One throwaway call before the clock starts, because Ollama loads the model on first
    # use and that load lands on whichever case happens to be first. Measured: 17.3s
    # against a ~9s median on 7b, 27.6s against ~5s on 1.5b -- and on a 5-case run it
    # exceeded GW_LLM_TIMEOUT outright, so case one was reported as an error that had
    # nothing to do with routing. Failures here are swallowed: if the backend is genuinely
    # down, all 24 cases are about to say so with their own error text.
    if not args.no_warmup:
        console.print("[dim]warming the model (not counted)...[/dim]")
        try:
            router.classify("what is our documented escalation path")
        except Exception as e:
            console.print(f"[yellow]warm-up call failed: {e}[/yellow]")

    before = provider.prompt_tokens, provider.output_tokens
    started = time.monotonic()

    rows = []
    for case in cases:
        case_started = time.monotonic()
        try:
            route = router.classify(case["question"], case.get("attachment", ""))
            routed, _ = router.decision(route)
            confidence, reason, error = route.confidence, route.reason, None
        except Exception as e:
            # One unreachable backend should not throw away the cases that did run: a
            # partial score with the failures visible is worth more than a traceback.
            routed = confidence = reason = None
            error = f"{type(e).__name__}: {e}"

        passed, why = (False, error) if error else grade(case, routed)
        rows.append(
            {
                "case": case,
                "passed": passed,
                "why": why,
                "routed": routed,
                # Kept apart from `routed is None`, which also means "declined". Without
                # this, a run against a dead backend prints a table of declines and a
                # summary blaming the model for an outage.
                "error": error,
                "confidence": confidence,
                "reason": reason,
                "seconds": time.monotonic() - case_started,
            }
        )

    elapsed = time.monotonic() - started
    tokens = (provider.prompt_tokens - before[0], provider.output_tokens - before[1])

    all_passed = report(rows, elapsed, tokens)

    # The one line eval_all.py at the repo root reads. Every eval in this repo ends with
    # it, so the cross-service table does not have to parse five report formats.
    print(
        "EVAL_RESULT "
        + json.dumps({"passed": sum(r["passed"] for r in rows), "total": len(rows)})
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
