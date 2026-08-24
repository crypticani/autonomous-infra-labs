"""Every service's eval, one command, one table -- Day 28.

The claim this repo makes at the end of thirty days is that four AI services are
production-ready. "Production-ready" for an AI system means something specific and
uncomfortable: the tests can be green while the model has quietly got worse, because
nothing in a unit test ever calls a model. So each service carries an eval that does, and
this runs all four:

    python eval_all.py                          # everything
    python eval_all.py security-triage          # one service
    python eval_all.py --list

Each eval is a subprocess in its own directory, not an import. They have different
dependencies, different providers and different module names that would collide in one
process (`app`, `provider` and `errors` exist three times over in this repo), and a
subprocess is the only boundary that makes "run this service's eval" mean the same thing
here as it does when you run it by hand.

The contract between them is one line. Each eval prints `EVAL_RESULT {"passed": n,
"total": n}` as its last act, and its exit status is the verdict. Both are reported here,
because they answer different questions: the counts say how much is right, the exit
status says whether the service considers that a pass. An eval that prints no
EVAL_RESULT line still shows up with its exit status and a `-` -- degrading, rather than
crashing the runner over a format change in one service.

**This needs the backends up.** Three of the four evals call a real model, and the fourth
needs a populated Chroma index. That is the point rather than a defect -- an eval you can
run without the thing it evaluates is testing something else -- but it does mean a run
here is minutes, not seconds, and that the Ollama host has to be awake.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).parent
console = Console()

RESULT_LINE = re.compile(r"^EVAL_RESULT (\{.*\})$", re.MULTILINE)


@dataclass(frozen=True)
class Eval:
    service: str
    command: list[str]
    measures: str
    backend: str


# One row per service, and the `measures` column is not decoration: the four evals grade
# genuinely different things, and a table that showed only pass counts would imply they
# are comparable. They are not -- 12/12 on triage bands and 10/12 on retrieval hit@1 are
# not the same kind of number.
EVALS = [
    Eval(
        service="log-analyzer",
        command=[sys.executable, "eval/run_eval.py"],
        measures="severity vs golden set",
        backend="ollama",
    ),
    Eval(
        service="knowledge-copilot",
        # No --floor: this one reports rather than gates until a baseline is on record.
        # See report_shipped_config() in eval_retrieval.py for why that is deliberate.
        command=[sys.executable, "eval_retrieval.py"],
        measures="soft hit@1, shipped config",
        backend="ollama (embeddings) + chroma",
    ),
    Eval(
        service="self-healing-agent",
        command=[sys.executable, "eval/run_eval.py"],
        measures="proposed action, incl. none",
        backend="gemini",
    ),
    Eval(
        service="security-triage",
        command=[sys.executable, "eval_triage.py"],
        measures="priority bands",
        backend="ollama",
    ),
]


def run(spec: Eval, verbose: bool) -> dict:
    """One eval, in its own service directory.

    Output is captured rather than streamed by default, because four rich tables
    interleaved with a fifth is unreadable -- but a failing eval prints its whole output
    below the table, since "3/4 passed" without the failing case is not a result anybody
    can act on.
    """
    workdir = ROOT / "services" / spec.service
    started = time.monotonic()
    try:
        completed = subprocess.run(
            spec.command,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        output = completed.stdout + completed.stderr
        code = completed.returncode
    except subprocess.TimeoutExpired:
        # An hour is generous even for a full Ollama triage run; past it something is
        # wedged rather than slow, and a runner that waits forever is one nobody runs.
        output, code = "timed out after 3600s", 124
    except FileNotFoundError as e:
        output, code = f"{e}", 127

    elapsed = time.monotonic() - started
    # The last line wins, not the first: an eval that retried, or one whose report
    # includes an earlier partial, must be scored on what it finished with.
    found = RESULT_LINE.findall(output)
    counts = json.loads(found[-1]) if found else None

    if verbose:
        console.print(f"[dim]--- {spec.service} ---[/dim]")
        console.print(output)

    return {
        "spec": spec,
        "code": code,
        "counts": counts,
        "seconds": elapsed,
        "output": output,
    }


def report(results: list[dict]) -> bool:
    table = Table(title="Evals, all services")
    table.add_column("service", style="cyan")
    table.add_column("measures")
    table.add_column("backend", style="dim")
    table.add_column("score", justify="right")
    table.add_column("seconds", justify="right", style="dim")
    table.add_column("status", justify="center")

    for row in results:
        spec, counts = row["spec"], row["counts"]
        score = f"{counts['passed']}/{counts['total']}" if counts else "-"
        if row["code"] == 0:
            status = "[green]PASS[/green]"
        elif row["code"] == 124:
            status = "[red]TIMEOUT[/red]"
        else:
            status = f"[red]FAIL ({row['code']})[/red]"
        table.add_row(
            spec.service,
            spec.measures,
            spec.backend,
            score,
            f"{row['seconds']:.0f}",
            status,
        )
    console.print(table)

    for row in results:
        if row["code"] != 0:
            console.print(f"\n[red]--- {row['spec'].service} failed ---[/red]")
            console.print(row["output"])

    failed = [r for r in results if r["code"] != 0]
    total_seconds = sum(r["seconds"] for r in results)
    console.print(
        f"\n[bold]{len(results) - len(failed)}/{len(results)} evals passed[/bold] "
        f"in {total_seconds / 60:.1f} minutes"
    )
    return not failed


def selftest() -> int:
    """The parser, checked without needing a model backend up.

    RESULT_LINE is the whole contract between this file and four others, and it is the
    kind of thing that breaks quietly: a service that stops emitting the line still
    exits 0, and the table would read `-` forever without anything failing. Runnable in
    milliseconds, so there is no excuse for not running it.
    """
    assert json.loads(
        RESULT_LINE.search('noise\nEVAL_RESULT {"passed": 3, "total": 4}\n').group(1)
    ) == {"passed": 3, "total": 4}
    # A rich table can print a line that merely mentions the token; only a line that
    # *starts* with it counts, which is what the ^ anchor plus MULTILINE buys.
    assert RESULT_LINE.search("see EVAL_RESULT {} below") is None
    assert RESULT_LINE.search("no result line here at all") is None
    # The last word: an eval that retried and printed twice must report the final run.
    output = (
        'EVAL_RESULT {"passed": 1, "total": 4}\nEVAL_RESULT {"passed": 4, "total": 4}'
    )
    assert json.loads(RESULT_LINE.findall(output)[-1]) == {"passed": 4, "total": 4}

    assert len({spec.service for spec in EVALS}) == len(EVALS), "duplicate service"
    for spec in EVALS:
        assert (ROOT / "services" / spec.service).is_dir(), spec.service
        assert (ROOT / "services" / spec.service / spec.command[1]).is_file(), spec

    console.print("[green]selftest ok[/green]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Day 28: run every service's eval")
    parser.add_argument(
        "services",
        nargs="*",
        help="service names to run; all four if omitted",
    )
    parser.add_argument("--list", action="store_true", help="show the evals and exit")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="check this runner's own parsing and paths; needs no backend",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="stream each eval's full output"
    )
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if args.list:
        for spec in EVALS:
            # command[1], not the whole command: command[0] is sys.executable, which is
            # an absolute venv path and wraps the line on any normal terminal.
            console.print(
                f"{spec.service:20} {spec.command[1]:20} "
                f"[dim]{spec.measures} via {spec.backend}[/dim]"
            )
        return 0

    selected = EVALS
    if args.services:
        known = {spec.service for spec in EVALS}
        unknown = set(args.services) - known
        if unknown:
            console.print(
                f"[red]unknown service(s): {', '.join(sorted(unknown))}[/red]"
            )
            console.print(f"known: {', '.join(sorted(known))}")
            return 2
        selected = [spec for spec in EVALS if spec.service in args.services]

    console.print(
        f"Running {len(selected)} eval(s). Model backends must be reachable; "
        "expect minutes, not seconds."
    )
    results = [run(spec, args.verbose) for spec in selected]
    return 0 if report(results) else 1


if __name__ == "__main__":
    sys.exit(main())
