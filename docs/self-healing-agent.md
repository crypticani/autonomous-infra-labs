# Self-Healing Infra Agent — how it works

A ground-up explanation of [`services/self-healing-agent`](../services/self-healing-agent): what
a tool call actually is, why retrieval alone can't diagnose a live problem, and how a loop that
calls a language model in a circle knows when to stop. Written for someone who has built the RAG
service in [`docs/knowledge-copilot.md`](./knowledge-copilot.md) and is now asking what changes
when the model needs to *act*, not just *answer*.

The service README covers *what was built and what it can do*. This document covers *why any of
it works*.

---

## The problem

An alert fires: `checkout-api` is crashlooping. You want to know why, and what to do about it —
and unlike a runbook question, you don't yet know what to look at. Maybe the logs say `OOMKilled`,
in which case the next question is whether a deploy changed the memory limit. Maybe the logs say
`connection refused`, in which case the next question is whether a dependency is also alerting.
The second question depends on the answer to the first.

That single sentence is the whole reason Week 2's RAG pattern — retrieve once, answer once — isn't
enough here. Retrieval assumes the question already contains what's needed to find the answer.
Diagnosis is a sequence of small investigations, each one chosen because of what the last one
found. An **agent** is that sequence made mechanical.

---

# Part 1 — The concepts

## 1.1 What a tool call actually is

The model cannot run code. It has never been able to. What "tool calling" (also called "function
calling") adds is much smaller than it sounds: the model is shown a list of function *signatures*
— names, descriptions, and a JSON Schema for the arguments — and instead of only being allowed to
answer in prose, it may also emit a structured request naming one of those functions and a set of
argument values that satisfy its schema.

```
system: "You diagnose Kubernetes problems. Tools available: get_pod_logs(namespace, pod)."
user:   "Pod api-7f9 in namespace sandbox is crashlooping. What do the logs say?"
                                    │
                                    ▼
model produces:  FunctionCall(name="get_pod_logs", args={"namespace": "sandbox", "pod": "api-7f9"})
```

That's it. The model did not read a log. It predicted — the same token-by-token process
`docs/log-analyzer.md` describes — that the shape of a good next move, given this system prompt
and this question, is a request for `get_pod_logs` with those two arguments. Nothing has happened
to any pod. Some *code you wrote* has to notice that request, decide whether to honor it, actually
call the Kubernetes API, and hand the result back to the model as another turn in the
conversation. That last part — notice, decide, call, hand back, repeat — is the entire agent loop.
There is no hidden execution engine on the model provider's side making this trustworthy by
default; the trust boundary is wherever your code decides to place it.

This is also why disabling a library's *automatic* function-calling matters enough to be the
first thing this service's design doc locks down. `google-genai`, the SDK this service uses, will
— if you let it — take that `FunctionCall`, look up a matching Python function you handed it, and
run it itself, no code of yours in between. For a tool like `restart_pod`, "no code of yours in
between" means no human is in between either. So this service never hands the SDK a callable at
all — only schemas — and turns that automatic behavior off explicitly, twice, so that removing
either safeguard alone still leaves the other standing.

## 1.2 Why RAG's loop shape can't do this

`POST /ask-runbook` in Week 2 is one pass: embed the question, retrieve the top-k chunks, build
one prompt, generate one answer. The number of round trips to any backend is fixed in advance —
one embedding call, one generation call — regardless of what the question turns out to need.

Diagnosis has no fixed number of round trips, because the right *next* question is discovered
mid-investigation. This is the same distinction as "look something up" versus "investigate":

```
RAG:    question ──▶ retrieve(question) ──▶ prompt(question, chunks) ──▶ answer
                                                                            (one shot, fixed cost)

Agent:  alert ──▶ think ──▶ call tool ──▶ observe result ──▶ think ──▶ call tool ──▶ ...
                                                                                       │
                                                              until: enough evidence ──┘
                                                                     to submit a diagnosis
```

This is usually described as an **observe → think → act loop**: observe the current state (the
alert, then each tool's result), think about what it implies (the model's turn), act (a tool
call), and repeat until there's enough to decide. RAG is the degenerate case of this loop where
the number of iterations happens to always be one.

## 1.3 The loop, and why it has to terminate on purpose

A loop driven by "keep calling tools until the model stops asking" has two failure modes: it
could spin forever on an ambiguous case, and it could stop the instant the model produces *any*
prose, even a half-formed guess, mistaking that for a finished diagnosis.

Two decisions close both gaps, and neither is a heuristic:

**A hard iteration cap.** The loop refuses to run more than `MAX_ITERATIONS` turns. This is not a
performance optimization — it's the difference between "the agent didn't figure it out" and "the
agent doesn't know it's stuck", which are very different things to tell a human waiting on a
diagnosis.

**Termination is itself a tool call.** Rather than treating any plain-text response as "the model
is done," the loop only accepts a diagnosis when the model calls a specific function —
`submit_diagnosis(summary, evidence, proposed_action, confidence)` — whose arguments are validated
against a schema the same way any other tool call's are. "Is the model finished?" stops being a
question answered by *inspecting prose* (which is exactly the kind of parsing that cost Week 2 two
separate bugs in a citation regex) and becomes a question answered by *which function got called*.
A model that runs out of turns without calling it produces no diagnosis at all — not a guessed
one — because a confidence score invented to fill a field is worse than an honest "I couldn't
tell."

## 1.4 Why the conversation history belongs to the model provider, not to your code

Once a tool has run, its result has to go back to the model so the next turn can use it. The
tempting design is a generic list of `{role, content}` dicts your own code builds and appends to.
That design breaks the first time you use a provider like Gemini, which requires the model's own
previous turn to be included **again, unmodified**, in the next request — not a summary of it, not
a re-serialization of the tool calls it contained, the literal object the API returned. Rebuilding
that object from a parsed list of "which tools were called" loses information the API put there
for its own bookkeeping (part ordering, internal reasoning markers) that your code has no reason
to know about and every reason to preserve.

The fix used here: the thing responsible for building a conversation's turns is the same thing
responsible for talking to the model — the provider — and the loop only ever passes what a
provider handed it straight back to that same provider. The loop reasons about *when* to call a
tool and *whether* it's allowed to; it has no opinion about *how* a turn is represented on the
wire, and doesn't need one.

## 1.5 Why the allowlist lives in the loop, not just in the request

Some providers let you constrain, in the request itself, which tool names the model is even
allowed to choose from this turn. That's a real feature and worth using — it means read-only
diagnosis and write-capable remediation can be the same code path, offered a different slice of
the same tool registry depending on which phase of the work is running.

But it's a hint to the model, enforced by that provider's servers, in a format specific to that
provider. A different backend may have no equivalent parameter at all. If the *only* thing
stopping a write tool from running during a read-only diagnosis is "we asked the model nicely not
to pick it," that stops being a safety property the moment you add a second provider, or the
moment the first one has a bug. So the loop checks every tool name the model returns against its
own allowlist before dispatching anything, on every provider, unconditionally. The request-level
constraint is optimization — fewer wasted turns where the model asks for something it can't have.
The loop's own check is the actual control.

---

*Part 2 (the tools and the loop) and Part 3 (approval, guardrails, and what the eval found) are
written as those pieces ship — Days 16–21.*
