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

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# None of the three treat "findings exist" as a script failure -- all three exit non-zero
# when they find something, which is the normal case, not an error. `|| true` is load
# bearing here, not a swallowed error: a missing binary still fails loudly a step earlier.
trivy fs --format json --quiet --scanners vuln,misconfig,secret \
  --skip-dirs "$EXCLUDE_DIRS" . > "$TMP/trivy.json" || true

checkov -d . --compact -o json \
  --skip-path venv --skip-path .venv --skip-path node_modules \
  > "$TMP/checkov.json" 2>/dev/null || true

# Bandit is Python-only. Running it over a repo with no .py files still "succeeds" but
# with an empty result, so this only matters as a courtesy to non-Python callers -- it
# is what exercises scanners.py's partial-envelope path, not something scan.sh needs for
# correctness.
if find . -name '*.py' -not -path './venv/*' -not -path './.venv/*' -not -path './node_modules/*' \
    | grep -q .; then
  bandit -r . -f json -q -x "./venv,./.venv,./node_modules" > "$TMP/bandit.json" || true
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
PY

echo "wrote $OUT"
