"""The pull-request comment.

The only part of the pipeline a human reads. A module rather than a heredoc in the
workflow, because YAML is a bad place for branching logic and code that runs only on
someone else's runner has no way to be wrong cheaply.

Stdlib only, and it takes the run as a plain dict: the workflow curls this file onto a
runner with no checkout and no pip install.

    python comment.py run.json [comment.md]
"""

import json


def _rows(top: list[dict]) -> list[str]:
    lines = ["| Priority | Rule | Where | Why |", "| --- | --- | --- | --- |"]
    for row in top:
        where = row["target"] + (f":{row['line']}" if row["line"] else "")
        lines.append(
            f"| {row['priority']} | `{row['rule_id']}` ({row['scanner']}) "
            f"| `{where}` | {row['explanation']} |"
        )
    return lines


def render(run: dict) -> str:
    """Markdown for one run, finished or failed.

    A `pending` run is refused rather than rendered: the poll loop only exits once the status
    leaves `pending`, so reaching here with one is a caller mistake, and the alternative is a
    comment claiming a verdict that does not exist yet.
    """
    if run["status"] == "pending":
        raise ValueError(
            f"run {run['id']} is still pending -- poll GET /triage/{run['id']} until "
            "its status is done or failed, then render"
        )

    if run["status"] == "failed":
        # Not silence, and not anything that reads like a pass: a missing verdict and a
        # clean one look identical to anyone reading only the check mark.
        body = [
            "## Security triage could not finish",
            "",
            f"`{run['error']}`",
            "",
            "Nothing was judged, so this is not a passing verdict -- it is the absence "
            "of one.",
        ]
    else:
        risk = run["risk"]
        counts = risk["counts"]
        mark = "❌" if risk["verdict"] == "fail" else "✅"
        body = [
            f"## Security triage {mark} score **{risk['score']}** "
            f"/ threshold {risk['threshold']}",
            "",
            f"{run['findings_raw']} findings from the scanners, {run['findings']} after "
            f"dedup, {run['triaged']} triaged.",
            "",
            " · ".join(f"**{k}** {v}" for k, v in counts.items() if v) or "clean",
        ]

        if risk["review_required"]:
            body += [
                "",
                f"> ⚠️ {counts['needs_human']} finding(s) the model declined to judge. "
                "They score nothing, so a passing verdict does not cover them.",
            ]

        if run["top"]:
            body += ["", *_rows(run["top"])]

        # Diffs only; the advice entries belong on the run record, not in something a
        # human scrolls past.
        diffs = [fix for fix in run["fixes"] if fix["kind"] == "diff"]
        if diffs:
            body += [
                "",
                f"<details><summary>{len(diffs)} proposed fix(es) — review, never "
                "apply blind</summary>",
                "",
            ]
            for fix in diffs:
                body += ["```diff", fix["diff"].rstrip(), "```", ""]
            body += ["</details>"]

    # Omitted rather than rendered empty: the runtime caller has no commit and never
    # will, and an empty one reads like a bug in the tool.
    footer = f"run `{run['id']}`"
    if run["commit"]:
        footer += f" · commit `{run['commit'][:8]}`"
    body += ["", f"<sub>{footer}</sub>"]
    return "\n".join(body) + "\n"


if __name__ == "__main__":
    import sys

    with open(sys.argv[1]) as f:
        markdown = render(json.load(f))

    if len(sys.argv) > 2:
        with open(sys.argv[2], "w") as f:
            f.write(markdown)

    # The file itself is always plain markdown -- an ANSI escape in a PR comment renders
    # as a literal `[1;31m`. The pretty version is for a terminal only, so it is gated on
    # a tty (redirection still writes markdown) and on rich being importable (the runner
    # has no pip install).
    if sys.stdout.isatty():
        try:
            from rich.console import Console
            from rich.markdown import Markdown
        except ImportError:
            print(markdown)
        else:
            Console().print(Markdown(markdown))
    else:
        print(markdown)
