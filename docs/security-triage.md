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

*Next: Day 24 adds proposed fixes — a diff generated from a finding's own context lines, never
applied, for the classes of finding mechanical enough to fix with confidence.*
