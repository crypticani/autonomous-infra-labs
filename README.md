# autonomous-infra-labs

Hands-on exploration of AI-native DevOps — self-healing infrastructure, a RAG-based ops copilot, and AI-assisted security triage — built service by service on top of a standard DevOps stack (Docker, Kubernetes, Jenkins, Prometheus/Grafana) rather than a new toolchain.

Each service here started as a learning exercise, but is built to a standard where every line is understood and defensible, not generated and accepted. No black-box agent frameworks until the underlying loop is understood by hand.

## Services

| Service | What it does | Status |
|---|---|---|
| [`services/log-analyzer`](./services/log-analyzer) | FastAPI service that turns a raw error log into structured `{severity, likely_cause, suggested_fix}` output via an LLM | Planned |
| [`services/knowledge-copilot`](./services/knowledge-copilot) | RAG service answering ops questions ("what's the usual fix for X") over runbooks, postmortems, and live alert/event data | Planned |
| [`services/self-healing-agent`](./services/self-healing-agent) | Tool-calling agent that diagnoses K8s alerts using read-only tools (logs, alerts, deploy history) and proposes a fix. Write actions are gated behind human approval and hard blast-radius limits | Planned |
| [`services/security-triage`](./services/security-triage) | Wraps existing scanners (Trivy, tfsec/Checkov, Bandit) and uses an LLM to deduplicate, prioritize, and explain findings. Proposes fixes as diffs — never auto-applies them | Planned |
| [`gateway`](./gateway) | Single FastAPI entrypoint tying the services above into one AI DevOps copilot | Planned |

## Architecture

```
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

- **Understand before you automate.** Every service is built by hand first; frameworks are added later, only once the underlying loop can be explained without one.
- **Nothing writes to infrastructure without a human in the loop.** The self-healing agent diagnoses and proposes; a human approves before any restart, scale, or rollback executes.
- **Sandboxed only.** Everything here runs against a local (kind/minikube) or personal cloud cluster — never against production infrastructure belonging to an employer or client.
- **A working demo isn't a finished service.** Cost, latency, and failure modes are tracked from day one; each service has a golden-set regression test so changes don't silently break it.

## Tech stack

Python · FastAPI · Claude API · Chroma · Docker · Kubernetes · Jenkins · Prometheus · Grafana · Trivy · tfsec/Checkov · Bandit

## Getting started

Each service is self-contained and will include its own setup instructions as it's built. Global prerequisites:

```bash
git clone https://github.com/crypticani/autonomous-infra-labs.git
cd autonomous-infra-labs

# LLM API key
export ANTHROPIC_API_KEY=your_key_here

# local K8s sandbox
kind create cluster   # or: minikube start
```

## Roadmap

- [ ] Log Analyzer — structured, LLM-backed log/error triage
- [ ] Knowledge Copilot — RAG over runbooks, postmortems, and live infra signals
- [ ] Self-Healing Agent — diagnose-and-propose remediation for K8s issues, with approval gating
- [ ] Security Triage — AI-triaged output from existing security scanners
- [ ] Gateway — unified entrypoint across all four

## License

MIT
