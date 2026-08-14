# Service: Self-Healing Infra Agent

An agent that diagnoses a Kubernetes alert using read-only tools, and — with human approval in
Slack — executes exactly one narrow remediation.

This is **Project 3 (Week 3)** of the
[30-day AI-Native DevOps challenge](../../docs/ai-devops-30-day-challenge.md). Days 15–21.

> New to tool-calling agents? [**docs/self-healing-agent.md**](../../docs/self-healing-agent.md)
> explains what a tool call actually is on the wire, why RAG's one-shot retrieval can't diagnose,
> and how the loop knows when to stop. This README is the *what and how much*; that one is the
> *why*.

**Status (Day 18):** the loop dispatches tools, terminates on `submit_diagnosis`, and is reachable
over `POST /diagnose`. A diagnosis carrying a write action now becomes a Slack proposal with
Approve/Reject buttons, and `POST /slack/interactive` is the only path by which a write tool runs.
Live-verified against a real alert and a real Gemini call — see below.

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

## `approvals.py`, `audit.py`, `slack.py` — the Day 18 gate

The write path is a separate path, and it starts where the loop ends:

```
alert → diagnose() → proposed_action → approvals.propose() → audit + Slack buttons
                                                                      ↓
                            k8s write ← approvals.decide() ← POST /slack/interactive
```

`agent.py` is unchanged and still passes `READ_ONLY`. `approvals.py` re-dispatches in three lines
rather than importing `agent._dispatch`, so there is no import edge at all from the reasoning loop
to a cluster mutation — a structural guarantee rather than a convention to remember. (`_dispatch`
also wraps every failure into a dict for the model to read; here a failure has to arrive as an
exception, so the audit line says `failed` instead of recording a success with an error inside it.)

**`_validate` is the first gate.** `proposed_action`'s schema is `{"type": ["object", "null"]}` —
the model can put anything there, including prose. Only a dict naming a **write** tool in
`REGISTRY`, with a dict of arguments, becomes a button. Read-only tools are refused too: there is
nothing to approve about reading a log, and offering it would train the on-call to click Approve
without reading.

**`decide()` checks four things in an order that is not rearrangeable** — unknown id, then expired,
then already-decided, then act. Expiry is checked *before* state so a stale proposal can never be
approved (`SHA_PROPOSAL_TTL`, default 3600; an approval clicked the next morning is not consent for
the cluster as it is now). And on approve, `state = APPROVED` happens *before* `_execute()`, so a
second click arriving while the first is still talking to the API server finds a non-`proposed`
state and refuses. That check-then-set ordering is the whole defence against one alert restarting a
pod twice.

**`audit.py` is fifteen lines and one idea.** `record()` writes a JSON line, then `flush()`, then
`os.fsync()`. Without the fsync the line sits in a kernel buffer, and a crash between deciding and
acting loses exactly the record the log exists for — afterwards you have to tell "never ran" from
"ran and died", and only a line already on disk tells you that. It has **no `try/except`**: if the
write fails, the `OSError` propagates and stops the approve handler *before* it touches the
cluster. No record, no action.

**`slack.py`'s new part is the wire format.** `verify_signature` is a deliberate copy of the
copilot's — two services in two containers, and the alternative to copying thirty lines is a shared
package that couples their deploys. What differs from Day 13 is that interactive components post
**form-encoded**, with the whole interaction JSON-encoded under a single `payload=` key, so parsing
is `parse_qs` → `json.loads` → `actions[0]`. The proposal id rides in the button's `value`, so the
click identifies itself without the handler guessing from the channel or the message text.
`replace_message` is the one place in the module that swallows an error, and deliberately: by the
time it runs the decision is made, audited and executed, so raising would turn a cosmetic failure
into a 500 on a request whose real work succeeded.

`POST /slack/interactive` carries no bearer token. The HMAC **is** the authentication, exactly as
on the copilot's `/slack/events` — Slack cannot send a bearer token, and adding one would only put
a shared secret in a URL somewhere. `await request.body()` comes first and nothing re-serialises
it, because the signature covers the raw bytes. Once the signature checks out the answer is always
`200`: a non-200 makes Slack retry, and a retried click is a second attempt at a cluster write.

## Tests

51 tests: 10 in `tests/test_provider.py` (unchanged from Day 15), 10 in `tests/test_tools.py`, 1 in
`tests/test_rbac.py`, 3 in `tests/test_agent.py` — a refused tool stays refused and the loop keeps
going, `MAX_ITERATIONS` exhausted produces no fabricated diagnosis, and an allowed tool's result
round-trips back into the transcript. Day 18 adds 9 in `tests/test_approvals.py` and 18 in
`tests/test_slack.py`.

The two that carry the most weight:

- **a second Approve executes nothing.** Two people see the same alert and both click; the pod is
  deleted once. `decide()` flips state before executing, so the second call finds a proposal that
  is no longer `proposed`. Reverse those two lines and this test is what fails.
- **the decision is on disk before the action that failed.** With a tool that raises, the audit log
  must still read `proposed → approved → failed`. Were the record written after the call instead, a
  crash mid-write and a call that never happened would be indistinguishable afterwards — and this
  is the test that would go green anyway.

Fully offline — no test needs a cluster, a key, or a network call. `FakeAgentProvider` scripts the
model's turns, the registry is data so a fake write tool is a `dataclasses.replace` rather than a
mock framework, and `k8s_client.get_apis` is monkeypatched wherever a test dispatches a real tool.
Every Slack request is fabricated and signed in-test, which is the only honest way to check that a
forged one is rejected.

```bash
cd services/self-healing-agent
python -m pytest tests/ -q      # 51 passed
black --check .                 # clean
```

Live-verified separately (not part of the offline suite, needs `GEMINI_API_KEY` and the app
actually running): a `PodOOMKilled` alert for `checkout-api`, posted to `/diagnose`, drove 6 real
tool-calling turns against Gemini and ended in `submit_diagnosis` with `confidence: 0.95` and a
proposed action to raise the memory limit — reasoning correctly around the fact that no Kubernetes
cluster is reachable from this sandbox at all.

## Deployment

Same shape as the other two services: a multi-stage `Dockerfile`, a CI workflow that lints, tests,
validates `k8s/` with kubeconform and pushes a multi-arch image to GHCR, and a compose entry that
binds to loopback while nginx terminates TLS in front of it.

Two constraints are specific to this service, and both are load-bearing:

**One worker, always.** `approvals._proposals` is in-process memory. With two uvicorn workers,
Slack's click can land on the worker that does not hold the proposal, and a legitimate Approve
comes back "expired or unknown". The copilot also runs `--workers 1`, but for an unrelated reason
(two alert-sync loops racing on the same writes) — the same flag, two different failures.

**The audit log needs a mount.** `SHA_AUDIT_PATH` points at `/app/audit/audit.jsonl`, and compose
bind-mounts `services/self-healing-agent/audit` over it. An append-only record that dies with the
container cannot answer "who approved that restart" a week later, which is the only question it
exists to answer.

**Its own subdomain, not a `location` on the copilot's.** `sha.crypticani.dev` gets its own server
block, sharing a certificate with `knowledge-copilot.crypticani.dev` via `certbot --expand`. The
tempting alternative — one more `location` in the copilot's existing block — was rejected: this
service can delete pods, and its public surface should be structurally isolated rather than one
mis-scoped `location` away from being served under the copilot's hostname. It also keeps the
deployment consistent with everything else here, where a service means one directory, one image,
one compose entry, one CI workflow, one hostname.

```nginx
server {
    server_name sha.crypticani.dev;

    # The agent's only public endpoint. An exact match, so everything else 404s:
    # /diagnose is bearer-authed but has no reason to be reachable from the internet,
    # and Day 20's /metrics and alert webhook are both reached over loopback from
    # Prometheus and Alertmanager on this same host.
    location = /slack/interactive {
        proxy_pass http://127.0.0.1:7200;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / { return 404; }

    # certbot --nginx fills in listen 443 / ssl_certificate / etc.
}
```

Nothing strips or rewrites the body: the HMAC is computed over the raw bytes, and a proxy that
re-encoded the form would break every signature.

Verify nginx before touching Slack, because Slack will not tell you:

```bash
curl -i https://sha.crypticani.dev/slack/interactive -X POST -d 'payload={}'   # expect 401
```

`401` is nginx reaching the agent and the agent rejecting an unsigned request — the whole path
working. `502` means the container is down; `404` means the `location` did not match.

Then set the Slack app's **Interactivity & Shortcuts → Request URL** to
`https://sha.crypticani.dev/slack/interactive`. Two things about that setting: it is global to the
app, so it is a one-way choice for the app the copilot also uses (safe today only because the
copilot uses events and never interactivity) — and unlike Event Subscriptions, Slack does **not**
send a verification challenge when saving it. A successful save proves nothing, which is why the
`curl` above comes first.

## Not built yet

- Guardrails: namespace allowlist, replica floor, rate limit, circuit breaker, LLM call cap —
  `later` (Day 19)
- Alertmanager webhook, `/metrics`, agent Deployment manifest — `later` (Day 20)
- Injected-failure capstone recording, eval harness over recorded tool transcripts — `later`
  (Day 21)
