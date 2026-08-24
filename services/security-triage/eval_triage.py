"""Golden-priority regression eval.

Catches one narrow thing worth catching: a prompt or schema change that silently
*downgrades* a finding that matters. Moving `priority` below `exploitability` and
`impact` inverted the judgments once, and nothing failed.

**Bands, not exact priorities.** Local Ollama is not reproducible even at
`temperature: 0`, so asserting `priority == "high"` would flap and get deleted. Each case
declares a floor, a ceiling, or both: an RCE CVE may be `high` or `critical`, never `low`.

**`needs_human` is graded by which bound the case carries.** A case with a `min` is one
this repo says is definitely serious, so declining it is a miss. A case with only a `max`
is noise, and declining it is conservative rather than wrong. So a model that declines
everything fails every serious case, which is the point.

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

# needs_human is absent: it is a declined judgment, not a fifth severity, and ranking it
# would let it satisfy a `max` bound by accident.
ORDER = ["low", "medium", "high", "critical"]

console = Console()


def load_cases(path: Path, findings_by_fingerprint: dict) -> list[dict]:
    """The eval set, checked against the corpus it claims to describe.

    A fingerprint is `(scanner, rule_id, target, line)` hashed, so a moved line or a renamed
    file changes it. Unchecked, that case silently stops being evaluated and the eval keeps
    passing while testing less than it claims.
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
        # triage.py drops unsent fingerprints, so a gap means the model answered about
        # fewer findings than it was given -- which a count alone calls a clean run.
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
        # A provider reporting no usage makes the cost figure a fiction, and a fiction
        # that looks like a number gets copied into a Readme.
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

    # Eval-set order, so batch composition is stable. The set is interleaved by band, so
    # a batch-level effect cannot be mistaken for a band-level one; --batch-size 1 is the
    # control.
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
