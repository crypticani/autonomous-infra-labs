#!/usr/bin/env bash
# The client-side half of security-triage: runs the three scanners over a checkout and
# emits one envelope for POST /triage. No scanners or git credentials live on the server
# side -- this script is what any onboarding repo's CI installs and calls.
#
# Usage: scan.sh [repo-root] [output-file]
set -euo pipefail

REPO_ROOT="${1:-.}"
OUT="${2:-scan-envelope.json}"
EXCLUDE_DIRS="venv,.venv,node_modules,.git"

cd "$REPO_ROOT"

# Every scanner is checked up front, because the alternative is silent and was not
# hypothetical. Found live on 2026-08-22: bandit and checkov live in the repo venv, the
# venv was not active, `bandit: command not found` was swallowed by the `|| true` below,
# and this script exited 0 having written an envelope containing only trivy -- 16 findings
# from one scanner, "wrote scan-envelope.json", no error. scanners.py accepts partial
# envelopes by design -- that is what makes a missing scanner invisible rather than loud,
# and a clean triage report over a third of the intended scan is the worst output this
# service can produce.
#
# The comment below used to claim a missing binary "fails loudly a step earlier". It did
# not. This is that step.
need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "scan.sh: '$1' not found on PATH -- refusing to write a partial envelope." >&2
    echo "  (in this repo the scanners live in the venv: source venv/bin/activate)" >&2
    exit 1
  }
}
need trivy
need checkov

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# None of the three treat "findings exist" as a script failure -- all three exit non-zero
# when they find something, which is the normal case, not an error. `|| true` is load
# bearing here, and it is also indiscriminate: it cannot tell "found findings" from
# "crashed", which is why the binaries are checked by need() above instead of being left
# to fail through this.
trivy fs --format json --quiet --scanners vuln,misconfig,secret \
  --skip-dirs "$EXCLUDE_DIRS" . > "$TMP/trivy.json" || true

checkov -d . --compact -o json \
  --skip-path venv --skip-path .venv --skip-path node_modules \
  > "$TMP/checkov.json" 2>/dev/null || true

# Bandit is Python-only. Running it over a repo with no .py files still "succeeds" but
# with an empty result, so this only matters as a courtesy to non-Python callers -- it
# is what exercises scanners.py's partial-envelope path, not something scan.sh needs for
# correctness.
#
# Test files are excluded, and this is the single largest cost decision in the service.
# Measured on Day 27 against this repo: 559 deduped findings, of which 515 -- 92% -- were
# B101 "use of assert detected" inside test files. An assert is what a test file is made
# of, so bandit's own guidance is to exclude test paths. Feeding those 515 to a model
# meant 103 of 112 calls, and minutes of CPU each, spent judging that tests contain
# asserts. Worse than the waste: the model declined them, so a full-corpus run would have
# returned ~92% needs_human and routed 515 non-issues to a human, which is the exact
# inverse of what this service is for.
#
# This does not create a blind spot for secrets in test files. Trivy still scans the whole
# tree including tests with its secret scanner above -- only bandit's Python lint is
# narrowed, and what is dropped with it (hardcoded passwords in fixtures, binding a test
# server to 0.0.0.0) is the same category of intentional-in-a-test finding.
# Overridable, because an onboarding repo's tests may not live in */tests/ -- and because
# being able to run this both ways is what makes the 92% claim above checkable rather than
# asserted:
#   BANDIT_EXCLUDE="./venv,./.venv,./node_modules" scan.sh . /tmp/before.json
BANDIT_EXCLUDE="${BANDIT_EXCLUDE:-./venv,./.venv,./node_modules,*/tests/*,*/test_*.py,*_test.py,*/conftest.py}"
if find . -name '*.py' -not -path './venv/*' -not -path './.venv/*' -not -path './node_modules/*' \
    | grep -q .; then
  # Checked here rather than beside trivy and checkov: a repo with no Python genuinely
  # does not need bandit, and requiring it there would break the non-Python caller this
  # branch exists for. A repo that *does* have Python and no bandit is the silent-partial
  # case, so it fails.
  need bandit
  bandit -r . -f json -q -x "$BANDIT_EXCLUDE" > "$TMP/bandit.json" || true
fi

python3 - "$TMP" "$OUT" <<'PY'
import json
import os
import subprocess
import sys

tmp_dir, out_path = sys.argv[1], sys.argv[2]


def load(name):
    path = os.path.join(tmp_dir, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        content = f.read().strip()
    return json.loads(content) if content else None


def git(*args):
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


scans = {}
for name in ("trivy", "bandit", "checkov"):
    data = load(f"{name}.json")
    if data is not None:
        scans[name] = data

envelope = {
    "repo": git("config", "--get", "remote.origin.url") or os.path.basename(os.getcwd()),
    "commit": git("rev-parse", "HEAD"),
    "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
    "scans": scans,
}

with open(out_path, "w") as f:
    json.dump(envelope, f, indent=2)

# Which scanners are actually in here, named on the way out. The binary checks above stop
# the known cause of a partial envelope; this makes any remaining one visible rather than
# hiding behind "wrote scan-envelope.json".
print(f"scanners in envelope: {', '.join(sorted(scans)) or 'NONE'}")
PY

echo "wrote $OUT"
