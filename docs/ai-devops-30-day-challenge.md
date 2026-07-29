# The 30-Day AI-Native DevOps Challenge

**Goal:** Go from "DevOps engineer who uses Docker/Jenkins/K8s/Prometheus" to "DevOps engineer who can design, build, and run AI-powered production systems" — self-healing infra, AI-assisted RCA, AI-triaged security scanning.

**How this is built:**
- It leans on what you already have — Python, Docker, K8s, Jenkins, Prometheus/Grafana, AWS/OCI. No detours into tools you don't need.
- Instead of 30 disconnected toy exercises, there are **3 flagship builds** — one per skill (RAG, agents, security triage) — each one interview- and portfolio-ready by the end of its week.
- Pace is a scaffold, not a stopwatch. ~1.5–2 hrs on weekdays, longer on the "capstone polish" days. If you're also deep in your Azure/AKS/Terraform cert prep right now, it's fine to stretch this to 5–6 weeks — the *order* of concepts matters more than hitting exactly 30 calendar days.
- Ground rule: build against a sandbox (kind/minikube locally, or a personal AWS/OCI account) — never point an autonomous or semi-autonomous agent at your employer's production clusters.

---

## Day 0 — Setup
- Get a free Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey) — no credit card needed
- `pip install fastapi uvicorn pydantic chromadb google-genai pytest`
- Set up a local K8s sandbox: `kind create cluster` or `minikube start`
- Create one GitHub repo to hold all the projects — you've already got it: `autonomous-infra-labs`

**Working with the free tier:** Gemini's free tier is fine for daily practice but rate-limited (roughly 10–15 requests/minute and a few hundred to ~1,500/day depending on the model, resetting at midnight Pacific) — it'll bite during Day 11's batch eval and Week 3's agent loop, where one diagnosis can mean several chained calls. Two things worth doing early: (1) wrap your LLM calls behind a thin interface now, so swapping providers later — Claude, OpenAI, a local model via Ollama — is a config change, not a rewrite; (2) default to a Flash/Flash-Lite-tier model for the frequent, cheap calls (tool selection, classification) and save a heavier model for the few calls that need real reasoning (RCA, security triage). One more gotcha: enabling billing on a Gemini project removes its free tier entirely rather than just raising the ceiling — use a separate project if you want to test paid limits without losing the free one.

---

## Week 1 — AI Engineering Foundations
**Capstone: AI Log/Error Analyzer microservice**

| Day | Learn | Build |
|---|---|---|
| 1 | How LLMs actually work for engineers: tokens, context window, temperature, system vs. user prompts, function/tool calling | A script that sends a raw error log to the LLM API and prints back a plain-English explanation |
| 2 | Prompt engineering: clear instructions, few-shot examples, XML tags for structure, forcing strict JSON output ([docs.claude.com prompt engineering guide](https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/overview)) | Turn Day 1 into a structured analyzer returning `{severity, likely_cause, suggested_fix, confidence}` as validated JSON |
| 3 | FastAPI as an AI-serving layer: async endpoints, Pydantic request/response models, handling LLM timeouts gracefully | Wrap Day 2 logic in `POST /analyze-log` with input validation and error handling |
| 4 | Packaging AI services safely: secrets management (never hardcode keys), health checks, graceful degradation | Dockerize the FastAPI app, run via docker-compose with env-based secrets, add `/health` |
| 5 | Observability for AI services: token cost, latency, error rate are first-class metrics — not just uptime | Instrument with `prometheus_client`, expose `/metrics`, build a Grafana dashboard for cost + latency + volume |
| 6 | Testing non-deterministic systems: schema validation tests, golden-set regression tests, mocking LLM calls so CI doesn't hit a paid API every run | Jenkins pipeline stage: lint → unit test (mocked) → build image → push to registry |
| 7 | **Capstone polish** | Deploy to your sandbox K8s cluster, write a README covering architecture and design decisions, push as Project 1 |

---

## Week 2 — RAG: DevOps Knowledge Copilot
**Capstone: RAG-powered runbook/incident Q&A service**

| Day | Learn | Build |
|---|---|---|
| 8 | Embeddings & vector search: how text becomes vectors, cosine similarity, why chunk size/overlap matters | Embed 5–10 of your own runbooks/READMEs in Chroma, run a similarity search |
| 9 | Ingestion pipeline design: idempotent re-indexing, metadata tagging (service, doc type, date) | Reusable ingestion script for docs, runbooks, past postmortems |
| 10 | The RAG pattern: retrieve top-k, build a context-augmented prompt, cite sources so answers stay grounded | `POST /ask-runbook` endpoint |
| 11 | Retrieval quality: hybrid keyword+vector search, reranking, metadata filters | Small eval set (10–15 Q&A pairs); measure whether retrieval surfaces the right chunk |
| 12 | Connecting live infra data into the index | Connector that ingests recent Prometheus alerts / K8s events into the vector store |
| 13 | Conversational interfaces for ops: session handling, routing to chat | A minimal Slack bot (or web chat UI) in front of the RAG service |
| 14 | **Capstone polish** | "DevOps Knowledge Copilot" — architecture diagram, auth on the endpoint, demo recording. Project 2 |

---

## Week 3 — Agentic AI: Self-Healing Infra Agent
**Capstone: an agent that diagnoses — and with approval, remediates — K8s issues**

| Day | Learn | Build |
|---|---|---|
| 15 | RAG vs. agents: the observe → think → act loop; tool/function calling as the mechanism agents use to *act* | Read Anthropic's tool-use docs; sketch the agent's tool list before writing code |
| 16 | Designing safe tools: narrow, well-scoped functions beat one giant `run_kubectl` | Implement `get_pod_logs`, `get_recent_alerts`, `get_recent_deploys`, `restart_pod`, `scale_deployment` as individually schema'd functions |
| 17 | The reasoning loop: how the model picks a tool and knows when it's done | Agent that, given an alert, calls *read-only* tools and outputs diagnosis + proposed fix + confidence — no execution yet |
| 18 | Human-in-the-loop design: why full autonomy is a bad default for anything that restarts or scales prod | Approval workflow (Slack or FastAPI) — human approves, then the write-tool executes; log every decision |
| 19 | Guardrails: blast-radius limits, rate limits, circuit breakers, cost caps | Add hard limits — e.g. never scale below N replicas, max N actions/hour, namespace allowlist |
| 20 | Closing the loop: wiring real alert sources to the agent | Connect Alertmanager/Grafana alerting webhook → agent's FastAPI endpoint, in the sandbox cluster |
| 21 | **Capstone polish** | Inject a real failure (kill a pod, stress-test CPU) and record the agent detect → diagnose → fix (with your approval) end to end. Project 3 |

---

## Week 4 — AI Security Scanning + Production Hardening + Capstone
**Capstone: AI-triaged DevSecOps layer + unified "AI DevOps Copilot" demo**

| Day | Learn | Build |
|---|---|---|
| 22 | Where AI fits in security scanning: a triage/summarization layer *on top of* real scanners (Trivy, tfsec/Checkov, Bandit) — never a replacement for them | Run Trivy + Bandit against one of your repos, save raw JSON output |
| 23 | Prompting for structured risk triage: dedup, prioritize by exploitability/business impact, avoid false confidence | Feed scanner JSON to the LLM, get back deduplicated, prioritized findings with plain-English risk explanations |
| 24 | Why fixes should be *proposed*, not auto-applied, for security findings | Output a suggested git diff (pin a base image, add a securityContext) for human review |
| 25 | Gating CI/CD on risk, not just pass/fail | Jenkins stage: scan → AI triage → PR/Slack comment → optionally fail the build above a risk threshold |
| 26 | Extending triage to runtime signals, not just static scans | Feed Falco or K8s audit log events through the same triage pipeline |
| 27 | Cost/latency tradeoffs at production scale | Benchmark a cheap model for simple triage vs. a stronger one reserved for complex RCA; document cost-per-run for all 3 services |
| 28 | What "production-ready" means for AI systems vs. a working demo | Add a lightweight eval harness (golden dataset + assertions) across all 3 services |
| 29 | Communicating this work | Write each project up as a short case study (problem → architecture → tradeoffs → results) and publish it |
| 30 | **Capstone demo day** | Tie the 3 services behind one FastAPI gateway as "AI DevOps Copilot," record a demo, clean up READMEs, write a short "what's next" section |

---

## Stack Cheat Sheet

| Category | Start with | Level up to |
|---|---|---|
| LLM API | Claude API | Add a local model via Ollama for the security-triage step if scan data shouldn't leave your network |
| Serving | FastAPI + Pydantic + Uvicorn | — |
| Vector DB | Chroma (fast to stand up locally) | Qdrant or pgvector if you want something closer to what you'd actually run in production |
| Agent framework | Raw function/tool-calling (you'll actually understand the loop) | LangGraph or CrewAI, once you can explain the loop *without* a framework |
| K8s sandbox | kind / minikube | A personal AWS/OCI account — never your employer's clusters |
| Security scanners | Trivy, tfsec/Checkov, Bandit | Semgrep for deeper SAST |
| CI/CD | Jenkins (what you already know) | — |
| Observability | Prometheus + Grafana (what you already know) | — |

---

## Beyond Day 30
- Fine-tuning a small open model for domain-specific triage
- Multi-agent orchestration — a planner agent delegating to specialist agents
- Hardening the AI systems themselves against prompt injection — a DevSecOps angle most candidates won't have thought about
- Cost forecasting / capacity planning using the same LLM+metrics pattern
- Turning the Day 18 audit log into evidence for SOC2-style compliance reviews
