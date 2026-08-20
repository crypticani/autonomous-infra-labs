# Security Triage — how it works

A ground-up explanation of [`services/security-triage`](../services/security-triage): why an LLM
sits *on top of* real scanners instead of replacing them, why three scanners produce three
schemas that agree on almost nothing, and how a dedup key can be deterministic without a rule-id
crosswalk between tools that were never designed to agree.

The service README covers *what was built and what it can do*. This document covers *why any of
it works*.

---

## The problem

Trivy, Bandit and Checkov already do the hard, precise, deterministic part: they know CVE
databases, they parse ASTs, they evaluate policy-as-code against IaC. None of that is worth
reimplementing, and none of it is what an LLM is good at — a model has no privileged access to the
NVD, and asking it to "find vulnerabilities" from scratch is asking it to hallucinate a worse
version of a tool that already exists and is already right.

What the scanners are *bad* at is everything downstream of finding something:

- **Volume.** A real repo's scan is hundreds of findings deep (this repo's own scan — see the
  README — comes back with 629 raw findings before dedup). A human reviewing a PR does not read
  629 lines of scanner output; in practice, they read none of it, and the scanner becomes a CI gate
  nobody actually looks at until it blocks a release.
- **Redundancy.** The same underlying problem is frequently reported more than once — the same CVE
  from two different scan angles, the same misconfigured Dockerfile line from two different tools
  with two different opinions about what to call it.
- **Priority.** A scanner's severity field is almost always a property of the vulnerability in the
  abstract (how bad *could* this be), not of this specific finding in this specific repo (is this
  code path even reachable). "23 criticals" is not automatically worse than "3 criticals" if 20 of
  the 23 are in a dependency's test fixtures that never ship.
- **Explanation.** `CVE-2026-45829` means nothing to most people reading a PR comment. "Why does
  this matter, and what do I do about it" is a sentence a scanner's JSON was never built to answer.

That is the actual shape of the job: **triage**, not detection. Deduplicate, prioritize by what
actually matters here, explain in plain language, and propose a fix a human can review — never
apply one. Everything Days 22–24 build follows directly from that split: the deterministic parts
stay deterministic (code), and only the parts that genuinely need judgment go to a model.

---

# Part 1 — The concepts

## 1.1 Three scanners, three schemas, one problem

Ask three tools to report "a security finding" independently and you get three different ideas of
what a finding even *is*:

```
Trivy   (per scan target):  Results[].Vulnerabilities[]   -- one CVE against one package
                              Results[].Misconfigurations[] -- one failed policy check
                              Results[].Secrets[]            -- one matched secret pattern

Bandit  (flat):              results[]                      -- one AST-pattern match

Checkov (list of reports):   [ { check_type, results: { failed_checks[] } }, ... ]
                              -- one framework's failed policy checks, in their own report
```

Nested vs. flat vs. a list of reports; `Severity` vs. `issue_severity` vs. `severity` (which is
often just `null` — Checkov's open-source checks don't carry one at all); a CVE id in one, a
scanner-invented rule id (`B104`, `CKV_K8S_21`, `DS-0026`) in the other two. None of that
disagreement is a bug in any of the three tools — they were built by different teams for different
purposes and never had to agree on a shared vocabulary, because until now nothing needed to read
all three at once.

The fix is the oldest one in the book: pick one shape, and translate at the boundary. Every one of
the four scanners in this envelope (three static, plus Day 26's runtime signals from K8s audit
logs) gets translated into the same `Finding` — and everything after that translation, including
the model, only ever sees `Finding`, never a scanner's native JSON. That boundary is what makes
"add a fourth scanner later" a translation function, not a rewrite of triage.

## 1.2 What makes a dedup key honest

Two findings are "the same" if a human would only want to hear about it once. That's easy to state
and hard to pin down mechanically, because "the same" means something different depending on what
kind of finding it is:

**A CVE is a property of a package version, not of who found it.** If a filesystem scan of
`requirements.txt` and an image scan of the built container both report `CVE-2026-45829` against
`chromadb==1.5.9`, that is one vulnerability, reported twice because it was looked for twice — not
two vulnerabilities. So the identity for this kind of finding is `(rule_id, package,
installed_version)`, and deliberately excludes *which scan* produced it. Whether it's Trivy's own
fs-vs-image duplication today, or a second scanner that also does CVE matching tomorrow, the same
identity rule keeps working without being told about the new source.

**A misconfiguration has no such shared id.** Checkov's `CKV_K8S_21` and Trivy's `KSV-01021` might
both be "the default namespace shouldn't be used" — or they might not be; there is no published
crosswalk between Bridgecrew's and Aqua's rule catalogs, and hand-building one is exactly the kind
of speculative machinery that isn't worth writing before there's evidence it's needed. The
fallback identity is `(target, line)` — the same file, the same line, is treated as the same
underlying issue, regardless of which tool's rule id named it. This is a **marked, deliberate
approximation**, not a design that was assumed complete: it will occasionally over-merge two
genuinely distinct issues that happen to land on the same line, and `scanners.py` says so in a
`ponytail:` comment naming exactly that ceiling and what raises it (a real crosswalk table, if
false-merges show up in practice).

Both rules share one property that matters more than either rule's precision: they're **pure
functions of the finding's own fields** — no network call, no model, no state carried between
requests. That's what makes 629 raw findings collapse to 559 in milliseconds, deterministically,
and it's what makes Day 23's batched LLM calls affordable at all — the model triages the 559 that
are actually distinct, not the 629 that include their own echoes.

---

# Part 2 — Reading the code

## 2.1 `scanners.py` — the translation boundary

`parse_envelope()` doesn't parse one schema; it dispatches to one parser per scanner key
(`_parse_trivy`, `_parse_bandit`, `_parse_checkov`) and concatenates whatever each one returns. The
loop that does the dispatching is three lines:

```python
for name, parser in _PARSERS.items():
    raw = (envelope.get("scans") or {}).get(name)
    if raw:
        findings.extend(parser(raw))
```

`envelope.get("scans") or {}` and `.get(name)` (not `[name]`) is the entire implementation of "a
partial envelope is not an error" — a Go repo's envelope simply never has a `"bandit"` key, `raw`
comes back `None`, `if raw` is false, and the loop moves to the next scanner having done nothing
wrong. There's no `try/except` anywhere in this path, because there's nothing here that should
ever raise: a missing key is data, not a fault.

Each `_parse_*` function knows exactly one scanner's shape and produces the same `Finding` fields
regardless. That asymmetry — three shapes in, one shape out — is the whole point of writing three
small functions instead of one clever one: `_parse_trivy` can walk three nested list types under
`Results[]`, `_parse_checkov` can walk a list of reports, and neither has to know the other exists.

## 2.2 The fingerprint, and why it's computed once at construction

```python
def _fingerprint(rule_id, target, line, package, installed_version):
    if package:
        key = ("dep", rule_id, package, installed_version)
    elif line is not None:
        key = ("loc", target, line)
    else:
        key = ("other", rule_id, target)
    return hashlib.sha256(repr(key).encode()).hexdigest()[:16]
```

Three branches, corresponding to the three identity rules a finding can have: tied to a package
(§1.2's CVE case), tied to a line but not a package (the misconfiguration case), or neither (a
last-resort identity for anything that's somehow both un-lined and un-packaged, so every `Finding`
still gets a fingerprint rather than a special case downstream that has to handle "no fingerprint
yet"). `_finding()` calls this once, when the `Finding` is built, and stores the result as a plain
field — not a property recomputed on every access — because a `Finding`'s identity shouldn't be
able to drift if one of its own fields changed after construction, and because `dedupe()` calls
`.fingerprint` in a loop and has no reason to pay for a hash function per comparison.

## 2.3 `dedupe()` is the whole reason order was preserved

```python
def dedupe(findings):
    seen: set[str] = set()
    deduped = []
    for finding in findings:
        if finding.fingerprint in seen:
            continue
        seen.add(finding.fingerprint)
        deduped.append(finding)
    return deduped
```

First occurrence wins, in input order. That's a small decision with a real consequence: `parse_envelope`
processes scanners in a fixed order (`trivy`, `bandit`, `checkov`), so when two findings collapse
to the same fingerprint, the one that's kept is always the one from whichever scanner ran first for
that finding kind — not arbitrary, and not whichever happened to be scanned last. Day 23's triage
batches see a stable, reproducible list, which matters once there's a golden eval set (Day 28)
comparing triage output across prompt changes: a dedup order that changed from run to run would
make "did the prompt regress" indistinguishable from "did dedup happen to keep a different
duplicate this time."

---

## 1.3 Why the model never sees free text back

Ask a model to "explain this vulnerability" and it answers in prose. Prose is fine for a human
reading a PR comment, but it is a bad shape for anything downstream that needs to *act* on the
answer — Day 25's risk gate needs a `priority` it can compare against a threshold, not a
paragraph it has to re-parse with a regex and hope the wording didn't drift.

The fix is the same one `log-analyzer` used from Day 2: hand the model a JSON Schema and ask the
*backend*, not the model's good behavior, to enforce it. Ollama's `/api/generate` takes a
`format` field; Gemini's `generate_content` takes a `response_schema`. Both constrain decoding
itself — the model literally cannot emit a token that would produce invalid JSON — which is a
different and stronger guarantee than a prompt that says "please respond in JSON" and hopes.
`triage.py` never parses prose looking for a severity; it calls `TriageBatch.model_validate_json()`
on the response and lets Pydantic reject anything that doesn't fit.

Schema-constrained decoding guarantees *shape*. It says nothing about whether the *content* is
honest — a model can still emit perfectly valid JSON that names a `fingerprint` nobody sent, or a
`confidence` that's really just its severity field wearing a decimal point. That is what the next
two sections are actually for.

It also guarantees nothing about *when the model stops* — found live on 2026-08-19, not
predicted. An unbounded `explanation: str` let `qwen2.5-coder:1.5b` fall into a repeating
conditional ("if it can be exploited... if it cannot...") dozens of times over, valid JSON the
entire way, right up until it ran out of context. A schema says what a value must look like *if*
generation ever produces one; it says nothing about how much text comes before it does. The fix
that actually held was making the schema itself say less: `max_length=280` on `explanation` is
still part of the JSON schema Ollama constrains against, so the grammar itself refuses a longer
string — a repetition penalty is a sampling-time nudge that can still lose, but a length bound in
the schema is a guarantee the grammar can't violate.

## 1.4 Batching, and why it's not "one call, more findings"

The obvious way to save calls is to stuff all 559 deduped findings into one prompt. That trades
559 slow calls for one enormous one — and on a CPU-only backend, prompt evaluation time scales
with input size, so one call carrying 559 findings' worth of context does not finish faster than
559 calls carrying one finding each; it just fails as a single unit instead of many small ones,
and a truncated or malformed response loses everything instead of one finding's worth.
`ST_BATCH_SIZE` (default 5) is the actual lever: small enough that one bad batch is a small loss,
large enough that the fixed overhead per call (loading the model's context window, restating the
system prompt) is amortized over more than one finding.

## 1.5 Two ways a valid-shaped answer can still be wrong

**A fingerprint the batch never sent.** Nothing about JSON Schema stops a model from naming a
`fingerprint` string that happens to look plausible but was never in the prompt — the schema only
constrains *shape*, not *membership*. This is exactly Day 10's invented-citation problem
(`knowledge-copilot`'s model occasionally cited a chunk marker like `[9]` that was never
retrieved) wearing a different field name, and the fix is the same shape: compute the set of
fingerprints actually sent, and drop anything the model returns that isn't in it. Trusting an
unverified fingerprint here is worse than the citation case, too — a wrong citation is a
readability bug, a wrong triage fingerprint is wrong *risk data* attached to a real finding.

**A confidence score that isn't actually calibrated.** The rubric asks the model to report its own
certainty, 0.0–1.0 — but a model under instructions to always produce a number will produce one,
whether or not it has grounds to be confident. That's the same failure as a scanner's `severity`
field misread as absolute rather than contextual (§1's "Priority" problem), just moved one layer
up: a number that looks calibrated is not automatically calibrated. The escape hatch is
`needs_human` as a *legal* `priority` value, sitting alongside `critical`/`high`/`medium`/`low` in
the same enum rather than being a separate error path the model has to reach for deliberately. A
model choosing among five options including "I don't know" is answering a different, more honest
question than one being forced to pick among four real severities on a finding it can't actually
judge.

---

# Part 3 — Reading the code (Day 23)

## 3.1 `provider.py` — one seam, two backends, no schema knowledge

`generate(system, user, schema)` takes a Pydantic **class**, not a dict, because the two backends
want it in different shapes: Ollama's `format` wants `schema.model_json_schema()`, Gemini's
`response_schema` wants the class itself. Passing the class once and letting each provider ask it
for whichever shape it needs is what keeps this module able to serve *any* schema — it never
imports `TriageBatch`, `triage.py` imports `provider.py`, and the dependency only ever points one
way.

## 3.2 `triage.py` — the guard is a filter, not a retry

```python
def _drop_unsent_fingerprints(results, sent, provider_name):
    kept = [r for r in results if r.fingerprint in sent]
    ...
    return kept
```

No re-prompting, no "ask the model to fix it" loop. A dropped fingerprint just means that finding
comes back untriaged rather than mistriaged — the same choice `ground_answer()` made for an
invented citation marker: strip what can't be trusted, keep what can, and let the gap be visible
(a log line here, an unresolved citation there) rather than silently patched over.

---

## 1.6 Why a security fix is proposed, never applied

An auto-fix bot for security findings sounds obviously good and is mostly a trap. Three separate
reasons, and they stack:

**The service has no checkout.** By design (§"The problem"): the caller's CI scans its own code and
POSTs the JSON. So the service cannot read the file it wants to change, cannot run the tests
afterwards to see whether the change broke anything, and has no branch to push to. Every one of
those is a precondition for committing a change. What it *can* do is emit a diff as text, which the
caller attaches to a PR for a human who has all three.

**A security fix is a behaviour change, not a correction.** This is the part that surprises people.
`readOnlyRootFilesystem: true` is unambiguously the more secure setting, and it breaks any container
that writes to its own filesystem — a temp file, a cache, a unix socket. `runAsUser: 10001` breaks
an image whose files are owned by another uid. Neither is a typo being corrected; both are trades
against how the workload actually behaves, which is information that exists nowhere in the finding.
"More secure" and "still works" are different questions, and a scanner only asked the first.

**The confident-looking wrong patch is worse than no patch.** A reviewer reading a diff trusts its
shape — the line numbers, the surrounding context, the fact that it *looks* like it came from the
file. That trust is the thing being spent. A patch that doesn't apply wastes a few minutes; a patch
that applies and is subtly wrong (an inserted key one indent level off, silently landing in the
wrong block) is a security fix that got merged and doesn't do anything. This is the reason the diff
builder is deterministic Python rather than the model that is already sitting in the pipeline: a
diff has a real oracle (`git apply --check`), so the cost of being wrong is cheap to detect — and
that is exactly the case worth spending code on instead of tokens. Day 23 measured this same model
returning five valid-shaped, factually worthless triage judgments. A plausible diff is that same
failure with a `+` in front of it.

## 1.7 What "mechanical" actually means

The week's plan called three fix classes mechanical: pin a base image to a digest, add a
`securityContext`, bump a pinned dependency. Against a real corpus one survives, and the reason each
of the other two fails is more instructive than the one that works:

- **Pin a base image to a digest.** The digest is not in the finding. Getting it means reaching a
  registry, which the service deliberately cannot do. A diff containing an invented digest is the
  confident-looking wrong patch from §1.6, in its purest form.
- **Bump a pinned dependency.** Needs a known-good target version. This corpus' one real CVE has no
  `FixedVersion` — the upstream fix doesn't exist yet. Where a fixed version *is* present the advice
  names it, but naming a version is not the same as editing a lockfile, which has its own resolver.
- **Add a `securityContext` key.** Works, because the value is a *constant*: `readOnlyRootFilesystem:
  true` is the right answer for every workload that can take it. Nothing has to be looked up.

So the honest test for "mechanical" is not *is the edit small* — all three are one-line edits. It is
**does the correct value have to be discovered from somewhere the service can't see.** A memory
limit, an image digest, a uid that matches the image: all small edits, none mechanical. That
distinction is the entire content of `_SECURITY_CONTEXT`, the table in `fixes.py`, and it's why the
table is short.

Verifying the round trip on 2026-08-20 surfaced a *second* axis, which the first re-scan of a
patched file made obvious in a way reading rule descriptions hadn't. Two rules kept firing on the
patched manifest, and neither has an unknown value:

- **`KSV-0118`** ("default security context configured") fires twice on one workload — once for the
  container, once for the *Deployment*. The container-level one is satisfied by the hunk; the
  pod-level one wants `spec.template.spec.securityContext`, which is a different insertion point
  from the `- name: <container>` line the finding anchored to. Known value, wrong scope.
- **`KSV-0117`** ("prevent binding to privileged ports") wants a `containerPort` below 1024 changed.
  That's a *modification* of an existing line rather than an insertion, the replacement port is a
  choice, and changing it cascades into the Service's `targetPort` — a second file the finding says
  nothing about.

So the second question is **does the edit land where the finding's own context lines reach**. A fix
whose insertion point is a different block, or whose correctness depends on a second file, is not
mechanical either, however constant its value. Both of these come back as advice, which is why the
one thing the module claims — "this hunk resolves these fingerprints" — stayed true when a scanner
was asked to check it.

---

# Part 4 — Reading the code (Day 24)

## 4.1 The context lines were always there

Every diff needs the lines around the change, and the service has no file to read them from. It
turns out all three scanners already ship them and `scanners.py` was dropping them on the floor:
Trivy in `CauseMetadata.Code.Lines`, Bandit in `code` (a single string, each line prefixed with its
unpadded number and one space), Checkov in `code_block` (a list of `[number, content]` pairs).

`Finding` gained `context`, `resolution` and `message` to carry them. None feeds `_fingerprint`,
which is deliberate — every dedup key Days 22 and 23 measured is unchanged, so adding three fields
invalidated no earlier result.

Two things the data does that a diff builder has to respect:

**Trivy truncates in the middle and tells you.** A block is capped at ten lines, with the cut marked
by an entry whose `Truncated` is true and whose `Content` is empty. So a 43-line container block
arrives as lines 22–30 and then a hole:

```python
for line in (holder.get("Code") or {}).get("Lines") or []:
    if line.get("Truncated"):
        break
    lines.append((line["Number"], line["Content"]))
```

That `break` is load bearing. A unified diff hunk header — `@@ -22,9 +22,15 @@` — is a *claim* about
the file: nine consecutive lines start at line 22. A gap anywhere inside the quoted lines makes the
claim false even though every individual line is correct, and `git apply` rejects it. `_contiguous()`
then takes the run from the start and stops at the first non-consecutive number, so a hunk is only
ever built from lines that really are adjacent in the file.

**A secret's context is pre-redacted, and lives somewhere else.** Trivy puts `Code` directly on the
secret object rather than under `CauseMetadata`, and the secret itself comes back as asterisks. It's
useful for showing a human *where*, and it must never be diffed back into a file. What guarantees
that isn't a check on secrets — it's that `fixes.py` builds diffs only for an allowlist of rule ids,
so a secret finding never reaches a fixer at all. An allowlist fails closed; a blocklist of things
not to patch would need updating every time a scanner adds a rule.

## 4.2 Nine rules, one hunk

Trivy raises nine separate `KSV-*` rules against a container with no `securityContext` — one each
for `runAsNonRoot`, `allowPrivilegeEscalation`, `runAsUser`, `runAsGroup`,
`readOnlyRootFilesystem`, seccomp (twice, under two rule ids) and capabilities (twice). Fix them
independently and you get nine diffs that each insert
their own `securityContext:` key. The first applies; the second applies *cleanly too*, and produces
a YAML document with a duplicate key that Kubernetes rejects. A patch that applies and is invalid is
the worst outcome available.

So the fixers are grouped by insertion point and emitted as one hunk carrying the union of the keys.
That's the same collapse Day 22 does with `dedupe()`, arrived at from the opposite direction: there
it was a cost optimisation, here it's a correctness requirement.

Which exposes what Day 22's dedup key costs. A misconfiguration is identified by `(target, line)`,
and all nine of those rules report the same block's `StartLine` — so they share one fingerprint and
`dedupe()` keeps exactly one. That is the *right* identity for triage: one judgment about one
misconfigured block, and nine model calls collapsed into one. It's the wrong identity for fixes,
because one surviving rule means one key in the hunk instead of nine. The resolution isn't to change
the fingerprint — it's that the two layers want different identities, so `propose_fixes()` reads the
pre-dedup list and dedups on the insertion point instead. The `# ponytail:` comment in
`scanners.py` predicted this collapse on Day 22 as a hypothetical; Day 24 is it happening.

## 4.3 Anchoring, and the `- name:` trap

The insertion point comes from the container's own `- name:` line, which also gives the indentation:

```python
match = _NAME_KEY.match(content)          # ^(\s*)-(\s+)name:\s*(\S+)\s*$
if match and match.group(3).strip("\"'") == wanted:
    indent = " " * (len(match.group(1)) + 1 + len(match.group(2)))
```

`- name:` puts its sibling keys at the dash's column, plus the dash, plus the space after it —
`        - name: x` means keys at column 10. YAML's whole meaning is in that arithmetic, which is
precisely why it isn't left to a model.

The trap: a container block contains `- name: http` inside its `ports:` list and `- name: LOG_LEVEL`
inside its `env:`. They match the same pattern, indented deeper. A first-match anchor cheerfully
inserts a `securityContext` inside `ports:` — a patch that applies, parses, and does nothing. What
disambiguates is `wanted`, the container name, and the only place it appears is Trivy's per-finding
`Message`:

```python
_CONTAINER_IN_MESSAGE = re.compile(r"[Cc]ontainer [\"']([^\"']+)[\"']")
```

Both quote styles are load bearing — `KSV-0012` writes `Container 'x'` and `KSV-0104` writes
`container "x"`, in the same scan. And the rules that name no container at all (`KSV-0030`: "Either
Pod or Container should set…", `KSV-0106`: "container should drop all") get prose advice instead of
a guess. In this repo's corpus that's 17 of the family's 21 findings anchored and 4 refused, which
is the trade taken deliberately: a refusal costs a reviewer nothing, and a wrong anchor costs them
their trust in every other diff in the file.

## 4.4 The refusals are the feature

`propose_fixes()` returns a `Fix` for every finding, and `kind="advice"` — with the reason in
`note` — for every one it won't build a diff for. Five refusal branches: no container named in the
message, a `securityContext` already visible in the lines, the container declared past the
truncation, a target that isn't a repo file (a container image reference like
`alpine:3.19 (alpine 3.19.1)`, or a path climbing out of the repo — that string arrives in a public
request body and ends up in a patch header), and a hunk whose lines overlap another hunk in the same
file.

That last one exists because `git apply` rejects an entire patch when two hunks share lines, not
just the offending hunk — so one bad pair would cost every other fix in that file. Dropping the
second to advice keeps the rest applicable.

The prose itself needs no model either. Every Trivy misconfiguration ships a `Resolution` written by
whoever wrote the check ("Set `containers[].securityContext.runAsUser` to an integer > 10000"),
Checkov ships a `guideline` URL, and a vulnerability with a `FixedVersion` gets "upgrade chromadb
1.5.9 -> 1.5.10" assembled from fields it already has. Asking a 7b model to paraphrase a sentence
that is already correct and already free is the kind of LLM call that makes a pipeline slower without
making it better — and at ~4 minutes per batch on CPU (§Day 23's measurement), 559 findings' worth of
paraphrase is hours spent to restate what the scanner said.

---

*Next: Day 25 turns triaged priorities into a single risk score with a threshold verdict, and puts
the whole pipeline behind `POST /triage` with bearer auth, a body-size cap and a per-token rate
limit — a public endpoint that spends CPU-minutes per call needs all three.*
