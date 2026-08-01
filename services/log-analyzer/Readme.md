# Service: Log Analyzer (Week 1 Complete)

## Architecture
The Log Analyzer is a synchronous FastAPI microservice that ingests raw application or infrastructure logs and uses an LLM to parse them into a strict, validated JSON schema (`{severity, likely_cause, suggested_fix, confidence}`). 

**Dual-Provider Interface:** The service implements a Strategy pattern (`BaseLLMProvider`) to seamlessly toggle between a cloud-hosted frontier model (Gemini) and a locally hosted, privacy-first model (Ollama) via the `LLM_PROVIDER` environment variable.

## The Golden-Set Evaluation
We built an offline evaluation harness (`eval/run_eval.py`) to prevent prompt regressions. 
* **The Finding:** During initial testing, the lightweight local model (`qwen2.5-coder:1.5b`) scored poorly, routinely under-calling severities (e.g., misclassifying cascading DB failures as HIGH instead of CRITICAL). Gemini scored 5/5.
* **The Fix:** We injected an explicit, strict **Severity Rubric** into the global `ANALYSIS_SYSTEM_PROMPT`. By explicitly defining the parameters for LOW, MEDIUM, HIGH, and CRITICAL, we successfully engineered the smaller, private local model to output correctly classified data matching the frontier model's logic.

## Deployment (Kubernetes)
1. Build the image locally or let GitHub Actions push it to GHCR.
2. If testing Gemini, create the secret manually:
   ```bash
   kubectl create secret generic log-analyzer-secret --from-literal=GEMINI_API_KEY='your_api_key'