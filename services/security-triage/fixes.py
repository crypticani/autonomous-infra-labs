"""Fixes proposed, never applied -- Day 24.

A security finding gets a diff and a human, not an auto-commit. Two reasons, and the
second is the one that shaped this module:

- The service has no checkout. It never sees the repo, only the scanner JSON that was
  POSTed to it, so it cannot read the file it is proposing to change, cannot run the
  tests afterwards, and has no branch to push to. A diff is the only honest artifact --
  text the caller's CI attaches to a PR for a human to read.
- A security fix is a behaviour change. `runAsUser: 10001` breaks an image whose files
  are owned by another uid; `readOnlyRootFilesystem: true` breaks a container that writes
  to its own filesystem. Whether those are acceptable is a judgment about the workload,
  which is exactly the thing neither a scanner nor a model has.

**No model call here.** The diff is built by deterministic Python from the finding's own
context lines, and anything that cannot be built that way returns the scanner's own
remediation sentence as prose instead. That split is deliberate: a diff either applies or
it doesn't, and `git apply --check` is a real oracle, so this is the one part of the
pipeline where a wrong answer is cheaply detectable and therefore worth writing
deterministically. A 7b model that miscounts one column of YAML indentation produces a
patch that fails to apply, and Day 23 already measured this model emitting five
valid-shaped, factually worthless judgments -- a plausible diff is the same failure with
a `+` in front of it. Prose advice needs no model either: every Trivy misconfiguration
already ships a `Resolution` written by the people who wrote the check.

**What survives contact with a real corpus.** Of the three fix classes the week's plan
called mechanical, one is constructible from context lines alone:

- *add a securityContext key* -- yes. The value is a constant (`readOnlyRootFilesystem:
  true`), and both the insertion point and its indentation are derivable from the
  `- name: <container>` line Trivy returns.
- *pin a base image to a digest* -- no. The digest is not in the finding and the service
  cannot reach a registry to look it up. A diff with an invented digest is exactly the
  patch that looks authoritative and doesn't apply.
- *bump a pinned dependency* -- no, not on this corpus. Its one real CVE has no
  `FixedVersion`, so there is nothing to bump to. The advice path names the upgrade when
  a fixed version does exist.

The interesting bug this design avoids: ten of Trivy's KSV rules fire on the *same*
container block, so ten independent diffs would each insert their own
`securityContext:` key and the second one applied would produce duplicate YAML keys. So
candidates are grouped by insertion point and emitted as one hunk carrying the union of
the keys -- the same collapse Day 22 does for cost, done here for correctness.

# ponytail: two hunks in one file whose context runs overlap are dropped to advice rather
# than merged. Ceiling: a pod whose containers Trivy reports against one shared StartLine
# gets a diff for the first container and prose for the rest. Upgrade path: merge
# overlapping runs into a single hunk, worth doing the first time a real caller's
# manifests trip it.
"""

import logging
import os
import re
from typing import Literal

from pydantic import BaseModel

from scanners import Finding

logger = logging.getLogger(__name__)

# rule_id -> (securityContext key, the lines that set it). Membership in this table is
# the whole definition of "mechanical": a rule is only in here if the correct value is a
# constant that holds for any workload. Rules whose right answer is a number somebody has
# to choose -- a memory limit, an image digest, a uid that matches the image -- are not,
# however tempting the template looks. runAsUser/runAsGroup sit on the line: the *value*
# is arbitrary (any uid > 10000 satisfies the check) but the *effect* is not, which is
# what the review caveat in the note is for.
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

# Emission order for a merged block, derived from the table above so there is no second
# constant to drift out of sync with it. Two rules mapping to one key (KSV-0030 and
# KSV-0104 both want seccompProfile) collapse here.
_KEY_ORDER = list(dict.fromkeys(key for key, _ in _SECURITY_CONTEXT.values()))

# Trivy names the container in the finding's message and nowhere else. Both quote styles
# are load bearing: KSV-0012 says Container 'x', KSV-0104 says container "x", and the
# rules that say neither ("container should drop all") are the ones that get advice.
_CONTAINER_IN_MESSAGE = re.compile(r"[Cc]ontainer [\"']([^\"']+)[\"']")

# The container's own `- name:` key. Matching the value against the message is what keeps
# this off `- name: http` inside a `ports:` list and `- name: LOG_LEVEL` inside `env:` --
# both of which appear in the same context block, indented deeper.
_NAME_KEY = re.compile(r"^(\s*)-(\s+)name:\s*(\S+)\s*$")


class Fix(BaseModel):
    target: str
    rule_ids: list[str]
    fingerprints: list[str]
    kind: Literal["diff", "advice"]
    diff: str | None = None
    note: str


def _diff_path(target: str) -> str | None:
    """One path shape for `git apply`, or None if this target isn't a repo file at all.

    The three scanners disagree on spelling: Trivy says `services/x/y.yaml`, Bandit
    `./services/x/y.py`, Checkov `/services/x/y.yaml`. A patch header needs one form, and
    `target` arrives inside a public request body and ends up in that header -- so
    anything that climbs out of the repo, or that is a container image reference rather
    than a file (`alpine:3.19 (alpine 3.19.1)`), gets refused rather than normalised.
    """
    path = os.path.normpath(target).lstrip("/")
    if path in ("", ".") or path.startswith("..") or ":" in path:
        return None
    return path


def _contiguous(context: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """The run of consecutive lines from the start of the context.

    A hunk header claims a start line and a line count, so a gap anywhere inside the
    quoted lines makes the whole hunk a lie about the file even if every individual line
    is right.
    """
    run: list[tuple[int, str]] = []
    for number, content in context:
        if run and number != run[-1][0] + 1:
            break
        run.append((number, content))
    return run


def _anchor(
    finding: Finding, run: list[tuple[int, str]]
) -> tuple[tuple[int, str] | None, str]:
    """Where the securityContext goes: the index of the container's `- name:` line within
    `run`, and the indent its sibling keys sit at. Returns (None, reason) when that can't
    be established from what the scanner sent, which is most of the interesting cases.
    """
    container = _CONTAINER_IN_MESSAGE.search(finding.message or "")
    if not container:
        return None, "the finding's message does not name a container"
    if any("securityContext" in content for _, content in run):
        # There is already one somewhere in the block, and the visible lines are only the
        # first ten -- merging into a mapping we can only partly see risks emitting a
        # second securityContext: key, which applies cleanly and then fails to parse.
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
    """Prose, from the scanner's own words wherever it has any. `why` is empty for a rule
    that was never a diff candidate -- no point telling a reader that B101 has no fixer.
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
    """A Fix for every finding: a unified diff where one can be built with certainty,
    prose advice everywhere else. Diffs first, sorted by file and line, so stdout is a
    patch file; advice after.

    Pass the **pre-dedup** findings. Day 22 identifies a misconfiguration by
    (target, line), so the securityContext rules that all report one container block's
    StartLine share a fingerprint and `dedupe` keeps one of them -- fine for triage, but
    it would silently reduce a five-key hunk to a one-key hunk. The grouping below is the
    same collapse done where it costs nothing: those five findings produce one diff, and
    because their fingerprints are identical the returned list carries the single
    fingerprint a triage result will be keyed by anyway.
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
        # Overlapping hunks make `git apply` reject the whole patch, not just the second
        # hunk -- so one bad pair would cost every other fix in the file. Adjacent runs
        # are fine and stay two hunks; only genuinely shared lines are a problem.
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
    # The Day 24 verify step: a patch on stdout, the accounting on stderr, so the round
    # trip the plan asks for is one pipe --
    #
    #   python fixes.py fixtures/this-repo.json > /tmp/proposed.patch
    #   git apply --check -v /tmp/proposed.patch     # from the repo root
    #
    # `git apply --check` is the only oracle that matters here: a proposed diff that does
    # not apply is worse than no diff at all, because a reviewer trusts the shape.
    #
    # Each Fix carries its own `--- a/ +++ b/` header, because a caller posts one fix into
    # one PR comment. Concatenating two of them for the same file is still a valid patch,
    # but the second one's line numbers were computed against the unpatched file, so
    # `git apply` matches it by context and says "applied with offset N" -- expected here,
    # not a failure.
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
    # Why the others got prose. A refusal reason with a high count is either a real
    # ceiling or a bug in the anchoring, and the two look identical from a total.
    refusals = Counter(
        fix.note.split(".")[0].removeprefix("no diff -- ")
        for fix in fixes
        if fix.kind == "advice" and fix.note.startswith("no diff")
    )
    for reason, count in refusals.most_common():
        print(f"  {count:>4}  {reason}", file=sys.stderr)
