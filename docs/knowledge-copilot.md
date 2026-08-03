# Knowledge Copilot — how it works

A ground-up explanation of the retrieval layer in
[`services/knowledge-copilot`](../services/knowledge-copilot): what embeddings and vector
search actually are, why each design choice was made, and what every piece of the code is
doing. Written for someone meeting embeddings for the first time.

The service README covers *what was built and what it scored*. This document covers *why any
of it works*.

---

## The problem

You have runbooks. At 2am something breaks and you need the right one. `grep` is the obvious
tool, and it fails in a specific, frustrating way:

```
$ grep -ri "pods keep dying after deploy" corpus/
$
```

Nothing. The runbook that answers this exists — it's `oomkilled-pod.md` — but it says
"restart on a loop" and "Exit Code: 137", not "keep dying". `grep` matches **characters**.
You need something that matches **meaning**.

That is the entire premise. Everything below is machinery for making "find me the document
that *means* roughly this" a computable operation.

---

# Part 1 — The concepts

## 1.1 What an embedding is

An **embedding model** is a function that takes text and returns a fixed-length list of
numbers:

```
"The pod was OOMKilled after exceeding its memory limit."
          │
          ▼   nomic-embed-text
[+0.0175, +0.0566, -0.1623, -0.0223, +0.0547, ... ]   ← 768 numbers
```

That list is called a **vector** or an **embedding**. The model was trained so that texts
with similar meaning produce vectors that are close together, and unrelated texts produce
vectors that are far apart. That training is the whole trick — there is no dictionary, no
keyword table, no rules. A neural network read an enormous amount of text and learned a layout.

Think of it as a map. Every possible piece of text gets a coordinate. "Pod ran out of memory"
and "container was killed for exceeding its memory limit" land near each other. "The TLS
certificate expired" lands somewhere else entirely. Search becomes: *put the question on the
map, and see what's nearby.*

### What does one of the 768 numbers mean?

Nothing, on its own. This is the single most common misunderstanding, so it's worth being
blunt: there is **no** dimension that means "urgency" or "is about networking". The model
was never told what the dimensions should represent, and nobody assigned them meanings
afterward.

Meaning lives in the vector's **position relative to other vectors**, not in any individual
coordinate. Dimension 42 in isolation is unreadable. Dimension 42 across ten thousand
documents contributes to a geometry where related things cluster. It's like asking what a
single pixel in a photograph depicts — the question is a category error.

This matters practically: you cannot debug an embedding by reading it. You can only compare it
to other embeddings.

### Why 768?

It's the output size `nomic-embed-text` was built with. Different models use different sizes —
384, 768, 1024, 3072. More dimensions means more room to express fine distinctions, at the
cost of more storage and slower comparison.

The rule that actually bites: **vectors from different models are not comparable.** Dimension
42 of a nomic vector and dimension 42 of a Gemini vector have nothing to do with each other.
An index built with one model and queried with another produces confident, plausible-looking
nonsense — no error, no crash, just wrong answers. This is why the code raises rather than
falling back when the provider is misconfigured (see §2.2).

Our Gemini provider is pinned to 768 output dimensions so the two backends produce
same-shaped vectors. That makes them *swappable*, not *interchangeable* — you still have to
re-index when you switch.

## 1.2 Measuring similarity

Two vectors are "close". Close how? There are two candidate measurements, and the difference
matters.

**Euclidean distance** — straight-line distance between the two points. Sensitive to how *long*
the vectors are.

**Cosine similarity** — the angle between the two vectors, ignoring their length. Points in the
same direction = similar. Perpendicular = unrelated.

```
        ▲                      cos = 1.0   same direction, identical meaning
        │    ╱ B               cos = 0.0   perpendicular, unrelated
        │  ╱                   cos = -1.0  opposite direction
        │╱  θ
        └──────────▶ A         cosine similarity = cos(θ)
```

We want **cosine**, because vector length tends to track things we don't care about — text
length, token count, how emphatic the writing is. Direction is what encodes topic. A
one-sentence description and a three-paragraph description of the same failure should be
"similar", and cosine says they are.

### Normalization, and a wrinkle

If you scale every vector to length exactly 1 — **L2 normalization** — they all sit on the
surface of a sphere, and two useful things follow.

First, cosine similarity becomes a plain dot product. The general formula is:

```
cos(a, b) = (a · b) / (|a| × |b|)
```

When `|a| = |b| = 1`, the denominator is 1 and it collapses to `a · b` — multiply
element-wise, sum. That's why `cosine_similarity()` in the code is a one-liner.

Second — and this is the wrinkle worth knowing — once everything is normalized, cosine and
Euclidean *rank results identically*. For unit vectors, `squared_L2 = 2 − 2·cos`, a strictly
decreasing relationship, so sorting by one is sorting by the other.

So why declare cosine space explicitly? Two reasons. It states the intent honestly — we mean
"same direction", not "same position". And the equivalence **only holds while everything is
normalized**. Gemini returns unnormalized vectors when you truncate its output dimensions
(§2.2), and the moment one unnormalized vector enters the index, L2 starts ranking by length.
Declaring cosine makes the system correct rather than accidentally correct.

### Cosine has a floor, and it isn't zero

From the actual run:

```
cos(related)   = 0.7427    OOMKill  vs  container ran out of memory
cos(unrelated) = 0.5918    OOMKill  vs  TLS certificate expired
```

Two completely unrelated sentences scored **0.59**, not 0. This surprises everyone.

The reason: all English text shares an enormous amount — grammar, common words, the general
register of technical writing. The model doesn't need to spread text across the whole sphere;
it only needs related things to be *closer than* unrelated things. In practice everything
lands in a fairly narrow cone. (The term for this is *anisotropy*, if you want to read more.)

The practical consequence is important: **absolute similarity scores are not interpretable.**
A rule like "only show results above 0.5" would show everything. A rule like "above 0.7" would
be tuned to this specific model and break the day you switch. Only the *ranking within a single
query* carries information — which is exactly why Day 11 needs reranking rather than a
threshold.

## 1.3 Why chunk documents at all

You could embed each runbook as one vector. Don't — for two reasons.

**Dilution.** One vector has to summarize the entire document. A 3,000-character runbook covers
symptoms, diagnosis commands, five possible causes, four fixes, and escalation. Averaged into
a single point, it becomes a vague "something about Postgres". A query about one specific
symptom matches that blur weakly, and a query about a *different* part of the same document
matches it just as weakly. Precision collapses.

**Day 10.** The retrieved text gets pasted into an LLM prompt. You want the paragraph that
answers the question, not eight pages, because context costs money, adds latency, and buries
the answer among irrelevant text.

So: split each document into pieces, embed each piece, and search over pieces.

### Why not split into sentences?

The opposite failure. "Run `kubectl describe pod`" appears in half the corpus and means nothing
alone. Too little context and every chunk is generic.

Chunk size is the dial between those two failures:

| | Small chunks (256) | Large chunks (1024) |
|---|---|---|
| Signal per vector | Concentrated, precise | Diluted across topics |
| Context in a hit | May lack the fix that follows the symptom | Self-contained |
| Vector count | High (109 for our corpus) | Low (27) |
| Good for | Narrow symptom lookups | Questions needing surrounding explanation |

There is no universally correct value. It depends on your documents and your questions — which
is why the code measures it instead of guessing.

### Why chunks overlap

Split a document at exactly 512 characters and something will land on the boundary:

```
...restart every few minutes. An out-of-memory kill shows Reason:  │  OOMKilled with Exit Code: 137...
                                                          chunk 1 │ chunk 2
```

Now neither chunk contains the complete fact. Chunk 1 sets it up and stops; chunk 2 starts
mid-thought. A query for "OOMKilled exit code 137" matches both weakly instead of one strongly.

Overlap fixes it: each chunk starts a little before the previous one ended, so any span shorter
than the overlap survives intact in at least one chunk. It costs some duplicated storage. We
use overlap = size ÷ 8.

## 1.4 What a vector database does

With text chunked and embedded, search is: compare the query vector against every chunk vector,
sort, take the top few. You could write that in twenty lines of numpy.

At our scale you *should* be able to — 109 chunks × 768 dimensions is about 84,000
multiplications, microseconds of work. **Chroma is not buying us speed today.** Being honest
about that matters, because "we needed a vector database" would be a false claim in an
interview.

What it actually buys us:

- **Persistence.** The index survives process restart. Re-embedding the corpus on every boot
  would be slow and, on a paid provider, expensive.
- **Metadata storage and filtering.** Each chunk carries `source`, `title`, `service`,
  `doc_type`, `chunk_index`. Day 9 uses these to filter ("only `service: data` runbooks"),
  and Day 10 uses them to cite sources. Doing this by hand means writing a small database.
- **Upsert-by-ID.** Re-index the same document and rows are replaced, not duplicated. This is
  what makes Day 9's idempotent re-index straightforward.

Speed becomes the reason at a different scale. Brute force is linear: 10 million chunks means
10 million comparisons per query. Chroma's **HNSW** index (Hierarchical Navigable Small World)
builds a navigable graph over the vectors so a search visits a small fraction of them —
roughly logarithmic instead of linear. It's an **approximate** nearest-neighbour method: it can
occasionally miss a true nearest neighbour, trading a little accuracy for a lot of speed. At
109 chunks that tradeoff is pure overhead. At 10⁶ it's the only option.

Adopting it now is a deliberate bet that the interface stays the same as the corpus grows.

## 1.5 Why queries and documents are embedded differently

This is the least obvious idea here, and the easiest to get wrong.

A question and the passage that answers it are **not the same kind of text**:

```
query:    "pods keep dying and restarting right after a deploy, exit code 137"
document: "## Symptom — Pods in a deployment restart on a loop. kubectl get pods
           shows a RESTARTS count climbing every few minutes..."
```

Short vs long. Interrogative vs declarative. Informal vs structured. If you want them to land
near each other on the map, the model needs to know which role each is playing.

Modern embedding models are trained for exactly this, with (question, answer-passage) pairs, and
they expose a way to declare the role:

- **nomic-embed-text** — prefix the text: `search_document: ...` or `search_query: ...`
- **Gemini** — a parameter: `task_type=RETRIEVAL_DOCUMENT` or `RETRIEVAL_QUERY`

Skipping this doesn't raise an error. It just makes retrieval quietly worse. That's why the
interface has two methods — `embed_documents()` and `embed_query()` — rather than one `embed()`:
you cannot forget which side you're on if the API won't let you express it.

---

# Part 2 — The code

Three modules, each with one job.

```
corpus/*.md ──▶ chunking.py ──▶ embeddings.py ──▶ Chroma ──▶ results
               (text → pieces)  (text → vectors)  (store + search)
```

## 2.1 `chunking.py` — text into identified pieces

Pure functions. No network, no Chroma, no configuration. That's deliberate: it's the only
module that can be fully tested offline, and it is where the subtle bugs live.

### `parse_front_matter()`

Each runbook opens with a metadata header:

```markdown
---
title: Pod OOMKilled and restarting
service: platform
doc_type: runbook
last_reviewed: 2026-07-15
---

## Symptom
...
```

This function splits the header from the body and returns both. The header becomes chunk
metadata; the body gets chunked.

It's hand-written rather than using PyYAML because the header is a flat `key: value` map by
convention — ten lines of code against a dependency to maintain, parse, and secure.

### `chunk_text(text, size, overlap)`

The core logic. Walk a window through the text:

```python
while start < len(text):
    end = start + size
    if end < len(text):
        boundary = text.rfind(" ", start, end)   # snap back to a space
        if boundary > start:
            end = boundary
    chunks.append(text[start:end].strip())
    ...
    next_start = end - overlap
    boundary = text.rfind(" ", start, next_start)  # snap the start too
    if boundary != -1:
        next_start = boundary + 1
    start = max(next_start, start + 1)
```

Three decisions in there:

**Snap `end` backwards to a space.** Cutting at exactly `size` characters splits words in half.
A fragment like `OOMKil` isn't a word the model knows — it gets tokenized into meaningless
pieces that still contribute to the vector. Pure noise.

**Snap `start` backwards too.** This one was a real bug, caught by a test. The original code set
`start = end - overlap` with no boundary snap, so every chunk after the first *began* mid-word:

```
chunk[1]:  "rd33 word34 word35 ..."     ← should be "word33 word34 ..."
```

No crash, no error, no visible symptom — just a garbage token leading 63 of the 107 chunks,
polluting every vector. Snapping *backwards* rather than forwards is intentional: it can only
widen the overlap, never drop text between chunks.

**`max(next_start, start + 1)`.** A progress guard. If a single word were longer than `size`, no
boundary would be found and `start` could fail to advance — an infinite loop. The `+1` makes
that impossible.

### `chunk_document()` — why IDs are `{slug}:{index}`

```python
Chunk(id=f"{doc.slug}:{index}", ...)   # "oomkilled-pod:3"
```

Deterministic, not random. Re-run the indexer and chunk `oomkilled-pod:3` **replaces** the
existing row instead of adding a duplicate. With UUIDs, every re-index would double the corpus.
This is the hook Day 9's idempotent re-indexing hangs on.

One caveat: the same ID means *different text* under a different chunk size. Safe here only
because each configuration gets its own collection.

## 2.2 `embeddings.py` — text into vectors

The boundary between "our text" and "somebody's neural network".

### The interface

```python
class BaseEmbeddingProvider(ABC):
    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...
```

An abstract base class — the same **Strategy pattern** the log analyzer uses for `BaseLLMProvider`.
Code downstream never knows which model produced a vector. Swapping Ollama for Gemini is an
environment variable, not a rewrite.

`embed_documents` takes a list and `embed_query` takes one string because that's how they're
actually used: batch the corpus once, embed queries one at a time. Batching matters — one HTTP
request for 109 chunks instead of 109 requests.

### `l2_normalize()`

```python
norm = math.sqrt(sum(v * v for v in vector))
return [v / norm for v in vector]
```

Scale to length 1, for the reasons in §1.2. Applied to *every* vector from *both* providers,
so the index can never mix normalized and unnormalized entries.

### The Ollama provider

```python
POST {base_url}/api/embed
{"model": "nomic-embed-text", "input": ["search_document: ...", ...]}
```

Note this is `/api/embed`, not `/api/generate`. Embedding models are a different kind of model —
`nomic-embed-text` has no text-generation capability at all. It cannot answer a question. Its
only output is a vector.

The `search_document:` / `search_query:` prefixes are applied here, inside the provider, so
callers never have to remember them.

### The Gemini provider

```python
config=types.EmbedContentConfig(
    task_type="RETRIEVAL_DOCUMENT",
    output_dimensionality=768,
)
```

`gemini-embedding-001` natively produces 3,072 dimensions. We ask for 768 to match nomic, which
works because the model was trained with **Matryoshka representation learning** — the most
important information is packed into the leading dimensions, so truncating still leaves a
usable vector, much as a lower-resolution image is still recognizable.

The catch: Gemini only guarantees unit-length output at its *native* size. Truncated vectors
come back unnormalized, which is why `l2_normalize()` is applied unconditionally. Without it,
cosine distance would be silently skewed by vector length.

Requests are batched at 100, the API's per-call limit.

### The factory raises

```python
raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {provider_type!r}")
```

The log analyzer's equivalent logs an error and falls back to Ollama. This one refuses to start.

With embeddings, a silent fallback is worse than a crash. A typo in `EMBEDDING_PROVIDER` would
index the corpus with an unintended model, and — per §1.1 — a mismatched index doesn't fail
loudly. It returns confident, plausible, wrong results forever. Crashing on startup is the
kinder failure.

## 2.3 `day8_embeddings.py` — the experiment

Not a service entrypoint; a dated artifact that measured chunk size. Four sections.

### Creating a collection

```python
collection = client.get_or_create_collection(
    name=f"runbooks_{provider.name}_{size}_{overlap}",
    configuration={"hnsw": {"space": "cosine"}},
    embedding_function=None,
)
```

**`space: "cosine"`** — Chroma defaults to squared L2. See §1.2 for why we override it.

**`embedding_function=None`** — this one is load-bearing. Chroma's default is *not* `None`; left
alone it downloads and runs its own ONNX MiniLM model. You would then have two embedding models
in the system: yours embedding queries, Chroma's embedding documents. Per §1.1, that produces
garbage with no error. Passing `None` says "I supply the vectors."

**Collection name includes the provider and config**, so an Ollama 512/64 index can never be
queried with Gemini vectors or compared against 256/32 chunks.

### Searching

```python
result = collection.query(query_embeddings=[qvec], n_results=3, ...)
score = 1 - distance
```

Chroma returns cosine **distance** — how far apart — while we want **similarity**. In cosine
space they're complements: `similarity = 1 − distance`. Distance 0 means identical direction
means similarity 1.

### Reading the output

```
cos(related)   = 0.7427
cos(unrelated) = 0.5918
```

Section 1 makes the abstraction concrete: 768 floats, and a demonstration that the geometry
actually separates related from unrelated text. If related did *not* score higher, something
would be wrong — most likely the task prefixes not reaching the model.

Sections 2–4 index the corpus at three chunk sizes and compare retrieval. **hit@1** is "was the
top result from the right document"; **hit@3** is "was it anywhere in the top three".

## 2.4 `ingest.py` — why upsert alone isn't idempotent

Day 8 gave chunks deterministic IDs so re-indexing *replaces* rather than *duplicates*. That
handles two of the four cases a real re-index faces. It does nothing for the other two:

- Shrink a doc from 8 chunks to 5 and IDs `{slug}:5..7` are orphaned — still in the index,
  still retrievable, now describing text that no longer exists.
- Delete a doc and every one of its chunks lingers forever.

Orphans never raise. They surface as confident, stale hits — the worst failure a RAG store
has, because nothing tells you it happened. This is the same shape of silent bug as the chunker
that stopped overlapping (§2.1): no crash, just quietly worse retrieval weeks later.

So idempotent re-indexing is **reconcile**, not upsert: compare the IDs the corpus *should*
produce against the IDs the index *currently holds*, and act on the difference. `ingest.py`
reads the stored `content_hash` of every chunk, and `plan_reconcile` sorts each into
add / update / skip / delete:

```
id absent                    -> add
id present, hash differs     -> update (re-embed)
id present, hash matches     -> skip
id in index, not in corpus   -> delete
```

The **delete** list — IDs in the index that the corpus no longer wants — is the entire point,
and it is the one thing `collection.upsert` can never give you. `plan_reconcile` is a pure
function: hand it the desired chunks and a `{id: hash}` map read from Chroma, get back the plan.
No network, no clock, no Chroma — so the risky logic is fully unit-testable offline, the same
discipline `chunking.py` follows.

### The hash covers front matter, not just body

`content_hash = sha256(body + front matter)`, one hash per document, computed *before* the
run's `indexed_at` is attached. Two consequences, both deliberate:

- A re-run with no edits embeds **nothing** — every hash matches, everything is skipped. On a
  rate-limited provider like Gemini's free tier, that is the difference between a re-index
  costing a whole quota and costing zero.
- A metadata-only edit — bumping `last_reviewed`, say — still re-indexes, because the front
  matter is inside the hash. Had the hash covered only the body, `plan_reconcile` (which
  compares nothing but the hash) would judge the chunk "unchanged" and let its stored metadata
  go stale. `indexed_at` is excluded from the hash for the mirror-image reason: a field that
  changes every run must never be part of the key, or nothing would ever count as unchanged.

"Computed *before* `indexed_at` is attached" is load-bearing enough to be structural rather
than a matter of statement order. `enrich()` builds a **new** `Document` with the extra fields
instead of mutating the one it was handed:

```python
return [
    replace(doc, metadata={**doc.metadata, "content_hash": content_hash(doc), ...})
    for doc in docs
]
```

`content_hash(doc)` there is unambiguously reading the *original* front matter, so the hash can
never accidentally include its own output or a timestamp. The mutating version worked, but only
because two lines happened to be in the right order — and it was quietly writing into a
`@dataclass(frozen=True)`, which stops attribute rebinding but not `metadata["k"] = v`. Frozen
that only holds if nobody reaches into the dict is not frozen; it is a comment with syntax.

### A dry run that wasn't

The first version of `ingest()` handled `--reset` like this:

```python
if reset:
    client.delete_collection(name)      # ← ran unconditionally
...
if dry_run:
    return plan                         # ← "change nothing" starts here
```

`--reset --dry-run` therefore printed a tidy plan and destroyed the collection on the way. The
output said `(dry run)` while the index was already gone; the damage only surfaced on the *next*
command, as a full re-embed of a corpus that hadn't changed.

The lesson generalizes past this bug. A read-only mode is not "the write at the end is skipped"
— it is a property of the whole call path, and every side effect above the early return has to
be checked against it. Reset and dry-run are also simply contradictory, so the fix is to refuse
the combination rather than order it correctly:

```python
if reset and dry_run:
    raise ValueError("reset drops the collection; it is never a dry run")
```

The guard lives in `ingest()`, not only in `argparse`, because Day 10's endpoint will call
`ingest()` as a library function and never see the CLI. A safety check that only exists in the
argument parser protects the one caller that was already easiest to get right.

### Making the pipeline testable at all

Worth asking why that bug survived a test suite that covers reconcile logic five ways. Because
`ingest()` built its own dependencies:

```python
provider = get_embedding_provider()          # reads env, opens a network client
client = chromadb.PersistentClient(...)      # writes to the real index
```

Every path through it needed a live embedding backend and the production Chroma directory, so
none of it was tested — and the untested function was the one with the bug. Accepting the
dependencies instead of constructing them fixes that in one line each:

```python
def ingest(reset=False, dry_run=False, provider=None, client=None, corpus_dir=CORPUS_DIR):
    provider = provider or get_embedding_provider()
    client = client or chromadb.PersistentClient(...)
```

Defaults preserved, so every existing caller is unaffected — but a test can now pass a
`FakeProvider` whose "embeddings" are the first four bytes of a sha256, a Chroma path under
`tmp_path`, and a two-file corpus, and assert the guarantees directly: dry run embeds zero
vectors, an unchanged re-run embeds zero vectors, deleting a source file removes its chunks from
the collection. Offline, deterministic, ~2 seconds.

The `FakeProvider` counting its own calls is the important part. "Re-running embeds nothing" is
a claim about work *not* done, and you cannot observe absent work by inspecting the result — the
index looks identical either way. You have to instrument the collaborator. That is the general
shape for testing any cache, skip, or idempotency claim.

One trap on the way there: `chromadb.EphemeralClient()` looks like the obvious in-memory choice,
but repeated calls with identical settings resolve to the same in-process system rather than a
fresh one. A collection written by one test was still there in the next, and
`test_unchanged_rerun_embeds_nothing` failed because its "first" ingest found the previous
test's data. A `PersistentClient` pointed at each test's own `tmp_path` is the isolation
`EphemeralClient` only appears to offer.

### Metadata as a filter

Every chunk now carries `service`, `doc_type`, and a date alongside `content_hash` and
`indexed_at`. Chroma's `where` clause filters on any of them *before* ranking, so
`--where doc_type=postmortem` and `--where doc_type=runbook` return different top hits for the
same question. That only became meaningful once the corpus stopped being eight uniform runbooks
— Day 9 added two postmortems and a reference doc precisely so `doc_type` is worth filtering on.

Filters are typed, which is easy to miss. `chunk_index` is stored as an integer, so a `where`
clause carrying the *string* `"0"` matches nothing at all — no error, no warning, just an empty
result that reads exactly like "there is no such chunk". `--where key=value` arrives from the
shell as text, so `parse_where` coerces digit-only values back to `int`. Same failure family as
the rest of this service: the retrieval bugs that hurt are the ones that return a plausible
answer instead of raising.

---

# Part 3 — What the run actually showed

Full numbers are in the [service README](../services/knowledge-copilot/Readme.md#findings).
Three things worth internalizing:

**The experiment saturated.** All three chunk sizes scored 5/5 on hit@1, 5/5 on hit@3 and 12/15
on precision@3 — identical. With eight topically unrelated runbooks, finding the right document
is too easy to separate chunk sizes. That's a finding about the *evaluation*, not about
chunking: the metric had no resolution left to spend. A useful eval set needs documents that
genuinely compete — three different causes of pod restarts, not one runbook per topic.

**The signal was in the margins, not the ranks.** The only discriminator left was the cosine gap
between the best correct chunk and the best incorrect one. It ruled out 1024-character chunks
(consistently the narrowest margin — dilution, exactly as §1.3 predicts) but could not separate
256 from 512 on two measurable queries. So the chunk size was chosen on downstream cost, and
the README says so, rather than presenting a clean table the evidence doesn't support.

**A "wrong" answer was semantically right.** The exit-code-137 query returned
`crashloopbackoff.md` at rank 2 in every configuration, scored as a miss. It isn't one — an
OOMKilled pod *presents* as CrashLoopBackOff, and the two runbooks cross-reference each other.
Binary single-label scoring can't express partial correctness. Day 11's eval set needs graded
relevance or it will penalize the model for being right.

---

# Glossary

| Term | Meaning |
|---|---|
| **Embedding** | A fixed-length list of numbers representing a piece of text, positioned so similar meanings are close together |
| **Vector** | The list of numbers itself; used interchangeably with embedding |
| **Dimension** | One number in the vector. Individually meaningless — see §1.1 |
| **Cosine similarity** | Similarity as the angle between two vectors, ignoring their length. 1 = same direction, 0 = unrelated |
| **Cosine distance** | `1 − cosine similarity`. What Chroma returns |
| **L2 normalization** | Scaling a vector to length 1, so cosine similarity becomes a plain dot product |
| **Chunk** | A piece of a document, small enough to be a precise search result and large enough to carry context |
| **Overlap** | Characters shared between consecutive chunks, so a fact on a boundary survives in one piece |
| **Vector database** | A store for vectors with similarity search, metadata filtering, and persistence. Chroma, here |
| **HNSW** | Hierarchical Navigable Small World — the graph index Chroma uses for fast approximate search at scale |
| **ANN** | Approximate Nearest Neighbour. Trades exactness for speed; the category HNSW belongs to |
| **Matryoshka** | Training so the leading dimensions carry the most information, making truncation viable |
| **RAG** | Retrieval-Augmented Generation — retrieve relevant text, put it in an LLM prompt, generate a grounded answer. Day 10 |
| **hit@k** | Fraction of queries where the correct document appears in the top *k* results |
| **Anisotropy** | The tendency of embeddings to occupy a narrow cone rather than the full sphere — why unrelated text still scores 0.59 |
