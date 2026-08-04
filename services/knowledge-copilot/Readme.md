# Service: Knowledge Copilot

A RAG service that answers ops questions — "what's the usual fix for X" — over runbooks,
postmortems, and live infra signals, with citations back to the source document.

This is **Project 2 (Week 2)** of the [30-day AI-Native DevOps challenge](../../docs/ai-devops-30-day-challenge.md) — in progress.

> New to embeddings and vector search? [**docs/knowledge-copilot.md**](../../docs/knowledge-copilot.md)
> explains the concepts from the ground up and walks through what every part of the code is doing
> and why. This README is the *what and how much*; that one is the *why*.

**Status (through Day 10):** the service answers questions over HTTP and cites what it used.
Text becomes vectors, vectors go into Chroma, `ingest.py` reconciles the index to the corpus on
every run (add / update / delete / skip), and `POST /ask-runbook` retrieves the top-k chunks,
builds a context-augmented prompt, and validates every citation the model emits against the
chunks that were actually retrieved. When retrieval comes back weak the endpoint refuses instead
of answering from pretraining. Retrieval *quality* — hybrid search, reranking, a real eval set —
is Day 11; what exists now is a defensible grounding contract, because a RAG service that
answers confidently from nothing is worse than one that says it doesn't know.

## Architecture (as of Day 10)

```text
corpus/*.md ──▶ load_corpus() ──▶ chunk_corpus(512, 64) ──▶ BaseEmbeddingProvider ──┬──▶ OllamaEmbeddingProvider  (nomic-embed-text)
 (front matter    (chunking.py)     (word-boundary windows,     (embeddings.py)      └──▶ GeminiEmbeddingProvider (gemini-embedding-001)
  + markdown)                        stable {slug}:{i} IDs)              │
                                            │                            ▼
                              content_hash per doc          Chroma collection (cosine space)
                                            │                     ▲            │
                     ingest.py: plan_reconcile(desired, existing) ┘            │
                     add / update / delete / skip  (idempotent)                │
                                                                               │
POST /ask-runbook ──▶ retrieve() ──▶ embed_query() ─────────────────────────────▶ top-k + cosine distance
   (app.py)          (retrieval.py)      │                                                   │
                                         │                     score = 1 - distance, drop < 0.65
                                         ▼                                                   │
                     nothing cleared the floor? ──▶ refuse, no LLM call ◀────────────────────┘
                                         │
                     build_context() ──▶ <chunk id="n"> blocks ──▶ BaseLLMProvider ──┬──▶ OllamaProvider  (qwen2.5:7b-instruct)
                                         │                            (llm.py)       └──▶ GeminiProvider  (gemini-3.6-flash)
                                         ▼
                     ground_answer() ──▶ markers vs retrieved set ──▶ AskResponse{answer, sources, grounded, answer_source}
```

**Modules, each with one job:**

1. **`chunking.py`** — turns the corpus into identified, embeddable pieces. Pure functions, no
   network, no Chroma. Unit-tested.
2. **`embeddings.py`** — the text-to-vector boundary. A `BaseEmbeddingProvider` ABC with two
   backends, mirroring the `BaseLLMProvider` Strategy pattern from the log analyzer. Nothing
   downstream knows which model produced a vector.
3. **`ingest.py`** (Day 9) — the durable ingestion pipeline. Reconciles the Chroma index to the
   corpus idempotently (add / update / delete / skip via a per-doc `content_hash`).
   `plan_reconcile` is a pure, unit-tested function.
4. **`retrieval.py`** (Day 10) — question to ranked chunks: embed the query, rank by cosine
   similarity, drop everything below the floor, return text *and* metadata. No LLM, no FastAPI.
5. **`llm.py`** (Day 10) — the text-generation boundary. A second `BaseLLMProvider` ABC whose
   `generate()` returns a plain `str`, and an `UpstreamError` carrying the HTTP status its
   failure should become.
6. **`app.py`** (Day 10) — FastAPI. Prompt assembly, citation validation, `POST /ask-runbook`,
   `GET /health`. The grounding logic inside it is pure functions over strings.
7. **`day8_embeddings.py`** — the Day 8 experiment. Indexes the corpus at three chunk sizes and
   compares retrieval across them. A dated artifact, not a service entrypoint.

## Design decisions worth defending

### Documents and queries get separate embedding methods

`embed_documents()` and `embed_query()` are two methods on the interface, not one `embed()`.
Both backends want to know which side of a search a piece of text is on — nomic uses a
`search_document:` / `search_query:` prefix, Gemini uses `task_type=RETRIEVAL_DOCUMENT` vs
`RETRIEVAL_QUERY` — because a question and the passage that answers it are not the same shape
of text. Embedding both identically is a real and common retrieval bug that never raises an
error; it just quietly costs accuracy. Two methods make it impossible to do by accident.

### Cosine space, set explicitly

Chroma defaults to squared L2. The collections here are created with
`configuration={"hnsw": {"space": "cosine"}}` because we care about *direction* — what a chunk
is about — not magnitude, which under L2 would let a longer chunk score differently for
reasons that have nothing to do with meaning. Every vector is L2-normalized on the way out of
the provider, which is also what lets the intuition check compute cosine similarity as a plain
dot product. Chroma returns cosine **distance**, so the reported score is `1 - distance`.

### We supply our own vectors

Collections are created with `embedding_function=None`. Left at its default, Chroma would
download and run its own ONNX MiniLM model — a second, invisible embedding model silently
competing with the one we configured. An index built by a different model than the one
embedding the queries produces plausible-looking garbage, which is the worst failure mode
available here.

### Chunk IDs are `{slug}:{index}`, not UUIDs

Deterministic IDs mean re-running the indexer upserts over the same rows instead of
duplicating them. That is the hook Day 9's idempotent re-index hangs on. Caveat: the same ID
means *different text* under a different `(size, overlap)`, which is safe today only because
each config gets its own collection.

### The chunker is hand-written

No LangChain splitter. Windows snap backwards to the nearest space so words are never cut in
half — a fragment like `OOMKil` is noise the model still folds into the vector. Windows
overlap so a fact straddling a boundary lands intact in at least one chunk. Both behaviours
are tested, because a chunker that silently stops overlapping doesn't crash; it just degrades
retrieval weeks later.

### The provider factory raises instead of falling back

`get_embedding_provider()` raises on an unknown `EMBEDDING_PROVIDER`, where the log analyzer's
equivalent logs an error and defaults to Ollama. With embeddings a silent fallback is worse
than a crash: a corpus indexed by an unintended model looks like it worked. Worth back-porting
to the log analyzer.

## The corpus

Eleven documents in [`corpus/`](./corpus), with front matter
(`title`, `service`, `doc_type`, `last_reviewed`) that becomes chunk metadata:

| Document | `service` | `doc_type` | Failure domain |
|---|---|---|---|
| `oomkilled-pod.md` | `platform` | `runbook` | Memory limit exceeded, exit code 137 |
| `crashloopbackoff.md` | `platform` | `runbook` | Container exits on start; reading `--previous` logs |
| `imagepullbackoff.md` | `platform` | `runbook` | Registry auth, wrong tag, rate limits |
| `node-disk-pressure.md` | `platform` | `runbook` | Kubelet eviction, image cache, inode exhaustion |
| `tls-cert-expiry.md` | `edge` | `runbook` | Expired ingress cert, cert-manager renewal failure |
| `postgres-conn-pool-exhaustion.md` | `data` | `runbook` | `too many clients already`, idle-in-transaction |
| `jenkins-agent-offline.md` | `ci` | `runbook` | Agent disconnect, workspace disk full, label mismatch |
| `coredns-resolution-failure.md` | `platform` | `runbook` | In-cluster DNS, `ndots:5`, CoreDNS OOM |
| `postmortem-2026-06-checkout-oom-outage.md` | `platform` | `postmortem` | Checkout OOM loop after an unbounded cache release |
| `postmortem-2026-07-ingress-tls-expiry.md` | `edge` | `postmortem` | Silent cert-manager renewal failure, then expiry |
| `reference-pod-resource-limits.md` | `platform` | `reference` | Requests vs limits, QoS classes, memory sizing |

The first eight were all `doc_type: runbook` deliberately — for the Day 8 experiment the only
variable should be chunk size. **Day 9 added the two postmortems and the reference doc**, which
is when `doc_type` becomes worth filtering on. They also overlap existing runbook topics on
purpose (checkout-OOM ↔ `oomkilled-pod`, TLS-expiry ↔ `tls-cert-expiry`), giving the Day 11
eval the topically-competing documents the original disjoint set lacked.

The retrieval questions in [`queries.json`](./queries.json) were written separately from the
corpus, without rereading it. That matters: if one author phrases both, a query matches its
runbook because of shared vocabulary habits, and the test proves nothing. The point is to
watch a 2am symptom description find a document that never uses those words — which is the
entire argument for embeddings over grep.

## Running it

```bash
pip install -r requirements.txt
python ingest.py                 # build or reconcile the index
uvicorn app:app --port 7100      # serve /ask-runbook and /health

curl -s localhost:7100/ask-runbook -H 'content-type: application/json' \
  -d '{"question":"why do my pods get OOMKilled after a deploy"}'
```

Requires two models on the configured Ollama host — one to embed, one to write prose:

```bash
ollama pull nomic-embed-text     # 768-dim, ~274MB
ollama pull qwen2.5:7b-instruct  # generation, ~4.7GB
```

The Day 8 experiment is still runnable on its own: `python day8_embeddings.py --reset`.

The script prints four sections: what a vector looks like and how cosine similarity separates
related from unrelated text; index construction at each chunk size; a per-query comparison of
top-3 hits across configs; and a hit@1 / hit@3 summary.

Config comes from the repo-root `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` or `gemini` |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model — not the generation model |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Shared with the log analyzer |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Pinned to 768 dims so the two are comparable |
| `CHROMA_PATH` | `services/knowledge-copilot/chroma_data` | Persistent index; gitignored, rebuildable |
| `LLM_PROVIDER` | `ollama` | Generation backend: `ollama` or `gemini` |
| `OLLAMA_CHAT_MODEL` | `qwen2.5:7b-instruct` | **Not** `OLLAMA_MODEL` — see below |
| `SIMILARITY_FLOOR` | `0.65` | Below this score a chunk is not context |
| `LLM_TIMEOUT` | `300` | Seconds. 120 was not enough — see [findings](#day-10-latency-is-the-real-constraint) |
| `COPILOT_PORT` | `7100` | Only used by `python app.py`; the log analyzer owns 7000 |

`OLLAMA_CHAT_MODEL` exists because the repo-root `.env` is shared, and `OLLAMA_MODEL` already
belongs to the log analyzer — which needs a *code* model for schema-constrained JSON output.
This service needs one that writes incident prose. Two services, one `.env`, two generation
models, so two variables. `OLLAMA_EMBED_MODEL` splits the same way for the same reason.

## Day 9 — idempotent ingestion (`ingest.py`)

`day8_embeddings.py` is a dated experiment. `ingest.py` is the durable pipeline: it reconciles
the Chroma index to `corpus/` in one pass — add new chunks, re-embed changed docs, delete
orphaned chunks, skip unchanged ones. **Re-running it on an unchanged corpus embeds nothing.**

Plain `collection.upsert` is not idempotent: it replaces rows that share an ID this run, but
it never removes chunks that should no longer exist. Shrink a doc from 8 chunks to 5 and
`{slug}:5..7` are orphaned; delete a doc and *all* its chunks linger — stale, still
retrievable, and silent. Day 9 is the reconcile step that fixes that.

The hook is a per-document `content_hash` (sha256 of body **+** front matter) stored on every
chunk. Each run reads the stored hashes back and `plan_reconcile` — a pure, unit-tested
function with no Chroma or network — sorts every chunk:

```text
id absent                    -> add
id present, hash differs     -> update (re-embed)
id present, hash matches     -> skip
id in index, not in corpus   -> delete   <- the half upsert can't do
```

Production collection: `knowledge_{provider}_512_64` — renamed from the Day 8 experiment's
`runbooks_*` because the corpus now holds runbooks, postmortems, and reference docs. The name
encodes the provider and config, so an Ollama index can never be queried with Gemini vectors;
switching providers requires `--reset`.

```bash
python ingest.py --dry-run                                    # preview the plan, change nothing
python ingest.py                                              # apply
python ingest.py --reset                                      # full rebuild (to switch provider)
python ingest.py --corpus ../../docs                          # any directory of markdown
```

`--reset` and `--dry-run` are refused together, by `argparse` at the CLI edge and by a
`ValueError` inside `ingest()` for callers that skip the CLI. They are contradictory: `--reset`
drops the collection, and a dry run is defined by changing nothing. The first version put the
drop *above* the dry-run return, so `--reset --dry-run` printed a plan and silently wiped the
index — a flag whose entire contract is "safe to run" was the destructive one. Both guards
exist because Day 10's endpoint will import `ingest()` directly and never touch `argparse`.

`--corpus` points the pipeline at any directory of front-mattered markdown; it defaults to
`./corpus`. That is what makes the script *reusable* rather than merely re-runnable, and it is
also what lets the tests drive the whole pipeline against a `tmp_path` corpus.

Every chunk carries `title, service, doc_type, last_reviewed, source, chunk_index,
content_hash, indexed_at`. Day 9 exposed a `--query` / `--where` pair on the CLI for searching
against those; **Day 10 deleted both** along with `ingest.py`'s thin `search()`, because that
function's `include` list omitted `"documents"` and so could never feed a prompt. Keeping a
second, weaker retrieval implementation alive to serve a demo flag is how two code paths drift
apart. `retrieval.retrieve()` is now the only one, and `POST /ask-runbook` is its only front
door. The tradeoff is real and noted below: there is no longer an offline way to eyeball the
similarity floor, and metadata filtering has to come back as a request parameter when Day 11's
eval needs it.

`ingest()` takes an optional `provider` and `client`, defaulting to the configured ones. That
one seam is what makes the pipeline testable offline: a fake provider that counts vectors and a
throwaway Chroma path prove the guarantees end to end without a network call. Without it, the
only untested surface was `ingest()` itself — which is precisely where the `--reset --dry-run`
bug lived.

## Day 10 — `POST /ask-runbook` (`retrieval.py`, `llm.py`, `app.py`)

The endpoint is the easy half. The half worth defending is what happens when retrieval comes
back weak, and whether a citation in the prose points at a chunk that was actually retrieved.

### Contract

```
POST /ask-runbook
  question: str          min_length 10
  k: int = 4             bounded 1-10

200 ->
  answer: str
  sources: [{marker, source, chunk_index, score}]
  grounded: bool
  answer_source: "runbooks" | "none"
```

```jsonc
// curl -d '{"question":"why do my pods get OOMKilled after a deploy"}'
{
  "answer": "The pods may get OOMKilled after a deploy if the memory limit was not increased
             to accommodate higher per-request memory use. In the `checkout` service incident,
             deploying v4.19 without raising the memory limit led to an OOMKill loop [3].",
  "sources": [{"marker": 3, "source": "postmortem-2026-06-checkout-oom-outage.md",
               "chunk_index": 0, "score": 0.723}],
  "grounded": true,
  "answer_source": "runbooks"
}
```

### `grounded` is three conditions, not one

True **only** when all three hold: at least one chunk cleared the floor, the model emitted at
least one citation marker, and every marker it emitted resolved to a retrieved chunk. Every
other combination is false.

`grounded` and "we produced an answer" are separate facts and must never collapse into one
boolean — conflating them is the failure this endpoint exists to prevent. `answer_source` is
what keeps them separate: an uncited answer and a refusal both carry `grounded: false` and an
empty `sources`, and only that field distinguishes `"runbooks"` from `"none"`. Without it a
caller — the Day 13 chat front-end, the Day 15 gateway — would have to string-match the refusal
text, which is not a contract.

### The similarity floor: 0.65, and it is a guess

Chunks scoring below `SIMILARITY_FLOOR` are not passed to the model. Nothing clears it → the
endpoint answers `Not covered in the runbooks.` with `answer_source: "none"` and **makes no LLM
call at all**.

The value comes from four observations on this corpus with `nomic-embed-text`: a bullseye scores
0.72–0.76, a clearly unrelated chunk 0.59–0.64. The `search_query:` / `search_document:`
prefixes compress the range hard, so a conventional 0.4–0.5 floor would pass everything (see
[the cosine floor](#three-things-the-table-doesnt-show)). **This pushes a requirement onto Day
11:** the eval set needs out-of-corpus questions — "how do I rotate an IAM key" — so the cutoff
is calibrated against real negatives rather than guessed from positives.

### Citation validation

A regex sweep for `[n]` over the model's output, checked against the ids actually placed in the
context block. Unresolvable markers are **stripped from the answer text**, `grounded` goes false,
and the event is logged at WARNING.

This departs from the log analyzer, which maps malformed model output straight to 502. The
departure is deliberate: a partially-cited answer still helps someone on call at 2am, whereas a
response that fails schema validation has no salvageable content. An invented citation therefore
never produces a 502 — the failure stays visible in `grounded` and in the logs instead of being
swallowed.

The regex is `(?<!\w)\[(\d+)\]`, and both halves of that lookbehind were bugs found by tests.
`\[(\d+)\]` alone reads `argv[1]` and `${nodes[0]}` in a shell snippet as citations, which
ungrounds a perfectly good answer. Excluding a preceding `]` as well — `(?<![\w\]])` — silently
broke the far more common case: models write consecutive citations as `[1][2]`, and the second
marker was then never extracted, so it stayed in the prose, never reached `sources`, and left
`grounded` claiming true about a citation the response could not resolve. Exactly the failure
mode the endpoint exists to prevent, introduced by the fix for a cosmetic one.

### Error handling

| Condition | Status |
|---|---|
| model timeout (`LLM_TIMEOUT` exceeded) | 504 |
| model backend unreachable | 503 |
| model backend returns 4xx/5xx — e.g. model not pulled | 502 |
| empty or unusable response body | 502 |
| embedding backend timeout / failure | 504 / 503 |
| collection empty or missing | 503 `run ingest.py` |
| question shorter than 10 chars, `k` outside 1–10 | 422 (Pydantic) |

Two shapes worth naming. **`UpstreamError(message, status)`** lets `llm.py` and `retrieval.py`
decide what their own failures mean in HTTP terms, so `app.py` has one `except` clause instead of
five and a Gemini SDK error can't escape as a 500. And `requests.exceptions.HTTPError` **must**
be caught before `RequestException`, since it is a subclass: without that ordering a model that
was never pulled — a 404 from Ollama — is reported as "backend unreachable", sending you to check
the network instead of running `ollama pull`.

**The empty-collection case is the important one.** If `EMBEDDING_PROVIDER` changes,
`get_or_create_collection` silently creates a fresh empty collection under the new name. Every
query then returns nothing, nothing clears the floor, and the endpoint answers "Not covered in
the runbooks." That answer is *false* — the runbooks are fine, the index is not built. So
`retrieve()` distinguishes "no rows in the collection" (raise) from "nothing cleared the floor"
(return `[]`). Same failure family as the Day 9 `--reset --dry-run` bug: the wrong answer was
plausible, which is what made it dangerous.

### `/health`

Returns `healthy | degraded` with an `issues` list, plus the configured provider, model,
embedding provider, collection name, chunk count, and floor.

```json
{"status": "healthy", "provider": "ollama", "model": "qwen2.5:7b-instruct",
 "embedding_provider": "ollama", "collection": "knowledge_ollama_512_64",
 "chunks_indexed": 68, "similarity_floor": 0.65, "issues": []}
```

Two checks have no equivalent in the log analyzer's health endpoint, and both cover failures that
would otherwise reach users as a confidently wrong answer or a mid-request 502:

- **the collection is non-empty** — catches the `EMBEDDING_PROVIDER` switch above.
- **the configured chat model is actually pulled** on the Ollama host. Constructing a provider
  does no I/O, so `/health` is the only place a missing model can surface *before* it becomes a
  502 in the middle of someone's question.

### Context assembly: flat top-k

The chunks that clear the floor become numbered blocks, each carrying its `source` and
`chunk_index`, so marker `[2]` resolves to exactly one chunk:

```xml
<context>
<chunk id="1" source="oomkilled-pod.md" chunk_index="0">...</chunk>
<chunk id="2" source="postmortem-2026-06-checkout-oom-outage.md" chunk_index="1">...</chunk>
</context>
<question>...</question>
```

Two alternatives were rejected. **Neighbour expansion** (pulling `{slug}:{i±1}` in, which the
deterministic IDs make a cheap `collection.get` with no second embedding) fixes facts truncated
at a chunk boundary, but a marker would then point at a three-chunk window, weakening the
citation claim — and Day 11's hit@k would measure something the endpoint doesn't serve. Good
Day 11 experiment once an eval exists to prove it helps. **Whole-document context** is viable at
these sizes (1.5–3.3 KB per doc) and would guarantee the model never sees a truncated sentence,
but it conflicts directly with inline markers: `[1]` would mean "somewhere in this 3 KB
document", and Day 8 already showed that picking the right *document* out of eleven is too easy
to discriminate.

### What was deliberately left out

- **`allow_general`** — a flag to answer from model knowledge when the runbooks don't cover a
  question. Designed, then dropped: the honest refusal is the more defensible default, and a 7B
  local model inventing *this* cluster's conventions is exactly where it would be least
  reliable. If it returns, `answer_source` already has a `"model_knowledge"` slot waiting.
- **Metadata filtering** on the request. Chroma supports it and Day 9's chunks carry the
  metadata; it comes back when the eval needs to compare `doc_type=runbook` against
  `doc_type=postmortem`.
- **`/metrics`**, auth, and a shared LLM package across the two services. Day 14, Day 14, and
  Day 30 respectively — the third wants three call sites to design against, not two.

## Testing

```bash
python -m pytest tests/ -q     # 57 tests, ~2s, no network
```

Every test file is offline and deterministic — no embedding call and no model call belongs in a
unit test.

`tests/test_chunking.py` covers chunk size bounds, that overlap actually carries text forward,
that no word is lost, ID stability across runs, and front matter parsing with and without a
header.

`tests/test_ingest.py` works at two levels. The pure layer covers `plan_reconcile` — new → add,
unchanged → no-op, edit → update, shrink/remove → delete — and that `content_hash` flips on a
body *or* front-matter change. The pipeline layer runs `ingest()` itself against a `tmp_path`
corpus and a `FakeProvider` that returns hash-derived vectors and counts everything it embeds,
which is what lets these four be asserted rather than assumed:

| Test | Guarantee |
|---|---|
| `test_dry_run_writes_nothing` | a plan is produced, `embed_calls == 0`, collection stays empty |
| `test_reset_is_refused_during_a_dry_run` | the contradictory combination raises, index survives |
| `test_unchanged_rerun_embeds_nothing` | second pass is all-`unchanged`, not one vector recomputed |
| `test_removed_doc_is_deleted_from_the_collection` | deleting a source file removes its chunks from Chroma |

Still no network: the fake provider is the whole trick. One gotcha found while writing these —
`chromadb.EphemeralClient()` is not per-call isolation. Repeated calls with identical settings
resolve to the same in-process system, so a collection written by one test was visible to the
next and the "re-run embeds nothing" assertion failed for the wrong reason. Each test now gets
a `PersistentClient` on its own `tmp_path`, which cannot leak.

`tests/conftest.py` (Day 10) holds what three files now share: `FakeProvider`, the `tmp_path`
Chroma client, and a throwaway corpus. Day 9 needed them in one file and defined them there.

`tests/test_retrieval.py` uses a `StubCollection` rather than real Chroma, because the assertions
are about *exact* scores — a hash-derived vector cannot produce a distance of 0.24 on demand.
Below-floor chunks are dropped, nothing-above-floor returns `[]` while an empty collection
**raises**, `k` reaches the query, `include` asks for `documents`, and embedding failures come
back as `UpstreamError` with 504/503. One test does run against real Chroma, to prove chunk text
actually comes back — the thing the deleted `search()` could not do.

`tests/test_grounding.py` is pure string work: byte-exact context blocks, marker extraction,
consecutive `[1][2]` markers, shell subscripts that are *not* markers, stripping that leaves
punctuation clean, and the `grounded` truth table (no chunks → false; chunks but no citation →
false; one invented marker → false and stripped; all resolve → true).

`tests/test_llm.py` covers the boundary that talks to a model: timeout → 504, HTTP error → 502,
connection refused → 503, empty body → 502, and that `temperature` is sent inside `options`
where Ollama actually reads it. That last one is a bug in the log analyzer, which passes it at
the top level of the payload and has therefore been running at the default 0.8, not 0.1.

`tests/test_api.py` drives the endpoint through `TestClient` with both upstreams replaced —
a spy LLM and a stubbed `retrieve`. The assertion that matters most is `spy.calls == 0` on the
below-floor path: *skipping* work is a claim that can only be proven by instrumenting the
collaborator, the same trick as `FakeProvider.embed_calls` on Day 9. A green suite would
otherwise say nothing about whether a refusal quietly called the model anyway.

There is no CI workflow for this service yet. The existing
[`log_analyzer_ci.yml`](../../.github/workflows/log_analyzer_ci.yml) is path-scoped to
`services/log-analyzer/**` and will not run these tests. Now that there is an endpoint to
protect, this is the most overdue item on the list.

## Findings

Run of 2026-08-02 — `nomic-embed-text` (768-dim) on self-hosted Ollama, 8 runbooks, 5 queries.

| Config | Chunks | Chunks/doc | hit@1 | hit@3 | precision@3 |
|---|---|---|---|---|---|
| 256/32 | 109 | ~14 | 5/5 | 5/5 | 12/15 |
| 512/64 | 55 | ~7 | 5/5 | 5/5 | 12/15 |
| 1024/128 | 27 | ~3 | 5/5 | 5/5 | 12/15 |

**All three rank metrics saturated — every config scores identically.** With 8 topically
disjoint runbooks, picking the right document is too easy to separate chunk sizes. That is a
finding about the eval, not about chunking: the metric had no resolution left to spend.

The only discriminator left in the data is **margin** — the cosine gap between the best correct
chunk and the best incorrect one. It is computable on 2 of the 5 queries; the other three
returned no wrong document anywhere in the top 3.

| Query | 256/32 | 512/64 | 1024/128 |
|---|---|---|---|
| "exit code 137 / restarts after deploy" | 0.039 | 0.046 | 0.021 |
| "builds queued, no executors" | 0.037 | 0.020 | 0.024 |
| **mean** | **0.038** | 0.033 | **0.023** |

`1024/128` is weakest on both queries — larger chunks dilute, averaging the passage that
answers the query together with paragraphs that do not. `256/32` and `512/64` swap places
between the two and **cannot be separated at n=2**.

### Chunk size chosen: 512/64 — and on what grounds

Not because this data chose it. The data rules out 1024 and cannot distinguish 256 from 512.
The tiebreak is downstream cost: Day 10 feeds retrieved chunks into an LLM prompt, where a
256-char chunk frequently carries a symptom without its resolution, forcing a larger `k` to
compensate. 512 keeps a runbook section largely intact at half the vector count. Revisit on
Day 11 with an eval set that can actually measure the difference.

### Three things the table doesn't show

**Cosine similarity has a floor, and it is not zero.** Two unrelated sentences — an OOMKill and
an expired TLS certificate — scored **0.5918**; the related pair scored **0.7427**. The whole
usable range on this model is roughly 0.59–0.80, so an absolute threshold like "reject below
0.5" would reject nothing, ever. Only *relative* ranking within one query carries information.
This is the argument for reranking on Day 11 rather than a score cutoff.

**A "wrong" hit can be semantically right.** Query 1 returns `crashloopbackoff.md` at rank 2 in
every config, scored as a miss. It is not wrong — an OOMKilled pod presents as
CrashLoopBackOff, and the two runbooks cross-reference each other. Binary single-label
exact-match cannot express that. Day 11's eval set needs graded or multi-label relevance, or it
will penalise correct behaviour.

**The vocabulary-mismatch test was softer than intended.** The queries avoid runbook titles, but
"exit code 137", "executors" and "resolve by name" appear in both the query and its target
document — standard DevOps idiom is shared vocabulary even when corpus and queries are written
independently. What is demonstrated is that retrieval survives paraphrase; bridging genuinely
disjoint wording is unproven.

### What the chunker bug cost

`test_every_word_survives_chunking` failed on the first run. `chunk_text` snapped the window
*end* back to a word boundary but not the *start*, so every chunk after the first opened with a
fragment — `rd64` instead of `word64`. No crash, no error, no visible symptom: just a garbage
token leading 63 of the 107 chunks it produced at 256/32, quietly polluting every vector. (The
fixed chunker produces 109.) Caught only because
a test asserted the boring invariant that no word is lost. That test earned its keep before the
service had a single user.

Five queries is a smoke test, not a benchmark — one query moving swings hit@1 by 20%. Day 11
builds the eval set that can settle this.

### Day 10: latency is the real constraint

Run of 2026-08-04 — `qwen2.5:7b-instruct` on self-hosted Ollama, 68 chunks indexed, `k=4`.

| Request | Result | Wall clock |
|---|---|---|
| `/health` | `healthy`, 68 chunks, model pulled | < 1s |
| out-of-corpus question, below floor | `answer_source: "none"`, no model call | < 1s |
| grounded question, cold model | **504** — `LLM_TIMEOUT` was 120s | 120s |
| grounded question, warm model | `grounded: true`, 1 source, score 0.723 | **195s** |
| trivial prompt, direct to Ollama | control measurement | 6.5s |

**Every grounded answer 504'd at the original 120-second timeout.** The cause is not the model
size but where it runs: `/api/ps` reports `size_vram: 0`, so appsrv is serving from CPU, and
prompt eval over four ~512-character chunks is what costs the time. The 6.5s control on a
two-word prompt against the same warm model isolates it — this is prompt-length cost, so it
scales with `k`, and raising `k` to improve recall makes it worse.

The default is now 300s, which makes the endpoint usable rather than fast. The real options are
a GPU on appsrv, a smaller prose model, or a lower `k`, and picking between them wants Day 11's
eval to say what recall actually costs. **Day 11's eval set therefore needs a latency column**;
measuring retrieval quality while ignoring a 3-minute answer would optimize the wrong thing.

The log analyzer never hit this because its prompts are one short log line. It is the first
place this project has paid for retrieval augmentation rather than just benefited from it.

## Not built yet

| Capability | Day |
|---|---|
| Hybrid keyword+vector search, reranking, a real eval set (with latency) | 11 |
| Connector ingesting Prometheus alerts / K8s events | 12 |
| Slack bot or web chat in front of the service | 13 |
| `/metrics`, auth, architecture diagram, demo recording | 14 |
| A CI workflow that runs these 57 tests | overdue |
