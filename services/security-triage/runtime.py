"""The runtime half of the same pipeline.

`scan.sh`'s counterpart: reads a Kubernetes audit log and emits the same envelope,
events under a fourth `scans` key. Everything after that is code that already existed.
No second endpoint and no second pipeline, which is the point: a runtime event has no
file, line, package or severity, so a `Finding` shaped around what scanners emit would
have needed its own half of the service.

**`repo` is a label, not a checkout** -- for a cluster it carries the cluster's name.

    python runtime.py <audit.log> [out.json] [repo-label]
"""

import json
import sys

from scanners import dedupe, parse_envelope

# One body against app.py's 16 MiB cap, at 1-2 KB per event, so a busy cluster's log
# needs a ceiling. Newest wins: the recent end is what anyone is asking about.
#
# ponytail: a flat tail, not a time window. Ceiling: on a cluster producing 4000 events
# between captures the older ones are dropped, reported only as the count below. Upgrade
# path is a `since` argument once anything polls this on a schedule.
MAX_EVENTS = 4000


def read_events(path: str, max_events: int = MAX_EVENTS) -> tuple[list[dict], int]:
    """Newest `max_events` events, plus a count of the lines that would not parse.

    Skipped rather than fatal, because the API server appends live and a log captured
    mid-write routinely ends in half an event. *Counted* rather than ignored: an entirely
    unparseable file is the wrong file or a policy that never loaded, which a silent skip
    would present as a clean empty run.
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
    """The same envelope `scan.sh` writes, with the audit events as the only scan."""
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
