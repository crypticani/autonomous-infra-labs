"""The pull-request comment -- Day 25.

This is the only part of the whole pipeline a human actually reads, and it started life
as a sixty-line heredoc inside the reusable workflow. It lives here instead for two
reasons: YAML is a bad place to keep logic that has branches, and code that only ever
runs on someone else's runner during a real pull request has no way to be wrong cheaply.

Stdlib only, and it takes the run as a plain dict rather than importing `Run` or
`RiskAssessment`. The workflow curls this file onto a runner that has no checkout of this
repo and no `pip install` step -- the same way it already fetches `scan.sh`.

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

    A `pending` run is refused rather than rendered. The workflow's poll loop only exits
    once the status leaves `pending`, so reaching here with one is a caller mistake --
    and the two honest responses to that are an error or a comment claiming a verdict
    that does not exist yet. Found by running this by hand against a run that had not
    finished, where the old code reached straight for `risk["counts"]` and died on a
    `TypeError` that said nothing about the actual problem.
    """
    if run["status"] == "pending":
        raise ValueError(
            f"run {run['id']} is still pending -- poll GET /triage/{run['id']} until "
            "its status is done or failed, then render"
        )

    if run["status"] == "failed":
        # Deliberately not silence, and deliberately not something that reads like a
        # pass. An absence of a verdict and a clean verdict look identical to anyone
        # reading only the check mark, which is the failure this whole day corrects.
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

        # Diffs only. The advice entries are the other several hundred fixes, and they
        # belong on the run record rather than in something a human has to scroll past.
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

    # The commit is omitted rather than rendered empty: Day 26's runtime caller has no
    # commit to send and never will, and `commit ``` in the footer of a real PR comment
    # reads like a bug in the tool rather than a property of the thing being triaged.
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

    # The file above is always plain markdown -- GitHub renders it, and an ANSI escape in
    # a PR comment shows up as a literal `[1;31m` in the middle of a table row. Rendering
    # is therefore only ever for a human looking at a terminal, and gated on both:
    #
    #   - a tty, so `python comment.py run.json > out.md` still writes markdown rather
    #     than box-drawing characters,
    #   - rich being importable, because the workflow curls this file onto a runner with
    #     no pip install and it must keep working there.
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
