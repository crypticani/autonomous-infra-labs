# Service: Self-Healing Infra Agent

An agent that diagnoses a Kubernetes alert using read-only tools, and — with human approval in
Slack — executes exactly one narrow remediation.

This is **Project 3 (Week 3)** of the
[30-day AI-Native DevOps challenge](../../docs/ai-devops-30-day-challenge.md). Days 15–21.

> New to tool-calling agents? [**docs/self-healing-agent.md**](../../docs/self-healing-agent.md)
> explains what a tool call actually is on the wire, why RAG's one-shot retrieval can't diagnose,
> and how the loop knows when to stop. This README is the *what and how much*; that one is the
> *why*.

**Status (Day 17):** the loop dispatches tools, terminates on `submit_diagnosis`, and is reachable
over `POST /diagnose`. Live-verified against a real alert and a real Gemini call — see below.

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

## The tool table

| Tool | Reads | Write? | RBAC verb | Why it's narrow |
|---|---|---|---|---|
| `get_pod_logs(namespace, pod, container?, tail_lines)` | pod logs | no | `pods/log:get` | `tail_lines` clamped server-side to 2000; no label selector — one pod, so the audit log names one pod |
| `get_recent_alerts(service?, since_minutes)` | Alertmanager v2 | no | — (HTTP) | same source knowledge-copilot already polls |
| `get_recent_deploys(namespace, deployment)` | ReplicaSet revisions | no | `replicasets:list` | real rollout history, not a hand-written changelog; filtered by ownerReferences so it never needs `deployments:get` |
| `restart_pod(namespace, pod)` | — | **yes** | `pods:delete` | deletes one pod by exact name; the ReplicaSet recreates it |
| `scale_deployment(namespace, deployment, replicas)` | — | **yes** | `deployments/scale:patch` | clamped to `SHA_MIN_REPLICAS`–`SHA_MAX_REPLICAS`, not rejected — Day 19's guardrail is the hard stop |
| `search_runbooks(question, k)` | knowledge-copilot, over HTTP | no | — (HTTP) | retrieval only — the call never reaches a generator |
| `submit_diagnosis(summary, evidence, proposed_action, confidence)` | — | terminal | — | the loop's exit condition (Day 17 dispatches it specially) |

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

## `k8s_client.py` and `tools/` — the Day 16 additions

`k8s_client.py` is one function, `get_apis()`, `@lru_cache(maxsize=1)`: it chooses
`load_incluster_config()` when the ServiceAccount token file exists and `load_kube_config()`
otherwise, and returns the `(CoreV1Api, AppsV1Api)` pair. Every tool takes that pair as its first
argument rather than importing this module — the same seam `store.py` plays for the copilot — so
every tool test passes a fake pair and none needs a cluster.

`tools/k8s.py` holds the four cluster-touching functions; `tools/external.py` holds the two that
reach out over HTTP (Alertmanager, knowledge-copilot); `tools/__init__.py` holds the registry
itself:

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict          # JSON Schema for the arguments object
    fn: Callable[..., dict]
    write: bool
    needs: tuple[str, ...] = ()   # RBAC verbs this tool requires
```

`REGISTRY` holds all seven `ToolSpec`s; `READ_ONLY` and `ALL` are the two name-tuples Day 17 and
Day 18 will pass as `agent.py`'s `allowed` set — no signature changes between the two days, per the
week's design doc. `needs` exists only so `tests/test_rbac.py` can compare the registry against
`k8s/rbac.yaml`'s actual verbs; the two are written in different files and would otherwise drift
silently until a 403 surfaced it live.

None of the tools wrap their return value as `{"output": ...}` / `{"error": ...}` — that
convention belongs to the loop's dispatch (Day 17), applied uniformly to whatever a tool returns
or raises. A tool here either returns its data or raises `K8sError` / `RunbookError` /
`UpstreamError`.

`k8s/rbac.yaml` — `ServiceAccount` + `Role` + `RoleBinding`, scoped to the `sandbox` namespace and
to exactly four rules: `pods/log:get`, `pods:delete`, `replicasets:list` (`apps`),
`deployments/scale:patch` (`apps`). No cluster is available in this environment to run
`kubectl apply` or `kubectl auth can-i` against, so the manifest is verified only by
`tests/test_rbac.py`'s drift check today — the live `can-i` check is Day 21's capstone item.

## `agent.py` — the loop, and `app.py` — the endpoint

`diagnose(alert, provider, allowed=READ_ONLY) -> Diagnosis` is the whole loop: build the initial
transcript, call `provider.chat()`, and for every tool call the model asks for, either dispatch it
or refuse it. `submit_diagnosis` is intercepted by name before generic dispatch — it's the loop's
exit condition, not a tool that does anything. `MAX_ITERATIONS` (default 6, `SHA_MAX_ITERATIONS`)
is the backstop: a loop that exhausts it without seeing `submit_diagnosis` returns
`confidence=None, incomplete=True` rather than inventing a number.

The allowlist check (`tool_call.name not in allowed`) is enforced here, in the loop, not only via
Gemini's `VALIDATED` tool-calling mode — Ollama's `/api/chat` has no equivalent, and a bug in a
provider's own constraint must not be the only thing standing between the model and a tool it
wasn't offered. A refusal is fed back to the model as a tool *error*, so the transcript shows the
refusal happened rather than silently dropping the call.

`app.py` adds `POST /diagnose`, gated by `require_token` — copied from knowledge-copilot's, bearer
auth from the first commit rather than bolted on at capstone.

**A live smoke test found a real bug.** `_dispatch` originally called `k8s_client.get_apis()`
unconditionally for every tool, including `get_recent_alerts` and `search_runbooks`, which never
touch the cluster and ignore the argument. On a box with no kubeconfig at all, every tool failed
with the same "invalid kube-config" error — including the two that had no reason to. The fix:
`_dispatch` only fetches a real client when `ToolSpec.needs` says the tool is one of the four that
touch Kubernetes; everything else gets a placeholder it was already ignoring. Re-running the same
alert afterward, `get_recent_alerts` genuinely reached the tailnet's Alertmanager (confirmed
separately with a bare `curl`, `200`) instead of failing on an unrelated error.

## Tests

24 tests: 10 in `tests/test_provider.py` (unchanged from Day 15), 10 in `tests/test_tools.py`, 1 in
`tests/test_rbac.py`, 3 in `tests/test_agent.py` — a refused tool stays refused and the loop keeps
going, `MAX_ITERATIONS` exhausted produces no fabricated diagnosis, and an allowed tool's result
round-trips back into the transcript. Fully offline — no test needs a cluster, a key, or a network
call; `FakeAgentProvider` scripts the model's turns and `k8s_client.get_apis` is monkeypatched
wherever a test dispatches a real tool.

```bash
cd services/self-healing-agent
python -m pytest tests/ -q      # 24 passed
black --check .                 # clean
```

Live-verified separately (not part of the offline suite, needs `GEMINI_API_KEY` and the app
actually running): a `PodOOMKilled` alert for `checkout-api`, posted to `/diagnose`, drove 6 real
tool-calling turns against Gemini and ended in `submit_diagnosis` with `confidence: 0.95` and a
proposed action to raise the memory limit — reasoning correctly around the fact that no Kubernetes
cluster is reachable from this sandbox at all.

## Not built yet

- Approval workflow, audit log, Slack interactive buttons — `later` (Day 18)
- Guardrails: namespace allowlist, replica floor, rate limit, circuit breaker, LLM call cap —
  `later` (Day 19)
- Alertmanager webhook, `/metrics`, agent Deployment manifest — `later` (Day 20)
- Injected-failure capstone recording, eval harness over recorded tool transcripts — `later`
  (Day 21)
