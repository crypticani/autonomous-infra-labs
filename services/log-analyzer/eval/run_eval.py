import os
import sys
import json
import logging
from typing import List, Dict, Any

from rich.console import Console
from rich.table import Table

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from log_analyzer import (
        ANALYSIS_SYSTEM_PROMPT,
        provider_type,
        BaseLLMProvider,
        OllamaProvider,
        GeminiProvider,
    )
except ImportError as e:
    print(
        f"Failed to import providers. Ensure this script is run from inside the log analyzer directory: {e}"
    )
    sys.exit(1)

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)
console = Console()


def load_cases(filepath: str) -> List[Dict[str, Any]]:
    """Loads the golden set JSON file."""
    with open(filepath, "r") as f:
        return json.load(f)


def run_case(provider: BaseLLMProvider, case: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes a single test case.
    Returns the parsed actual severity, pass/fail status, and raw analysis.
    """
    expected = case.get("expected_severity")
    raw_log = case.get("raw_log")

    try:
        analysis = provider.generate(
            system_prompt=ANALYSIS_SYSTEM_PROMPT,
            user_prompt=f"RAW LOG:\n{raw_log}",
            temperature=0.0,
        )

        actual = analysis.severity
        passed = actual.upper() == expected.upper()

        return {
            "actual": actual,
            "passed": passed,
            "confidence": analysis.confidence,
            "likely_cause": analysis.likely_cause,
            "suggested_fix": analysis.suggested_fix,
            "error": None,
        }
    except Exception as e:
        return {
            "actual": "PARSE_ERROR",
            "passed": False,
            "confidence": 0.0,
            "likely_cause": "N/A",
            "suggested_fix": "N/A",
            "error": str(e),
        }


def print_report(results: List[Dict[str, Any]]) -> bool:
    """Renders the Rich table and returns True if all passed, False otherwise."""
    print(f"\n=== EVALUATION REPORT (Provider: {provider_type.upper()}) ===")
    table = Table(title="Golden Set Regression Evaluation")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Expected", style="cyan")
    table.add_column("Actual", style="magenta")
    table.add_column("Status", justify="center")
    table.add_column("Conf", justify="right")

    total = len(results)
    passed_count = 0

    for r in results:
        case_id = r["case"]["id"]
        expected = r["case"]["expected_severity"]
        actual = r["result"]["actual"]
        passed = r["result"]["passed"]
        confidence = r["result"]["confidence"]

        if passed:
            status_text = "[green]PASS[/green]"
            passed_count += 1
        else:
            status_text = "[red]FAIL[/red]"

        table.add_row(case_id, expected, actual, status_text, f"{confidence:.2f}")

    console.print(table)

    console.print("\n[bold]Manual Review: Generative Fields[/bold]")
    for r in results:
        if r["result"]["error"]:
            console.print(f"[red]{r['case']['id']} Error:[/red] {r['result']['error']}")
        else:
            console.print(
                f"[cyan]{r['case']['id']} Error:[/cyan] Cause: {r['result']['likely_cause']}"
            )
            console.print(f"    Fix:  {r['result']['suggested_fix']}")

    console.print(f"\n[bold]Summary: {passed_count}/{total} passed.[/bold]")
    return passed_count == total


if __name__ == "__main__":
    provider_type = os.getenv("LLM_PROVIDER", "ollama").lower()
    console.print(
        f"Running eval harness against provider: [bold]{provider_type}[/bold]"
    )

    if provider_type == "ollama":
        provider = OllamaProvider()
    elif provider_type == "gemini":
        provider = GeminiProvider()
    else:
        console.print(f"[red]Unsupported provider: {provider_type}[/red]")
        sys.exit(1)

    eval_file = os.path.join(os.path.dirname(__file__), "golden_set.json")
    cases = load_cases(eval_file)

    results = []
    for case in cases:
        result = run_case(provider, case)
        results.append({"case": case, "result": result})

    all_passed = print_report(results)

    if not all_passed:
        sys.exit(1)
    sys.exit(0)
