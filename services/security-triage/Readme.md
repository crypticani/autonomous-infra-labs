# Service: Security Triage

Wraps three existing security scanners — Trivy, Bandit, Checkov — and adds an AI triage layer on
top of their raw output: deduplicate, prioritize, explain, and propose (never apply) a fix.

This is **Project 4 (Week 4)** of the
[30-day AI-Native DevOps challenge](../../docs/ai-devops-30-day-challenge.md). Days 22–28.

> New to security scanning or triage? [**docs/security-triage.md**](../../docs/security-triage.md)
> explains why AI sits *on top of* real scanners instead of replacing them, and what it takes to
> make three disagreeing JSON schemas look like one. This README is the *what and how much*; that
> one is the *why*.

**Status (Day 22):** the boring layer — `scan.sh` and `scanners.py` — is built and proven, with no
LLM involved yet. No server exists yet either; `POST /triage` starts Day 25.

## Why the service never sees a checkout

Security findings come *to* the service; code never does. `scan.sh` is the piece every onboarding
repo installs and runs in its own CI, over its own checkout, with its own scanners. It POSTs the
raw JSON in one envelope, with `repo`/`commit`/`branch` as request-body fields — no `TARGET_REPO`
env var, no git credentials, no scanner binaries, on the server. That is also what makes the
service target-agnostic: onboarding a new repo is copying a ~10-line CI step, not filing a config
change against this one.

## `scan.sh` — the client-side half

Runs `trivy fs` (vulnerabilities, misconfigurations, secrets), `checkov` (Dockerfile/Kubernetes
misconfigurations — no Terraform in this repo, so tfsec isn't needed), and `bandit` (Python code
issues), and assembles one envelope:

```json
{
  "repo": "git@github.com:...",
  "commit": "<sha>",
  "branch": "main",
  "scans": { "trivy": { ... }, "bandit": { ... }, "checkov": [ ... ] }
}
```

Bandit only runs if the checkout has `.py` files at all — the guard exists so a non-Python repo's
CI doesn't pay for a scan that will always report nothing, and it's what a Go repo's envelope
actually looks like: two `scans` keys, not three. None of the three scanners treat "found
something" as a failure worth stopping the script for — all three exit non-zero when they find
issues, which is the normal case — so each invocation is followed by `|| true`; a missing binary
still fails the script a step earlier, at the shebang-adjacent command itself.

```bash
services/security-triage/scan.sh <repo-root> <output-file>
```

## `scanners.py` — one `Finding` for three schemas

None of the three scanners agree on shape:

| Scanner | Where findings live |
|---|---|
| Trivy | `Results[].Vulnerabilities[]` / `Misconfigurations[]` / `Secrets[]`, nested under a scan target |
| Bandit | a flat `results[]` |
| Checkov | a *list* of per-framework reports, each `results.failed_checks[]` |

`parse_envelope()` turns any subset of those three into one list of `Finding` — a Pydantic model
with `scanner`, `rule_id`, `severity_raw`, `title`, `target`, `line`, `package`,
`installed_version`, `fixed_version`, `cwe`, and a computed `fingerprint`. A `scans` key that's
absent (or present but empty) contributes nothing; it is never an error, since that's exactly what
a partial envelope from a non-Python repo looks like.

`dedupe()` collapses same-finding duplicates deterministically — code, not a model call:

- A finding tied to a package (a CVE) is identified by `(rule_id, package, installed_version)`,
  **not** by which scan produced it — the same CVE from a filesystem scan and an image scan of the
  same package collapses into one.
- A finding tied to a line and no package (a misconfiguration or code-scan hit) is identified by
  `(target, line)` alone — dropping the rule id and scanner name, since Checkov's `CKV_*` and
  Trivy's `KSV-*`/`DS-*` IDs share no common vocabulary and there is no crosswalk table between
  them. This is a deliberate, marked simplification (see the `ponytail:` comment in `scanners.py`)
  with a known ceiling: two genuinely different findings on the same line would incorrectly merge.

## Verified against this repo's own scan

`fixtures/this-repo.json` is a real `scan.sh` run against this repo, committed as-is (public repo,
nothing to scrub). It's also the eval corpus Day 27 benchmarks against.

```
raw findings:     629  (trivy 45, bandit 534, checkov 50)
after dedupe:      559
```

## Tests

10 tests in `tests/test_scanners.py`, TDD — each written and watched fail (`ModuleNotFoundError`,
then real assertion failures) before `scanners.py` existed. Covers each scanner's real shape (built
from actual scanner output, not guessed), a missing-scanner-key envelope, CVE dedup across two
scans of the same package, distinct packages sharing a CVE ID staying distinct, cross-scanner
location dedup, fingerprint stability, and a sanity check against the real fixture above.

```bash
cd services/security-triage
python -m pytest tests/test_scanners.py -v   # 10 passed
```

## Not built yet

- Triage: batched LLM calls over deduped findings, with a `needs_human` escape hatch (Day 23).
- Proposed fixes as diffs, never applied (Day 24).
- `POST /triage` / `GET /triage/{id}`, bearer auth, the reusable GitHub Actions workflow (Day 25).
- Runtime signals (K8s audit log events) through the same pipeline (Day 26).
- Cost/latency benchmark, Ollama vs. Gemini (Day 27).
- Metrics, Grafana dashboard, eval harness, deployment to appsrv (Day 28).
