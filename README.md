# autonomous-infra-labs

Hands-on exploration of AI-native DevOps — self-healing infrastructure, a RAG-based ops copilot, and AI-assisted security triage — built service by service on top of a standard DevOps stack (Docker, Kubernetes, Jenkins, Prometheus/Grafana) rather than a new toolchain.

Each service here started as a learning exercise, but is built to a standard where every line is understood and defensible, not generated and accepted. No black-box agent frameworks until the underlying loop is understood by hand.

## Services

| Service | What it does | Status |
|---|---|---|
| [`services/log-analyzer`](./services/log-analyzer) | Turns a raw error log into strict, typed `{severity, likely_cause, suggested_fix, confidence}` output via a pluggable LLM provider (Ollama or Gemini). Exposed as a containerized FastAPI service with `/analyze-log`, `/health`, and Prometheus `/metrics` endpoints; golden-set eval harness next | In progress |
| [`services/knowledge-copilot`](./services/knowledge-copilot) | RAG service answering ops questions ("what's the usual fix for X") over runbooks, postmortems, and live alert/event data | Planned |
| [`services/self-healing-agent`](./services/self-healing-agent) | Tool-calling agent that diagnoses K8s alerts using read-only tools (logs, alerts, deploy history) and proposes a fix. Write actions are gated behind human approval and hard blast-radius limits | Planned |
| [`services/security-triage`](./services/security-triage) | Wraps existing scanners (Trivy, tfsec/Checkov, Bandit) and uses an LLM to deduplicate, prioritize, and explain findings. Proposes fixes as diffs — never auto-applies them | Planned |
| [`gateway`](./gateway) | Single FastAPI entrypoint tying the services above into one AI DevOps copilot | Planned |

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

Python · FastAPI · Ollama / Gemini API (pluggable) · Pydantic · Rich · Chroma · Docker · Kubernetes · Jenkins · Prometheus · Grafana · Trivy · tfsec/Checkov · Bandit

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
* [ ] **Service Build:** Log Analyzer — golden-set eval harness so behavior is regression-tested.

### Future Phases

* [ ] **Knowledge Copilot** — RAG over runbooks, postmortems, and live infra signals.
* [ ] **Self-Healing Agent** — diagnose-and-propose remediation for K8s issues, with approval gating.
* [ ] **Security Triage** — AI-triaged output from existing security scanners.
* [ ] **Gateway** — unified entrypoint across all four services.

## License

MIT
