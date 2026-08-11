# Service: Self-Healing Infra Agent

An agent that diagnoses a Kubernetes alert using read-only tools, and — with human approval in
Slack — executes exactly one narrow remediation.

This is **Project 3 (Week 3)** of the
[30-day AI-Native DevOps challenge](../../docs/ai-devops-30-day-challenge.md). Days 15–21.

> New to tool-calling agents? [**docs/self-healing-agent.md**](../../docs/self-healing-agent.md)
> explains what a tool call actually is on the wire, why RAG's one-shot retrieval can't diagnose,
> and how the loop knows when to stop. This README is the *what and how much*; that one is the
> *why*.

**Status (Day 15):** the model-provider seam is built and tested against real Gemini. Nothing in
this service can reach a Kubernetes cluster yet — that starts Day 16.

## Why this is an agent and not another RAG service

Week 2's copilot retrieves once, then answers. That is enough when the question already contains
what's needed to find the answer. Diagnosis doesn't work that way: "why is checkout-api
crashlooping" cannot be settled from a single retrieval, because what to look at next depends on
what the last thing said. An agent is that dependency made mechanical — the model requests the
next tool call based on what the previous one returned, and a loop dispatches it.

## Design decisions locked before any tool exists

**Automatic function calling is disabled on every request.** `google-genai` will execute tool
functions *itself* by default, up to 10 per request (`AutomaticFunctionCallingConfig`). Left on,
the SDK could call `restart_pod` with no human anywhere in the path — the whole premise of Day 18.
Tools are declared as JSON-schema `FunctionDeclaration`s, never as Python callables, so there is
no callable for the SDK to invoke even if the flag were dropped. Verified with a live call: a
declared tool comes back as a `ToolCall` the loop must dispatch, not as a side effect that already
happened.

**The tool allowlist is enforced in the loop, not only by the provider.** Gemini can be told
`mode="VALIDATED", allowed_function_names=[...]`, which narrows what the model is *offered*.
Ollama's `/api/chat` has no equivalent. So the provider hint is an optimization; the loop's own
`name in allowed` check is the control that actually holds, on every provider.

**The transcript is provider-shaped, on purpose.** Gemini requires the model's own turn to be
echoed back into the next request byte-for-byte — reconstructing it from a parsed `ToolCall` list
drops part ordering and thought signatures. So `BaseAgentProvider` owns three operations, not one:
`user()`, `chat()`, and `tool_result()`. The agent loop (Day 17) will never construct a message
itself; it only ever passes what a provider gave it back to that same provider.

**Termination is a tool call, `submit_diagnosis(...)`, not parsed prose.** Week 2 spent two bug
fixes on a citation regex before it was right. The fix here is structural instead of textual: the
model ends the loop by calling a schema'd function, which the SDK validates, rather than by
writing JSON in prose for us to parse.

## The tool table (target shape — none of these exist yet)

| Tool | Reads | Write? | Why it's narrow |
|---|---|---|---|
| `get_pod_logs(namespace, pod, container?, tail_lines)` | pod logs | no | `tail_lines` clamped server-side; no label selector — one pod, so the audit log names one pod |
| `get_recent_alerts(service?, since_minutes)` | Alertmanager v2 | no | same source knowledge-copilot already polls |
| `get_recent_deploys(namespace, deployment)` | ReplicaSet revisions | no | real rollout history, not a hand-written changelog |
| `restart_pod(namespace, pod)` | — | **yes** | deletes one pod by exact name; the ReplicaSet recreates it |
| `scale_deployment(namespace, deployment, replicas)` | — | **yes** | clamped by `SHA_MIN_REPLICAS` / `SHA_MAX_REPLICAS` |
| `search_runbooks(question, k)` | knowledge-copilot, over HTTP | no | retrieval only — the call never reaches a generator |
| `submit_diagnosis(summary, evidence, proposed_action, confidence)` | — | terminal | the loop's exit condition |

`restart_pod` deletes a pod rather than issuing a rollout restart deliberately: a rollout restart
needs `patch` on `deployments` — the same verb that can change an image — while deleting one pod
needs only `delete` on `pods`, and cannot do anything else. The narrower verb fixes the common
case (one wedged pod); when the whole Deployment is unhealthy, that's a diagnosis for a human, not
an action for this agent. `search_runbooks` calls knowledge-copilot over HTTP rather than importing
its retrieval code directly, for two reasons: Chroma's `PersistentClient` is not safe for
multi-process access and the copilot's alert-sync loop writes to it every 60 seconds, and this
service's Docker build context can't `COPY` a sibling service's modules anyway.

## `provider.py` — the model seam

```python
class BaseAgentProvider(ABC):
    def user(self, text: str) -> Any: ...
    def tool_result(self, call: ToolCall, result: dict) -> Any: ...
    def chat(self, system, contents, tools, allowed=None) -> AgentTurn: ...
```

`AgentTurn(text, tool_calls, raw)` — `raw` is the provider's own object for the turn, opaque to
everything except that same provider. `GeminiProvider` is the only implementation with a real
backend today; `FakeAgentProvider` in `tests/conftest.py` is a second, fully scripted
implementation used by every test in this service, and its existence is the actual proof that the
interface isn't Gemini's shape wearing an abstract base class. An Ollama implementation is not
built yet — this week's loop runs on Gemini Flash, because one diagnosis is 4–6 chained calls, and
generation against the CPU-only Ollama on appsrv measured 165–204s per call in Week 2. Twenty
minutes for one diagnosis has no consumer yet, so it isn't built speculatively.

`errors.py` declares the full failure taxonomy on day one: `UpstreamError` → `AgentProviderError`,
`K8sError`, `RunbookError`; and `GuardrailViolation`, which is not an `UpstreamError` at all —
nothing upstream failed when a guardrail refuses an action, and collapsing "refused" into "broke"
would make a working safety check look like an outage.

## Tests

10 tests, `tests/test_provider.py`, fully offline against a recording stand-in for
`client.models`. The one that matters most: `test_automatic_function_calling_is_always_disabled`
— it is the assertion that keeps Day 18's approval gate meaningful rather than decorative.

```bash
cd services/self-healing-agent
python -m pytest tests/ -q      # 10 passed
black --check .                 # clean
```

Live-verified separately (not part of the offline suite, needs `GEMINI_API_KEY`): a declared
`get_pod_logs` tool, given "pod api-7f9 in namespace sandbox is crashlooping", comes back as
`ToolCall(name='get_pod_logs', args={'namespace': 'sandbox', 'pod': 'api-7f9'})` — a request the
loop must dispatch, with nothing executed by the SDK itself.

## Not built yet

- `tools/` — the five infrastructure tools plus `search_runbooks`, and the RBAC manifests that
  must match them verb-for-verb — `later` (Day 16)
- `agent.py` — the loop itself, `MAX_ITERATIONS`, `POST /diagnose` — `later` (Day 17)
- Approval workflow, audit log, Slack interactive buttons — `later` (Day 18)
- Guardrails: namespace allowlist, replica floor, rate limit, circuit breaker, LLM call cap —
  `later` (Day 19)
- Alertmanager webhook, `/metrics`, agent Deployment manifest — `later` (Day 20)
- Injected-failure capstone recording, eval harness over recorded tool transcripts — `later`
  (Day 21)
