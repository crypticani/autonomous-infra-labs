# Working in this repo

Five FastAPI services that apply LLMs to DevOps problems, built one per week over 30 days.
Each is self-contained: its own `requirements.txt`, `Dockerfile`, tests, eval, and README.
Read the service's README before changing it — they carry the measurements behind the
defaults, and most numbers in the code were set by an eval rather than chosen.

## Layout

```
services/log-analyzer/        raw log  -> typed {severity, cause, fix, confidence}
services/knowledge-copilot/   RAG over runbooks, Slack bot, alert sync
services/self-healing-agent/  tool-calling agent, approval gate, guardrails
services/security-triage/     scanner output -> risk score -> CI verdict
services/gateway/             one address in front of the four, LLM intent router
eval_all.py                   runs all five evals, one table
docs/                         explainers (why it works) + case studies
```

`docs/<service>.md` explains the concepts; `services/<name>/README.md` records what was
built and what it scored. The gateway has no explainer on purpose — it introduces no new
concept, so its reasoning lives in its own README.

## Running things

```bash
cd services/<name> && python -m pytest tests/ -q      # offline, no model, no network
cd services/<name> && black --check --line-length=88 .
python eval_all.py --selftest                          # no backend needed
python eval_all.py                                     # needs models up; minutes, not seconds
```

598 tests across the five services (86 · 197 · 8 · 184 · 123). All suites are offline —
every provider and HTTP call is stubbed. A test that reaches the network is a bug.

Evals are the opposite: they call real models and are the only thing that catches a model
getting quietly worse. A green suite says nothing about answer quality.

## Conventions that matter

**Comments are short and factual.** One or two lines naming the constraint. The narrative,
the measurements and the reasoning go in the service README, and a comment can point at it
(`See README.md`). No dated incident reports in source, no multi-paragraph docstrings. Test
docstrings name what the test protects, not the bug that prompted it.

**`ponytail:` marks a deliberate shortcut** with a known ceiling and an upgrade path, e.g.
`# ponytail: in-memory store. Upgrade path is a JSON file per run.` Keep these; they are the
debt ledger.

**Commit messages are one short line.** No body, no trailers.

**No personal infrastructure in tracked files.** No IPs, no hostnames, no machine-specific
paths. Use placeholders that fail loudly — `.invalid` addresses never resolve, so a deploy
that forgot to set one breaks visibly instead of silently reaching somewhere unexpected.

**Design docs stay untracked.** `docs/superpowers/` is gitignored scratch. Delete a plan once
the thing is built.

## Traps that have cost real time

**One `.env`, five services.** All five read the same root `.env`, which is why service-scoped
prefixes exist (`ST_`, `SHA_`, `GW_`). Two consequences:

- Every test suite must pin the env it depends on in `tests/conftest.py`, assigned rather
  than `setdefault`-ed. `load_dotenv()` runs at import in several modules and `override=False`
  means an explicit assignment wins. Without this a suite reads real tokens locally and none
  on a runner, and both versions pass sometimes. This has broken CI twice.
- An explicit `.env` value beats a code default. A config change is not deployed until the
  running process reports it — that is why `/health` echoes the loaded policy rather than
  describing what the image ships.

**Gemini model names are per-service on purpose.** The free-tier quota is scoped
per-project-per-model, so `SHA_GEMINI_MODEL` / `ST_GEMINI_MODEL` / `GW_GEMINI_MODEL` are
distinct names giving each service its own bucket. Sharing a name drains one bucket twice.

**`provider.py`, `app.py` and `errors.py` exist five times over.** That is deliberate, not
duplication to refactor: the services ship as separate images and would collide in one
process. `eval_all.py` runs each eval as a subprocess for the same reason.

**A schema constrains structure, not values.** Grammar-constrained decoding makes an enum
member impossible to violate and does nothing about `ge`/`le` or `maxLength` — those are
checked after generation, so they produce a 502 rather than a guard. Prefer an enum where the
value set is closed; truncate rather than reject where it isn't.

**FastAPI parses the body before solving dependencies.** A body-size cap has to be
middleware; a `Depends` guard fires after the megabytes it exists to refuse are already
parsed. And an un-annotated `Any` body parameter is read from the *query string* — use
`Body(...)`.

**Endpoints are `def`, not `async def`.** The provider clients are blocking, so a blocking
call inside `async def` stalls the event loop and freezes health checks with it. A plain `def`
gets offloaded to starlette's threadpool.

## Ports

| service | port | env var |
|---|---|---|
| log-analyzer | 7000 | `PORT` |
| knowledge-copilot | 7100 | `COPILOT_PORT` |
| self-healing-agent | 7200 | `SHA_PORT` |
| security-triage | 7300 | `ST_PORT` |
| gateway | 7500 | `GW_PORT` |

Inside compose, services address each other by **service name** (`http://knowledge-copilot:7100`),
never `localhost` — three of them bind loopback on the host. 7400 is free for a sixth backend.

## Deployment

Images publish to GHCR from `.github/workflows/<service>_ci.yml` on push to `main`, gated on
tests and manifest validation. `docker-compose.prod.yml` runs the published images; the dev
compose builds from source. nginx terminates TLS and proxies to loopback binds.

Two things that are not obvious: nginx defaults `client_max_body_size` to 1 MiB, which
silently overrides a larger application-level cap; and Cloudflare caps how long an origin may
take, so a slow synchronous endpoint returns `524` regardless of what nginx and the app allow.

## Before claiming something works

Run the thing. Every service in this repo has had a bug that the test suite could not see —
a silently ignored `temperature` parameter, a proxy forwarding an empty body, a config value
the process never actually loaded. The suite proves the code does what the tests say. The
round trip is what proves it does what you meant.
