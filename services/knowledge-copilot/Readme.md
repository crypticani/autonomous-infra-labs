# Service: Knowledge Copilot

A RAG service that answers ops questions — "what's the usual fix for X" — over runbooks,
postmortems, and live infra signals, with citations back to the source document.

This is **Project 2 (Week 2)** of the [30-day AI-Native DevOps challenge](../../docs/ai-devops-30-day-challenge.md) — in progress.

> New to embeddings and vector search? [**docs/knowledge-copilot.md**](../../docs/knowledge-copilot.md)
> explains the concepts from the ground up and walks through what every part of the code is doing
> and why. This README is the *what and how much*; that one is the *why*.

**Day 8 status:** the retrieval half is standing. Text becomes vectors, vectors go into
Chroma, and a query finds the right runbook. There is no LLM in the loop yet and no HTTP
endpoint — Day 10 adds both. Today is about being able to defend the retrieval layer, because
a RAG service is only ever as good as what it retrieves.

## Architecture (as of Day 8)

```text
corpus/*.md ──▶ load_corpus() ──▶ chunk_text(size, overlap) ──▶ BaseEmbeddingProvider ──┬──▶ OllamaEmbeddingProvider  (nomic-embed-text)
 (front matter    (chunking.py)      (word-boundary windows,      (embeddings.py)        └──▶ GeminiEmbeddingProvider (gemini-embedding-001)
  + markdown)                         stable {slug}:{i} IDs)               │
                                                                           ▼
                                                             Chroma collection (cosine space)
                                                                           │
                           query ──▶ embed_query() ─────────────────────▶ top-k + cosine distance
```

**Three modules, each with one job:**

1. **`chunking.py`** — turns the corpus into identified, embeddable pieces. Pure functions, no
   network, no Chroma. This is why it is the only module with real unit tests today.
2. **`embeddings.py`** — the text-to-vector boundary. A `BaseEmbeddingProvider` ABC with two
   backends, mirroring the `BaseLLMProvider` Strategy pattern from the log analyzer. Nothing
   downstream knows which model produced a vector.
3. **`day8_embeddings.py`** — today's experiment. Indexes the corpus at three chunk sizes and
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

Eight runbooks in [`corpus/`](./corpus), each ~2.8–3.3k characters, with front matter
(`title`, `service`, `doc_type`, `last_reviewed`) that becomes chunk metadata:

| Document | `service` | Failure domain |
|---|---|---|
| `oomkilled-pod.md` | `platform` | Memory limit exceeded, exit code 137 |
| `crashloopbackoff.md` | `platform` | Container exits on start; reading `--previous` logs |
| `imagepullbackoff.md` | `platform` | Registry auth, wrong tag, rate limits |
| `node-disk-pressure.md` | `platform` | Kubelet eviction, image cache, inode exhaustion |
| `tls-cert-expiry.md` | `edge` | Expired ingress cert, cert-manager renewal failure |
| `postgres-conn-pool-exhaustion.md` | `data` | `too many clients already`, idle-in-transaction |
| `jenkins-agent-offline.md` | `ci` | Agent disconnect, workspace disk full, label mismatch |
| `coredns-resolution-failure.md` | `platform` | In-cluster DNS, `ndots:5`, CoreDNS OOM |

All eight are `doc_type: runbook` deliberately — for the Day 8 experiment the only variable
should be chunk size. Day 9 adds postmortems and reference docs, which is when `doc_type`
becomes worth filtering on.

The retrieval questions in [`queries.json`](./queries.json) were written separately from the
corpus, without rereading it. That matters: if one author phrases both, a query matches its
runbook because of shared vocabulary habits, and the test proves nothing. The point is to
watch a 2am symptom description find a document that never uses those words — which is the
entire argument for embeddings over grep.

## Running it

```bash
pip install -r requirements.txt
python day8_embeddings.py --reset
```

Requires an embedding model on the configured Ollama host:

```bash
ollama pull nomic-embed-text     # 768-dim, ~274MB
```

Config comes from the repo-root `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` or `gemini` |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model — not the generation model |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Shared with the log analyzer |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Pinned to 768 dims so the two are comparable |
| `CHROMA_PATH` | `services/knowledge-copilot/chroma_data` | Persistent index; gitignored, rebuildable |

The script prints four sections: what a vector looks like and how cosine similarity separates
related from unrelated text; index construction at each chunk size; a per-query comparison of
top-3 hits across configs; and a hit@1 / hit@3 summary.

## Testing

```bash
python -m pytest tests/ -q
```

`tests/test_chunking.py` is offline and deterministic — no embedding call belongs in a unit
test. It covers chunk size bounds, that overlap actually carries text forward, that no word is
lost, ID stability across runs, and front matter parsing with and without a header.

There is no CI workflow for this service yet. The existing
[`log_analyzer_ci.yml`](../../.github/workflows/log_analyzer_ci.yml) is path-scoped to
`services/log-analyzer/**` and will not run these tests. Worth adding once there is an
endpoint to protect.

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
token leading ~85 of 109 chunks at 256/32, quietly polluting every vector. Caught only because
a test asserted the boring invariant that no word is lost. That test earned its keep before the
service had a single user.

Five queries is a smoke test, not a benchmark — one query moving swings hit@1 by 20%. Day 11
builds the eval set that can settle this.

## Not built yet

| Capability | Day |
|---|---|
| Idempotent re-indexing, metadata tagging and filtering | 9 |
| `POST /ask-runbook` — retrieve top-k, augment the prompt, cite sources | 10 |
| Hybrid keyword+vector search, reranking, a real eval set | 11 |
| Connector ingesting Prometheus alerts / K8s events | 12 |
| Slack bot or web chat in front of the service | 13 |
| Auth, architecture diagram, demo recording | 14 |
