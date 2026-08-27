# Case studies

Four services, four weeks, one repo. I wrote each of them up the same way: the problem that
justified building the thing, the architecture that fell out of that problem, what I traded away,
and what it actually measured when I pointed something at it.

One rule for the numbers — every figure below comes out of the service's own README rather than my
memory of it. That cost a couple of hours and caught three mistakes, which is about the exchange
rate I expected.

| Project | Week | What it does | The number that matters |
|---|---|---|---|
| [Log Analyzer](#1-log-analyzer) | 1 | Raw error log → validated `{severity, likely_cause, suggested_fix, confidence}` | 65–85% of every call is the fixed system prompt |
| [Knowledge Copilot](#2-knowledge-copilot) | 2 | RAG over runbooks, postmortems and live alerts, with citations | The similarity floor I guessed at 0.65 measured out at 0.64 |
| [Self-Healing Agent](#3-self-healing-agent) | 3 | Diagnoses a K8s alert with read-only tools, fixes it after a human clicks Approve | `proposed → approved → executed` against a real cluster |
| [Security Triage](#4-security-triage) | 4 | Deduplicates, prioritises and explains scanner output, then gates CI on one risk score | 94% of the corpus was noise I should never have scanned |

---

## 1. Log Analyzer

*Week 1, Days 1–7 — [README](../services/log-analyzer/Readme.md) ·
[explainer](./log-analyzer.md)*

**The problem.** An error log is a wall of text, and whoever got paged has to turn it into a
decision — how bad is this, what caused it, what do I do now. Getting a model to explain a log is
the easy part. The hard part is that prose is useless to everything downstream. A dashboard can't
read "this looks like a memory problem". Neither can an alert route or a CI gate. What I needed was
something a program could act on every time, including on the runs where the model has a bad day.

**The architecture.** Three layers, one job each. FastAPI validates the request and maps upstream
failures onto 502/503/504. `BaseLLMProvider` is a Strategy seam with Ollama and Gemini behind it,
picked by an env var: local inference keeps logs on the box, hosted inference is sharper, and
nothing downstream knows which one answered. `LogAnalysis` is a Pydantic model — `severity` is a
four-value `Literal`, `confidence` is bounded 0.0–1.0. Both backends ask for structured output
natively, and I re-validate the result on the way out anyway, so a malformed generation turns into a
502 instead of bad data in somebody's dashboard.

One line I'd defend at length: `/analyze-log` is a plain `def`, not `async def`. Both provider
clients block. Starlette runs a sync endpoint in a threadpool, so a slow call sits there quietly
rather than stalling the event loop for everyone else.

**The tradeoffs.** There is nothing to batch here — one log per request is the contract — so the
fixed prompt cost gets paid on every single call. The image tag is `:latest`, so I can't pin a
rollout to an exact build; acceptable for a sandbox, not for anything after that. And `/health`
returns 200 even while it reports `degraded`, which means the readiness probe confirms the process
is up but won't pull the pod out of service when the model backend is down. I know. It's written
down in the README, which is not the same as fixed.

**The results.** A five-case golden set caught the small local model at 2/5, under-calling severity
on everything, where Gemini scored 5/5. The prompt had never said what "severe" meant, so I wrote an
explicit rubric and the small model came into line. Clean story. I told it for three weeks.

Day 27 is when I went back to measure what a run costs, and found two things I did not enjoy.

The service's model had moved from 3b to 7b weeks earlier and I never re-ran the eval. It was
sitting back at 2/5 the whole time.

Then the worse one. I was sending `temperature` at the top level of the Ollama payload. Ollama reads
it from `options`. So every call this service has ever made ran at the default 0.8 — including the
eval harness, which passes 0.0 explicitly and believed it. Three runs over the same five logs gave
me three different severity sets. The score I had been quoting as evidence that my rubric worked was
never a measurement of anything.

Cost, once the numbers meant something: five calls, 168.6s, 2,067 prompt and 388 output tokens.
Logs ranging 106–289 characters moved prompt tokens only from 382 to 455, so 65–85% of every call is
the system prompt riding along.

---

## 2. Knowledge Copilot

*Week 2, Days 8–14 — [README](../services/knowledge-copilot/Readme.md) ·
[explainer](./knowledge-copilot.md)*

**The problem.** Somebody asks "what's the usual fix for X" at 2am. `grep` is no help, because they
are describing a symptom and the runbook is written in the vocabulary of whoever wrote it. Ask a
model the same question with no documents attached and it will invent this cluster's conventions
very fluently, which is worse than getting nothing back. So the job is to answer out of documents
that exist, cite them, and refuse outright when there is nothing worth citing.

**The architecture.** The corpus goes through a hand-written chunker (512/64, stable
`{slug}:{index}` ids) into an embedding provider — `nomic-embed-text` or `gemini-embedding-001`,
both pinned to 768 dimensions. That interface has two methods, `embed_documents` and `embed_query`,
because both backends want to know which side of a search a piece of text is on. Embedding a
question the same way you embedded the passage that answers it is a real retrieval bug that never
raises an error; it just quietly costs you accuracy.

Vectors land in Chroma in cosine space, configured explicitly, with `embedding_function=None` so
Chroma can't silently run a second model of its own alongside the one I picked. `ingest.py`
reconciles rather than upserts — a per-document `content_hash` sorts every chunk into add, update,
skip or delete, and that last bucket is the half `upsert` can never give you.

Retrieval builds a dense pool of 15 and a lexical pool of 15 from BM25 I wrote by hand, then fuses
them with Reciprocal Rank Fusion on ranks, because cosine lives in 0.65–0.90 here and BM25 is
unbounded and there is no honest way to add those. The similarity floor stays on cosine, so a
keyword rescue can pull up a chunk dense search buried at rank 20 without smuggling it past the
refusal guard. A Slack bot sits in front. Slack gives you 3 seconds to acknowledge an event and a
grounded answer takes 165–204s on CPU, so the ack and the answer are two separate HTTP conversations
travelling in opposite directions.

**The tradeoffs.** Hybrid ships even though BM25 on its own scored better on three of four metrics.
The differences are one or two queries out of twelve, which this eval cannot resolve, and lexical
ranking needs shared vocabulary — a failure my eval has exactly two paraphrase queries to observe.
I built MMR, measured it, and didn't ship it. Metadata filters cost recall and bought nothing.
Sessions live in memory, and one uvicorn worker is load-bearing, because two would race two
alert-sync loops against the same writes.

**The results.** My Day 8 eval saturated: hit@1, hit@3 and precision@3 came back identical at every
chunk size. That is a finding about the eval, not about chunking — the metric had no resolution left
to spend. Margin was the only discriminator left, and it ruled out 1024-char chunks without
separating 256 from 512.

Unrelated text scores 0.59 cosine on this model, not 0, which makes absolute thresholds meaningless
and only per-query ranking useful. Hybrid took hit@1 from 8/12 to 9/12 and cost 2ms against a
195-second generation. MMR changed nothing at all, and when I measured the embedding space to find
out why: same-document chunk pairs average 0.787 cosine, different-document pairs 0.699. Separation
of 0.088, where the knob needs a gap above 0.156 to move a single rank position.

The floor is the part I'd point at. I set it to 0.65 by hand on Day 10 from three observations, and
my own backlog said to lower it to 0.60. On Day 14 I measured instead — nine more questions the
corpus genuinely cannot answer, every case retrieved once with no floor applied, then sixteen
candidate floors swept over the recorded scores in memory. 0.64 came out as the honest value, with
one total error against two at 0.65. Dropping to 0.60 would have let in 7 of the 10 unanswerable
questions and gained nothing, because false rejections are already zero at 0.64. The first real
production answer cited its source at 0.659 and cleared the floor by nine thousandths.

---

## 3. Self-Healing Agent

*Week 3, Days 15–21 — [README](../services/self-healing-agent/Readme.md) ·
[explainer](./self-healing-agent.md)*

**The problem.** Week 2's copilot retrieves once and then answers, which is enough when the question
already contains whatever you need to find the answer. "Why is checkout-api crashlooping" doesn't
work that way — what you look at next depends on what the last thing told you. That dependency is
the entire reason this one is an agent instead of another RAG service. And the thing that fixes it
writes to a cluster, so most of the work here is not the reasoning loop. It's everything standing
between the loop and a `kubectl delete`.

**The architecture.** Seven tools, declared as data. A frozen `ToolSpec` carries the JSON schema, a
`write` flag, and `needs` — the RBAC verbs that tool requires, which a drift test compares against
the `Role` I actually ship, because the two live in different files and would otherwise disagree
silently until a 403 turned up live. Four of the seven touch the cluster, two of those being the
only writes in the set. Two reach Alertmanager and the copilot over HTTP. The last one is
`submit_diagnosis`, which is how the loop ends: a schema-validated tool call rather than prose I'd
have to parse. Run out of iterations and it returns `incomplete: true` instead of inventing a
confidence number.

The write path is a separate path — `propose()`, then Slack buttons, then `decide()` — with six
guardrails that run twice. Not belt and braces. The interesting guards change their answer between
those two moments: another action ran, an execution failed, somebody scaled the Deployment by hand
while the message sat unread in a channel. `audit.py` writes an fsynced JSON line before the action
and carries no `try/except`, so if the write fails, the approve handler stops before it touches the
cluster. No record, no action.

**The tradeoffs.** `restart_pod` deletes one pod by name, needing only `pods:delete`, rather than
issuing a rollout restart, which would need `patch` on `deployments` — the same verb that can change
an image. Gemini only: CPU Ollama measured 165–204s per turn in week 2 and a diagnosis is four to
six chained turns, so twenty minutes for one answer had no consumer. One worker, because proposals
live in process memory. I skip certificate verification over the tailnet on purpose, since the kind
API server's cert has no SAN for a Tailscale identity that didn't exist when the cluster came up,
and the WireGuard tunnel underneath is a stronger guarantee than that certificate was adding.

**The results.** The whole loop, recorded end to end: a webhook to `/alerts`, a diagnosis that
survived a run of genuine Gemini `503`s on the retry logic, an Approve click in Slack, and
`scale_deployment` putting `flaky-app` back to 2/2 on a real cluster. The audit trail reads
`proposed → approved → executed` — the first `executed` this project ever produced against a cluster
that exists.

Two findings from that week outrank the build itself.

A guard turned out to be unreachable at its own defaults. The action rate limit allows 3 per hour
and the circuit breaker opens after 3 consecutive failures. Three failures is also three attempts,
so the rate limit answered first every time and the breaker could never fire. Every breaker test
passed, because each one had raised the rate limit "to isolate the breaker" — and I wrote those
tests. What caught it was a demo script I put together to show the guards off.

The second is Gemini's free tier, which caps 5 requests per minute per project per model. A
`CrashLoopBackOff` diagnosis fired seven model calls in eight seconds and got refused with a stated
51-second retry delay. No amount of 1s/2s backoff rides that out, so the provider paces itself
before the call now instead of recovering after it.

---

## 4. Security Triage

*Week 4, Days 22–28 — [README](../services/security-triage/Readme.md) ·
[explainer](./security-triage.md)*

**The problem.** Trivy, Bandit and Checkov are already right about finding things and bad at
everything after that. A small repo's scan comes back 111 findings deep. The same misconfigured
block gets reported by two tools in two vocabularies with no crosswalk between them. And the gate at
the end answers "were there any criticals" when the question I care about is "is this safe to ship" —
forty mediums and no critical is not a clean bill of health.

**The architecture.** The service never sees a checkout. `scan.sh` runs in the caller's own CI, over
their own code, with their own scanners, and POSTs one envelope with `repo`, `commit` and `branch`
as body fields. No scanner binaries on the server, no git credentials, no env var naming a target.
Onboarding a repo is a URL and a token.

`scanners.py` normalises three disagreeing schemas into one `Finding`, and dedup is arithmetic on
strings rather than a model call — a package-tied finding keyed by
`(rule_id, package, installed_version)`, a line-tied one by `(target, line)` alone. `triage.py`
sends one schema-constrained call per five findings and gets back exploitability, impact, priority,
explanation and confidence per fingerprint, with two guards: every returned fingerprint has to be
one I actually sent, and `needs_human` is a legal priority rather than an error path. Force a model
to pick among four real severities on something it can't judge and it won't refuse, it will guess,
and the guess looks exactly like a real triage.

`fixes.py` builds unified diffs in deterministic Python with no model call anywhere in the module,
because `git apply --check` is a real oracle and that makes it the one place where a wrong answer is
cheap to detect. `risk.py` turns priorities into a weighted sum capped at 100. `/triage` answers 202
with a run id and the caller polls. Adding a fourth kind of input in week 4 — Kubernetes audit
events — cost one parser and one line in `_PARSERS`, and nothing downstream needed to learn that
runtime events existed.

**The tradeoffs.** Dropping the rule id from the location key is the only thing that can catch two
scanners describing the same block, and it will incorrectly merge two genuinely different findings
on one line. That's marked in the code with the ceiling named. Fixes are proposed and never applied,
because `readOnlyRootFilesystem: true` is a behaviour change and whether it's acceptable is a
judgment about the workload, which is the one thing neither a scanner nor a model has. `confidence`
stays out of the risk formula — I've now measured it flat three separate times.

**The results.** Measuring cost meant reading the whole corpus for the first time instead of the
first 15 findings, and 94% of it should never have existed. 791 of 841 deduped findings were one
Bandit rule, "use of assert detected", inside test files. An assert is what a test file is made of.
One line of exclusion took the corpus to 44 findings, model calls from 168 to 9, and a full run from
about six hours to twenty minutes. The model declines those findings too, so the run I had been
planning would have marked ~94% of its output as needing human review and routed 791 non-issues to a
person, with every guard green the whole way. I had spent that morning tuning batch size against
input I should have deleted.

The token curve is the part I'm happiest with. Fitted to three single calls — `prompt = 368 + 70.5n`,
`output = 14 + 78.5n` — it predicted a nine-call, 43-finding run to within 0.13% and 0.85%, and held
to 1.9% and 4.9% the following day through a container on a server it was never fitted on. With that in hand I don't have to
sit through a corpus run to know what one costs.

`ST_BATCH_SIZE` stays at 5 and it is knowingly not the cheapest setting. Batch 10 uses 14% fewer
tokens and half the requests. It also ran 339.7s against a 600s ceiling, on a configuration I watched
vary 2x on a loaded laptop, and a timeout spends the whole budget and returns nothing.

Field order turned out to be behaviour rather than formatting. I had declared `priority` first in the
schema, and Ollama constrains generation in declared field order, so the model produced its verdict
before `exploitability` and `impact` existed — which makes the prompt's "weighing both of the above"
impossible to obey. It came out anti-correlated with its own inputs.

Live, it gated a real pull request: 131 findings, 58 after dedup, score capped at 100 against a
threshold of 60, `fail`. It also found two findings on its own manifests, written the same day and
claimed in the README to pass — one fixed, one a documented refusal.

Then the eval I shipped with it failed it. One in-band judgment out of twelve, at batch 5 and again
at batch 1, from two defects that turned out to be separate. The declines are missing input:
`_format_finding` sends rule id, title, target, line and severity, and not the context lines
`scanners.py` already captures. The model told me so five times unprompted — *"insufficient context
to judge exploitability and impact"*. The over-calling is a missing rubric, since nothing in the
system prompt says what `critical` means for a scanner finding. Both are written down and left
alone rather than patched at the end of a long day.

---

## What the four have in common

Reading them back together, four things decided all of this.

**The refusal is the feature.** `answer_source: "none"`, `proposed_action: null`, `needs_human`,
`incomplete: true`. Every service has a legal way to say it doesn't know, and that is what makes the
confident answers worth anything. I built each of those separately for local reasons and only saw
the pattern writing this. Two of the four evals only work because declining is gradeable — the
agent's golden set scores an always-propose agent and a never-propose agent identically at 2/4, so
passing means telling the two situations apart.

**A green suite is a claim about one machine.** Days 24 through 28 each found a real bug by running
the thing after the tests passed. One of those bugs was that this repo's CI had been red on every
run for four days, because a hosted runner has no `.env`, while the suite passed on my laptop every
single time. Nobody looked at the badge, which is exactly the point.

**Structure beats prompting.** Termination as a tool call instead of prose I parse. A length bound
the decoding grammar enforces instead of a sentence asking politely for one. Field order as the
model's reasoning order. Every time I swapped a textual guarantee for a structural one, that failure
stopped coming back.

**The eval is the product.** Not one unit test in this repo calls a model, so 512 of them can be
green while quality quietly rots. `python eval_all.py` runs all five evals and prints one table,
which is the closest thing I have to a checkable version of "production-ready".

---

## Where it stands

Five services, 598 tests, five evals. Each one has been driven end to end against something real
rather than a fixture — a public endpoint behind TLS, a Slack workspace, a `kind` cluster that
really did scale back up, a pull request on this repo that really was gated. Every service exposes
`/metrics` and every one has a documented way to refuse.

That eval table is also the least flattering thing here, so it may as well go last. One of the four
has never been run — the agent's golden set is written and hasn't met a live model yet. One reports
rather than gates, because a regression bar picked before the baseline was measured is just a number
chosen to pass. And the two that do gate are both red right now: the log analyser scored 2/5 and 3/5
on consecutive runs of the same five cases, and triage landed one in-band judgment out of twelve.

I'm leaving them red. The first case study on this page is an eval that passed once, never got
re-run, and had been measuring nothing for three weeks — a green number nobody re-runs is a record
of one afternoon. A red one with a cause written next to it is a work queue.

Since writing that, the gateway in front of all four is built, and it turned up another refusal of
the same shape from a completely different direction — only one of these four services can answer a
bare sentence at all, so the router has to be able to name a service *and* say what you still have
to attach. That one has its own write-up: [`services/gateway`](../services/gateway).

After that, the two triage fixes: context lines first, then the rubric, in that order, so the eval
can tell me which one moved the number.
