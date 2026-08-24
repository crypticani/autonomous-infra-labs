"""Golden-set eval for the diagnosis loop.

What is graded is **the proposed action, including when it should be none.** Not the
summary or the evidence -- grading prose needs a human or a second model. The proposed
action is the only output a human can click to make something happen in a cluster.

Two of the four cases expect `null`, which is the design: an OOMKill wants a higher
memory limit and no write tool can set one. An agent that always proposes something
scores 2/4, and so does one that never does -- passing means telling them apart.

The cluster is stubbed and the model is not. `agent._dispatch` becomes a lookup into
each case's `tool_outputs`. A live kind cluster wedged into four broken states is not
something anybody runs before a commit, and an eval nobody runs is not a gate.

    python eval/run_eval.py                # exits non-zero if any case fails
"""

import json
import os
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import guardrails  # noqa: E402
from provider import get_agent_provider  # noqa: E402

GOLDEN_SET = Path(__file__).parent / "golden_set.json"
console = Console()


def stub_dispatch(tool_outputs: dict):
    """`agent._dispatch`'s contract, served from a fixture."""

    def _dispatch(name: str, args: dict) -> dict:
        canned = tool_outputs.get(name)
        if canned is None:
            return {"error": f"{name} is unavailable in this environment"}
        # A case can script a failure by writing {"error": ...} directly.
        return canned if "error" in canned else {"output": canned}

    return _dispatch


def run_case(case: dict, provider) -> dict:
    prompt_before, output_before = provider.prompt_tokens, provider.output_tokens
    started = time.monotonic()

    agent._dispatch = stub_dispatch(case["tool_outputs"])
    try:
        diagnosis = agent.diagnose(case["alert"], provider)
        error = None
    except Exception as e:
        diagnosis, error = None, f"{type(e).__name__}: {e}"

    proposed = (diagnosis.proposed_action or {}) if diagnosis else {}
    actual = proposed.get("tool")
    expected = case["expected_action"]

    return {
        "actual": actual,
        # An incomplete diagnosis has no proposed action, so a case expecting null would
        # otherwise *pass* by the loop giving up -- the one way this grader could call a
        # total failure a success.
        "passed": (
            diagnosis is not None and not diagnosis.incomplete and actual == expected
        ),
        "incomplete": bool(diagnosis and diagnosis.incomplete),
        "confidence": diagnosis.confidence if diagnosis else None,
        "summary": diagnosis.summary if diagnosis else None,
        "evidence": len(diagnosis.evidence) if diagnosis else 0,
        "error": error,
        "elapsed": time.monotonic() - started,
        "prompt_tokens": provider.prompt_tokens - prompt_before,
        "output_tokens": provider.output_tokens - output_before,
    }


def report(results: list[dict]) -> bool:
    table = Table(title="Self-healing agent golden set -- proposed action")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Expected", style="cyan")
    table.add_column("Actual", style="magenta")
    table.add_column("Status", justify="center")
    table.add_column("Conf", justify="right", style="dim")
    table.add_column("Evid", justify="right", style="dim")
    table.add_column("Sec", justify="right", style="dim")
    table.add_column("Tok in", justify="right", style="dim")
    table.add_column("Tok out", justify="right", style="dim")

    for row in results:
        case, result = row["case"], row["result"]
        status = "[green]PASS[/green]" if result["passed"] else "[red]FAIL[/red]"
        if result["incomplete"]:
            status = "[red]INCOMPLETE[/red]"
        table.add_row(
            case["id"],
            case["expected_action"] or "none",
            result["actual"] or ("none" if not result["error"] else "ERROR"),
            status,
            f"{result['confidence']:.2f}" if result["confidence"] is not None else "-",
            str(result["evidence"]),
            f"{result['elapsed']:.1f}",
            str(result["prompt_tokens"]),
            str(result["output_tokens"]),
        )
    console.print(table)

    seconds = [r["result"]["elapsed"] for r in results]
    prompt_tokens = sum(r["result"]["prompt_tokens"] for r in results)
    output_tokens = sum(r["result"]["output_tokens"] for r in results)
    console.print(
        f"\n[bold]Cost:[/bold] {len(results)} diagnoses, {sum(seconds):.1f}s total, "
        f"{min(seconds):.1f}-{max(seconds):.1f}s each, "
        f"{prompt_tokens} prompt + {output_tokens} output tokens "
        f"({(prompt_tokens + output_tokens) / len(results):.0f} tokens/diagnosis avg)"
    )
    if prompt_tokens == 0:
        console.print(
            "[yellow]No token counts reported -- the cost figures above are not real.[/yellow]"
        )

    console.print("\n[bold]Manual review: what it concluded[/bold]")
    for row in results:
        case, result = row["case"], row["result"]
        if result["error"]:
            console.print(f"[red]{case['id']} Error:[/red] {result['error']}")
        else:
            console.print(f"[cyan]{case['id']}:[/cyan] {result['summary']}")
            if not result["passed"]:
                console.print(f"    [red]why it matters:[/red] {case['description']}")

    passed = sum(r["result"]["passed"] for r in results)
    console.print(f"\n[bold]Summary: {passed}/{len(results)} passed.[/bold]")
    return passed == len(results)


def main() -> int:
    cases = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
    provider = get_agent_provider()
    console.print(
        f"{len(cases)} cases against [bold]{provider.name}[/bold] "
        f"({provider.model_name}), cluster stubbed from the golden set"
    )

    # Each case is a full diagnosis of up to SHA_MAX_ITERATIONS turns, and the LLM budget
    # is a rolling window over the whole process -- four cases back to back can trip a
    # guardrail written for a service handling one alert at a time. Clearing between
    # cases keeps the eval measuring the model rather than the budget.
    results = []
    for case in cases:
        guardrails._llm_calls.clear()
        results.append({"case": case, "result": run_case(case, provider)})

    all_passed = report(results)

    print(
        "EVAL_RESULT "
        + json.dumps(
            {
                "passed": sum(r["result"]["passed"] for r in results),
                "total": len(results),
            }
        )
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    os.environ.setdefault("SHA_LLM_PROVIDER", "gemini")
    sys.exit(main())
