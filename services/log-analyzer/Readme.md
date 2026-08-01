# Service: Log Analyzer

Turns a raw error log into strict, typed diagnostic data. A `POST /analyze-log` call
returns validated JSON — `{severity, likely_cause, suggested_fix, confidence}` — produced
by a pluggable LLM provider (local Ollama or cloud Gemini) behind one interface.

This is **Project 1 (Week 1)** of the [30-day AI-Native DevOps challenge](../../docs/ai-devops-30-day-challenge.md) — complete.

## Architecture

```text
POST /analyze-log ──▶ FastAPI layer ──▶ BaseLLMProvider ──┬──▶ OllamaProvider  (local, /api/generate)
   (LogRequest)      (validation,       (Strategy pattern)  └──▶ GeminiProvider  (cloud, google-genai)
                      error mapping)            │
                                                ▼
                                     LogAnalysis (Pydantic, strict schema)
```

**Three layers, each with one job:**

1. **FastAPI layer** (`log_analyzer.py`) — request validation (`LogRequest`), calls the
   active provider, maps upstream failures to HTTP status codes, and records metrics.
   The `/analyze-log` endpoint is deliberately a **sync `def`**, not `async def`: the
   provider clients (`requests`, `google-genai`) are blocking, so a sync endpoint lets
   Starlette run it in a threadpool — a blocking call there can't stall the event loop.
2. **Provider interface** (`BaseLLMProvider`, an ABC) — a Strategy pattern so the rest of
   the code never knows which model it's talking to. Swapping providers is a config change
   (`LLM_PROVIDER`), not a rewrite.
3. **Output contract** (`LogAnalysis`, a Pydantic model) — `severity` is a
   `Literal["LOW","MEDIUM","HIGH","CRITICAL"]`, `confidence` is bounded `0.0–1.0`. Both
   providers request structured output natively (Ollama's `format`, Gemini's
   `response_schema`) and the result is re-validated against this model before it leaves
   the service, so a malformed model response becomes a `502`, never bad data to the caller.

### Why dual-provider?

| Provider | Use it for | Tradeoff |
|---|---|---|
| **Gemini** (cloud) | Highest accuracy, no local GPU, fast | Needs an API key; data leaves the network; rate-limited on free tier |
| **Ollama** (local) | Privacy — logs never leave the box; no key; offline | Smaller local models need more prompt guidance (see the eval finding below) |

### Endpoints (port `7000`)

| Endpoint | Method | Purpose |
|---|---|---|
| `/analyze-log` | POST | Analyze a raw log → strict `LogAnalysis` JSON |
| `/health` | GET | Liveness/readiness. Actively probes the provider (Ollama `/api/tags`, or Gemini key presence) and reports `status: degraded` with the underlying issue |
| `/metrics` | GET | Prometheus metrics |
| `/docs` | GET | Interactive OpenAPI docs |

### Observability

Prometheus metrics are first-class, not just uptime:

- `llm_requests_total{provider, status}` — request volume and outcome (200 / 502 / 503 / 504)
- `llm_request_duration_seconds{provider}` — end-to-end latency histogram
- `llm_tokens_total{provider, token_type}` — prompt vs. completion tokens, for cost tracking

## The golden-set eval harness — and what it caught

`eval/run_eval.py` runs a small golden set (`eval/golden_set.json`, 5 labelled cases) against
the **same `ANALYSIS_SYSTEM_PROMPT`** the live service uses, and exact-matches on `severity`.
It exists to catch prompt regressions before they ship.

**The finding:** the lightweight local model (`qwen2.5-coder:3b`) initially scored **2/5**,
consistently **under-calling severity** — e.g. classifying a cascading DB failure as `HIGH`
instead of `CRITICAL`. Gemini scored **5/5** on the same set. The gap wasn't the model being
"wrong" — it was the prompt leaving "how severe is severe?" implicit.

**The fix:** an explicit **severity rubric** was injected into `ANALYSIS_SYSTEM_PROMPT`
(defining what LOW / MEDIUM / HIGH / CRITICAL each mean). With the rubric, the smaller local
model classifies in line with the frontier model — a prompt-engineering fix, verified by the
harness, not guessed. Because `run_eval.py` imports that same constant, the eval and the live
service can never drift apart.

```bash
LLM_PROVIDER=ollama python eval/run_eval.py   # exits non-zero if any case fails (CI-friendly)
```

## Running locally

**Direct:**
```bash
pip install -r requirements.txt
python log_analyzer.py            # or: uvicorn log_analyzer:app --host 0.0.0.0 --port 7000
```

**Docker (multi-stage build, non-root user):**
```bash
docker compose up --build         # from the repo root; reads config from .env
```

**Try it:**
```bash
curl -s http://localhost:7000/analyze-log \
  -H "Content-Type: application/json" \
  -d '{"raw_log": "2026-07-29 09:12:03 ERROR OOMKilled: container api exceeded memory limit 512Mi, restarted 4 times"}'
```

## Testing & CI

```bash
python -m pytest tests/ -q        # `-m` puts the package root on sys.path so `import log_analyzer` resolves
```

- `tests/test_schema.py` — `LogAnalysis` validation (valid payload + bad severity / out-of-range confidence / missing field).
- `tests/test_api.py` — endpoint tests that **mock the provider**, so CI never calls a live/paid model.

CI ([`.github/workflows/log_analyzer_ci.yml`](../../.github/workflows/log_analyzer_ci.yml)) runs
`lint (black) → test (mocked) → validate manifests (kubeconform --strict)` and only then
`build & push` the image — fail-fast on cheap checks before spending time on a build. (The
challenge suggests Jenkins; GitHub Actions is used here as an equivalent — same stage ordering.)

## Kubernetes deployment

Manifests live in [`k8s/`](./k8s): a `Deployment`, a `Service` (ClusterIP, port 7000), and a
`ConfigMap`. Config is split by sensitivity — non-secret env vars (`LLM_PROVIDER`,
`OLLAMA_MODEL`, `OLLAMA_BASE_URL`, …) in the ConfigMap; **`GEMINI_API_KEY` in a Secret that is
created manually and never committed**. The Deployment runs as non-root (`runAsNonRoot`, dropped
capabilities) with liveness/readiness probes on `/health`.

```bash
# 1. (Gemini only) create the secret out-of-band — never in git
kubectl create secret generic log-analyzer-secret --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY"

# 2. apply the manifests
kubectl apply -f k8s/

# 3. verify and reach it
kubectl rollout status deploy/log-analyzer
kubectl port-forward svc/log-analyzer 7000:7000
curl -s http://localhost:7000/health
```

**On a local kind/minikube cluster**, side-load a locally built image instead of pulling from GHCR:
```bash
docker build -t log-analyzer:local .
kind load docker-image log-analyzer:local --name <cluster>   # minikube: `minikube image load`
```
then set the Deployment's `image:` to `log-analyzer:local` with `imagePullPolicy: Never`.

### Notes worth defending

- **`OLLAMA_BASE_URL` can't be `localhost` in-cluster** — inside a pod `localhost` is the pod,
  not your host. Point it at a routable host/LAN/Tailscale IP (or `host.minikube.internal`).
- **Image tag is `:latest` for now** — mutable, so a rollout can't be pinned/rolled back to an
  exact build. Acceptable for a sandbox; will move to immutable SHA tags once the projects are done.
- **Readiness probe caveat** — `/health` returns HTTP 200 even when `status: degraded`, so the
  probe confirms the process is serving but does not withhold traffic when the LLM backend is
  down. To make readiness provider-aware, return a non-2xx from `/health` on `degraded`.

## Observability (Prometheus + Grafana)

The service exposes plaintext Prometheus metrics at `GET /metrics` (port 7000, no auth):

| Metric | Type | Labels | Answers |
|---|---|---|---|
| `llm_requests_total` | counter | `provider`, `status` | volume & error rate |
| `llm_request_duration_seconds` | histogram | `provider` | latency (p50/p95/p99) |
| `llm_tokens_total` | counter | `provider`, `token_type` | token cost |

Prometheus and Grafana run **outside this repo** (an existing external stack), so the only
requirement is that the service's `/metrics` endpoint is reachable from your Prometheus host.

### 1. Wire it into your external Prometheus

Expose the endpoint so Prometheus can reach it — a K8s `NodePort`/`Ingress`, or
`kubectl port-forward svc/log-analyzer 7000:7000` for a quick test — then add a static scrape
job to your existing `prometheus.yml` and reload:

```yaml
# on your external Prometheus
scrape_configs:
  - job_name: log-analyzer
    metrics_path: /metrics
    static_configs:
      - targets: ["<REACHABLE_HOST>:7000"]   # node IP:nodePort, ingress host, or host:7000
```

```bash
curl -X POST http://<prometheus-host>:9090/-/reload   # needs --web.enable-lifecycle; else SIGHUP the process
```

Confirm at `http://<prometheus-host>:9090/targets` — `log-analyzer` should show `UP`, and
`llm_requests_total` should return data on the `/graph` page.

> If your Prometheus instead lives *inside* the same cluster, prefer a `ServiceMonitor`
> (kube-prometheus-stack) or `prometheus.io/scrape` pod annotations over a static target.

### 2. Import the Grafana dashboard

Import [`observability/grafana/dashboards/log-analyzer.json`](./observability/grafana/dashboards/log-analyzer.json)
via **Dashboards → New → Import**. It's exported in portable form: on import Grafana prompts for a
Prometheus datasource (`DS_PROMETHEUS`) — pick your existing one and every panel binds to it.
The `$provider` dropdown auto-populates from the data.

Panels cover the three first-class concerns from Day 5: **volume** (request rate by status,
error %), **latency** (p50/p95/p99 from the histogram, avg), and **cost** (token throughput by
type, total tokens).

> Token panels only populate on **real** provider calls — mocked test traffic never hits the
> token-counting path, so drive live Ollama/Gemini requests to see them move.
