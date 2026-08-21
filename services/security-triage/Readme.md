# Service: Security Triage

Wraps three existing security scanners — Trivy, Bandit, Checkov — and adds an AI triage layer on
top of their raw output: deduplicate, prioritize, explain, and propose (never apply) a fix.

This is **Project 4 (Week 4)** of the
[30-day AI-Native DevOps challenge](../../docs/ai-devops-30-day-challenge.md). Days 22–28.

> New to security scanning or triage? [**docs/security-triage.md**](../../docs/security-triage.md)
> explains why AI sits *on top of* real scanners instead of replacing them, and what it takes to
> make three disagreeing JSON schemas look like one. This README is the *what and how much*; that
> one is the *why*.

**Status (Day 25):** the boring layer (`scan.sh`, `scanners.py`), the triage layer (`provider.py`,
`triage.py`), the fix layer (`fixes.py`), the policy layer (`risk.py`) and the HTTP surface
(`app.py`) are built, with a reusable GitHub Actions workflow any repo can call. Not deployed —
the endpoint runs locally with `uvicorn`; appsrv, the image and the metrics are Day 28.

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
`installed_version`, `fixed_version`, `cwe`, a computed `fingerprint`, and (added Day 24, for
`fixes.py`) `context`, `resolution` and `message`. A `scans` key that's
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

### Measured 2026-08-19: model size decides whether this works at all

One 5-finding batch, laptop CPU, single run each (temp-0 is not reproducible here — see above — so
these are orders of magnitude, not precise figures):

| `ST_OLLAMA_MODEL` | per batch | full corpus, serial | output quality |
|---|---|---|---|
| `qwen2.5-coder:1.5b` | 78.7s | ~2.4h | **unusable** |
| `qwen2.5-coder:7b` | 258.9s | ~8h | good |

The quality gap is the finding, not the latency. **1.5b did not triage at all** — it returned five
byte-identical explanations, generic boilerplate ("requires a specific payload to trigger") that
matched none of the findings, and `needs_human` for all five at `conf 0.80`. Valid JSON, correct
fingerprints, all guards satisfied, and completely worthless. That is the same repetition attractor
one level up: `repeat_penalty` suppresses repeated tokens *within* a sequence, so the model
templated across *array elements* instead, which no sampling knob addresses.

7b, same prompt, produced five distinct and factually correct judgments — `DS-0026` identified as a
missing HEALTHCHECK and rated low, `KSV-0013` as an unpinned `:latest` tag, `KSV-01010` as
ConfigMap data leakage, the CVE as pre-auth RCE rated high. Spread: 1 high, 2 medium, 2 low.

**Still not calibrated: `confidence` was `1.00` on all five.** A model asserting perfect certainty
on every judgment puts no information in that field — the flat `0.80` from 1.5b with different
wording. Day 25's risk score should lean on `priority`, and treat `confidence` as unproven until
Day 28's eval set can actually check it against known-correct answers.

Day 27 still owns the real benchmark (batch-size sweep, Gemini comparison, cost per run). What
Day 23 settles is narrower and load-bearing for it: **the floor is ~7B for this task**, so any
latency tuning starts from 258.9s/batch, not 78.7s.

## `fixes.py` — proposed diffs, never applied

A finding gets a diff and a human, not an auto-commit. Two reasons, and the second is the one that
shaped the module:

- **The service has no checkout.** It never sees the repo, only the JSON that was POSTed to it — so
  it cannot read the file it wants to change, cannot run the tests afterwards, and has no branch to
  push. A diff as text is the only honest artifact.
- **A security fix is a behaviour change.** `runAsUser: 10001` breaks an image whose files are owned
  by another uid; `readOnlyRootFilesystem: true` breaks a container that writes to its own
  filesystem. Whether that is acceptable is a judgment about the workload, which is the one thing
  neither a scanner nor a model has.

**No model call in this module.** The diff is built by deterministic Python from the finding's own
context lines; anything that can't be built that way returns the scanner's own remediation sentence
as prose instead. That split is the point: `git apply --check` is a real oracle, so this is the one
place in the pipeline where a wrong answer is cheaply detectable and therefore worth writing by
hand. A 7b model that miscounts one column of YAML indentation emits a patch that fails to apply —
and Day 23 already measured this model producing five valid-shaped, worthless judgments. A plausible
diff is that same failure with a `+` in front of it. Prose needs no model either: every Trivy
misconfiguration ships a `Resolution` written by whoever wrote the check.

### Three "mechanical" fix classes, one survivor

The week's plan named three. Against the real corpus:

| class | verdict | why |
|---|---|---|
| add a `securityContext` key | **diff** | the value is a constant, and both the insertion point and its indentation are derivable from the `- name: <container>` line Trivy returns |
| pin a base image to a digest | advice | the digest isn't in the finding and the service can't reach a registry. A diff with an invented digest is exactly the patch that looks authoritative and doesn't apply |
| bump a pinned dependency | advice | this corpus' one real CVE has no `FixedVersion` — nothing to bump to. The advice names the upgrade when a fixed version does exist |

Membership in `_SECURITY_CONTEXT` is the whole definition of *mechanical*: a rule is in the table
only if the correct value is a constant that holds for any workload (`readOnlyRootFilesystem: true`,
`capabilities.drop: [ALL]`, `seccompProfile.type: RuntimeDefault`). Rules whose right answer is a
number somebody has to choose — a memory limit, a uid that matches the image — are not, however
tempting the template looks.

### What the scanners were already sending, and Day 22 was dropping

`Finding` gained three fields, none of which feeds the fingerprint, so every dedup key Days 22–23
measured is unchanged:

| field | Trivy | Bandit | Checkov |
|---|---|---|---|
| `context` — `(line, content)` pairs | `CauseMetadata.Code.Lines` (38 of 43 misconfigs) | `code`, always (534 of 534) | `code_block` — **0 of 50**, see below |
| `resolution` | `Resolution`, a real sentence | none | `guideline`, a URL |
| `message` | `Message` — the only place the container is named | n/a | n/a |

Two constraints found in the data, both of which shape the diff builder:

- **Trivy caps a code block at ten lines** and marks the cut with an entry whose `Truncated` is true
  and whose content is empty. So a 43-line container block arrives as lines 22–30 and then a hole.
  A hunk header claims a start line and a count, so a gap inside the quoted lines makes the whole
  hunk a lie about the file — `_contiguous()` takes the run before the sentinel and the hunk is
  built from that alone.
- **`scan.sh` runs `checkov --compact`, which is exactly the flag that strips `code_block`.** All 50
  Checkov findings are therefore advice-only today. The parser handles the field anyway, so dropping
  one flag and re-scanning is the only change needed to get Checkov diffs.

### The bug this design exists to avoid

Ten of Trivy's `KSV-*` rules fire on the *same* container block. Ten independent diffs would each
insert their own `securityContext:` key, and the second one applied would produce duplicate YAML
keys — a patch that applies cleanly and then fails to parse. So candidates are grouped by insertion
point and emitted as **one** hunk carrying the union of the keys: the same collapse Day 22 does for
cost, done here for correctness.

Which surfaces the cost of Day 22's dedup key. A misconfiguration is identified by `(target, line)`,
and all five securityContext rules on one block report that block's `StartLine` — so they share one
fingerprint and `dedupe()` keeps exactly one of them. That is the right identity for triage (one
judgment about one misconfigured block) and the wrong one for fixes (one surviving rule means one
key in the hunk instead of five). `propose_fixes()` therefore reads the **pre-dedup** list and
dedups on the insertion point instead. Because those fingerprints are identical, the single
fingerprint it reports is still the one a triage result is keyed by.

### Refusals, and why they're the interesting output

A `Fix` is `{target, rule_ids, fingerprints, kind, diff, note}`. `kind="advice"` — with the reason
in `note` — is what comes back when:

| refusal | cause |
|---|---|
| the message doesn't name a container | `KSV-0030` says "Either Pod or Container should set…", `KSV-0106` says "container should drop all". 19 of the family's 23 findings in this corpus name one, in either quote style; 4 don't |
| a `securityContext` is already in the visible lines | only the first ten lines are visible, and merging into a mapping we can only partly see risks a second `securityContext:` key |
| the container isn't in the returned lines | it's declared past Trivy's truncation |
| the target isn't a repo file | a container image reference (`alpine:3.19 (alpine 3.19.1)`), or a path that climbs out of the repo — `target` arrives in a public request body and ends up in a patch header |
| the hunk overlaps another in the same file | overlapping hunks make `git apply` reject the *whole* patch, so one bad pair would cost every other fix in the file |

The `--name:`-matching is not cosmetic: a container block's `ports:` list contains `- name: http`
and its `env:` list contains `- name: LOG_LEVEL`, both indented deeper. A first-match anchor inserts
the `securityContext` inside `ports:`.

### Verify: the round trip is the only test that counts

```bash
cd services/security-triage
python fixes.py fixtures/this-repo.json > /tmp/proposed.patch   # patch to stdout, tally to stderr
cd ../.. && git apply --check -v /tmp/proposed.patch
```

A proposed diff that doesn't apply is worse than no diff, because a reviewer trusts the shape.

**Measured 2026-08-20:** 629 raw findings → 3 diffs (26 inserted lines across
`log-analyzer/k8s/deployment.yaml`, `kube-state-metrics.yaml`, `sandbox-demo.yaml`) and 610 advice,
with 4 findings refused for naming no container. The three hunks absorb 19 findings between them —
eight rules on each of the two `self-healing-agent` manifests, three on `log-analyzer`'s — which is
the merge doing its job: 19 separate diffs, each inserting its own `securityContext:`, is 16
patches that apply cleanly and then fail to parse. `git apply --check` clean on all three, no
offsets.
Applied in a scratch clone and re-scanned, the only `KSV-00xx` rule still firing on
`sandbox-demo.yaml` was `KSV-0001` — and that survivor is what the round trip was for:
`allowPrivilegeEscalation: false` is as mechanical as the eight rules that were already in the
table, and had simply been missed when the table was written by eye. `pytest` could never have
found that; only re-scanning a patched file could. The same re-scan showed `KSV-0030` gone despite
coming back as advice, because the hunk's `seccompProfile` key (contributed by `KSV-0104`)
satisfies it too — the fingerprint accounting under-claims on purpose, crediting only what it can
prove.

With `KSV-0001` added the patched `sandbox-demo.yaml` reports only `KSV-0117` and `KSV-0118`, and
both are correctly advice: `KSV-0118`'s pod-level half wants `spec.template.spec.securityContext`,
a different insertion point from the container line the finding anchored to, and `KSV-0117` wants an
existing `containerPort` *changed* to a value someone picks, which also cascades into the Service's
`targetPort` in another file. Known value, wrong scope — the second half of the "mechanical" test,
and the reason a scanner asked to check the patch agreed with what the module claimed.

Two things to expect from `git apply`: "applied with offset N" where one file got two hunks (each `Fix`
carries its own header, since a caller posts one fix per PR comment, so the second hunk's line
numbers were computed against the unpatched file), and a genuine failure if the fixture is older
than the manifests it describes — re-run `scan.sh` if the repo has moved on.

## `risk.py` — one score, and a threshold somebody chose

A risk threshold is a policy decision, and this module exists to make it explicit and per-repo
instead of leaving it implicit in *"did any scanner say CRITICAL"*. "No criticals" and "safe" are
different claims.

The score is a **weighted sum capped at 100**, not worst-finding-wins:

| Corpus | Score | At threshold 40 |
|---|---|---|
| 1 critical | 40 | fail |
| 3 highs, no critical | 45 | fail |
| 40 mediums, nothing worse | 100 | fail |
| 2 lows | 2 | pass |

Worst-finding-wins would score the last two rows 40 and 10 — one medium and two hundred mediums
indistinguishable — which is exactly the failure the day is about. The cap exists because past 100
the number stops carrying information: a repo at 340 and one at 980 both need "stop and look".

Two things are deliberately **not** in the formula:

- **`confidence`.** Day 23 measured it flat at every model size tried — 0.8 on a judgment the model
  had no business making, 0.8 again on an obvious one. Weighting a score by a number that doesn't
  vary buys nothing and disguises where the score came from. `priority` is the only judgment the
  model demonstrably makes.
- **`needs_human`.** It scores zero and is counted separately, because a declined judgment is not a
  low-risk one. It surfaces as `review_required` on the assessment and as a callout in the PR
  comment. Marked as a `ponytail:` ceiling in the module: a run that declined *everything* still
  returns `verdict: pass` with `review_required: true` beside it, and its own threshold isn't worth
  inventing until Day 27 measures what a normal `needs_human` rate even looks like.

`assess()` and `top_findings()` are separate functions on purpose. The score is a policy calculation
over judgments alone; the ranked list is presentation, and it has to join judgments back to the
findings they were about — a PR comment full of sixteen-character fingerprints tells a reviewer
nothing. Ties in the ranking break on the fingerprint so two runs over an unchanged corpus produce
a byte-identical comment; an unstable top-10 reads as a change in the codebase when nothing changed.

## `app.py` — 202 now, verdict later

```
POST /triage       -> 202 {run_id, status: "pending", findings_raw, findings}
GET  /triage/{id}  -> the run: pending | done | failed
GET  /health       -> unauthenticated, reports the policy this process actually loaded
```

The ack-now/answer-later split is Day 13's Slack bot and Day 20's `/alerts` again, at a worse ratio:
one model call per `ST_BATCH_SIZE` findings, and this repo's own fixture is 559 deduped findings —
over a hundred calls, minutes each on CPU Ollama. A synchronous endpoint would time out on every
real request, and the caller (a GitHub Actions job) would retry, doubling the work it just abandoned.

**Parsing and dedup happen synchronously**, in the request, even though they'd fit just as well in
the background task. Both are string arithmetic and take milliseconds on a 2.7 MB envelope, and
doing them up front means a malformed `scans` block is a `422` the caller can read instead of a
`failed` run it has to poll for. It also puts the finding count in the ack, which is the only number
the caller has to guess how long to poll for.

`propose_fixes()` gets the **pre-dedup** list and `triage_findings()` the deduped one — Day 24's
asymmetry, preserved: nine `securityContext` rules on one container block share a `(target, line)`
fingerprint, which is the right identity for one triage judgment and the wrong one for a hunk that
needs all nine keys.

### Three controls, from the first commit

This is a public multi-tenant endpoint whose work costs CPU-minutes of somebody else's inference,
so none of these are capstone work:

| | |
|---|---|
| **Bearer auth** | `ST_API_TOKENS`, comma-separated. Plural unlike `SHA_API_TOKEN` and the copilot's single token — one per onboarded repo, so revoking a leaked one is a list edit, and the rate limit has something per-caller to count against. The bucket key is a SHA-256 prefix of the token, never the token |
| **Body cap** | `ST_MAX_BODY_BYTES`, default 16 MiB. This repo's own envelope is 2.7 MB and it is a small repo |
| **Rate limit** | `ST_MAX_RUNS_PER_HOUR` per token. Not only anti-abuse: a repo whose CI retries a failed workflow four times would otherwise queue four full runs against a backend that serves about one. `429` is the honest answer — asking again doesn't make the work faster |

**The body cap is middleware, not a `Depends`, and that's the whole point of it.** FastAPI reads and
parses the request body *before* it solves a route's dependencies, so a `Depends(require_small_body)`
guard fires only after the megabytes it was meant to refuse have already been read and turned into
dicts. It was written as a dependency first; moving it is the only reason it caps anything. Middleware
returns a `JSONResponse` rather than raising `HTTPException`, because a raise there is outside the
handlers FastAPI installs and would surface as a `500`.

Run records live in a process-local dict, which makes `--workers 1` load-bearing for the third time
in this repo (the copilot's cache, the agent's proposals, now this), and a restart mid-run strands a
polling job on an id that will never exist again — it gets a `404` and fails the job, which is at
least the loud version. The dict is capped at `ST_MAX_RUNS` and evicts oldest-first, or a long-lived
container accumulates every envelope it has ever seen.

## The CI gate

Two workflows, and one of them is the product:

- **`.github/workflows/security-triage.yml`** — reusable (`workflow_call`). Job `scan` checks out the
  *caller's* repo, curls `scan.sh` from this one, installs the three scanners, and POSTs. Job
  `report` polls, renders a comment, posts it with the caller's own `GITHUB_TOKEN`, and exits
  non-zero above the threshold.
- **`.github/workflows/security_triage_ci.yml`** — this service's own lint and tests. No
  `build-and-push` yet: there is no `Dockerfile` until Day 28, and a publish job pointing at a
  context that doesn't exist is a red X on every merge to `main`.

**Why two jobs and not one**, given that the polling job occupies a runner for the whole wait
anyway: permissions. `scan` builds a request body out of untrusted repo contents and is granted no
token scopes at all (`permissions: {}`); `report` is the only half that can write to a pull request,
and it never touches the checkout.

`scan.sh` and `comment.py` are fetched with `curl` into `$RUNNER_TEMP` rather than by a second
`actions/checkout`, for a reason that only shows up once: `actions/checkout` can't place a repo
outside the workspace, and anything inside the workspace is a directory `scan.sh` then scans — the
triage tooling would appear in the caller's own findings. Both are stdlib-only and need no `pip
install` on the runner.

`comment.py` exists as a module rather than a heredoc because it is **the only output of the whole
pipeline a human reads**, and it has real branches. Inside the YAML it could only ever be exercised
by a live pull request; as a module it has nine tests and runs locally against any saved run:
`python comment.py run.json`.

The workflow also **overrides `repo`/`commit`/`branch` from the GitHub context**. `scan.sh` derives
them from git, and a `pull_request` checkout is detached, so `git rev-parse --abbrev-ref HEAD`
returns the literal string `HEAD`.

### Onboarding another repo

The whole integration, in the calling repo:

```yaml
# .github/workflows/security-triage.yml
name: Security Triage
on:
  pull_request:
    branches: ["main"]

jobs:
  triage:
    uses: crypticani/autonomous-infra-labs/.github/workflows/security-triage.yml@main
    permissions:
      pull-requests: write
    with:
      endpoint: https://triage.example.net
      risk-threshold: '40'      # omit to take the service's default
    secrets:
      api_token: ${{ secrets.SECURITY_TRIAGE_TOKEN }}
```

A URL and a token. Nothing on the service side is per-repo: `repo` is a label, not configuration.
This repo dogfoods it through `.github/workflows/security_triage_self.yml`, which is that same
snippet plus an `if: vars.SECURITY_TRIAGE_ENDPOINT != ''` guard — the endpoint isn't live until Day
28, and a skipped job is better than a red X on every pull request for a week.

## Tests

```bash
cd services/security-triage
python -m pytest -v   # 15 scanners + 10 provider + 11 triage + 21 fixes
                      #   + 14 risk + 14 app + 9 comment = 94, if green
```

`test_scanners.py` (15, Days 22 and 24): each scanner's real shape, a missing-scanner-key envelope, CVE
dedup across two scans, distinct packages sharing a CVE ID staying distinct, cross-scanner
location dedup, fingerprint stability, and a sanity check against the real fixture. Day 24 added
five: Trivy's context stopping at the truncation sentinel (and a genuine blank line surviving it),
a secret's context arriving already redacted and from a different place in the JSON, Bandit's
numbered `code` string recovering the original indentation, Checkov's `guideline`/`code_block`, and
a finding with none of the three defaulting to empty rather than null.

`test_provider.py` (10, Day 23): transport-failure status mapping and the schema/JSON-body shape
sent to each provider, using the same fake-response/monkeypatch style as
[knowledge-copilot's test_llm.py](../knowledge-copilot/tests/test_llm.py) — no real network call.

`test_triage.py` (11, Day 23): the two guards above, using a `FakeProvider` injected in place of
`get_triage_provider()` rather than mocking `requests` or `google.genai` a second time — the same
dependency-injection shape `triage_findings(provider=...)` exists for. No test triages the real
fixture end to end; that needs a real model and minutes per batch, which is what `triage.py`'s
`__main__` script above is for, run by hand, not by `pytest`.

`test_fixes.py` (21, Day 24): the exact hunk text for a synthetic container block — derived from the
block rather than typed out, so a miscounted space in a test literal can't be the reason it fails —
the sibling-rules-into-one-hunk merge and its key ordering, the `ports:`/`env:` anchor trap, a second
container getting its own anchor, every refusal branch above, path normalisation from all three
scanners' spellings, and a check that every diff the real fixture produces is arithmetically
well-formed (start lines equal, quoted-line count matching the header, no `-` lines in an
insert-only patch). What `pytest` can't prove is that a patch applies; that's the `git apply --check`
round trip above.

`test_risk.py` (14, Day 25): the scoring rows in the table above, including the one that justifies
the whole design — forty mediums with no critical and no high scoring 100 and failing, which
worst-finding-wins would pass. Plus the per-request threshold override, a threshold of `0` failing a
clean run (a legal bar for a repo to ask for), `needs_human` scoring nothing while still setting
`review_required`, and the ranking being stable when the same corpus arrives in a different order.

`test_app.py` (14, Day 25): the endpoint, through `TestClient`, with `triage_findings` monkeypatched
— `TestClient` runs background tasks before returning from the POST, so without that every test
would make real Ollama calls. The envelope in it is a real Bandit document rather than a
pre-built `Finding`, so the test exercises `scanners.py` on the way through. Covers the 202 → verdict
round trip and the fingerprint join reaching `top`, a provider failure being a *recorded* failure
rather than a run stuck `pending` forever, `401` on both routes, the per-token rate limit (one token
being refused while another isn't), the body cap, and oldest-first eviction.

`test_comment.py` (9, Day 25): the markdown itself — the pass/fail mark, the table carrying rule ids
and paths rather than fingerprints, zero counts omitted, the `needs_human` callout, advice fixes
staying out of the diff block, and the one that matters most: a `failed` run producing a comment
that cannot be mistaken for a clean bill of health.

## Running it

```bash
cd services/security-triage
uvicorn app:app --port 7300 --workers 1     # --workers 1 is load-bearing, see above
```

## Not built yet

- Runtime signals (K8s audit log events) through the same pipeline (Day 26).
- Cost/latency benchmark, Ollama vs. Gemini (Day 27).
- Metrics, Grafana dashboard, eval harness, deployment to appsrv (Day 28).
