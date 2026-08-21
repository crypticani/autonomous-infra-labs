# autonomous-infra-labs

Hands-on exploration of AI-native DevOps — self-healing infrastructure, a RAG-based ops copilot, and AI-assisted security triage — built service by service on top of a standard DevOps stack (Docker, Kubernetes, Jenkins, Prometheus/Grafana) rather than a new toolchain.

Each service here started as a learning exercise, but is built to a standard where every line is understood and defensible, not generated and accepted. No black-box agent frameworks until the underlying loop is understood by hand.

## Services

| Service | What it does | Status |
|---|---|---|
| [`services/log-analyzer`](./services/log-analyzer) | Turns a raw error log into strict, typed `{severity, likely_cause, suggested_fix, confidence}` output via a pluggable LLM provider (Ollama or Gemini). Containerized FastAPI service with `/analyze-log`, `/health`, and Prometheus `/metrics`; golden-set eval harness, mocked-provider tests, CI, and K8s manifests | ✅ Complete |
| [`services/knowledge-copilot`](./services/knowledge-copilot) | RAG service answering ops questions ("what's the usual fix for X") over runbooks, postmortems, and live alert/event data — now live in production, taking questions in Slack, with a measured similarity floor and bearer-token auth | ✅ Complete |
| [`services/self-healing-agent`](./services/self-healing-agent) | Tool-calling agent that diagnoses K8s alerts using read-only tools (logs, alerts, deploy history) and proposes a fix. Write actions are gated behind human approval and hard blast-radius limits | ✅ Complete |
| [`services/security-triage`](./services/security-triage) | Wraps existing scanners (Trivy, Checkov, Bandit) and uses an LLM to deduplicate, prioritize, and explain findings. Proposes fixes as diffs — never auto-applies them | 🚧 In progress |
| [`gateway`](./gateway) | Single FastAPI entrypoint tying the services above into one AI DevOps copilot | Planned |

## Explainers

Each service has two documents: a README covering *what was built, how to run it, and what it
scored*, and a companion explainer covering *why it works*. The explainers assume no prior
exposure to the AI side — they build up the concepts from scratch, then walk the code.

| Doc | Covers |
|---|---|
| [`docs/log-analyzer.md`](./docs/log-analyzer.md) | Tokens, context windows, temperature, system vs. user prompts, constrained decoding, why `def` beats `async def` here, and how to test a non-deterministic system |
| [`docs/knowledge-copilot.md`](./docs/knowledge-copilot.md) | Embeddings, cosine similarity, chunk size and overlap, what a vector database actually buys you, and why queries and documents are embedded differently |
| [`docs/self-healing-agent.md`](./docs/self-healing-agent.md) | What a tool call actually is, why RAG's one-shot retrieval can't diagnose a live problem, and how an observe-think-act loop knows when to stop |
| [`docs/security-triage.md`](./docs/security-triage.md) | Why AI triages scanner output instead of replacing scanners, why three scanners produce three disagreeing schemas, how a dedup key stays deterministic without a rule-id crosswalk between tools, and why a proposed fix is a deterministic diff plus a human rather than an auto-commit |

## Architecture

```text
                 ┌─────────────────────┐
   alerts /      │                     │
   logs / scans  │       gateway       │
   ────────────▶ │   (FastAPI, auth)   │
                 └──────────┬──────────┘
                             │
        ┌───────────┬────────┴────────┬───────────────┐
        ▼           ▼                 ▼                ▼
   log-analyzer  knowledge-      self-healing      security-triage
   (LLM call)    copilot (RAG)   agent (tools +     (scanner output
                                 approval gate)      + LLM triage)

```

Every service exposes `/metrics` for Prometheus (token cost, latency, error rate — not just uptime) and ships with a small eval harness so behavior is tested, not just demoed once.

## Design principles

* **Understand before you automate.** Every service is built by hand first; frameworks are added later, only once the underlying loop can be explained without one.
* **Nothing writes to infrastructure without a human in the loop.** The self-healing agent diagnoses and proposes; a human approves before any restart, scale, or rollback executes.
* **Sandboxed only.** Everything here runs against a local (kind/minikube) or personal cloud cluster — never against production infrastructure belonging to an employer or client.
* **A working demo isn't a finished service.** Cost, latency, and failure modes are tracked from day one; each service has a golden-set regression test so changes don't silently break it.

## Tech stack

Python · FastAPI · Ollama / Gemini API (pluggable) · Pydantic · Rich · Chroma · Docker · Kubernetes · Jenkins · Prometheus · Grafana · Trivy · Checkov · Bandit

## Getting started

Each service is self-contained and will include its own setup instructions as it's built. Global prerequisites: Python 3.10+, and Docker + kind/minikube once you reach the K8s labs.

### 1. Clone and install

```bash
git clone https://github.com/crypticani/autonomous-infra-labs.git
cd autonomous-infra-labs

python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Pick an LLM provider

The log analyzer runs against **Ollama** (local, default) or **Gemini** (cloud). Configure via `.env`:

```bash
cp .env.example .env
```

**Option A — Ollama (default, runs locally, no API key).**

```bash
# install (Linux/macOS); see https://ollama.com/download for other platforms
curl -fsSL https://ollama.com/install.sh | sh

# pull the model referenced in .env (OLLAMA_MODEL)
ollama pull qwen2.5-coder:7b

# start the server (listens on http://localhost:11434)
ollama serve
```

Leave `LLM_PROVIDER=ollama` in `.env`. `OLLAMA_MODEL` must match a model you've pulled, and `OLLAMA_BASE_URL` defaults to `http://localhost:11434`. Smaller machines can use a lighter model, e.g. `ollama pull qwen2.5-coder:1.5b` and set `OLLAMA_MODEL=qwen2.5-coder:1.5b`.

**Option B — Gemini (cloud).** Set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=<your key>` in `.env` (get a key from https://aistudio.google.com/apikey). `GEMINI_MODEL` selects the model.

> `.env` is gitignored — never commit real API keys.

### 3. (Optional) Local K8s sandbox — needed for later phases

```bash
kind create cluster   # or: minikube start
```

### Run the log analyzer

The service is a FastAPI app that listens on port `7000`. Run it directly, or in a container.

**Option A — run directly:**

```bash
python services/log-analyzer/day5_analyzer.py
```

**Option B — run with Docker (multi-stage build, non-root user):**

```bash
docker compose up --build
```

The container reads config from `.env`. When `LLM_PROVIDER=ollama`, point `OLLAMA_BASE_URL` at `http://host.docker.internal:11434` so the container can reach Ollama running on the host.

Once it's up, POST a raw log to `/analyze-log`:

```bash
curl -s http://localhost:7000/analyze-log \
  -H "Content-Type: application/json" \
  -d '{"raw_log": "2026-07-29 09:12:03 ERROR OOMKilled: container api exceeded memory limit 512Mi, restarted 4 times"}'
```

Response is strict, validated JSON:

```json
{"severity": "HIGH", "likely_cause": "...", "suggested_fix": "...", "confidence": 0.9}
```

Interactive API docs are at `http://localhost:7000/docs`, and a liveness/readiness probe at `http://localhost:7000/health` — it reports `status: degraded` with the underlying issue when the configured provider is misconfigured or unreachable (e.g. Ollama down). Prometheus metrics (request count, latency, token usage by provider) are exposed at `http://localhost:7000/metrics`. The earlier `day1_analyzer.py` / `day2_analyzer.py` are standalone CLI scripts kept for reference.

## Roadmap & Challenge Log

This repository follows a scaffolded 30-day learning path.

### Phase 1: Log Analyzer

* [x] **Day 1: Fundamentals & Baseline**
* **Learn:** How LLMs actually work for engineers (tokens, context window, temperature, system vs. user prompts, function/tool calling).
* **Build:** A script that sends a raw error log to an LLM and prints back a plain-English explanation — with a pluggable provider (Ollama / Gemini) behind one interface.
* [x] **Day 2: Strict typed output** — enforce a validated `LogAnalysis` schema (`severity`, `likely_cause`, `suggested_fix`, `confidence`) via Pydantic instead of free text.
* [x] **Day 3: FastAPI wrapper** — expose the analyzer as a `POST /analyze-log` endpoint with typed request/response models and mapped upstream error handling (502/503/504).
* [x] **Day 4: Containerization** — multi-stage Dockerfile (non-root user, slim runtime), `docker compose` for local runs, and a `/health` endpoint wired to a container healthcheck.
* [x] **Day 5: Observability** — Prometheus `/metrics` (request count, latency, token usage by provider) and a health check that actively probes the provider.
* [x] **Day 6: Testing & CI** — pytest schema tests + endpoint tests that mock the provider (CI never hits a paid API), and a GitHub Actions pipeline: lint → test → validate manifests → build/push.
* [x] **Day 7: Deploy & document** — Kubernetes manifests (`Deployment`, `Service`, `ConfigMap` + manually-created `Secret`) with `/health` probes and non-root hardening, plus a service README covering architecture and the golden-set finding.
* [x] **Service Build:** Log Analyzer — golden-set eval harness (imports the live `ANALYSIS_SYSTEM_PROMPT`) that caught the smaller local model under-calling severity; fixed with an explicit severity rubric. **Project 1 complete.**

### Phase 2: Knowledge Copilot

* [x] **Day 8: Embeddings & vector search** — a `BaseEmbeddingProvider` interface (Ollama `nomic-embed-text` / Gemini `gemini-embedding-001`, both pinned to 768 dims) with **separate document and query methods**, since both backends need to know which side of a search the text is on. Hand-written word-boundary chunker with stable `{slug}:{index}` IDs; 8 runbooks indexed into Chroma at three chunk sizes in explicitly-configured cosine space.
* **The finding:** hit@1, hit@3 and precision@3 all came back **identical across every chunk size** — with 8 topically disjoint runbooks the retrieval task is too easy to discriminate, so the metric had no resolution left. The only signal remaining was *margin* (cosine gap between best correct and best incorrect hit), which ruled out 1024-char chunks but could not separate 256 from 512. Chunk size was chosen on downstream cost, not on this data — and the honest version of that is written up rather than a clean-looking table that the evidence doesn't support.
* **Also worth keeping:** unrelated text scores **0.59** cosine, not 0. Absolute similarity thresholds are meaningless on this model; only per-query ranking is. And a unit test asserting the boring invariant "no word is lost in chunking" caught a real defect — the chunker snapped window *ends* to word boundaries but not *starts*, so ~85 of 109 chunks opened with a fragment. Silent, symptomless, and polluting every vector.
* [x] **Day 9: Ingestion pipeline** — idempotent re-indexing and metadata tagging (service, doc type, date). Re-indexing is **reconcile, not upsert**: a per-document `content_hash` sorts every chunk into add / update / skip / **delete**, and that last list — IDs in the index the corpus no longer wants — is the one `collection.upsert` can never give you. Re-running on an unchanged corpus embeds zero vectors, which a `FakeProvider` counting its own calls proves rather than assumes.
* **The finding:** `--reset --dry-run` dropped the collection *and* reported a tidy plan, because the destructive step sat above the early return. A flag whose entire contract is "changes nothing" was the destructive one, and the damage only surfaced on the next command. Read-only is a property of the whole call path, not of the last write — and the guard belongs inside `ingest()`, not only in `argparse`, because the endpoint calls it as a library.
* [x] **Day 10: `POST /ask-runbook`** — retrieve top-k, augment the prompt, cite sources. Chunks below a **0.65 similarity floor** are not context: nothing clears it and the service refuses *without calling the model at all*. Every `[n]` the model emits is validated against the chunks actually placed in the prompt; invented ones are stripped and `grounded` goes false, but never 502 — a partially-cited answer still helps at 2am. `grounded` is three conditions (chunks cleared, model cited, all markers resolve) and `answer_source` keeps "we answered" separate from "it's grounded", because an uncited answer and a refusal otherwise look identical.
* **The finding:** one grounded answer took **195 seconds** — and 504'd at first, on a 120s timeout. Not the model's size but its host: `/api/ps` reports `size_vram: 0`, so appsrv infers on CPU, where reading ~2,000 characters of retrieved context costs everything (the same warm model answers a two-word prompt in 6.5s). Chunk size and `k` were framed as a precision tradeoff on Day 8; on CPU they are the latency dial too, so Day 11's eval has to measure time alongside recall or it will recommend a configuration nobody can wait for.
* **Also worth keeping:** the citation regex took two bugs. `\[(\d+)\]` reads `argv[1]` in a quoted shell snippet as a citation and ungrounds a good answer; excluding a preceding `]` to fix that then silently dropped the second marker of `[1][2]` — the most common way models cite — leaving it in the prose, absent from `sources`, with `grounded` still claiming true. The cosmetic fix reintroduced exactly the failure the endpoint exists to prevent.
* [x] **Day 11: Retrieval quality** — a 12-query eval set built to *fail*, then one technique at a time kept only if it moved a number. Relevance is a set (`primary` + `acceptable`) plus one substring the winning chunk must contain, which is the chunk-level check for the price of an `in` test. Hybrid ships — hand-written BM25 fused with dense by Reciprocal Rank Fusion, on ranks because cosine's 0.65–0.90 and BM25's unbounded scale cannot be added — and takes hit@1 from 8/12 to 9/12 for 2ms. Fusion only ever reorders; the 0.65 floor stays on **cosine**, so a keyword match can rescue a chunk dense search ranked 20th without smuggling it past the refusal guard.
* **The finding:** **BM25 alone beat semantic search on three of four metrics** — on a service whose premise is that `grep` fails because it matches characters instead of meaning. The corpus is full of *identifiers* (`137`, `too many clients already`, `x509`), and embedding a token whose value is being precisely itself is exactly the wrong operation. Hybrid shipped anyway, over the better headline number: the differences are 1–2 queries out of 12, and BM25-only needs shared vocabulary — a failure the eval has only two paraphrase queries to see.
* **Also worth keeping:** MMR reranking changed *nothing* on all 12 queries, so the embedding space got measured — same-document chunk pairs average 0.787 cosine, different-document 0.699. Separation **0.088**, while MMR at `lam=0.7` needs a gap above **0.156** to move one rank. Day 8's "unrelated text scores 0.59" looked like a threshold-calibration nuisance; it is the same anisotropy, and it decides whether an entire technique can function. Metadata filters cost recall (0.85 → 0.81) and bought nothing, because the filter removed a document the query's own `acceptable` set wanted. And the eval repeated the mistake it was built to fix: the June postmortem outranks the OOM runbook at 0.796 on the exit-137 query, containing the literal string `Code 137)`, and still scores a miss because `acceptable` was drawn too tightly — a label change worth making in a decision separate from the run that exposed it.
* [x] **Day 12: Live infra signals** — `connectors/alertmanager.py` polls Alertmanager's v2 API every 60s and reconciles firing/resolved alerts into the index (retained 24h after resolution), rendered as prose rather than JSON because the embedding model was trained on text. Resolution is detected by *absence* — Alertmanager only returns active alerts, so an indexed fingerprint a poll no longer mentions is what "resolved" means.
* **The finding:** live data broke two assumptions the static-corpus design rested on. Rendering "firing for 47 minutes" would change an alert's text — and its `content_hash` — on every single poll, reconciling the whole set as an update against a CPU-only Ollama every 60 seconds; every timestamp had to be absolute instead. And Chroma's `upsert` *merges* metadata rather than replacing it, so a flapping alert (resolve, re-fire, resolve again) would read back its *first* resolution timestamp forever unless `resolved_at` is explicitly gated on `status` each poll.
* [x] **Day 13: Conversational interface** — a Slack bot (`slack_events.py`, `sessions.py`) answering in-thread, with per-thread session history and HMAC-verified events.
* **The finding:** Slack allows 3 seconds to acknowledge an event; a grounded answer takes 165–204s on CPU. The ack and the answer became two separate HTTP conversations traveling in opposite directions — an inbound one authenticated by Slack's signature, an outbound `chat.postMessage` authenticated by the bot token — rather than one request held open for three minutes.
* [x] **Day 14: Capstone polish** — `GET /metrics` (answer outcomes, per-stage latency, retrieval-similarity distribution) with a Grafana dashboard; bearer-token auth on `POST /ask-runbook`; `retrieval.py` no longer imports from `ingest.py` (new `store.py` seam); both CI workflows actually trigger on pull requests. Project 2 complete.
* **The finding:** the `SIMILARITY_FLOOR` of 0.65 had been set from three anecdotes and no negative class. A `--floor-sweep` mode measured it properly — 9 more deliberately-unanswerable questions added to the eval set, every candidate floor swept from one recorded embedding pass per case — and the honest floor turned out to be **0.64**, the unique minimum at 1 total error against 2 at 0.65.

### Phase 3: Self-Healing Infra Agent

* [X] **Day 15: RAG vs. agents** — the observe → think → act loop, and the model-provider seam (`provider.py`) with automatic function calling explicitly disabled, verified against a live Gemini call.
* [X] **Day 16: Safe tools** — `get_pod_logs`, `get_recent_alerts`, `get_recent_deploys`, `restart_pod`, `scale_deployment` as individually schema'd functions, plus `search_runbooks` over HTTP into knowledge-copilot; RBAC `Role` scoped verb-for-verb, checked against the tool registry by a drift test.
* [X] **Day 17: The reasoning loop** — `agent.py`'s `diagnose()`, terminating on a `submit_diagnosis` tool call rather than parsed prose, capped by `MAX_ITERATIONS`; `POST /diagnose` with bearer auth from the first commit. A live smoke test against a real alert caught a real bug: `_dispatch` fetched a Kubernetes client for every tool, so `get_recent_alerts` and `search_runbooks` — neither of which touches the cluster — failed too whenever no cluster was reachable.
* [X] **Day 18: Human-in-the-loop** — `approvals.py`'s `proposed → approved → executed` state machine, with `rejected` and `expired` beside it; `audit.py` writing an fsynced JSONL line *before* the action, not after; Slack Block Kit approve/reject buttons and `POST /slack/interactive`, HMAC-verified over the raw form-encoded body. Write tools run from `decide()` and nowhere else — `agent.py` still passes `READ_ONLY`, and `approvals.py` re-dispatches in three lines rather than importing `agent._dispatch`, so no import edge exists from the reasoning loop to a cluster mutation.
* **The finding:** the deployment constraint fell out of the data structure. `_proposals` is in-process memory, so a second uvicorn worker means Slack's click can land on the worker that does not hold the proposal — and a legitimate Approve comes back "expired or unknown". `--workers 1` is load-bearing here for a reason entirely unlike the copilot's (two alert-sync loops racing on the same writes): the same flag on both services, guarding two different failures. The audit log needed the mirror-image fix — a bind mount, because a safety record that dies with its container cannot answer "who approved that restart" a week later.
* [X] **Day 19: Guardrails** — `guardrails.py`: namespace allowlist, replica floor, live-replica check, action rate limit, circuit breaker, model-call budget. Each one runs **twice** — in `propose()` before a button exists, and in `decide()` between `approved` and the write — because the guards worth having change their answer in between: another action ran, an execution failed, someone scaled the Deployment by hand while the message sat unread. The counts come from `audit.jsonl` rather than in-process counters, so a restart cannot silently refill an hourly budget or close an open breaker, and `blocked` joins the state machine as a terminal state distinct from `failed`: nothing broke, the agent refused.
* **The finding:** a guard can be unreachable at its own default. With `SHA_MAX_ACTIONS_PER_HOUR` and `SHA_BREAKER_THRESHOLD` both 3, three consecutive failures is also three attempts — so the rate limit, checked first, answered every time and the circuit breaker could never fire. The only symptom was a slightly less useful sentence in Slack. Every breaker test passed, because each one had raised the rate limit "to isolate the breaker", which is exactly what hid it; a demo script written to show the guards off is what surfaced it. The fix is a one-line reorder plus one test that runs at the shipped config and refuses to be isolated.
* [X] **Day 20: Closing the loop** — `alerts.py` and `POST /alerts`: Alertmanager's webhook, deduplicated on fingerprint and answered `202` while the diagnosis runs in the background. Synchronous was never an option — a diagnosis is six to ten model calls and runs for minutes, while Alertmanager gives up after seconds and re-POSTs, so answering inline would guarantee a timeout *and* a duplicate on every alert. Also `metrics.py` and `/metrics`, and a bounded retry on transient model errors: a diagnosis is a transcript held only in memory, so one `503` on the ninth call used to discard the eight that worked. Survivable while a human drove it; from today a discarded diagnosis is an alert that silently gets none. Also a Grafana dashboard — nine panels, three of them deliberately uncolored (approval rate, alert suppression rate) because a low number there isn't failure, it's the guardrails and dedup working as designed.
* **The finding:** the interesting webhook problem is not delivery, it is *repetition*. Alertmanager re-sends a firing group every `group_interval` — five minutes — until it resolves, so the naive endpoint turns one flapping alert into a fresh diagnosis every five minutes and spends a day's model budget before anyone reads the first proposal. The endpoint that receives alerts needs a memory more than it needs a queue.
* [X] **Day 21: Capstone polish** — the first real cluster this project has ever had. A local `kind` cluster, Day 19's `k8s/rbac.yaml` applied for the first time, and a real failure: `flaky-app` scaled to 0 in a `sandbox` namespace. The only gap left was reachability — the agent lives on appsrv, the cluster on a laptop — closed with `tailscale serve --tcp`, since `kind` binds its API server to loopback and no ACL fixes a port nothing is listening on. The agent authenticates with a ServiceAccount token scoped to exactly Day 19's RBAC, not an admin kubeconfig, over a channel that skips TLS verification deliberately: the cert has no SAN for a tailnet identity that didn't exist when the cluster came up, and Tailscale's own WireGuard tunnel is already the stronger guarantee. Recorded end to end: detect (a webhook POST, standing in for Alertmanager) → diagnose (survived a run of genuine Gemini `503`s on Day 20's retry logic) → approve (Slack) → fix (`scale_deployment`, verified `2/2` on the real cluster). Project 3 complete.
* **The finding:** the quota exhaustion blocking the first two attempts wasn't the diagnosis being expensive, it was an invisible coupling. `SHA_GEMINI_MODEL` has looked like its own setting since Day 16, but it was never set to anything other than `GEMINI_MODEL`'s default — and Gemini's free-tier quota is scoped per-project-*per-model*, not per-service. Every Slack question the copilot answered and every diagnosis this agent ran were spending the same 20-a-day budget without either service knowing the other existed. A distinct model name is what the separate variable only implied it already did.

### Phase 4: Security Triage

* [X] **Day 22: Scanners, and one schema for three of them** — `scan.sh` (the **client-side** half: Trivy, Bandit and Checkov over a checkout, emitting one envelope) and `scanners.py`, which turns three schemas that agree on almost nothing into one `Finding`. Deduplication is arithmetic on strings, not a model call: a finding tied to a package is identified by `(rule_id, package, installed_version)` regardless of which scan found it, so the same CVE from a filesystem scan and an image scan collapses into one; a finding tied to a line is identified by `(target, line)` alone. The service is target-agnostic on purpose — `repo`/`commit`/`branch` are request-body fields, so there is no env var naming a target, no scanners and no git credentials on the server, and onboarding a repo is a caller workflow plus a URL.
* **The finding:** dropping the rule id from the location key is the only thing that can catch two scanners describing the same misconfigured block, because Checkov's `CKV_*` and Trivy's `KSV-*` share no vocabulary and there is no crosswalk table between them. It is a marked simplification with a known ceiling, and Day 24 is where the ceiling arrived.
* [X] **Day 23: Batched, structured triage** — `provider.py` (one Ollama/Gemini seam) and `triage.py`: one schema-constrained model call per `ST_BATCH_SIZE` findings, returning `{priority, exploitability, impact, explanation, confidence}` per fingerprint. Two guards, both inherited: every returned fingerprint must be one that was actually sent (Day 10's invented-citation problem wearing a different field name), and `needs_human` is a *legal* priority rather than an error path — a model forced to choose among four real severities on a finding it can't judge doesn't refuse, it guesses, and the guess is indistinguishable from a real triage.
* **The finding:** two, and neither was the one the plan predicted. The appsrv timeout blamed on CPU-only hardware was a **runaway generation loop** — greedy decoding with no repetition penalty and no `num_predict` ceiling let the model fall into a repeating conditional and fill the context, never emitting a stop token; a `max_length` inside the Pydantic schema is the reliable fix, because it becomes part of the grammar generation is constrained against, where `repeat_penalty` is only a nudge. And **1.5b does not triage at all**: once the loop was capped it returned five byte-identical boilerplate explanations, `needs_human` for everything, every guard satisfied and all of it worthless. Counting results proves the pipeline ran; only reading them catches that. 7B is the measured floor.
* [X] **Day 24: Fixes proposed, never applied** — `fixes.py`: a unified diff built by deterministic Python from the finding's own context lines, which all three scanners were already sending and `scanners.py` was dropping on the floor. No model call in the module — a diff has a real oracle in `git apply --check`, so it is the one place a wrong answer is cheaply detectable and therefore worth code instead of tokens, and every Trivy misconfiguration already ships a remediation sentence written by whoever wrote the check. Ten `KSV-*` rules fire on one container block, so candidates are grouped by insertion point and emitted as **one** hunk carrying the union of the keys: ten independent diffs would each insert their own `securityContext:` and the second one applied would produce duplicate YAML keys — a patch that applies cleanly and then fails to parse. Verified end to end: 629 findings → 3 diffs, `git apply --check` clean, applied in a scratch clone and re-scanned.
* **The finding:** of the three fix classes the plan called mechanical, one survives — and "mechanical" turned out to have two axes, the second only visible after re-scanning a patched file. First, does the correct value have to be discovered from somewhere the service cannot see (an image digest, a memory limit, a uid that matches the image — all one-line edits, none constructible). Second, does the edit land where the finding's own context lines reach: `KSV-0118`'s pod-level half wants a different insertion point than the container line it anchored to, and `KSV-0117` wants an existing port changed in a way that cascades into another file. The re-scan also found a rule missing from the table that no test could have caught — which is the entire argument for the round trip over the green suite.

* [X] **Day 25: Gate CI on risk, not pass/fail** — `risk.py` turns triaged priorities into one score and a threshold verdict, and `app.py` puts it behind `POST /triage` → `202` + run id with `GET /triage/{id}` for the verdict, the same ack-now/answer-later split as Day 13's Slack bot and Day 20's `/alerts` and for the same reason: a run is over a hundred model calls, and a synchronous endpoint would time out on every real request and be retried. The score is a **weighted sum capped at 100**, not worst-finding-wins — forty mediums with no critical score 100 and fail the gate, which is the difference between "no criticals" and "safe". `confidence` is excluded from the formula because Day 23 measured it flat at every model size; `needs_human` scores zero and is reported separately, because a declined judgment is not a low-risk one. A reusable `workflow_call` workflow makes onboarding any repo a URL and a token: it scans on the caller's runner, POSTs, polls, and comments with the **caller's own** `GITHUB_TOKEN` — the service never holds a credential for anybody's repository.
* **The finding:** the body-size cap was written as a `Depends(...)` and capped nothing. FastAPI reads and parses the request body *before* it solves a route's dependencies, so the guard fired only after the megabytes it existed to refuse had already been read and turned into dicts — a control that passes every test you'd write for it while doing none of its job. Middleware runs before the route is matched, which is the only place the check means what its name says. The same ordering trap is why the two jobs in the reusable workflow are split on *permissions* rather than time: the half that handles untrusted repo contents is granted no token scopes at all.

### Future Phases

* [ ] **Gateway** — unified entrypoint across all four services.

## License

MIT
