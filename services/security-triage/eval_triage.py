"""Golden-priority regression eval -- Day 28.

The thing this catches is narrow and worth catching: a prompt or schema change that
silently *downgrades* a finding that matters. Day 27 moved `priority` below
`exploitability` and `impact` in the schema and the judgments inverted, which was only
visible because a full corpus happened to get read that afternoon. Nothing would have
failed. This is that afternoon, in one command.

**Bands, not exact priorities**, and that is the central design decision. Local Ollama is
not reproducible even at `temperature: 0` -- Day 27 changed two things off eval movements
that turned out to be pure batch-dependent variance -- so an eval asserting
`priority == "high"` would flap, get ignored, and then get deleted. Each case instead
declares a floor (`min`), a ceiling (`max`), or both, and asserts only what is actually
defensible about that finding. A CVE with remote code execution may be `high` or
`critical` and both are correct answers; what is never correct is `low`.

**`needs_human` is graded by which bound the case carries**, and this follows from what
declining means rather than from where it would sit on a scale:

- A case with a `min` is one this repo says is definitely serious. Declining it is a
  miss -- the model had the context and did not use it.
- A case with only a `max` is noise. Declining it is conservative, not wrong: the finding
  reaches a human via `review_required` instead of being scored, which is the outcome the
  finding deserved anyway.

So a run where the model declines everything scores well on the noise cases and fails
every serious one, which is the right shape -- Day 23's 1.5b model, which declined all
five findings it was shown while satisfying every guard, would fail this eval outright.

    python eval_triage.py                    # the committed fixture, default batch size
    python eval_triage.py --batch-size 1     # one finding per call, no batch interaction
"""

import argparse
import json
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from provider import get_triage_provider
from scanners import dedupe, parse_envelope
from triage import BATCH_SIZE, triage_findings

EVAL_SET = Path(__file__).parent / "eval_set.json"
FIXTURE = Path(__file__).parent / "fixtures" / "this-repo.json"

# needs_human is deliberately absent: it is a declined judgment, not a fifth severity, and
# giving it a rank here would let it satisfy a `max` bound by accident.
ORDER = ["low", "medium", "high", "critical"]

console = Console()


def load_cases(path: Path, findings_by_fingerprint: dict) -> list[dict]:
    """The eval set, checked against the corpus it claims to describe.

    A fingerprint is `(scanner, rule_id, target, line)` hashed, so regenerating the
    fixture against a moved line or a renamed file changes it. Without this check that
    turns into a case that silently stops being evaluated -- the eval keeps passing while
    testing less than it says it does, which is worse than failing.
    """
    cases = json.loads(path.read_text(encoding="utf-8"))

    missing = [c for c in cases if c["fingerprint"] not in findings_by_fingerprint]
    if missing:
        console.print(
            f"[red]{len(missing)} eval case(s) name a fingerprint that is not in the "
            f"fixture[/red] -- the corpus moved under the eval set:"
        )
        for case in missing:
            console.print(f"  {case['fingerprint']}  {case['label']}")
        sys.exit(2)

    for case in cases:
        if "min" not in case and "max" not in case:
            console.print(f"[red]case {case['label']!r} asserts nothing[/red]")
            sys.exit(2)
    return cases


def grade(case: dict, priority: str | None) -> tuple[bool, str]:
    """(passed, why). `priority` is None when the model returned no result for it."""
    if priority is None:
        # triage.py drops fingerprints that were never sent, so a gap here means the
        # model answered about fewer findings than it was given -- Day 23's failure mode,
        # and one that a count of results alone reports as a clean run.
        return False, "no result returned"

    if priority == "needs_human":
        if "min" in case:
            return False, f"declined a finding that must be at least {case['min']}"
        return True, "declined, which is allowed for a noise case"

    rank = ORDER.index(priority)
    if "min" in case and rank < ORDER.index(case["min"]):
        return False, f"{priority} is below the {case['min']} floor"
    if "max" in case and rank > ORDER.index(case["max"]):
        return False, f"{priority} is above the {case['max']} ceiling"
    return True, "in band"


def band(case: dict) -> str:
    lo, hi = case.get("min"), case.get("max")
    if lo and hi:
        return f"{lo}..{hi}"
    return f">= {lo}" if lo else f"<= {hi}"


def report(rows: list[dict], elapsed: float, tokens: tuple[int, int]) -> bool:
    table = Table(title="Triage golden-priority eval")
    table.add_column("finding", style="cyan", no_wrap=False)
    table.add_column("band")
    table.add_column("got", style="magenta")
    table.add_column("conf", justify="right", style="dim")
    table.add_column("status", justify="center")

    for row in rows:
        mark = "[green]PASS[/green]" if row["passed"] else "[red]FAIL[/red]"
        table.add_row(
            row["case"]["label"],
            band(row["case"]),
            row["priority"] or "-",
            f"{row['confidence']:.2f}" if row["confidence"] is not None else "-",
            mark,
        )
    console.print(table)

    for row in rows:
        if not row["passed"]:
            console.print(f"[red]{row['case']['label']}[/red]: {row['why']}")
            console.print(f"    expected because: {row['case']['why']}")
        if row["explanation"]:
            console.print(f"[dim]{row['case']['label']}: {row['explanation']}[/dim]")

    passed = sum(r["passed"] for r in rows)
    prompt_tokens, output_tokens = tokens
    console.print(
        f"\n[bold]{passed}/{len(rows)} in band[/bold] -- {elapsed:.1f}s, "
        f"{prompt_tokens} prompt + {output_tokens} output tokens"
    )
    if prompt_tokens == 0:
        # Same loud line log-analyzer's harness carries: a provider reporting no usage
        # makes the cost figure above a fiction, and a fiction that looks like a number
        # is the kind that gets copied into a Readme.
        console.print(
            "[yellow]No token counts reported -- the cost figure above is not real.[/yellow]"
        )
    return passed == len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Day 28: golden-priority triage eval")
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help="findings per model call; 1 removes batch interaction from the result",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="evaluate only the first N cases, for a quick check against a slow backend",
    )
    args = parser.parse_args()

    envelope = json.loads(args.fixture.read_text(encoding="utf-8"))
    findings = {f.fingerprint: f for f in dedupe(parse_envelope(envelope))}
    cases = load_cases(EVAL_SET, findings)[: args.limit]

    provider = get_triage_provider()
    console.print(
        f"{len(cases)} cases against [bold]{provider.name}[/bold] "
        f"({provider.model_name}), batch size {args.batch_size}"
    )

    # Eval-set order, not fixture order, so which findings share a call is stable across
    # runs. Batch composition changes the prompt, and a shuffled corpus would show up as
    # model variance rather than as the eval's own doing.
    selected = [findings[case["fingerprint"]] for case in cases]

    before = provider.prompt_tokens, provider.output_tokens
    started = time.monotonic()
    results = triage_findings(selected, provider=provider, batch_size=args.batch_size)
    elapsed = time.monotonic() - started
    tokens = (provider.prompt_tokens - before[0], provider.output_tokens - before[1])

    by_fingerprint = {r.fingerprint: r for r in results}
    rows = []
    for case in cases:
        result = by_fingerprint.get(case["fingerprint"])
        passed, why = grade(case, result.priority if result else None)
        rows.append(
            {
                "case": case,
                "passed": passed,
                "why": why,
                "priority": result.priority if result else None,
                "confidence": result.confidence if result else None,
                "explanation": result.explanation if result else None,
            }
        )

    all_passed = report(rows, elapsed, tokens)

    # The one line eval_all.py at the repo root reads. Every eval in this repo ends with
    # it, so the cross-service table does not have to parse four different report formats.
    print(
        "EVAL_RESULT "
        + json.dumps({"passed": sum(r["passed"] for r in rows), "total": len(rows)})
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
