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
raw findings:     111  (trivy 45, bandit 13, checkov 53)
after dedupe:       43
```

**These numbers were 629 raw / 559 deduped until Day 27**, and the difference is one line in
`scan.sh`. Measuring cost-per-run meant looking at the whole corpus for the first time rather than
the first 15 findings, and the overwhelming majority of it turned out to be `bandit:B101`, "use of
assert detected", inside test files. Five consecutive lines of `test_alertmanager.py` were five
separate findings. An assert is what a test file is made of, so bandit's own guidance is to exclude
test paths; the scan was asking for them.

`BANDIT_EXCLUDE` is overridable precisely so this is checkable rather than asserted — running the
same scan both ways, same repo, same afternoon:

```bash
BANDIT_EXCLUDE="./venv,./.venv,./node_modules" scan.sh . /tmp/before.json
scan.sh . /tmp/after.json
```

| | tests scanned | tests excluded |
|---|---|---|
| deduped findings | **841** | **44** |
| of which `bandit:B101` | 791 (**94%**) | 0 |
| model calls at `ST_BATCH_SIZE=5` | 168 | 9 |
| full-corpus run, CPU Ollama | ~6 hours | ~20 min |

(The committed fixture is 43 rather than 44 — it was generated a few commits earlier. The 559 figure
in the first paragraph is the fixture as committed on 2026-08-21; the 841 above is a same-day
both-ways scan, which is the honest comparison since the repo grew in between.)

And the part that isn't about cost at all: the model *declines* B101 findings. Sampling five of them
returned `needs_human` five times out of five, so a full-corpus run would have marked ~94% of its
output as needing human review — routing 791 non-issues to a person, which is the exact inverse of
what this service is for. Every guard would have stayed green: no invented fingerprints, no dropped
results, valid JSON throughout. Day 23 found that a model can satisfy every check and produce
garbage; this is the same lesson one layer earlier, where the *input* satisfies every check and is
still garbage. The cheapest model call is the one you don't make.

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
`repeat_penalty: 1.3`, and — the more reliable of the two — a `max_length` on
`TriageResult.explanation`, which is part of the JSON schema Ollama grammar-constrains generation
against, not a check applied after the fact. A sampling parameter is a nudge; a schema bound is a
guarantee the grammar itself won't produce a longer string, loop or not. (That bound was 280 here
and is `EXPLANATION_MAX = 160` since Day 27 — see the cost section below for why the prompt now
quotes the same number the schema enforces.)

## `triage.py` — batched, structured triage

`triage_findings()` chunks deduped findings into groups of `ST_BATCH_SIZE` (default 5) and sends
one model call per group, asking for `{fingerprint, exploitability, impact, priority,
explanation, confidence}` per finding — schema-constrained decoding (Ollama's `format`, Gemini's
`response_schema`) rather than parsing free text, so a malformed answer is a validation error to
catch, not a regex to write. **That field order is deliberate and was wrong until Day 27** — see
"Field order is behaviour" below.

Two guards:

- **Every returned fingerprint must be one that was actually sent.** The same failure shape as Day
  10's invented citations — a model naming something it wasn't given — gets the same fix: drop
  what wasn't in the input rather than trust it. `triage_batch()` logs and drops any fingerprint
  outside the batch it sent.
- **`needs_human` is a legal `priority`**, not an error path. A model forced to choose among four
  real severities on a finding it can't actually judge doesn't refuse — it guesses, and the guess
  is indistinguishable from a real triage. The fifth option is what makes the other four
  trustworthy.

Run it against a slice of the real fixture (defaults to the first 15 of the 43 deduped findings;
since Day 27 the full corpus is 9 calls at batch size 5, so `bench.py` runs all of it):

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
one model call per `ST_BATCH_SIZE` findings, minutes each on CPU Ollama. Day 27's scan fix took this
repo's own scan from 841 deduped findings to 44 — 9 calls instead of 168 — but 9 calls still runs
20+ minutes, so the split is not something the smaller corpus makes optional. A synchronous endpoint
would time out on every real request, and the caller (a GitHub Actions job) would retry, doubling
the work it just abandoned. A repo larger than this one puts it straight back into the hundreds.

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

## Runtime signals through the same pipeline — Day 26

A Kubernetes audit event has no file, no line, no package and no scanner severity, and it describes
something that **already happened** rather than something that might. It is still a `Finding`. The
cost of accepting one was a parser and one line in `_PARSERS`:

```python
_PARSERS = {"trivy": ..., "bandit": ..., "checkov": ..., "k8s_audit": _parse_k8s_audit}
```

No second endpoint, no second pipeline, no second score. `dedupe`, `triage.py`, `risk.py` and
`comment.py` are untouched and never learned that runtime events exist — which is the day's whole
claim: **static findings and runtime events are the same triage problem if the normalisation seam is
shaped around "a thing worth a judgment, and where it is" rather than around what scanners happen to
emit.**

`runtime.py` is the client-side half, `scan.sh`'s counterpart: it reads an audit log, keeps the
newest 4000 events, and writes the same envelope with the events under a fourth `scans` key. `repo`
carries the cluster name — it is a label, and `commit`/`branch` stay empty because a cluster has no
commit and inventing one would put a lie in the PR comment header. It also runs the server's own
normalisation and prints the result, which is the only way to see both halves of the seam agree
before spending a model call on them — through `rich` when stdout is a tty and as plain markdown
otherwise, the same two gates `comment.py` uses, so the JSON it writes never carries an escape code.

### The audit policy is two filters, not one

`kind/audit-policy.yaml` bounds *volume*; the rule table in `scanners.py` bounds *meaning*. An
audited event that maps to no rule is not a finding, and that is the normal case — `kubectl create
secret` is logged and ignored, because creating a Secret is how the cluster is supposed to work and
reading one is the interesting verb.

Two decisions inside the policy matter more than the rest. **The control plane's own traffic is
excluded first**, because the kubelet reads a Secret for every ServiceAccount token and image pull
and the controllers list them on a loop — rules match in order, so without those two `level: None`
rules at the top a human reading a Secret is one line in ten thousand. And **the level is `Metadata`,
never `RequestResponse`**: the request body of a Secret write and the response body of a Secret read
both contain the secret, so the higher level would turn the audit log into a second, plaintext copy
of every credential in the cluster. Metadata still carries who, what, when, from where, and
allowed-or-refused, which is everything triage judges on.

Seven rule ids: `K8S-EXEC`, `K8S-ATTACH`, `K8S-PORTFORWARD`, `K8S-SECRET-READ`, `K8S-TOKEN-MINT`,
`K8S-RBAC-WRITE` (the four RBAC resources share one row — they are the same judgment, that something
changed who can do what), and `K8S-FORBIDDEN` for anything the API server refused. That last one is
not keyed on a resource at all and short-circuits the table: a refusal against an audited resource is
worth triaging whatever the verb was.

### The actor is in `target`, and that is not cosmetic

```
K8S-SECRET-READ  kubernetes-admin@sandbox/demo-creds
K8S-FORBIDDEN    kubernetes-admin as system:serviceaccount:sandbox:default@kube-system/secrets
```

Events are grouped into one finding per (rule, actor, object) with a count and a last-seen
timestamp, because an audit log is a stream and a triage queue is a list of decisions — fifty `get
secret` requests from one identity against one Secret is one judgment with a count on it, not fifty
identical ones, and at one model call per `ST_BATCH_SIZE` findings that difference is the run.

`target` is where the actor is spelled, because it is one of the three fields `_fingerprint` reads
for a finding with no package and no line. Keeping the actor beside `target` instead of inside it
would give two different users reading the same Secret one shared fingerprint, and `dedupe` would
silently drop one of them. It also lands in the PR comment's `Where` column, where "who" is the
first thing a reviewer asks. Impersonation names both halves — `kubectl --as` is how an admin
borrows a lesser identity and also how an attacker does something while it looks like it came from
somewhere else.

`severity_raw` stays `None`. The other three parsers copy a severity their scanner assigned; nothing
assigned one here, and a table of invented severities would be `scanners.py` quietly doing the
judging `triage.py` exists to do.

### The cluster, and the two things that cost the afternoon

The audit policy can only be enabled at cluster-creation time — `kind` takes `kubeAPIServer` args
and the policy file through cluster config and nothing else. Rather than recreate the Day 21 cluster
(which would mean re-applying its RBAC, re-minting the agent's ServiceAccount token, rewriting
`.secrets/kubeconfig` and restarting `tailscale serve`), Day 26 stands up a **second** cluster named
`audit` beside it. `kind` runs both; nothing the self-healing-agent depends on was touched.

Two failures worth writing down, neither of them about auditing:

**kubeadm v1beta4 takes `extraArgs` as a list of `name`/`value` pairs, not a map.** `kind` generates
a v1beta4 config for current node images, and every audit-logging example in circulation — including
the one on kind's own configuration page — uses the older map form. A map-shaped patch does not
error: it fails to merge, and the cluster comes up perfectly healthy with no audit log at all. The
committed config was written against `docker exec <node> cat /kind/kubeadm.conf` from the existing
cluster rather than against the documentation.

**`fs.inotify.max_user_instances` is 128 by default, and one kind cluster spends most of it.** The
second cluster's kubelet died 89 times with `inotify_init: too many open files`, cAdvisor failing to
start, no static pods ever created — so `kubeadm init` sat there getting connection-refused until
its client rate limiter gave up, and the error it printed (`client rate limiter Wait returned an
error`) named nothing that had actually gone wrong. The fix is one sysctl. The lesson is that the
loudest error in the output was three layers above the cause, and `crictl ps -a` returning *nothing*
— not a crash-looping apiserver, nothing at all — is what pointed at the kubelet instead.

### Verified end to end, and what the first real log actually contained

The first capture, of a cluster that had been alive for six minutes:

```
36 events -> 22 findings
```

**19 of those 22 were the cluster installing itself** — kubeadm and kind wiring up CoreDNS,
kube-proxy, kindnet and the local-path provisioner, every one of them a genuine RBAC write by
`kubernetes-admin`. Three findings were things a human did. Recreating the cluster and capturing
before touching anything reproduces it without the human half: **31 events → 19 findings, all
nineteen of them the install.** There is no code fix for this and deliberately none was written: `kubeadm` and `kubectl` authenticate as the same identity, and the
only field that separates them is `userAgent`, so filtering on it would mean anyone who sets
`User-Agent: kubeadm` disappears from triage. Choosing the window is the operator's job, so the run
sheet truncates the log once the cluster is up.

After truncating, the run the plan asked for:

```
5 events -> 4 findings, score 21 / threshold 40 -> pass
high 1 · medium 1 · low 2
```

| Priority | Rule | Where |
| --- | --- | --- |
| high | `K8S-RBAC-WRITE` | `kubernetes-admin@sandbox/demo-peek` |
| medium | `K8S-SECRET-READ` | `kubernetes-admin@sandbox/demo-creds` (2 requests) |
| low | `K8S-FORBIDDEN` | `kubernetes-admin as system:serviceaccount:sandbox:default@kube-system/secrets` |
| low | `K8S-EXEC` | `kubernetes-admin@sandbox/flaky-app-d8dcd64c-ss27d` |

**The round trip found a bug 111 green tests could not.** A `create` has no `name` in its
`objectRef` — the name is in the request body, which `Metadata` level deliberately does not carry —
and neither does a `list`. So a refused `create clusterrolebinding` and a refused `list secrets`
both came out as `kubernetes-admin@cluster`: one fingerprint for two unrelated refusals, and
`dedupe` keeps one. The resource now stands in when there is no name. The test that existed asserted
the lossy behaviour as if it were correct, which is the second time this week a test has documented
a bug instead of catching it.

**Two model-quality problems, both visible only in the rendered comment.** Every explanation in that
table asserts "The impact is high" — including both findings the model itself rated `low`, so its
prose contradicts its own priority on half the rows. And the explanations still run to ~270
characters against a prompt asking for "one short sentence", the same measurement Day 25 recorded.
Both are Day 27 prompt work with a token count attached, not a patch today.

### Run sheet

```bash
cd services/security-triage/kind
mkdir -p /tmp/audit
sed "s|AUDIT_DIR|$PWD|" audit-cluster.yaml > /tmp/audit/cluster.yaml   # bind mounts need an
kind create cluster --config /tmp/audit/cluster.yaml                   # absolute path

docker exec audit-control-plane ls -l /var/log/kubernetes/             # audit.log, growing
docker exec audit-control-plane truncate -s 0 /var/log/kubernetes/audit.log

kubectl --context kind-audit create namespace sandbox
kubectl --context kind-audit apply -f ../../self-healing-agent/k8s/sandbox-demo.yaml
kubectl --context kind-audit -n sandbox create secret generic demo-creds --from-literal=token=x

kubectl --context kind-audit -n sandbox exec deploy/flaky-app -- ls /
kubectl --context kind-audit -n sandbox get secret demo-creds -o yaml
kubectl --context kind-audit --as=system:serviceaccount:sandbox:default -n kube-system get secrets

docker exec audit-control-plane cat /var/log/kubernetes/audit.log > /tmp/audit/audit.log
cd .. && python runtime.py /tmp/audit/audit.log /tmp/envelope.json kind-audit
```

`AUDIT_DIR` is a placeholder on purpose: a docker bind mount needs an absolute host path and this
repo does not commit machine-specific ones, so the committed file fails loudly instead of quietly
mounting the wrong thing. The envelope then POSTs to `/triage` exactly like a scanner envelope, and
`comment.py` renders the result with no idea it came from a cluster.

## Cost and latency — Day 27

`bench.py` sets `ST_BATCH_SIZE` from a measurement instead of Day 23's guess of 5.

**The design is shaped by a 15-minute Ollama budget, and that constraint made it better rather than
worse.** A matched comparison — every batch size triaging the same findings — is the obvious
benchmark and it is unaffordable: batch 1 over ten findings is ten CPU calls, ~20 minutes on its
own. But latency per batch size does not need heavy sampling, because it is analytically predictable
from token counts. The system prompt is a fixed cost charged once per call regardless of how many
findings ride along; prompt and output tokens grow with the findings in it. So measure tokens
carefully at a few sizes and the curve falls out. Wall clock is there to check the model, not to be
it.

### The sweep

One call per config, against the cleaned 43-finding corpus. Every row measured.

| batch | wall | p_tok | o_tok | p_tok/finding | find/min | ret/sent | nhuman | expl max | trunc | contra | primis | tok/1k | calls/1k |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 83.5s | 488 | 90 | 488.0 | 0.72 | 1/1 | 1 | 62 | 0 | 0 | 0 | 578,000 | 1000 |
| 3 | 77.3s | 618 | 262 | 206.0 | **2.33** | 3/3 | 0 | 118 | 0 | 0 | 2 | 293,333 | 333 |
| 5 | 129.9s | 770 | 392 | 154.0 | 2.31 | 5/5 | 0 | 89 | 0 | 0 | 4 | 232,400 | 200 |
| 10 | 339.7s | 1176 | 825 | **117.6** | 1.77 | 10/10 | 0 | 118 | 0 | 0 | 9 | **200,100** | **100** |

`p_tok/finding` falls monotonically — 488 → 206 → 154 → 117.6 — which is the fixed system prompt
being spread across more findings, and the whole case for batching in one column.

Refitting the curve on these four points: `prompt = 396 + 77.2n`, `output = 6.6 + 81.2n`. Close to
the fit taken from three calls on the old corpus (`368 + 70.5n` / `14 + 78.5n`), which is the more
useful result — the per-finding and per-call constants survived the corpus changing underneath them,
so they are properties of the prompt and the model rather than of one fixture.

An earlier partial sweep, run before the corpus was cleaned and while `ST_LLM_TIMEOUT` was still
300s, produced two things this table cannot show. Batch 10 **timed out** at the old ceiling. And the
same five findings at batch 5 measured 158.7s, 208.4s, then over 300s — a 2x spread on identical
work, from CPU throttling under sustained load plus the fact that a batch the model declines writes
longer explanations than one it judges, so output tokens vary with the *answer*. That variance is
why the timeout moved to 600s, and it is load-bearing for the decision below.

### The token curve, and a prediction that held

Least squares over sample 1's three points:

```
prompt tokens = 368 + 70.5 x findings
output tokens =  14 + 78.5 x findings
```

The 368 is the system prompt. At batch 1 it is **85% of the call** — which is the entire argument
for batching, in one number.

Fitted on batches 1 and 3 alone, the model predicted batch 5 at 702 prompt tokens and 140.4 per
finding *before that row was measured*. It came in at 724 and 144.8, within 3%. That is what makes
extrapolating a larger corpus honest rather than decorative.

### What actually bounds batch size

`ST_MAX_TOKENS=1536` is the obvious ceiling and it is the wrong one — at ~78 output tokens per
finding it does not bind until roughly 19 findings. **The timeout binds first.** Wall clock here
tracks output token count with a large per-call constant, and the observed generation rate was
1.7–3.2 output tokens/sec on laptop CPU, so ten findings' worth of output needs ~270–320s. Batch 10
straddled the then-default 300s ceiling and lost.

So raising `ST_BATCH_SIZE` means raising `ST_LLM_TIMEOUT` first, and that is the order this was done
in — the 600s default makes batch 10 reachable where 300s did not, and the table above is that
change paying off: **339.7s, completed, 10/10 returned.** The row exists because the timeout moved.
`ST_MAX_TOKENS` becomes the binding ceiling only on a backend fast enough that the clock stops
mattering, which is the hosted one.

### Variance is the real finding

The same five findings at batch 5 measured **158.7s, 208.4s, and then over 300s**. Same input, same
batch size, a 2x spread ending in a timeout. Two causes, both real: a laptop CPU under sustained
load throttles, and a batch the model *declines* writes longer explanations than one it judges — so
output tokens, which set the wall clock, vary with the answer and not just the input.

A timeout is the worst available outcome, because the full budget is spent and nothing comes back.
That is what moved this service off the shared `LLM_TIMEOUT` onto its own **`ST_LLM_TIMEOUT`,
defaulting to 600s**: patience converts a wasted 300s into a slow success, and nothing here is
latency-sensitive enough to prefer the failure — `/triage` returns a `run_id` immediately and joins
the work in the background. A copilot answer is one call a human is waiting on; a triage batch is
one of many inside a background run. One shared knob could only ever be right for one of them.

### The decision

**`ST_BATCH_SIZE` stays 5**, and the honest version is that it is not the cheapest option.

**Batch 10 is cheaper.** 200,100 tokens per 1,000 findings against batch 5's 232,400, and 100
requests where batch 5 needs 200 — and on a free-tier quota, requests are the scarce unit. On cost
alone the default should be 10.

What keeps it at 5 is **margin against the variance measured above**. Batch 10 ran 339.7s, which is
57% of the 600s timeout; double it, as the same config demonstrably did on a loaded laptop, and it
exceeds the ceiling and returns nothing. Batch 5 at 129.9s doubles to 260s and still lands. A
timeout is not a slow answer, it is a lost batch plus the full budget spent — so the cheaper
configuration is only cheaper when it completes, and 2x variance says batch 10 will not always.
Throughput agrees but weakly: 2.31 findings/min at batch 5 against 1.77 at batch 10.

There is a quality argument in the same direction, though it is a weaker one because the checker is
coarse: `primis` — verdicts that contradict their own ratings — climbs with batch size, 0, 2, 4, 9.
Whatever is going wrong with priority gets worse the more findings share a call.

Day 23 guessed 5. It was right, and not for the reason it assumed: not because bigger batches are
worse, but because bigger batches on this hardware are less reliable.

### The full corpus, and the token model checked against it

The whole 43 findings, 9 calls at batch 5: **1,781s (29.7 min), 6,352 prompt + 3,472 output
tokens, 43/43 returned, `needs_human` 4 (9.3%)**.

The point of that run is what it does to the curve fitted from three single calls. Predicting
9 calls over 43 findings from `368 + 70.5n` and `14 + 78.5n`:

| | predicted | measured | error |
|---|---|---|---|
| prompt tokens | 6,343 | 6,352 | **0.13%** |
| output tokens | 3,501 | 3,472 | **0.85%** |

Three calls' worth of measurement predicted a nine-call run to within 1% on both axes. That is the
justification for the whole approach: with the token curve in hand, a corpus figure does not need to
be sat through. It also retires the "extrapolated" caveat the plan expected this table to carry — the
number is measured, and the extrapolation is what got validated.

`needs_human` at 9.3% is also the first trustworthy reading of that rate. The two 5-finding slices
had said 0% and 100%; both were artefacts of what happened to be in them.

### Field order is behaviour, not formatting

The full-corpus output showed `priority` **anti-correlated with its own inputs** — `expl=medium
imp=high` scoring `low`, `expl=low imp=medium` scoring `high`. The prompt says priority is "your
overall call, weighing both of the above", and the cause is that "above" was false: `priority` was
the first field in `TriageResult`, and Ollama grammar-constrains generation in declared field order,
so it was decided before either rating existed.

Reordering to `exploitability → impact → priority → explanation → confidence` — the dependency order
the prompt always described — measurably changed three things on a 15-finding re-run:

| | before | after |
|---|---|---|
| longest explanation | 160 (3 cut mid-clause) | **89** |
| explanations pinned to the cap | 3 | **0** |
| priority/rating mismatches | anti-correlated throughout | 8 of 10 |

Explanations stopped truncating, got much terser, and began *citing the ratings above them* — "No
HEALTHCHECK defined is low exploitability with medium impact." The model is visibly conditioning on
fields generated earlier, which is chain-of-thought obtained through the schema rather than asked
for in the prompt.

**It did not fix priority calibration, and the honest version of that is worth more than a claim
that it did.** The failure changed shape rather than going away: from random anti-correlation to
consistent *over*-calling, with 8 of 10 judged findings landing on `high`, all at `expl=low,
imp=medium`. The prompt rule added the same day forbids only the two extremes — high+high is not
low, low+low is not high — and `low+medium` sits between them, unconstrained. When 80% of findings
are `high`, the field carries no information whichever way the mismatch counter is banded.

Fixing that means an explicit severity matrix, and validating one needs golden labels to measure
against rather than 15 unlabelled findings. `bench.py` now reports the mismatch count (`primis`) so
the defect is tracked rather than rediscovered.

The same reorder, for the same reason, was applied to `log-analyzer`'s `LogAnalysis` on the same day.
Two services, one structural bug: **whichever field a grammar-constrained schema declares first is
decided before the model has reasoned about anything.**

### Quality, which is why this is not a throughput benchmark

`ret/sent` was perfect at every size that completed — no dropped or invented fingerprints. And both
Day 26 prompt problems are fixed and verified on real output:

- **Explanations ran ~270 characters against a prompt asking for one short sentence.** The cause was
  two bounds disagreeing, only one of which the model treats as real: `max_length` is
  grammar-constrained token by token during decoding, "one short sentence" is a suggestion. Given a
  280-character budget it wrote 280 characters. `EXPLANATION_MAX` is now 160, quoted in the prompt
  *and* enforced in the schema, with a test asserting the two still match. Measured: 84–131
  characters on judgments.
- **Explanations asserting "the impact is high" on findings rated `impact: low`.** Each field was
  individually valid, so every guard passed. The prompt now requires the sentence to agree with the
  ratings. `bench.py` counts violations; the count is **0** across every config above.

And one correction worth recording, because it cost an hour of chasing the wrong thing: the
full-corpus run reported **4 contradictions, and all 4 were the checker's fault**. It matched the
substring `"easily exploit"` inside `"not easily exploitable"` — a phrase that *agrees* with an
`exploitability: low` rating. A checker that invents violations is worse than one that misses them,
because it sends you looking for a model bug that was never there. Negation guard and a test are in;
the true count was 0 all along.

Explanation length pressing the cap turned out to be a symptom of the field-order bug rather than a
tuning problem. It reached 158–160 while `explanation` was generated before the ratings — the model
was reasoning *toward* a verdict inside the sentence — and fell to 89 once the ratings came first and
the sentence only had to report them. `EXPLANATION_MAX` stayed at 160 throughout; nothing about the
cap changed, only what the model had to do inside it.

## `metrics.py` and `/metrics` — Day 28

Same shape as [self-healing-agent/metrics.py](../self-healing-agent/metrics.py), with one label that
none of the other three services needed: **`repo`**. This is the only multi-tenant service here, so
"how many runs" and "how much risk" are the wrong questions on their own — the useful ones are
whose. A `failed` counter climbing for one repo is a broken envelope; climbing across all of them is
the model backend.

| metric | labels | what it answers |
|---|---|---|
| `st_runs` | `repo`, `outcome` | volume and failure rate per tenant (`accepted`/`done`/`failed`) |
| `st_findings` | `repo`, `stage` | `raw` → `deduped` is the dedup ratio; `deduped` → `triaged` is the model dropping findings |
| `st_verdicts` | `repo`, `verdict` | how often the gate actually blocks a repo |
| `st_risk_score` | `repo` | score distribution, bucketed on `risk.WEIGHTS`' own boundaries |
| `st_priorities` | `priority` | the `needs_human` rate — a model property, deliberately not per-repo |
| `st_run_duration_seconds` | — | wall clock, buckets to an hour, failures included |
| `st_model_tokens` | `direction` | Day 27's token counters where a deploy can read them |
| `st_refusals` | `reason` | which of the three controls refused (`auth`/`rate_limit`/`body_size`) |

Two of those are worth the words.

`st_risk_score` is a **histogram, not a gauge.** A gauge holds the last run's score, which on a repo
that scans every push is whatever landed most recently and says nothing about the trend. The buckets
are `1, 4, 15, 40, 55, 70, 85, 100` — one low, one medium, one high, one critical (and the default
threshold), three highs, and so on — so a bucket boundary means something rather than being a round
number.

`st_priorities` carries **no `repo` label** and that is the one deliberate asymmetry. The
`needs_human` rate is a property of the model, not of a tenant: Day 23 established that a model can
satisfy every guard and decline everything, and splitting that signal across repos would divide the
evidence for a question nobody is asking per-repo.

### The label that arrives from the internet

`repo` is a bare string in the request body — `app.py` never validates it, because it is a display
value rather than an identifier. That makes it an **unbounded-cardinality hazard**: a holder of one
valid token can send a distinct `repo` per request and grow the registry until the process dies,
carrying the whole set on every scrape along the way. So `metrics.repo_label()` bounds it — the
first `ST_MAX_REPO_LABELS` (default 50) distinct repos get their own series and everything after
folds into `other`. Well above any plausible deploy, since each onboarded repo needs a token added
by hand, so in practice it never fires and the graphs stay per-repo.

## `eval_triage.py` — the gate the test suite cannot be

153 unit tests can be green while the model has quietly got worse, because **no unit test in this
repo ever calls a model**. Day 27 is the worked example: moving `priority` below `exploitability`
and `impact` in the schema inverted the judgments, and the only reason anybody noticed is that a
full corpus happened to get read that afternoon. Nothing failed. This is that afternoon in one
command.

```bash
cd services/security-triage
python eval_triage.py                 # the committed fixture, ST_BATCH_SIZE findings per call
python eval_triage.py --batch-size 1  # one per call, removes batch interaction from the result
python eval_triage.py --limit 4       # a quick check against a slow backend
```

`eval_set.json` is 12 findings from the committed corpus, each with a **band rather than an exact
priority**, and that is the central design decision. Local Ollama is not reproducible even at
`temperature: 0` — Day 27 made two changes off eval movements that turned out to be pure
batch-dependent variance — so an eval asserting `priority == "high"` would flap, get ignored, and
then get deleted. Each case declares only what is defensible about that finding:

| case | band | why |
|---|---|---|
| `CVE-2026-45829` chromadb RCE | `>= high` | a published CVE with arbitrary code execution, in a dependency this repo installs |
| `jwt-token` in `.secrets/kubeconfig` | `>= high` | a live ServiceAccount token on disk — the highest-stakes finding in the corpus |
| `KSV-0118` no securityContext | `>= medium` | real, but on a read-only metrics exporter |
| `KSV-0001` privilege escalation | `>= medium` | a genuine container-escape precondition |
| `KSV-0048` RBAC can manage pods | `>= medium` | the self-healing agent's own Role — the permission that lets it delete things |
| `CKV2_GHA_1` workflow `write-all` | `>= medium` | the supply-chain finding: anything in that workflow can push to the repo |
| `B101` assert in `triage.py` | `<= low` | in a `__main__` demo block. Day 27 deleted 791 of these as noise; this one is kept so the eval notices if they start getting inflated again |
| `B105` "hardcoded password" | `<= low` | the literal is `/var/run/secrets/.../token`, a well-known path |
| `B106` "password: prompt" | `<= low` | Bandit matched a keyword argument name |
| `DS-0026` / `CKV_DOCKER_2` no HEALTHCHECK | `<= low` | an availability nicety, reported twice by two scanners |
| `KSV-0013` image tag `:latest` | `<= medium` | a real operability problem, a security one only at one remove |

**`needs_human` is graded by which bound the case carries**, which follows from what declining
means rather than from where it would sit on a scale. A case with a `min` is one this repo says is
definitely serious, so declining it is a miss. A case with only a `max` is noise, and declining it
is conservative rather than wrong — the finding reaches a human through `review_required` instead of
being scored, which is where it belonged. The consequence is the useful one: **Day 23's 1.5b model,
which declined all five findings it was shown while satisfying every guard, fails this eval
outright.**

Two smaller things the harness does because they were cheap and the alternative is silent:

- **A case naming a fingerprint that is not in the fixture exits `2`.** A fingerprint is
  `(scanner, rule_id, target, line)` hashed, so a regenerated fixture — a moved line, a renamed file
  — changes it. Without the check that case just stops being evaluated, and the eval keeps passing
  while testing less than it claims.
- **Cases run in eval-set order, not fixture order,** so which findings share a call is stable. Batch
  composition changes the prompt, and a shuffled corpus would show up as model variance rather than
  as the eval's own doing.

Not measured yet: an actual run of this against Ollama. The bands are written from the corpus, and
the first real run is what says whether the model clears them — that number goes here when it exists.

## Every service's eval, one command

```bash
python eval_all.py                    # all four, from the repo root
python eval_all.py security-triage    # one
python eval_all.py --selftest         # the runner's own parsing; needs no backend
```

The claim this repo makes at the end of thirty days is that four AI services are production-ready,
and for an AI system "every service has an eval you can run in one command" is that claim in a form
somebody can check. [`eval_all.py`](../../eval_all.py) is it: four subprocesses, one table.

| service | measures | backend |
|---|---|---|
| log-analyzer | severity vs a 5-case golden set | ollama |
| knowledge-copilot | soft hit@1 on the shipped retrieval config | ollama + chroma |
| self-healing-agent | the proposed action, including when it should be none | gemini |
| security-triage | priority bands | ollama |

Subprocesses rather than imports, because `app`, `provider` and `errors` each exist three times over
in this repo and would collide in one process — and because a subprocess is the only boundary that
makes "run this service's eval" mean the same thing here as it does by hand. The contract is one
line: each eval prints `EVAL_RESULT {"passed": n, "total": n}` last, and its exit status is the
verdict. Both are reported, because they answer different questions. An eval that prints no such
line still gets its row and a `-` rather than crashing the runner.

The `measures` column is not decoration. The four evals grade genuinely different things, and a table
of bare pass counts would imply they are comparable — 12/12 on triage bands and 10/12 on retrieval
hit@1 are not the same kind of number.

**knowledge-copilot reports rather than gates,** and that is deliberate: `eval_retrieval.py` is a
configuration *sweep* where most rows are controls that are supposed to score worse, and this repo
has no measured baseline to set a regression bar from. It grew a `--floor N` flag that exits 1 below
a threshold; the flag has no default, because a bar picked before the baseline was measured is just
a number chosen to pass. Set it in `eval_all.py` once a run is on record.

## Deploying it — Day 28

`Dockerfile`, a multi-arch GHCR publish job in `security_triage_ci.yml`, a `docker-compose.prod.yml`
entry behind a loopback bind, and `k8s/` manifests. Three things in there are not copies of the
sibling services.

**`--workers 1` is load-bearing three times over here,** where in the other services it was once.
`_runs` holds every run's verdict in process memory, `_starts` holds the per-token rate-limit
buckets, and `prometheus_client`'s default registry is per-process — so a second worker would 404 on
runs it never saw, allow double the rate limit, and export half the metrics on each scrape. The
Deployment pins `replicas: 1` for the same reason, with `strategy: Recreate`, because a
RollingUpdate would briefly run two pods and that is the two-worker problem arriving during every
deploy.

**The uid is numeric.** `adduser --system` picks whatever is free in 100–999 and `USER appuser`
leaves the image config with a non-numeric user — which a pod running `runAsNonRoot: true` refuses to
start, because the kubelet cannot verify that a *name* is not root. Pinning `--uid 10001` and
`USER 10001` means the manifest's `runAsUser` and the image agree by construction rather than by
whatever the base image had spare on build day.

**The manifests pass this service's own gate.** Their own namespace instead of `default`
(`KSV-0110`, `CKV_K8S_21` — both of which this repo's scan flags on log-analyzer's manifests), an
explicit `securityContext` at both levels (`KSV-0118`, `KSV-0001`, `CKV_K8S_20`),
`readOnlyRootFilesystem` with a `/tmp` `emptyDir` under it, and `capabilities: drop: [ALL]`. Shipping
a fifth manifest set carrying findings that this service exists to triage would be the project
arguing with itself.

`OLLAMA_BASE_URL` in the ConfigMap is `http://ollama-host.invalid:11434` — `.invalid` is reserved by
RFC 2606 and never resolves, so a deploy that forgot to set it gets a DNS failure recorded on the run
rather than quietly triaging against something unexpected.

### `ST_OLLAMA_BASE_URL`, and why it had to exist

The compose deploy runs all four services off **one shared `.env`**, and this is the only one whose
model backend is a laptop over Tailscale. Editing the shared `OLLAMA_BASE_URL` to point there would
have taken log-analyzer and knowledge-copilot along with it, onto a host that is asleep most of the
time, for no reason either of them asked for — the deploy would have "worked" and quietly broken two
other services. `provider.py` reads `ST_OLLAMA_BASE_URL` first and falls back to the shared name, so
the blast radius is one service and a host where everything does share a backend needs no new
variable at all.

Third time this shape has been needed here — `ST_GEMINI_MODEL` for per-model quota isolation,
`ST_LLM_TIMEOUT` for a background run against a waiting human, now the backend host. One shared knob
can only ever be right for one of its readers.

### The subdomain, and the cap that would have silently overridden the other cap

One hostname per service, matching the other three — a service means one directory, one image, one
compose entry, one CI workflow, one hostname. `triage.crypticani.dev` gets its own server block
rather than a `location` on an existing one, for the reason the agent's Readme gives about
`sha.crypticani.dev`: a mis-scoped `location` is one edit away from serving this endpoint under
another service's name, and this one holds every onboarded repo's API token.

Three steps, in order, because each depends on the one above:

```bash
# 1. DNS: an A record for triage.crypticani.dev at appsrv's public address.
#    Confirm it resolves before touching nginx -- certbot's HTTP-01 challenge needs it.
dig +short triage.crypticani.dev

# 2. The server block below, then a syntax check before a reload.
sudo nginx -t && sudo systemctl reload nginx

# 3. TLS. --expand adds this name to the existing certificate rather than issuing a
#    second one, the same way sha.crypticani.dev was added.
sudo certbot --nginx --expand -d triage.crypticani.dev
```

```nginx
server {
    server_name triage.crypticani.dev;

    # POST /triage and GET /triage/{id}. A prefix match, unlike the agent's exact one,
    # because the poll URL carries a run id.
    location /triage {
        proxy_pass http://127.0.0.1:7300;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;

        # THE ONE THAT MATTERS. nginx defaults client_max_body_size to 1 MB, and this
        # repo's own scan envelope is 2.7 MB. Without this line every real caller gets
        # nginx's own 413 -- HTML, not the service's JSON detail -- and ST_MAX_BODY_BYTES
        # never gets a say, because nginx refuses the body before the app sees a byte.
        # A cap silently overridden by a smaller cap one layer up is worse than no cap:
        # the control you tested is not the control that fired.
        client_max_body_size 16m;
    }

    location / { return 404; }

    # certbot --nginx fills in listen 443 / ssl_certificate / etc.
}
```

**Verified end to end 2026-08-24**, and the debugging is worth recording because both faults
produced a 404 and neither 404 said which.

**First: the wrong upstream port**, which returned `{"detail":"Not Found"}` -- the app's own JSON, so
nginx was plainly proxying. That body is ambiguous here in a way it would not be elsewhere: all four
services in this repo are FastAPI, and every one answers `{"detail":"Not Found"}` for an unknown
path, so pointing at 7100 or 7200 gets a *sibling service's* 404, byte-identical to this one's. A
trailing slash on `proxy_pass` produces the same body by a different route -- `proxy_pass
http://127.0.0.1:7300/` replaces the matched prefix, so `/triage` arrives as `/`. A bare `/` counts
as a URI; without one the request URI passes through unchanged.

**Then: `location = /triage` instead of `location /triage`.** The exact-match form proxies `/triage`
and nothing else, so `POST /triage` worked while `GET /triage/{run_id}` fell through to
`location / { return 404; }` and returned nginx's own HTML page. This service needs the prefix form
because the poll URL carries a run id -- unlike the agent's `location = /slack/interactive`, which is
correctly exact and is the easy thing to copy from.

Two probes separate all of it, and neither needs a live run or a model call:

- **`GET /triage` should be 405.** Path arrived intact, FastAPI knows the route and refused the
  method. No wrong-port or rebased-URI configuration can produce that.
- **`GET /triage/does-not-exist` should be `{"detail":"no run 'does-not-exist'"}`.** Proves the
  *prefix* matches and reaches the app. nginx HTML here means the location is still exact.

A third thing cost time and was nobody's fault: redeploying the container mid-debug wiped `_runs`, so
a previously valid run id started 404ing from the app just as the nginx fix landed -- two different
404s a minute apart. That is the in-process store's documented tradeoff arriving live.

`/metrics` and `/health` are deliberately **not** routed. Both are unauthenticated, both are reached
over loopback on the host — Prometheus for the first, the container's own healthcheck for the second
— and `/metrics` publishes per-repo volume, which is somebody else's business.

No `proxy_read_timeout` tuning is needed, and that is Day 25's ack-now/answer-later split paying off:
`POST /triage` returns `202` in milliseconds and the caller polls. A synchronous endpoint would have
needed a proxy timeout longer than a triage run, which is to say longer than nginx will sensibly hold
a connection open.

## Prometheus and the dashboard

`/metrics` is a loopback scrape, alongside the other three:

```yaml
  - job_name: security-triage
    static_configs:
      - targets: ["127.0.0.1:7300"]
```

```bash
curl -X POST http://localhost:9090/-/reload    # or SIGHUP without --web.enable-lifecycle
```

Then import [`observability/grafana/dashboards/security-triage.json`](observability/grafana/dashboards/security-triage.json)
— eleven panels, `${DS_PROMETHEUS}` picked on import so it carries no datasource uid from
whichever Grafana exported it.

Eight of the panels are the obvious ones: runs by outcome and repo, findings through the pipeline,
judgments by priority, verdicts by repo, run duration p50/p95, tokens per hour, refusals by control,
risk score p50/p95. Three are worth explaining, and two of those are **deliberately uncoloured** —
the same choice the agent's dashboard makes for approval rate, where a low number is the guardrails
working rather than a failure.

**needs_human rate — uncoloured.** A high number here is not automatically bad, and the first live
run is why. `_format_finding` sends rule_id, title, target, line and scanner severity, but **not the
scanner's context lines** — so for `bandit:B105` the model is shown "possible hardcoded password at
settings.py:3, LOW" and cannot see `password = 'hunter2'`. Declining that is the correct answer to
the question actually asked. Read this panel as *how much of the corpus is unjudgeable as currently
prompted*; the fix is the prompt, not the model. See the note in **Not built yet** below.

**Gate fail rate — uncoloured.** A high rate can be a gate doing its job on a repo that needs work;
a zero rate can be a threshold set too high to ever fire. Neither is good or bad without knowing the
repo, which is exactly why `risk_threshold` is per-request.

**Findings dropped by the model — coloured, red above zero.** `deduped` minus `triaged`. There is no
benign reading: `triage.py` refuses fingerprints that were never sent, so a model answering about
four of the five findings it was given loses one silently and the run still reports success. This is
the one number on the dashboard where any value but zero is a bug.

`tests/test_dashboard.py` checks every panel's expression against `metrics.py` — every metric name,
every selector label, every `by (...)` clause. The failure it exists for is silent: a panel naming a
label the metric does not carry renders an empty graph, which is indistinguishable from a quiet
service. A `by (repo)` on a metric with no `repo` label is worse, because it collapses every series
into one and looks like it works.

### Measured on the live deploy, 2026-08-24

Two single-finding runs through the deployed container on appsrv, against Ollama on the laptop over
Tailscale. Both sent byte-identical work — 430 prompt, 88 output tokens.

| run | wall clock | throughput |
|---|---|---|
| first, cold | 86.59s | 1.02 tok/s |
| second, warm | 22.61s | 3.89 tok/s |
| model load | **64.0s** | — |

Two things fall out, and the second is the useful one.

**Day 27's token curve holds on a machine it was not fitted on.** It predicted
`prompt = 368 + 70.5n` and `output = 14 + 78.5n`; at n=1 that is 438.5 and 92.5 against a measured
430 and 88 — **1.9% and 4.9%**. Fitted on laptop-local calls, validated through a container on
appsrv over the tailnet.

**The wall clock is dominated by model load, not by the network hop.** The 64s gap is Ollama loading
`qwen2.5-coder:7b`; warm, this path runs *faster* than the bench machine's measured 1.7–3.2 tok/s
band, so batch 5 extrapolates to ~104s rather than the 158.7s Day 27 measured locally. The 600s
`ST_LLM_TIMEOUT` has more headroom here than the measurement that set it, not less.

The operational consequence is that **most real runs will be cold.** Ollama unloads a model after
`keep_alive` — five minutes by default — and this deploy is triggered sporadically by CI. For a
full-corpus run the load amortises across nine batches and disappears; for a two-finding pull
request it *is* the runtime. Deliberately not fixed with a longer `OLLAMA_KEEP_ALIVE`: 64s against
the reusable workflow's 45-minute poll budget is noise, and pinning ~5GB of laptop RAM permanently
for a service that runs a few times a day is the wrong trade.

Also worth recording: both runs returned `needs_human` on `bandit:B105`, with identical token counts.
That is not a contradiction of Day 27's finding that local Ollama is not reproducible at
`temperature: 0` — that was about *batch composition* changing the prompt. Two identical
single-finding calls producing identical output is the consistent case.

### The backend is a laptop, and that is not a bug

The model runs on a laptop over Tailscale, and the laptop does not need to be awake outside a demo.
A CI-triggered run while it sleeps ends as a recorded `failed` run saying the backend is
unreachable, which is the correct and visible outcome. There is no fallback logic, no keepalive and
no `ST_LLM_PROVIDER=gemini` failover — the seam exists if it is ever wanted, and this is a learning
build, not a service anyone depends on. `/health` reports `degraded` in that state but still answers
`200`, which is why the readiness probe uses it: an unreachable model is not a reason to pull the pod
out of service.

## Tests

### Green locally, red on the runner, for four days

`Security Triage CI` failed on **every run from Day 25 to Day 28** — seven pushes, never once green
— and the suite passed on my machine every one of those days. Six of `test_provider.py`'s Gemini
tests are the cause: `genai.Client()` reads the environment at construction and raises without a key,
and no test here reaches the real backend but `GeminiProvider()` *is* instantiated to test its
translation logic, which runs the constructor. Locally `provider.py`'s `load_dotenv()` finds the
repo's `.env` and the key is there. A runner has no `.env`.

Three things about it are worth more than the fix.

**The fix already existed in this repo and could not reach here.**
[self-healing-agent/tests/conftest.py](../self-healing-agent/tests/conftest.py) has carried
`os.environ.setdefault("GEMINI_API_KEY", "test-key-never-sent")` before its provider import since
Day 15, with a comment explaining exactly this. security-triage never inherited it because it had
**no `conftest.py` at all** until Day 28 added one for the metrics helper. A shared convention does
not propagate to a service that has nowhere to put it, and "the other service already solved this"
is not the same as "this service has it".

**The failure was invisible in the place it should have shouted.** `build-and-push` is
`needs: [lint-and-test, validate-manifests]`, so every red run rendered the publish job as
*skipped*, not failed. Skipped reads like a deliberate condition — which it was, until Day 28, when
the job genuinely did not exist yet and the workflow said so in a comment. Two different reasons for
the same grey circle, and the honest one was covering for the other.

**It is the round trip again, one layer out.** Days 24, 25, 26 and 27 each found a real bug by
running the thing after the suite was green. This one is the same lesson applied to the suite
itself: *green* is a claim about the machine that ran it, and the machine that matters is the one
nobody was watching.

```bash
cd services/security-triage
python -m pytest -v   # 26 scanners + 17 provider + 13 triage + 21 fixes + 14 risk
                      #   + 14 app + 11 comment + 5 runtime + 10 bench
                      #   + 7 metrics + 17 eval + 29 dashboard = 184, if green
```

`test_scanners.py` (26, Days 22, 24 and 26): each scanner's real shape, a missing-scanner-key envelope, CVE
dedup across two scans, distinct packages sharing a CVE ID staying distinct, cross-scanner
location dedup, fingerprint stability, and a sanity check against the real fixture. Day 24 added
five: Trivy's context stopping at the truncation sentinel (and a genuine blank line surviving it),
a secret's context arriving already redacted and from a different place in the JSON, Bandit's
numbered `code` string recovering the original indentation, Checkov's `guideline`/`code_block`, and
a finding with none of the three defaulting to empty rather than null. Day 26 added eleven for the
audit parser: each rule firing, repeated events collapsing into one finding with a count and the
newest timestamp, a Secret *read* being a finding while a Secret *create* is not, a refusal being one
whatever its verb, impersonation naming both identities, an unaudited resource producing nothing, a
nameless `objectRef` keeping its resource so two refusals cannot share a fingerprint, and one
envelope carrying both scanner and audit findings through a single `parse_envelope` call.

`test_provider.py` (17, Days 23, 27 and 28): transport-failure status mapping and the schema/JSON-body
shape sent to each provider, using the same fake-response/monkeypatch style as
[knowledge-copilot's test_llm.py](../knowledge-copilot/tests/test_llm.py) — no real network call.
Day 27 added the token counters, including Gemini's reasoning tokens landing in `output`. Day 28
added the two that matter to the deploy: `ST_OLLAMA_BASE_URL` beating the shared `OLLAMA_BASE_URL`,
and an empty override falling back rather than pointing the service at an empty string.

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

`test_comment.py` (11, Days 25 and 26): the markdown itself — the pass/fail mark, the table carrying
rule ids and paths rather than fingerprints, zero counts omitted, the `needs_human` callout, advice
fixes staying out of the diff block, a `pending` run being refused rather than rendered, a run with
no commit omitting it instead of printing empty backticks, and the one that matters most: a `failed`
run producing a comment that cannot be mistaken for a clean bill of health.

`test_metrics.py` (7, Day 28): the counters, through the endpoint rather than by calling `metrics.*`
directly — a test that increments a counter and reads it back tests `prometheus_client`, and the
question worth asking is whether `app.py`'s own paths reach it. Reads through the registry, so a
metric incremented under the wrong *label* fails here rather than showing up later as an empty graph.
Covers a completed run's volume/verdict/priority rows including the zero ones, a failed run being
counted as failed *and* still timed, each of the three refusal controls naming itself, the `repo`
label cap holding at its ceiling while already-seen repos keep their series, and `/metrics` serving
without a token.

`test_eval_triage.py` (17, Day 28): the eval's own grading, which is the part that can be wrong in a
way nobody notices — an eval that passes everything looks exactly like a healthy model. Day 27 has
the worked example: two prompt changes made off eval movements that turned out to be a checker bug
and batch nondeterminism. So the checker gets tests before it gets trusted. Every band shape against
every priority, `needs_human` failing a `min` case and passing a `max` one, a finding the model never
answered about failing rather than being absent, `needs_human` staying off the severity scale, and a
check that every committed case actually asserts something.

`test_dashboard.py` (29, Day 28): every panel expression in the committed Grafana JSON checked
against `metrics.py` — metric names, selector labels, `by (...)` clauses, the datasource placeholder,
and that every panel carries a description. Derived from the metric *objects* rather than from
`REGISTRY.collect()`, because a labelled counter with no observations emits no samples at all and
collect() would report every metric here as missing. The failure it exists for is silent: an empty
graph reads as a quiet service, and a `by (repo)` on a metric with no `repo` label collapses every
series into one and looks like it works.

`test_runtime.py` (5, Day 26): the collector — one event per line, a torn last line being *counted*
rather than swallowed (the API server appends to this file live, so a log captured mid-write
routinely ends in half an event, and a file of nothing but torn lines is a different problem that a
silent skip would present as a clean empty run), the newest-events tail, an envelope that claims no
commit, and a captured log going all the way to findings through the same `parse_envelope` the
endpoint calls.

## Running it

```bash
cd services/security-triage
uvicorn app:app --port 7300 --workers 1     # --workers 1 is load-bearing, see above
```

Or the published image, alongside the other three:

```bash
docker compose -f docker-compose.prod.yml pull security-triage
docker compose -f docker-compose.prod.yml up -d security-triage
curl -s localhost:7300/health | jq       # `healthy`, or `degraded` with the reason
```

## Not built yet

- **A `RequestValidationError` handler that drops `input`.** Found while testing the body cap
  through nginx: FastAPI's 422 includes the offending body in `detail[].input`, so a malformed
  1.5 MB envelope came back as a 1.5 MB error. At `ST_MAX_BODY_BYTES=16777216` that is a 16 MiB
  response to a caller who already has the data, landing in their Actions log. Not a security hole
  -- the caller sent it -- but it is bandwidth amplification on the one endpoint whose callers are
  other people's CI runners. One `@app.exception_handler(RequestValidationError)` that rewrites
  each error to `{type, loc, msg}` fixes it.

- **Context lines in the triage prompt.** `scanners.py` captures `context` from all three scanners
  and `fixes.py` is the only module that reads it — `_format_finding` never sends it to the model.
  The first live run made that visible: `bandit:B105` on `password = 'hunter2'` came back
  `needs_human`, correctly, because all the model saw was "possible hardcoded password at
  settings.py:3, LOW". It is indistinguishable from the `/var/run/secrets/.../token` false positive
  that is case 8 of the eval set.

  Not a free fix. Day 27 measured prompt at `368 + 70.5n` tokens, and ten context lines per finding
  roughly triples the per-finding prompt cost. Wall clock is output-bound so latency moves less than
  tokens do, but prompt eval on CPU is not nothing. It also changes what the eval means: the three
  noise cases carry only a `max`, so a decline *passes* them, and 12/12 today cannot distinguish
  "correctly triaged as noise" from "could not see it".
- **Aggregating repeated same-rule findings** — a known ceiling, deliberately not built. Day 27 fixed
  the B101 flood by not scanning test files, which is the right fix for that case but not a general
  one: 7 `bandit:B104` and 6 `checkov:CKV2_GHA_1` findings survive, and for a human
  "bind-to-all-interfaces in 7 places" is one item, not seven. The fingerprint is `(target, line)` by
  design — right for one triage judgment, wrong for one *report* — so collapsing them is a
  presentation change, not a dedupe change. 43 findings is a readable report, so this earns its place
  only on a repo where it isn't.
- **A `needs_human` rate worth trusting.** The rate measured 0% on a heterogeneous slice and 100% on
  a lint-heavy one, so the sample decides the number and neither figure is the service's real
  behaviour. Day 28 built the two things that fix that — `eval_set.json`, which is a *fixed* sample,
  and `st_priorities`, which tracks the rate in production — but neither has a run behind it yet.
  The number goes here after the first one, not before.
