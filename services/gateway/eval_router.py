"""Golden-route regression eval, and the measurement that picks the router's model.

The set is built so both degenerate routers score badly: a named `expect` makes declining a
miss, `expect: null` makes naming a service a miss. A list accepts any of its members, the
same concession eval_triage.py makes with bands. See Readme.md.

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

    A case naming a retired service would otherwise look like a model regression.
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
    """`routed` is post-floor: grading the raw pick would score a decision the gateway
    then overrides."""
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
    # Clipped, not wrapped: a connection traceback here turns one row into fifteen.
    table.add_column(
        "why", max_width=30, style="dim", no_wrap=True, overflow="ellipsis"
    )
    table.add_column("", justify="center")

    for row in rows:
        confidence = row["confidence"]
        if row["error"]:
            got = "[red](error)[/red]"
        elif row["declined_by"] == "floor":
            got = "(floor)"
        elif row["declined_by"] == "none":
            got = "(none)"
        else:
            got = _name(row["routed"])
        table.add_row(
            row["case"]["label"],
            _name(row["case"]["expect"]),
            got,
            confidence or "-",
            f"{row['seconds']:.1f}",
            row["why"] or "",
            "[green]ok[/green]" if row["passed"] else "[red]miss[/red]",
        )
    console.print(table)

    passed = sum(r["passed"] for r in rows)
    errored = [r for r in rows if r["error"]]

    # Both have to stay low; optimising either alone produces a router nobody would ship.
    # Errored cases are excluded -- counting them as declines writes an outage down as a
    # finding.
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

    # Only floor declines respond to GW_ROUTE_ON, so the split says whether the bar earns
    # its place.
    by_model = sum(1 for r in graded if r["declined_by"] == "none")
    by_floor = sum(1 for r in graded if r["declined_by"] == "floor")
    if by_model or by_floor:
        # Local, like main()'s: --model must reach the env before provider.py is read.
        import router

        console.print(
            f"declines: {by_model} the model's own, {by_floor} the "
            f"{router.ROUTE_ON} floor's"
        )
    if errored:
        console.print(
            f"[red]{len(errored)} case(s) never reached the model[/red] -- the score "
            f"below is not a measurement of routing: {errored[0]['why']}"
        )

    # The mean is honest here: every case is one call on the same prompt.
    seconds = [r["seconds"] for r in rows]
    console.print(
        f"{elapsed:.0f}s total, {sum(seconds) / len(seconds):.1f}s per route, "
        f"{tokens[0]} prompt + {tokens[1]} output tokens "
        f"({sum(tokens) / len(rows):.0f} per route)"
    )

    # The field-order trick only works if the reason is actually reasoning, and a
    # degenerate two-word label is not. Hence both medians, and every reason below.
    if graded:
        hit_words = [_words(r) for r in graded if r["passed"]]
        miss_words = [_words(r) for r in graded if not r["passed"]]
        console.print(
            f"median reason: {median(hit_words) if hit_words else 0:.0f} words when "
            f"right, {median(miss_words) if miss_words else 0:.0f} when wrong"
        )

    # Wrong for a stated reason is a prompt problem; wrong for an unrelated one is a model
    # problem. Only the sentence tells them apart.
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

    # Before importing provider: get_router_provider is lru_cached.
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

    # Ollama loads the model on first use, and that load otherwise lands on case one --
    # once badly enough to blow through GW_LLM_TIMEOUT. Failures here are swallowed: the
    # real cases are about to report the same outage themselves.
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
        declined_by = None
        try:
            route = router.classify(case["question"], case.get("attachment", ""))
            routed, _ = router.decision(route)
            confidence, reason, error = route.confidence, route.reason, None
            if routed is None:
                # Which of the two declines it was; only the floor one responds to
                # GW_ROUTE_ON.
                declined_by = "none" if route.service == router.NONE else "floor"
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
                # Apart from `routed is None`, which also means declined.
                "error": error,
                "declined_by": declined_by,
                "confidence": confidence,
                "reason": reason,
                "seconds": time.monotonic() - case_started,
            }
        )

    elapsed = time.monotonic() - started
    tokens = (provider.prompt_tokens - before[0], provider.output_tokens - before[1])

    all_passed = report(rows, elapsed, tokens)

    # The one line eval_all.py reads; every eval in this repo ends with it.
    print(
        "EVAL_RESULT "
        + json.dumps({"passed": sum(r["passed"] for r in rows), "total": len(rows)})
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
