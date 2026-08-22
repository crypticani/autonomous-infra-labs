"""The runtime half of the same pipeline -- Day 26.

`scan.sh` is the client side for static findings: it runs three scanners over a checkout
and emits one envelope for POST /triage. This is the same job for runtime signals -- it
reads a Kubernetes audit log and emits the same envelope, with the events under a fourth
`scans` key. Everything after that is code that already existed: `scanners.py` normalises
them into `Finding`s, `triage.py` judges them in the same batches, `risk.py` scores them
with the same weights, `comment.py` renders them in the same table.

There is no second endpoint and no second pipeline, and that is the whole point of the
day. A runtime event has no file, no line, no package and no scanner severity, so if the
`Finding` seam were shaped around what scanners happen to emit rather than around "a thing
worth a judgment, and where it is", this would have needed its own half of the service.

**`repo` is a label, not a checkout.** The envelope field is named for the static case;
for a cluster it carries the cluster's name, which is what the PR comment header and the
run record show. Nothing server-side treats it as anything but a string.

    python runtime.py <audit.log> [out.json] [repo-label]
"""

import json
import sys

from scanners import dedupe, parse_envelope

# The envelope POSTs as one body against app.py's 16 MiB cap and an audit event runs
# 1-2 KB, so a busy cluster's log would be refused outright without a ceiling here.
# Newest events win: an audit log is append-only and the recent end is the part anyone is
# asking a question about.
#
# ponytail: a flat tail, not a time window -- `--since 1h` would need timestamp parsing
# and a clock, and the log this reads is captured on demand rather than streamed.
# Ceiling: on a cluster loud enough to produce 4000 audited events between captures, the
# older ones are dropped silently apart from the count printed below. Upgrade path is a
# `since` argument once anything actually polls this on a schedule.
MAX_EVENTS = 4000


def read_events(path: str, max_events: int = MAX_EVENTS) -> tuple[list[dict], int]:
    """Events from a JSON-lines audit log, newest `max_events` kept, plus a count of the
    lines that would not parse.

    Unparseable lines are skipped rather than fatal, because the API server appends to
    this file live and a log captured mid-write routinely ends in half an event. They are
    *counted* rather than ignored: a handful is that torn last line, and a file that is
    entirely unparseable is a different problem -- the wrong file, or a policy that never
    loaded -- which a silent skip would present as a quiet, clean, empty run.
    """
    events: list[dict] = []
    malformed = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1
    return events[-max_events:], malformed


def build_envelope(events: list[dict], repo: str) -> dict:
    """The same envelope `scan.sh` writes, with the audit events as the only scan.

    `commit` and `branch` are empty and stay empty: a cluster has no commit, and inventing
    one would put a lie in the PR comment header. Both fields already default to `""` in
    `TriageRequest` for exactly this kind of caller.
    """
    return {"repo": repo, "commit": "", "branch": "", "scans": {"k8s_audit": events}}


if __name__ == "__main__":
    log_path = sys.argv[1] if len(sys.argv) > 1 else "audit.log"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "runtime-envelope.json"
    repo_label = sys.argv[3] if len(sys.argv) > 3 else "cluster"

    events, malformed = read_events(log_path)
    if malformed:
        print(f"skipped {malformed} unparseable line(s)")
    if not events:
        sys.exit(
            f"no audit events in {log_path} -- check that the API server took the "
            "policy file (see the Readme's run sheet)"
        )

    envelope = build_envelope(events, repo_label)
    with open(out_path, "w") as f:
        json.dump(envelope, f)

    # The server's own normalisation, run here and printed. Not a convenience: this is
    # the only place the two halves of the seam can be compared by eye before a model
    # call is spent on them, and the ratio it prints (thousands of events, a handful of
    # findings) is the day's actual result.
    findings = dedupe(parse_envelope(envelope))
    preview = "\n".join(
        [
            f"**{len(events)} events -> {len(findings)} findings**, "
            f"wrote `{out_path}`",
            "",
            "| Rule | Who, and what it was about | What happened |",
            "| --- | --- | --- |",
            *(f"| `{f.rule_id}` | `{f.target}` | {f.title} |" for f in findings),
        ]
    )

    # The same two gates as comment.py's renderer, for the same two reasons: only a human
    # at a terminal wants box-drawing characters, and `python runtime.py ... | tee` should
    # still produce text. The envelope written above is plain JSON either way -- the
    # formatting never reaches anything a machine reads.
    if sys.stdout.isatty():
        try:
            from rich.console import Console
            from rich.markdown import Markdown
        except ImportError:
            print(preview)
        else:
            Console().print(Markdown(preview))
    else:
        print(preview)
