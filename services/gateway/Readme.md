# Service: Gateway

One address in front of the other four, and one endpoint that decides which of them a
plain-English question belongs to.

The reverse-proxy half of this is not interesting — nginx already does it, and I wrote about
twenty lines of it. The interesting half is `POST /ask`: a sentence goes in, and a model
picks which of four services can answer it, with the answer coming back attributed to
whichever one produced it. It is Day 2's structured-output lesson and Week 3's
tool-selection lesson applied one layer up, and it is what makes four ports read as one
product.

It is also where the four services stop being four services I built and start being a thing
with a front door, which is the only reason Day 30 is a build day and not a victory lap.

## The problem the plan didn't survive

I planned this endpoint on Day 22 with three example questions. Reading the four request
models before writing any code killed all three.

| service | endpoint | body | answerable from a sentence? |
|---|---|---|---|
| knowledge-copilot | `POST /ask-runbook` | `{question, k}` | **yes** |
| log-analyzer | `POST /analyze-log` | `{raw_log}`, min 15 chars | no — needs the log |
| self-healing-agent | `POST /diagnose` | `{alert: dict}` | no — needs the alert |
| security-triage | `POST /triage` | `{repo, scans}` | no — needs scanner output |

*"Why did checkout start 500ing at 3am"* routes to log-analyzer perfectly well and then has
nothing to send it: log-analyzer reads log text it is handed and cannot fetch, search or
tail anything. *"Is this Dockerfile safe to ship"* is security-triage's question and
security-triage reasons about scanner output rather than running scanners. Exactly one of my
four services answers a bare question.

The tempting fix is to let the router fill in the body — extract an `alert` dict from the
prose, synthesise a plausible `raw_log`. I did not build that, and refusing to is the whole
design. The self-healing agent has write tools behind that `alert`. A router that invents
evidence to hand to an agent that acts on evidence is not a convenience, it is the failure
this repo has spent four weeks building guards against.

So the router never fabricates a body. It says which service, and it says what to attach.

## The split that makes it honest

The model decides **which service**. Plain Python decides **whether the request can
proceed**. There are exactly two refusals here and only one of them can be a model's fault:

```
"I don't know which of these you want"    <- router.py, the model's call, as service "none"
"I know, but you didn't send the data"    <- app.py, a dict lookup, cannot be wrong
```

That second one is the part I like. `needs_input` is not a judgment — it is
`if backend.needs and not request.attachment`. It never calls a model, it never guesses, and
it is right every single time. Pushing a decision out of the model and into a lookup is
almost always available and almost never the first idea.

The first one is Day 23's lesson one layer up. A 1.5b model satisfied every guard in triage
and produced five byte-identical explanations with `needs_human` on everything. What made
triage's confident answers worth reading afterwards was that declining was a legal move —
so `none` is a member of the router's enum, not an error path.

## `backends.py` — one table, three readers

Each service is described once, and three things read the description: the router prompt is
generated from `answers`, the needs-check reads `needs`, and the proxy reads `url()` and
`token()`.

```python
Backend(
    name="log-analyzer",
    answers=(
        "Reads raw log text and returns a structured diagnosis: severity, error type, "
        "root cause and a suggested fix. It analyses log text it is handed and cannot "
        "fetch, search or tail logs itself, so the log must be supplied."
    ),
    path="/analyze-log",
    url_env="GW_LOG_ANALYZER_URL",
    default_url="http://log-analyzer:7000",
    token_env="",
    needs="raw_log",
    hint="attach the log text itself as `attachment`",
    body=lambda question, attachment: {"raw_log": attachment},
)
```

`answers` is prompt text, so it is written for a model rather than for me.

**The first version of this paragraph gave a reason that turned out to be wrong**, and it is
worth leaving the correction visible. I had written that every entry names what its service
needs *"because the model's classification and the code's needs-check have to agree about
that or the two halves of a decline contradict each other."* They do not have to agree. The
needs-check is `if backend.needs and not request.attachment` — it never consults the model,
which was the entire point of the split two sections up. And the prompt says so out loud:

> *Whether the caller supplied the data a service needs is not your problem. Route on what
> is being asked; the gateway tells them what to attach.*

Then three entries end with *"so the log must be supplied"*, *"so the scan results must be
supplied"*, and — the agent's — *"a description of an alert is not an alert."* I told the
model not to do the needs-check and wrote the needs-check into the descriptions anyway. The
measured cost of that contradiction is in *The two misses left* below: the one case that has
missed every single run is a bare *"can something scale it back up?"*, which gets **declined**
rather than routed to the agent — and routing it there would have passed, because
`needs_input` is exactly what should have handled the missing alert.

Two details that are less obvious than they look:

**`token()` splits on comma and takes the first.** `ST_API_TOKENS` is plural because triage
issues one per onboarded repo; `KC_API_TOKEN` and `SHA_API_TOKEN` are singular. A singular
value contains no comma, so splitting is a no-op on it — one code path covers both shapes
and there is no per-backend flag saying which to expect.

**Triage's `body` forwards the attachment unchanged.** `scan.sh` already emits exactly the
envelope `POST /triage` takes, `repo` and all. Re-assembling one here would only have been a
chance to assemble it differently.

**`url()` is read per call, not frozen at import.** The sibling services read their config
once at import and report it on `/health`, which is right for a policy value somebody might
have got wrong. A backend address is not policy — it is the difference between a test
pointing at a stub and a container pointing at compose DNS, and an import-time read makes
the first of those need `object.__setattr__` on a frozen dataclass.

## `router.py` — the schema is the guard, not the validator

```python
NONE = "none"
ServiceName = Literal[backends.NAMES + (NONE,)]

class Route(BaseModel):
    reason: str = Field(max_length=200, ...)
    service: ServiceName
    confidence: Literal["low", "medium", "high"]
```

Four decisions in nine lines. Three are things I got wrong somewhere earlier in the
month.

**`none` is an enum member, not a nullable field.** `Literal[...] | None` compiles to a
JSON-schema `anyOf`, and `anyOf` is the part of grammar-constrained decoding least likely to
survive a backend change. Built from `backends.NAMES`, a flat five-value enum comes out as
one `"enum": [...]` that Ollama's `format` and Gemini's `response_schema` handle identically:

```json
{"enum": ["knowledge-copilot", "log-analyzer", "self-healing-agent",
          "security-triage", "none"], "type": "string"}
```

An invented service name is therefore not validated away after the fact — it is
unrepresentable. And because the enum is generated from the table, a fifth backend cannot be
added without the model being allowed to name it, or named by the model unless it is in the
table.

**Field order is behaviour.** Day 27 found this the expensive way in triage: moving
`priority` below `exploitability` and `impact` inverted judgments and nothing failed.
Generation runs left to right, so `reason` first means the service name is produced *after*
the sentence explaining it — the explanation is a premise. Reverse the two and the model
picks first and writes whatever justifies the pick, which reads identically and is worth
nothing.

**`max_length=200` on `reason` is enforced, not requested.** Day 26 asked the prompt for
"one short sentence" and shipped 270-character paragraphs for a month.

**`confidence` is three levels because a float's range is not a guard.** This shipped as
`float = Field(ge=0.0, le=1.0)` and I described it, in this file, as the same kind of
constraint as `service`'s enum. It is not, and two runs proved it: **grammar-constrained
decoding enforces structure, not value ranges.** `service` never once left its enum in 72
calls, because the sampler physically cannot emit a token outside it. `ge`/`le` on a number
are Pydantic rejecting a value *after* generation — so `"confidence": 10` from the 1.5b (four
times) and `"confidence": 2` from the 7b that actually ships were each a 502, on the one field
that had changed no outcome anyway. Two constraints in the same `Field(...)` call and only one
of them load-bearing.

`Literal["low", "medium", "high"]` makes it unrepresentable instead of rejected, the same way
`service` already was. It also loses nothing: the float it replaces only ever came back `1.00`
or `0.00`, so three levels are strictly more than the boolean it was really reporting.

### The floor, and why the prompt doesn't name it

`GW_ROUTE_ON=medium` — the lowest level the gateway acts on. Under it the route is declined
*with the service it would have picked still named*, so a caller who disagrees can retry
explicitly. `medium` and not `high` because the float it replaces was 0.6 on a 0–1 scale,
which accepted the middle of the range; tightening the policy while translating it would have
hidden a behaviour change inside a refactor.

Levels compare by position in `LEVELS`, not as strings — `"low" < "medium"` is true
alphabetically and `"medium" < "high"` is not, so the obvious comparison is wrong in a way
that passes half its cases. A `GW_ROUTE_ON` outside the three fails at import rather than
silently leaving the gateway with no floor at all.

It fired zero times while `confidence` was a float, because the model only ever answered
`1.00` or `0.00`. It plausibly fires now that the field is three levels and the model uses all
three — see *The field's shape changed the model's behaviour* below, which is both the negative
result this paragraph earned and the more interesting thing that replaced it.

The prompt defines the three levels, because the model needs the vocabulary or it is guessing
at an enum. It does not say which one the gateway acts on. Name a threshold to a model and
you get a model that reports one notch above
it, and then the floor is measuring its own instruction. There is a test asserting the bar
does not appear in the prompt, because that is exactly the kind of helpful edit a later me
would make.

### Attachments as routing evidence

The router sees the first 200 characters of any attachment. A JSON alert is nearly proof
it's the agent's; a scan envelope is nearly proof it's triage's. That signal is entirely in
the first couple of hundred bytes, and sending a 16 MiB scan envelope to a classifier would
be paying LLM prices to re-read what a dict lookup already knows.

## `app.py` — five outcomes, and which of them are 200s

| outcome | HTTP | when |
|---|---|---|
| `answered` | 200 | backend returned 200; its body is `answer` |
| `accepted` | 200 | backend returned 202 — triage only — plus a `poll` path |
| `needs_input` | 200 | service known, `backend.needs` unsatisfied |
| `unroutable` | 200 | `service: "none"`, or confidence under the floor |
| `failed` | the backend's own | backend unreachable or errored |

The two refusals are 200s, and that is a deliberate call rather than laziness. A declared
refusal being a field inside a successful response is already how two services here behave:
triage returns `needs_human` as a priority inside a 200, and the copilot returns
`grounded: false` the same way. `outcome` is the contract. A backend *failure* is a
different thing and does surface as the backend's real status code — a 422 means the
attachment was wrong and a 429 means try later, and flattening both to 502 throws away the
only instruction the caller could act on.

`service`, `confidence` and `reason` ride on **every** outcome, successes included. A router
you cannot second-guess after the fact is one whose mistakes are invisible, and its
confident answers are only worth something if a wrong route is legible next to them.

```jsonc
// POST /ask {"question": "why did checkout start 500ing at 3am"}
{
  "outcome": "needs_input",
  "service": "log-analyzer",
  "confidence": "high",
  "reason": "asks why a service returned errors, which is a log question",
  "detail": "log-analyzer can answer this, but attach the log text itself as `attachment`",
  "needs": "raw_log",
  "answer": null
}
```

Send the same question again with the log attached and it comes back `answered`, with
`attributed_to: "log-analyzer POST /analyze-log"`.

### The edge controls

`GW_API_TOKENS` is plural for the same reason triage's is: one token per caller, so
revoking a leaked one is a list edit rather than a rotation everybody has to be told about.

The 16 MiB body cap is middleware on `Content-Length`, not a `Depends` guard — FastAPI
parses the body before it solves dependencies, so a dependency-level cap fires after the
megabytes it exists to refuse are already dicts. It matches triage's cap exactly, because
the same scan envelope arrives here as an `attachment` and is then forwarded there; a
smaller number at the edge would make the gateway refuse bodies the service behind it
accepts.

The rate limit is 60 asks/hour per token, against triage's 5. A human is typing these. It
exists at all because `/ask` spends a model call *before* any backend's own limit gets a
say, so without it one valid token can burn the router freely.

### Everything is `def`, not `async def`

log-analyzer's own comment is the reason, written on Day 3 and still correct: the provider
clients are blocking, so a blocking call inside `async def` stalls FastAPI's event loop and
freezes health checks and metrics along with it, while a plain `def` gets offloaded to
starlette's threadpool. That decision is why this service uses `requests` throughout and
`ThreadPoolExecutor` for the health fan-out rather than pulling in a second HTTP client.
`httpx` is in `requirements.txt` for `TestClient` only, same as in three of the four
siblings.

## The passthrough

```
GET|POST|PUT|PATCH|DELETE  /s/{service}/{path}
```

One edge token in, each backend's own token out. No model, no routing, no rewriting — this
is what a caller uses when it already knows which service it wants, which is every CI job.

```bash
curl -sS -H "Authorization: Bearer $GW_TOKEN" \
  https://gw.example.dev/s/security-triage/triage/9fd2c1a4b0e7 | jq .risk
```

Under `/s/` rather than at the root so it can never shadow `/ask`, `/health` or `/metrics`.
A bare `/{service}/{path}` would work today and depend on route declaration order to keep
working, which is a thing to discover during an incident.

**It shipped with a bug I want written down**, because it is a good one. The handler
originally took `body: Any = None`. FastAPI reads an un-annotated `Any` from the **query
string**, not the body — so every POST through the proxy forwarded an empty body to a
backend that then answered 422 about a field the caller had definitely sent. `Body(default=
None)` is the fix. What makes it worth a paragraph is that my first test used a GET, which
passes either way; the bug survived being written *and* tested and only died when I added a
POST case. A proxy test that never sends a body is not a proxy test.

## Aggregated `/health`

Unauthenticated, so the container's own `HEALTHCHECK` can run it. It reports every
backend's own health rather than this process's liveness — a gateway that says `healthy`
while three of the four services behind it are down is reporting on the wrong thing.

The shape, with one of each state in it:

```jsonc
{
  "status": "degraded",
  "provider": "ollama",
  "model": "qwen2.5:7b-instruct",
  "auth": "2 token(s)",
  "policy": { "route_on": "medium", "max_asks_per_hour": 60, ... },
  "backends": [
    {"service": "log-analyzer",       "status": "healthy",     "http": 200,  "latency_ms": 6,  "issues": []},
    {"service": "knowledge-copilot",  "status": "degraded",    "http": 200,  "latency_ms": 41,
     "issues": ["collection 'runbooks' is empty; run ingest.py"]},
    {"service": "self-healing-agent", "status": "unreachable", "http": null, "latency_ms": 5001,
     "issues": ["HTTPConnectionPool(host='self-healing-agent', port=7200)..."]},
    {"service": "security-triage",    "status": "healthy",     "http": 200,  "latency_ms": 9,  "issues": []}
  ],
  "issues": ["knowledge-copilot is degraded", "self-healing-agent is unreachable"]
}
```

Four things here I would get wrong if I wrote it again quickly.

**It pings the router's own model backend too, not just the four services.** Constructing a
provider does no I/O, so without an `/api/tags` call this endpoint reports a healthy router
while Ollama is unreachable — and the caller finds out as a 503 from `/ask` instead. The
copilot's `/health` has carried this check since Day 12 and log-analyzer's does it too; I
left it out of the first version of this file and only noticed when I went to write the
run-it instructions. It matters more here than in either sibling, because the laptop hosting
Ollama is *expected* to be asleep — "unreachable model" is a normal state, so it has to be
legible rather than a surprise. The same call catches a `GW_OLLAMA_MODEL` that is set to
something plausible and not pulled, which otherwise 502s every `/ask` while the pod looks
perfectly healthy.

**It believes each backend's own word rather than its status code.** All four of these
answer 200 while calling themselves `degraded` — that is the case that matters, and a
status-code check is exactly the check that misses it.

**The fan-out is concurrent.** Sequentially this would be four `GW_HEALTH_TIMEOUT`s, which
is long enough to fail the container healthcheck it exists to serve. The pool is
module-level, not per-request, because `with ThreadPoolExecutor(...)` spawns and joins four
OS threads on every call and Prometheus scrapes forever.

**It reports `degraded`, never `unhealthy`.** `/ask` still routes correctly with every
backend down — it routes and then declines, which is a useful answer. A container that
reports itself dead gets restarted, and restarting this one fixes nothing when the fault is
somebody else's.

## `eval_router.py` — grading a router that is allowed to decline

`python eval_router.py`. Twenty-four labelled questions in `eval_set.json`, and the set is
built around one constraint: **two degenerate routers exist and both have to score badly.**

A router that names a service for everything is easy to build by accident. So is one that
declines everything — Day 23 shipped its equivalent. So a case with a named `expect` is one
this repo says is definitely that service's, and declining it is a miss; a case with
`expect: null` is one nothing should be confident about, and naming a service is a miss. The
score means something only because both mistakes cost the same.

Nineteen cases name a service, five expect a decline. The two numbers printed under the
table are the ones worth reading, because they are the two ways to be wrong and each
degenerate router maxes out exactly one of them:

```
n/24 routed as expected  declined a definite question: n/19  answered a vague one: n/5
```

Both have to stay low. Optimising either alone is trivial and produces a router nobody
would ship. The shipped model scores **22/24** — see *Picking the model* below.

A few cases accept a list, which is the same concession `eval_triage.py` makes with bands:
local Ollama is not reproducible even at `temperature: 0`, and a question that genuinely
fits two services should not flap the score. A list containing `null` means declining is
also defensible.

The rows I care most about are four questions arranged in two pairs, because they are where
keyword matching and intent come apart:

| question | expects |
|---|---|
| "The logs say the deployment is unhealthy. What does that log line actually mean?" | log-analyzer |
| "The logs say the deployment is unhealthy. What should I do about the deployment?" | self-healing-agent |
| "What does our runbook say we should do about OOMKilled pods?" | knowledge-copilot |
| "This pod was OOMKilled — what is the kubelet message telling me happened?" | log-analyzer |

Same subjects, different asks. A router that passes the rest of the set and fails these is
matching words rather than intent, and the prompt has a rule against exactly that.

Grading happens on the route the gateway would **act on** — after the confidence floor, not
before. Scoring the model's raw pick would be scoring a decision the gateway then overrides,
which is not the thing anybody uses.

### Picking the model — measured 2026-08-26

`GW_OLLAMA_MODEL=qwen2.5:7b-instruct`, and now for a reason rather than as a fail-safe.

| run | model | score | declined a definite | answered a vague | s/route | tokens/route |
|---|---|---|---|---|---|---|
| 1 | `qwen2.5:7b-instruct` | 18/24 | 2/19 | 1/5 | 10.2 | 543 |
| 1 | `qwen2.5-coder:1.5b` | 13/24 | 2/17 | 2/3 | 6.3 | 561 |
| 2 | `qwen2.5:7b-instruct` | 20/24 | 1/18 | 1/5 | 9.8 | 566 |
| 3 | `qwen2.5:7b-instruct` | **22/24** | 2/19 | **0/5** | 10.6 | 599 |

One change per run, so each step is attributable. Run 2: the copilot's catalogue entry
narrowed. Run 3: `confidence` became a three-level enum. Nothing else moved either time.

The score movements are each +2 and I have already put the noise band at ±1–2, so treat
18 → 20 → 22 as suggestive. What is *not* noise is distributional, and there is a lot of it
below.

1.5b is 1.6x faster and costs the same in tokens, and it is not close on the thing being
bought. Note its denominators: 17 and 3, not 19 and 5, because **four of its 24 calls never
produced a valid route at all** — the score is 13/20 on the ones it answered, dressed up as
13/24. Day 23 said 1.5b cannot write triage explanations. This says it cannot reliably emit
three fields either.

**And the way it failed is the more useful half.** All four bad calls looked like this:

```json
{ "reason": "The question asks for an analysis of an Alertmanager alert...",
  "service": "self-healing-agent", "confidence": 10 }
```

`confidence: 10` against a schema declaring `le=1.0`. So the claim at the top of this file —
*the schema is the guard* — is true of exactly the part I built it for and not of the part I
assumed came free. **Grammar-constrained decoding enforces structure, not value ranges.**
`service` never once left its enum in 48 calls across both models, because an enum is
structural and the sampler cannot emit a token outside it. `ge`/`le` on a float are not
structural: the grammar permits any number, and Pydantic rejects the value afterwards, which
is a 502 rather than a guard. Two constraints written in the same `Field(...)` call, and only
one of them is load-bearing.

My first instinct was to leave it as a 502 — a model that cannot keep a number inside `[0,1]`
when asked is not one whose routing I would act on either, and rewriting `10` to `1.0` guesses
at intent. **That was the wrong call, and the next run is what showed it.** The 7b did it too,
once in 24. So this was never "a bad candidate model disqualifying itself"; it was **~4% of
production `/ask` calls returning 502 for a reason unrelated to routing quality**, on the one
field that changed no outcome anyway.

`confidence` is therefore a `Literal["low", "medium", "high"]` — structural, so the sampler
cannot produce an illegal value, exactly like `service`. The float never carried more than a
boolean here in any case. The floor became `GW_ROUTE_ON=medium` rather than a number, and the
error text now names the field and the offending value regardless, because `"1 field(s) bad"`
sent me to the raw-body log line to learn something the exception already knew.

Runs 1 and 2 in the table above were taken with the float, so the 20/24 includes one case lost
to `confidence: 2` that had passed twice before — 20/23 on routes that got as far as being
graded. Run 3 is the enum, and it did considerably more than stop the 502; see *The field's
shape changed the model's behaviour* below, which is the part of this I did not expect.

```bash
python eval_router.py                              # the shipped default
python eval_router.py --model qwen2.5-coder:1.5b   # the cheap candidate
python eval_router.py --provider gemini
python eval_router.py --limit 5                    # smoke test before the full run
```

There is one throwaway call before the clock starts, because Ollama loads the model on first
use and that load otherwise lands on whichever case is first: 17.3s against a ~9s median on
7b, 27.6s against ~5s on 1.5b, and on a five-case run it blew through `GW_LLM_TIMEOUT`
outright and reported case one as an error that had nothing to do with routing. `--no-warmup`
puts it back if you want to see that.

### What the 7b got wrong, and why three of those were my fault

Run 1 had six misses, not evenly distributed. **Three went to knowledge-copilot**, which was
picked 8 times out of 24 — the catalogue's attractor. The model's stated reason for
the worst of them, a bare *"something is wrong with checkout"* that should have been declined,
was two words:

> `operational question`

Which is a quotation. The description I had written for that service opened *"Answers
operational questions from the team's own runbooks"* — the broadest phrase in the whole
catalogue, sitting in the entry for the one backend that needs no attachment, and ending with
*"Needs nothing but the question."* I wrote a default and then recorded surprise that the
model chose it. It also said *"what an alert means"*, which collides head-on with both
log-analyzer and the agent.

That description is now narrow, says it *"knows nothing whatever about the running system"*,
and ends by pushing back instead of inviting: *"If the answer is not already written in a
runbook, this is the wrong service."* **One variable changed** — not the catalogue ordering,
which is the other live hypothesis for the same bias. Changing both would leave neither
attributable, which is the same discipline the triage backlog is ordered by.

**It worked, and cleanly.** Run 2, nothing else touched:

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| knowledge-copilot picks | 8 | 5 | 5 |
| of those, wrong | **3** | **0** | **0** |
| total score | 18/24 | 20/24 | 22/24 |

It has stayed fixed through a further change, which is the part that makes it a fix rather
than a run.

Both OOMKilled questions now go to log-analyzer. *"Is this Dockerfile safe to ship"* — which I
had not attributed to this bias at all — went from declined to security-triage. Net +2 with one
case lost to an unrelated `confidence: 2`, so 20/23 on routes that were graded at all.

The honest caveat: the two vague misses **swapped identity** between runs 1 and 2. *"Something
is wrong with checkout"* went miss → ok and *"is this actually safe or not"* went ok → miss,
same count, different rows. So ±1–2 is noise here and each +2 sits at the edge of what one run
can claim. The 3 → 0 on knowledge-copilot is the part that is not ambiguous, and it held
through run 3.

### The two misses left, and why only one of them is a router problem

By run 3 both remaining misses are **declines at `low`**, not confident misroutes — which is
the failure mode you want if you must have one, and `answered a vague one: 0/5` is the other
side of the same coin.

*"The sandbox deployment has fewer replicas than it should. Can something scale it back up?"*
has missed **every run**, and I do not think the router is wrong so much as talked out of it.
The agent's catalogue entry says it *"diagnoses a firing Prometheus or Alertmanager alert"* and
ends *"a description of an alert is not an alert."* There is no alert here, so the model backs
off — reporting `low` in run 3, which is the first run where I could see it hesitate rather
than just decline. But routing to the agent is the right answer: `needs_input` exists precisely
to say "that one, and attach the alert." The description is doing the needs-check that the
prompt explicitly tells it not to do, which is the contradiction written up under
`backends.py`. That is the fix, and it is a description edit rather than anything about the
router.

*"Is this Dockerfile safe to ship"* has now gone miss → ok → miss. It is the plan's own example
and security-triage is the right service, but a Dockerfile-safety question with nothing
attached is genuinely near the line, and a case that flips on every run is measuring variance
rather than the router. Left in the set as-is: it is a real question somebody would type, and
`declined a definite question: 2/19` is honest about the cost.

### `reason` is naming the destination, not reading the question

**The thing those misses share is worth more than the fix.** In run 1, five of six were
explained in three words or fewer: `OOMKilled`, `scaling question`, `log analysis required`,
`operational question`. Run 2 put a number on it — `median reason: 6 words when right, 3 when
wrong` — which supports the hypothesis but weakly, because there are two-word *passes* too
(`log content`, `time reference`, `vague`).

The decisive evidence is not the median. It is this pair:

```
ok   discrimination pair A: what does the log mean:   log analysis required
miss discrimination pair B: what do I do about it:    log analysis required
```

**Byte-identical reasons, opposite correctness.** The prompt asks for *"what in the question
decided it"* and the model is emitting a name for the service it picked — `scaling question`,
`security scanner findings`, `log content`. Those describe the destination, not the question.
Which means `reason`-before-`service` is buying nothing here: the model is not reasoning and
then choosing, it is choosing and then labelling. The two questions in that pair differ only
in what they ask for, and the field that exists to capture that difference produced the same
string for both.

Note which way this does *not* point: the 1.5b wrote long, careful, fully-formed sentences —
*"This question fits both `log-analyzer` and `self-healing-agent`..."* — and scored worse.
Length is not the thing.

**And then it fixed itself, from a change aimed at something else entirely.** Run 3 changed
`confidence` to an enum and touched nothing about `reason`, and the reasons came back
describing the question:

| | run 2 | run 3 |
|---|---|---|
| pair A | `log analysis required` | `asked for meaning of a specific log entry` |
| pair B | `log analysis required` → **miss** | `the question asks for remediation` → **ok** |
| cluster change | `scaling question` | `asked about scaling` |
| log line | `log content` | `log message` |
| median words, right / wrong | 6 / 3 | 6 / 6 |

The identical-reason pair separated, pair B started passing, and the short degenerate labels
mostly went. My best guess is that being asked to grade its own fit — *plainly belongs /
probably does / guessing* — is what forced an actual read of the question, and that the
`reason` field was downstream of that all along. It is a guess: I did not set out to change
this, and one run is not a finding.

So the planned experiment is still worth running, but it is now a *confirmation* rather than a
fix: a prompt that demands `reason` quote the words in the question that decided it. If the
reasons are already question-shaped without it, that tells me something too.

### The field's shape changed the model's behaviour, not just its legal values

This started as a bug report and turned into the most interesting thing on the page.

With `confidence: float = Field(ge=0.0, le=1.0)`, the 7b emitted `1.00` or `0.00` across 48
graded routes and **nothing in between** — every `0.00` attached to `service: "none"`, which
`decision()` declines on the enum branch before the floor is ever consulted. So the floor
changed no outcome at all, zero times. Confidence was a boolean wearing a float's clothing,
which is the `gw_router_confidence` series' own comment coming true faster than expected: *a
router that is always 0.95 is a router that never doubts.*

With `Literal["low","medium","high"]`, the same model on the same 24 questions used **all
three**: 15 `high`, 2 `medium`, 7 `low`.

That is not a bug fix. Nothing prevented the float from returning `0.6`. Asking for a number
got a number treated as a switch; asking for one of three named judgements got a judgement.
The distribution is the evidence — one score moving is noise, but 7 lows and 2 mediums where
there had been none across twice as many routes is not.

**Two honest limits on that claim.** It was one design decision but two edits — the schema
field *and* the prompt sentence that describes it, which went from "how sure you are" to
"`high` if the question plainly belongs there, `medium` if it probably does, `low` if you are
guessing". Which half did the work is unknown, and separating them is a run I have not done.
And it is one run: the direction is clear, the size is not.

What follows for the floor is that it plausibly fires now, and the report could not tell me.
Both remaining misses show `low` and a decline — but a decline is either the model answering
`none` or the floor overriding a service it named, and those are different facts. `GW_ROUTE_ON`
owns the second and has nothing to do with the first. The table now prints `(none)` versus
`(floor)` and the summary counts each, because whether the bar is earning its place or costing
two cases is a question one column answers and no amount of reasoning does.

## `metrics.py` and `/metrics`

Seven series, and the label sets all come from closed sets this service can see — four
service names, five outcomes — so unlike triage's `repo` label there is nothing a request
body can invent a new time series with.

| metric | what it answers |
|---|---|
| `gw_routes{service,outcome}` | the whole day in one metric: what got routed where, and how it ended |
| `gw_router_confidence{level}` | a router pinned at `high` is a router that never doubts |
| `gw_router_duration_seconds` | the classification call alone |
| `gw_backend_duration_seconds{service}` | the forward, kept separate — "/ask is slow" has two causes |
| `gw_proxy_requests{service,status}` | the passthrough, which never involves a model |
| `gw_refusals{reason}` | auth / rate_limit / body_size / unknown_service / bad_attachment |
| `gw_model_tokens{direction}` | what routing costs |

`unroutable` is counted under `service="none"`, deliberately kept apart from a backend
outage. Both are "no answer" and they need completely different fixes.

## Tests

86, offline, no model and no backends:

```bash
cd services/gateway && python -m pytest tests/ -q
```

```
test_backends.py       the table, the body builders, plural-vs-singular tokens
test_router.py         the schema enum, field order, the floor, prompt invariants
test_app.py            the five outcomes end to end, the edge controls, the proxy
test_metrics.py        which label each outcome lands under
test_eval_router.py    the grading, and that both degenerate routers fail the set
```

`tests/conftest.py` assigns its environment rather than using `setdefault`, and that block
is load-bearing. Five services share one root `.env`, `load_dotenv()` runs at import in
three modules here, and `load_dotenv`'s `override=False` means an explicit assignment wins.
Without it the suite reads real tokens and real backend addresses on my laptop and none of
either on a runner — and both versions pass, sometimes. Day 28 lost a whole workflow to
exactly this with `KC_API_TOKEN`.

Two tests exist purely to protect a decision from a future well-meaning edit: one asserts
the confidence floor's value does not appear in the system prompt, and one asserts
`Route`'s field order. Both would look like harmless cleanups.

## Running it

```bash
cd services/gateway
pip install -r requirements.txt
python app.py                                  # :7500
```

Locally the four backends are on localhost rather than compose DNS, so:

```bash
export GW_LOG_ANALYZER_URL=http://localhost:7000
export GW_KNOWLEDGE_COPILOT_URL=http://localhost:7100
export GW_SELF_HEALING_AGENT_URL=http://localhost:7200
export GW_SECURITY_TRIAGE_URL=http://localhost:7300
```

```bash
# what it thinks it can reach
curl -sS localhost:7500/health | jq '.status, .backends[] | {service, status}'

# a question one backend answers from a sentence
curl -sS -X POST localhost:7500/ask \
  -H "Authorization: Bearer $GW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"question": "what is our documented procedure for draining a node"}' | jq

# a question that needs material, without the material
curl -sS -X POST localhost:7500/ask \
  -H "Authorization: Bearer $GW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"question": "why did checkout start 500ing at 3am"}' | jq '.outcome, .needs, .detail'

# and with it
curl -sS -X POST localhost:7500/ask \
  -H "Authorization: Bearer $GW_TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -n --rawfile log /tmp/checkout.log \
        '{question: "why did checkout start 500ing at 3am", attachment: $log}')" | jq
```

## Deploying it

Fifth entry in `docker-compose.prod.yml`, loopback bind on 7500, GHCR image published by
`.github/workflows/gateway_ci.yml` on push to `main`. No `depends_on`, deliberately: the
gateway is correct with every backend down, and making it wait would delay the one endpoint
that can report which of the four is missing.

### 1. `.env` on the host

Two keys are required and the rest have working defaults:

```bash
GW_API_TOKENS=<one per caller, comma-separated>
GW_OLLAMA_BASE_URL=http://<laptop tailnet ip>:11434
```

`GW_API_TOKENS` unset means auth is off, and `/health` says `degraded` / `auth: disabled` —
the loud version of a deploy that left the front door to all four services open.

**`GW_OLLAMA_BASE_URL` is the one that will catch you.** The provider falls back to the
shared `OLLAMA_BASE_URL`, and on the host that points wherever the *other* services' models
live — which is exactly why `ST_OLLAMA_BASE_URL` had to exist on Day 28. Only triage's
backend moved to the laptop; the router's model lives there too, so it needs the same
override. Without it `/ask` answers 503 and `/health` names the unreachable address, which is
at least a legible failure rather than a silent one.

No `GW_*_URL` entries are needed. The defaults are the compose service names, which is what
resolves inside the network.

### 2. Pull and start

```bash
docker compose -f docker-compose.prod.yml pull gateway
docker compose -f docker-compose.prod.yml up -d gateway

# every backend's own status, not just this process's liveness
curl -sS localhost:7500/health | jq '.status, .auth, (.backends[] | {service, status})'
```

`qwen2.5:7b-instruct` has to be pulled on whichever Ollama `GW_OLLAMA_BASE_URL` points at.
`/health` checks that too and names the model if it is missing.

### 3. nginx

Deny by default, like the sibling blocks: three locations proxied and everything else 404.
`/metrics` is deliberately **not** among them — Prometheus scrapes it over the compose
network, and publishing routing volumes and token spend to the internet is free operational
intelligence for anyone who asks.

```bash
dig +short gw.crypticani.dev          # before touching nginx; certbot's HTTP-01 needs it
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx --expand -d gw.crypticani.dev
```

```nginx
server {
    server_name gw.crypticani.dev;

    # /ask, /health, and the /s/{service}/{path} passthrough. Prefix matches, because
    # every one of them carries something after the first segment.
    location ~ ^/(ask|health|s/) {
        proxy_pass http://127.0.0.1:7500;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;

        # THE ONE THAT MATTERS, for the second time this month. nginx defaults to 1 MiB;
        # this service's own cap is 16 MiB to match triage's, because the same scan
        # envelope arrives here as an `attachment` and gets forwarded there. Without this
        # line a 2.7 MB envelope gets nginx's HTML 413 and GW_MAX_BODY_BYTES never gets a
        # say. A cap silently overridden by a smaller cap one layer up is worse than no
        # cap: the control you tested is not the control that fired.
        client_max_body_size 16m;

        # A router call is one CPU-bound model call and the copilot behind it can take
        # 300s. nginx's default 60s proxy_read_timeout would 504 a request the gateway
        # was still legitimately serving.
        proxy_read_timeout 320s;
    }

    location / { return 404; }

    # certbot --nginx fills in listen 443 / ssl_certificate / etc.
}
```

**Not yet run.** Everything above is derived from the four blocks that came before it rather
than from a verified deploy, so treat the `proxy_read_timeout` and the regex location as the
two lines most likely to want adjusting.

**The laptop hosting Ollama does not need to be awake.** Accepted on Day 28 and it applies
here too: with the model unreachable, `/ask` answers 503 naming the router provider, and
`/health` says `degraded` with the reason. That is the correct and visible outcome for a
learning build, and the passthrough keeps working throughout — it never touches a model.

## Not built

A UI, response caching, and a queue. Those are a fifth project, and I said so before
starting rather than after.

Also deliberately absent: any retry on the router. A routing decision has nothing to retry
*into* — if the model cannot say which service a question belongs to, saying so is the
answer, not something to ask again for. And no fallback service when the router fails: a
502 is correct, because picking a default would be exactly the behaviour `none` exists to
prevent, decided by a bug rather than by the model.
