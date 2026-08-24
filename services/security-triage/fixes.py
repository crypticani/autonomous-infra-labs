"""Fixes proposed, never applied.

A security finding gets a diff and a human, not an auto-commit:

- The service has no checkout. It sees only the scanner JSON that was POSTed, so it
  cannot read the file, run the tests, or push a branch.
- A security fix is a behaviour change. `runAsUser: 10001` breaks an image whose files
  are owned by another uid, and whether that is acceptable is a judgment about the
  workload.

**No model call here.** The diff is deterministic Python over the finding's own context
lines; anything not constructible that way returns the scanner's own remediation
sentence as prose. A diff has a real oracle in `git apply --check`, so it is the one
place a wrong answer is cheaply detectable and therefore worth code instead of tokens.

Of the three fix classes the plan called mechanical, one survives: adding a
securityContext key, whose value is a constant and whose insertion point is derivable
from the `- name: <container>` line. Pinning a digest needs a registry this cannot
reach, and bumping a dependency needs a FixedVersion this corpus does not have.

The bug this design avoids: ten KSV rules fire on the same container block, so ten
independent diffs would each insert their own `securityContext:` and the second applied
would produce duplicate YAML keys. Candidates are grouped by insertion point.

# ponytail: two hunks in one file whose context runs overlap are dropped to advice
# rather than merged. Ceiling: a pod whose containers share one StartLine gets a diff
# for the first and prose for the rest. Upgrade path is merging overlapping runs.
"""

import logging
import os
import re
from typing import Literal

from pydantic import BaseModel

from scanners import Finding

logger = logging.getLogger(__name__)

# rule_id -> (securityContext key, the lines that set it). Membership is the whole
# definition of "mechanical": a rule is here only if the correct value is a constant that
# holds for any workload. A memory limit or an image digest is not, however tempting the
# template looks.
_SECURITY_CONTEXT: dict[str, tuple[str, tuple[str, ...]]] = {
    "KSV-0012": ("runAsNonRoot", ("runAsNonRoot: true",)),
    "KSV-0001": ("allowPrivilegeEscalation", ("allowPrivilegeEscalation: false",)),
    "KSV-0020": ("runAsUser", ("runAsUser: 10001",)),
    "KSV-0021": ("runAsGroup", ("runAsGroup: 10001",)),
    "KSV-0014": ("readOnlyRootFilesystem", ("readOnlyRootFilesystem: true",)),
    "KSV-0030": ("seccompProfile", ("seccompProfile:", "  type: RuntimeDefault")),
    "KSV-0104": ("seccompProfile", ("seccompProfile:", "  type: RuntimeDefault")),
    "KSV-0003": ("capabilities", ("capabilities:", "  drop:", "    - ALL")),
    "KSV-0004": ("capabilities", ("capabilities:", "  drop:", "    - ALL")),
    "KSV-0106": ("capabilities", ("capabilities:", "  drop:", "    - ALL")),
}

# Emission order, derived from the table above so there is no second constant to drift.
# Two rules mapping to one key collapse here.
_KEY_ORDER = list(dict.fromkeys(key for key, _ in _SECURITY_CONTEXT.values()))

# Trivy names the container in the message and nowhere else. Both quote styles are load
# bearing: KSV-0012 says Container 'x', KSV-0104 says container "x". Rules that name
# none get advice.
_CONTAINER_IN_MESSAGE = re.compile(r"[Cc]ontainer [\"']([^\"']+)[\"']")

# Matching the value against the message is what keeps this off `- name: http` in a
# `ports:` list and `- name: LOG_LEVEL` in `env:`, both of which share the context block.
_NAME_KEY = re.compile(r"^(\s*)-(\s+)name:\s*(\S+)\s*$")


class Fix(BaseModel):
    target: str
    rule_ids: list[str]
    fingerprints: list[str]
    kind: Literal["diff", "advice"]
    diff: str | None = None
    note: str


def _diff_path(target: str) -> str | None:
    """One path shape for `git apply`, or None if this is not a repo file."""
    path = os.path.normpath(target).lstrip("/")
    if path in ("", ".") or path.startswith("..") or ":" in path:
        return None
    return path


def _contiguous(context: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """The run of consecutive lines from the start of the context."""
    run: list[tuple[int, str]] = []
    for number, content in context:
        if run and number != run[-1][0] + 1:
            break
        run.append((number, content))
    return run


def _anchor(
    finding: Finding, run: list[tuple[int, str]]
) -> tuple[tuple[int, str] | None, str]:
    """Where the securityContext goes: the index of the container's `- name:` line in
    `run`, and its siblings' indent. (None, reason) when the scanner did not send enough.
    """
    container = _CONTAINER_IN_MESSAGE.search(finding.message or "")
    if not container:
        return None, "the finding's message does not name a container"
    if any("securityContext" in content for _, content in run):
        # One already exists somewhere in the block and only ten lines are visible, so
        # merging risks a second securityContext: key -- applies cleanly, fails to parse.
        return None, "a securityContext is already present in the visible lines"

    wanted = container.group(1)
    for index, (_, content) in enumerate(run):
        match = _NAME_KEY.match(content)
        if match and match.group(3).strip("\"'") == wanted:
            # `- name: x` puts the sibling keys at the dash's column plus the dash plus
            # the space after it: `        - name: x` -> keys at column 10.
            indent = " " * (len(match.group(1)) + 1 + len(match.group(2)))
            return (index, indent), ""
    return None, f"container {wanted!r} is not in the lines the scanner returned"


def _hunk(
    path: str,
    run: list[tuple[int, str]],
    insert_at: int,
    indent: str,
    keys: dict[str, tuple[str, ...]],
) -> str:
    inserted = [f"+{indent}securityContext:"]
    for key in _KEY_ORDER:
        for line in keys.get(key, ()):
            inserted.append(f"+{indent}  {line}")

    body = [f" {content}" for _, content in run]
    body[insert_at + 1 : insert_at + 1] = inserted

    start = run[0][0]
    header = f"@@ -{start},{len(run)} +{start},{len(run) + len(inserted)} @@"
    return "\n".join([f"--- a/{path}", f"+++ b/{path}", header, *body]) + "\n"


def _advice(finding: Finding, why: str) -> Fix:
    """Prose, from the scanner's own words. `why` is empty for a rule that was never a
    diff candidate.
    """
    remediation = finding.resolution or (
        f"upgrade {finding.package} {finding.installed_version} -> {finding.fixed_version}"
        if finding.package and finding.fixed_version
        else finding.title
    )
    return Fix(
        target=finding.target,
        rule_ids=[finding.rule_id],
        fingerprints=[finding.fingerprint],
        kind="advice",
        note=f"no diff -- {why}. {remediation}" if why else remediation,
    )


def propose_fixes(findings: list[Finding]) -> list[Fix]:
    """A Fix for every finding: a diff where one can be built with certainty, prose
    advice everywhere else. Diffs first, sorted by file and line, so stdout is a patch.
    """
    groups: dict[tuple[str, int], dict] = {}
    advice: list[Fix] = []

    for finding in findings:
        entry = _SECURITY_CONTEXT.get(finding.rule_id)
        if entry is None:
            advice.append(_advice(finding, ""))
            continue

        path = _diff_path(finding.target)
        if path is None:
            advice.append(
                _advice(finding, f"{finding.target!r} is not a repo file path")
            )
            continue

        run = _contiguous(finding.context)
        if not run:
            advice.append(_advice(finding, "the scanner returned no code context"))
            continue

        anchor, why = _anchor(finding, run)
        if anchor is None:
            advice.append(_advice(finding, why))
            continue

        insert_at, indent = anchor
        group = groups.setdefault(
            (path, run[insert_at][0]),
            {
                "run": run,
                "insert_at": insert_at,
                "indent": indent,
                "keys": {},
                "rule_ids": [],
                "fingerprints": [],
                "findings": [],
            },
        )
        key, lines = entry
        group["keys"].setdefault(key, lines)
        group["rule_ids"].append(finding.rule_id)
        group["fingerprints"].append(finding.fingerprint)
        group["findings"].append(finding)

    diffs: list[Fix] = []
    emitted: dict[str, list[tuple[int, int]]] = {}
    for (path, anchor_line), group in sorted(groups.items()):
        run = group["run"]
        first, last = run[0][0], run[-1][0]
        # An overlap makes `git apply` reject the whole patch, not just the second
        # hunk. Adjacent runs are fine; only genuinely shared lines are a problem.
        if any(
            first <= other_last and other_first <= last
            for other_first, other_last in emitted.get(path, [])
        ):
            logger.warning(
                f"{path}: hunk at line {anchor_line} overlaps an earlier one; advice only"
            )
            overlap = "its lines overlap another proposed hunk in the same file"
            advice.extend(_advice(dropped, overlap) for dropped in group["findings"])
            continue
        emitted.setdefault(path, []).append((first, last))

        keys = ", ".join(key for key in _KEY_ORDER if key in group["keys"])
        diffs.append(
            Fix(
                target=path,
                rule_ids=sorted(set(group["rule_ids"])),
                fingerprints=sorted(set(group["fingerprints"])),
                kind="diff",
                diff=_hunk(
                    path, run, group["insert_at"], group["indent"], group["keys"]
                ),
                note=(
                    f"adds an explicit securityContext ({keys}) to the container at line "
                    f"{anchor_line}. Review before applying: runAsUser and runAsGroup "
                    f"change the uid the image runs as, and readOnlyRootFilesystem "
                    f"breaks a container that writes to its own filesystem."
                ),
            )
        )

    return diffs + advice


if __name__ == "__main__":
    # A patch on stdout, the accounting on stderr, so the round trip is one pipe:
    #
    #   python fixes.py fixtures/this-repo.json > /tmp/proposed.patch
    #   git apply --check -v /tmp/proposed.patch     # from the repo root
    #
    # `git apply --check` is the only oracle that matters: a diff that does not apply is
    # worse than no diff, because a reviewer trusts the shape.
    #
    # Each Fix carries its own header, since a caller posts one fix per comment.
    # Concatenating two for one file is still valid, but the second's line numbers were
    # computed against the unpatched file, so git reports "applied with offset N".
    import json
    import sys
    from collections import Counter

    from scanners import parse_envelope

    fixture_path = sys.argv[1] if len(sys.argv) > 1 else "fixtures/this-repo.json"
    with open(fixture_path) as f:
        envelope = json.load(f)

    findings = parse_envelope(envelope)
    fixes = propose_fixes(findings)

    for fix in fixes:
        if fix.diff:
            sys.stdout.write(fix.diff)

    diffs = [f for f in fixes if f.kind == "diff"]
    print(
        f"{len(findings)} findings -> {len(diffs)} diffs, "
        f"{len(fixes) - len(diffs)} advice",
        file=sys.stderr,
    )
    for fix in diffs:
        print(f"  diff  {fix.target}  {' '.join(fix.rule_ids)}", file=sys.stderr)
    # Why the others got prose. A high count is either a real ceiling or an anchoring
    # bug, and the two look identical from a total.
    refusals = Counter(
        fix.note.split(".")[0].removeprefix("no diff -- ")
        for fix in fixes
        if fix.kind == "advice" and fix.note.startswith("no diff")
    )
    for reason, count in refusals.most_common():
        print(f"  {count:>4}  {reason}", file=sys.stderr)
