# Service: Security Triage

Wraps three existing security scanners — Trivy, Bandit, Checkov — and adds an AI triage layer on
top of their raw output: deduplicate, prioritize, explain, and propose (never apply) a fix.

This is **Project 4 (Week 4)** of the
[30-day AI-Native DevOps challenge](../../docs/ai-devops-30-day-challenge.md). Days 22–28.

> New to security scanning or triage? [**docs/security-triage.md**](../../docs/security-triage.md)
> explains why AI sits *on top of* real scanners instead of replacing them, and what it takes to
> make three disagreeing JSON schemas look like one. This README is the *what and how much*; that
> one is the *why*.

**Status (Day 23):** the boring layer (`scan.sh`, `scanners.py`) and the triage layer
(`provider.py`, `triage.py`) are both built. No server exists yet; `POST /triage` starts Day 25 —
today's triage runs from a script, against the fixture corpus, not over HTTP.

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

## `provider.py` — the Ollama/Gemini seam

Same shape as [self-healing-agent's provider.py](../self-healing-agent/provider.py): an ABC, a
provider-specific error carrying an HTTP status (`TriageProviderError`, in `errors.py`), and a
factory switched by `ST_LLM_PROVIDER` so Day 27's cost/latency benchmark can flip providers with
nothing but an env var. What it doesn't carry over is that module's retry and rate-limit pacing —
both earned their complexity from specific incidents (a chained diagnosis losing a call to a
transient 503; Gemini's free-tier per-minute cap) that don't apply to one batched call against a
provider with no quota ceiling. `generate(system, user, schema)` takes the *class*, not a JSON
Schema dict: Gemini's `response_schema` wants the class itself, Ollama's `format` wants
`schema.model_json_schema()`, and passing it once keeps this module ignorant of what `TriageBatch`
actually contains.

**Found live on 2026-08-19, not predicted:** an identical prompt that took 47s and ~750 tokens on
one run took 20+ minutes on another — not slower hardware, a runaway. Greedy decoding
(`temperature: 0`) with no repetition penalty has no escape once it enters a repeating loop, and
with `num_predict` unset nothing bounds it — it fills the 4096 context and never emits a stop
token. What looked like "appsrv's 2 cores are too slow" and later "the GPU path is broken" were
both this, on different hardware, at different speeds. Also worth knowing: `temperature: 0` turns
out not to mean reproducible here — prompt-cache reuse changes batch splits and can flip a
near-tie logit, so the identical prompt above genuinely produced two different outcomes.

Getting to a real fix took two rounds, because the first one only bounded the damage instead of
stopping it: `num_predict` (`ST_MAX_TOKENS`, default 1536) turns an infinite hang into a real
`TriageProviderError` in seconds, but a milder `repeat_penalty: 1.05` still let the model fall into
the same repeating conditional ("if it can be exploited... if it cannot...") dozens of times over —
just now truncated at 1536 tokens instead of running to 4096. Two changes closed it: a real
`repeat_penalty: 1.3`, and — the more reliable of the two — a `max_length=280` on
`TriageResult.explanation`, which is part of the JSON schema Ollama grammar-constrains generation
against, not a check applied after the fact. A sampling parameter is a nudge; a schema bound is a
guarantee the grammar itself won't produce a longer string, loop or not.

## `triage.py` — batched, structured triage

`triage_findings()` chunks deduped findings into groups of `ST_BATCH_SIZE` (default 5) and sends
one model call per group, asking for `{fingerprint, priority, exploitability, impact,
explanation, confidence}` per finding — schema-constrained decoding (Ollama's `format`, Gemini's
`response_schema`) rather than parsing free text, so a malformed answer is a validation error to
catch, not a regex to write.

Two guards:

- **Every returned fingerprint must be one that was actually sent.** The same failure shape as Day
  10's invented citations — a model naming something it wasn't given — gets the same fix: drop
  what wasn't in the input rather than trust it. `triage_batch()` logs and drops any fingerprint
  outside the batch it sent.
- **`needs_human` is a legal `priority`**, not an error path. A model forced to choose among four
  real severities on a finding it can't actually judge doesn't refuse — it guesses, and the guess
  is indistinguishable from a real triage. The fifth option is what makes the other four
  trustworthy.

Run it against a slice of the real fixture (defaults to the first 15 of the 559 deduped findings —
the full corpus at batch size 5 is ~112 calls, which on CPU Ollama could be hours):

```bash
cd services/security-triage
python triage.py fixtures/this-repo.json 15
```

Prints wall-clock per batch. The plan's risk section expected the bottleneck here to be CPU
Ollama's raw speed; the real one, found live, was a runaway generation loop (see `provider.py`'s
`ST_MAX_TOKENS`/`repeat_penalty` and `TriageResult.explanation`'s `max_length` above) that made a
47s answer take 20+ minutes on an unrelated run of the identical prompt.

**Measured on 2026-08-19, one 5-finding batch, after the fix:** 40.0s on a laptop (CPU-only —
the same laptop's Nvidia MX110 GPU path hit an unrelated empty-response error earlier and hasn't
been re-tested since; left open, not resolved by this fix), 68.2s on appsrv's 2-core host. Both
clean: 5/5 triaged, 0 invented fingerprints. A single run each, and temp-0 is
not reproducible here (see below), so treat these as "the timeout is gone and the order of
magnitude is tens of seconds," not a precise number — Day 27 benchmarks properly across batch
sizes. Extrapolating appsrv's 68.2s/batch across all 559 deduped findings at batch size 5 (~112
batches) is roughly two hours run serially; whether that's acceptable, and whether a larger batch
size changes the per-finding cost, is exactly what Day 27 exists to settle.

## Tests

```bash
cd services/security-triage
python -m pytest -v   # 10 (scanners) + 10 (provider) + 11 (triage) = 31, if green
```

`test_scanners.py` (10, Day 22): each scanner's real shape, a missing-scanner-key envelope, CVE
dedup across two scans, distinct packages sharing a CVE ID staying distinct, cross-scanner
location dedup, fingerprint stability, and a sanity check against the real fixture.

`test_provider.py` (10, Day 23): transport-failure status mapping and the schema/JSON-body shape
sent to each provider, using the same fake-response/monkeypatch style as
[knowledge-copilot's test_llm.py](../knowledge-copilot/tests/test_llm.py) — no real network call.

`test_triage.py` (11, Day 23): the two guards above, using a `FakeProvider` injected in place of
`get_triage_provider()` rather than mocking `requests` or `google.genai` a second time — the same
dependency-injection shape `triage_findings(provider=...)` exists for. No test triages the real
fixture end to end; that needs a real model and minutes per batch, which is what `triage.py`'s
`__main__` script above is for, run by hand, not by `pytest`.

## Not built yet

- Proposed fixes as diffs, never applied (Day 24).
- `POST /triage` / `GET /triage/{id}`, bearer auth, the reusable GitHub Actions workflow (Day 25).
- Runtime signals (K8s audit log events) through the same pipeline (Day 26).
- Cost/latency benchmark, Ollama vs. Gemini (Day 27).
- Metrics, Grafana dashboard, eval harness, deployment to appsrv (Day 28).
