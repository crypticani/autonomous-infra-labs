# Knowledge Copilot — how it works

A ground-up explanation of
[`services/knowledge-copilot`](../services/knowledge-copilot): what embeddings and vector
search actually are, how retrieved text becomes a grounded answer, why each design choice was
made, and what every piece of the code is doing. Written for someone meeting embeddings for the
first time.

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

## 1.6 What RAG actually is

Everything above finds text. **Retrieval-Augmented Generation** is the small idea that turns
found text into an answer: paste it into the prompt.

```
question ──▶ retrieve top-k chunks ──▶ build a prompt containing them ──▶ LLM ──▶ answer
```

That's it. There is no fine-tuning, no training, no model weights involved. The model is not
"taught" your runbooks — it reads them, in the prompt, at the moment you ask. Which means the
answer's quality is bounded by what retrieval found: **a RAG system's ceiling is its retriever.**
This is why Days 8–9 came before Day 10, and why Day 11 is an eval rather than a feature.

### Why not just ask the model?

`qwen2.5:7b-instruct` genuinely knows what OOMKilled means. It does *not* know that your checkout
service ran out of memory in June because a cache release removed a bound, that the fix was
raising the limit to 512Mi, or that your convention is to set requests at 60% of limits. Nothing
in its training data contains your infrastructure. Asked anyway, it will produce a fluent,
plausible, generic answer — and the failure is invisible, because fluency reads as knowledge.

### The failure RAG introduces

Putting text in a prompt does not force the model to use it. Three things can go wrong, and only
the first is obvious:

1. **Retrieval finds nothing relevant**, so the context is noise. Answering anyway produces a
   confident answer from the model's general knowledge, dressed in your runbooks' credibility.
2. **The model cites something that isn't there.** Asked to write `[1]`, `[2]`, it will sometimes
   emit `[7]` — a reference to a source that was never in the prompt. A reader who trusts
   citations has no way to know.
3. **The answer blends retrieved fact and pretraining**, two sentences from a runbook and a third
   invented, in one paragraph. The reader cannot tell them apart, and neither can a log.

The endpoint's whole design is a response to these. A **similarity floor** answers (1) by
refusing when nothing scores well enough. **Marker validation** answers (2) by checking every
`[n]` against the chunks actually placed in the prompt. And a **`grounded` boolean that is
separate from "we answered"** answers (3) by refusing to let one flag stand for both. §2.7 is
how each is implemented.

---

# Part 2 — The code

Seven modules, each with one job.

```
corpus/*.md ──▶ chunking.py ──▶ embeddings.py ──▶ Chroma ◀── retrieval.py ◀── app.py ──▶ llm.py
               (text → pieces)  (text → vectors)  (store)   (rank + floor)  (prompt,   (text →
                    ▲                                            ▲          cite,      prose)
                    └────────── ingest.py (reconcile) ───────┘    │          validate)
                                                          hybrid.py
                                                    (bm25, rrf, mmr — no I/O)
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
result that reads exactly like "there is no such chunk". Day 9's `--where key=value` arrived from
the shell as text, so it coerced digit-only values back to `int`. Same failure family as the rest
of this service: the retrieval bugs that hurt are the ones that return a plausible answer instead
of raising.

Day 10 removed that flag along with `ingest.py`'s `search()` — see §2.5. The metadata is still on
every chunk and Chroma still filters on it; what's gone is the CLI surface that exposed it. It
returns as a request parameter when Day 11's eval needs to compare `doc_type=runbook` against
`doc_type=postmortem`, and the string-vs-int trap will be waiting there too.

## 2.5 `retrieval.py` — ranked chunks, and a floor

One function does the work: embed the question, ask Chroma for the top `k`, convert distance to
similarity, drop what scores too low.

```python
result = collection.query(
    query_embeddings=[provider.embed_query(question)],
    n_results=k,
    include=["documents", "metadatas", "distances"],
)
```

`include=["documents", ...]` is the whole reason this module exists. Day 9 had a `search()`
helper whose `include` list asked only for metadata and distances — fine for printing "which
document matched", useless for Day 10, because **you cannot put a citation in a prompt without
the text it cites**. Day 10 deleted `search()` rather than patching it, so there is exactly one
retrieval implementation. Two implementations of the same idea drift, and the one used by the
demo is never the one that gets tested.

### The similarity floor

```python
kept = [hit for hit in hits if hit.score >= floor]     # floor = 0.65
```

Chroma always returns `k` results. It has no concept of "nothing here is relevant" — ask an index
of Kubernetes runbooks how to rotate an IAM key and you get four confident chunks about pods,
ranked. Without a floor those become context, and the model dutifully answers an AWS question
from a Kubernetes runbook.

§1.2 explained why an absolute threshold is dangerous: cosine similarity on this model has a
floor around 0.59 and a ceiling around 0.80, so the usable band is narrow and model-specific.
That makes 0.65 a *calibrated guess*, not a principle — it sits between the 0.72–0.76 a bullseye
scores on this corpus and the 0.59–0.64 an unrelated chunk scores. It is `SIMILARITY_FLOOR` in
the environment precisely because it will need retuning the day the embedding model changes, and
Day 11's eval set needs out-of-corpus questions so the number is measured against real negatives
instead of inferred from positives.

### Empty is not the same as irrelevant

```python
if collection.count() == 0:
    raise EmptyIndexError(...)
```

Two situations produce zero usable chunks, and they demand opposite responses. *Nothing cleared
the floor* means the corpus genuinely doesn't cover the question — the honest answer is "not
covered", and it is correct. *The collection is empty* means the index was never built, and "not
covered in the runbooks" is then a **lie**: the runbooks are fine.

This is not hypothetical. Change `EMBEDDING_PROVIDER` and `get_or_create_collection` creates a
fresh, empty collection under the new name (§2.3) — no error anywhere. Every query returns
nothing, and a service that treated empty as irrelevant would answer "not covered" to every
question ever asked, forever, while looking healthy. So `retrieve()` raises on one and returns
`[]` on the other, and `app.py` maps the raise to a 503 that names the fix.

## 2.6 `llm.py` — the second Strategy boundary

Structurally the twin of `embeddings.py`: an ABC with an Ollama and a Gemini implementation,
selected by an environment variable. One difference is worth explaining, because it looks like an
inconsistency.

```python
# log analyzer
def generate(self, system_prompt, user_prompt, temperature=0.1) -> LogAnalysis: ...
# here
def generate(self, system_prompt, user_prompt, temperature=0.1) -> str: ...
```

The log analyzer's entire output *is* structure — a severity, a cause, a fix, a confidence — so
its provider validates against a Pydantic schema and anything malformed is a 502. Here the model
writes prose with citation markers in it. Prose has no schema, and the parsing that matters
(which markers appear, do they resolve) is a property of the *answer plus the retrieved set*,
which the provider has never seen. So the provider returns a string, and grounding happens in
`app.py`. Same pattern, deliberately different contract — which is also why the ABC is copied
into this service rather than shared. A shared abstraction would have to be generic enough to
mean nothing.

### `UpstreamError`, and why exception order is a bug

Every provider fails in its own vocabulary: `requests` raises `Timeout`, the `google-genai` SDK
raises `APIError`. Callers don't care which — they need to know what to tell the client. So each
provider translates its own failures into one exception that carries the answer:

```python
raise UpstreamError("the model took too long to answer", 504) from e
```

One `except` clause in the endpoint, and no SDK-specific exception can escape as an unhandled 500.

The ordering trap is worth internalizing because it is silent:

```python
except requests.exceptions.Timeout:          # 504
except requests.exceptions.HTTPError:        # 502  <- must come before the next one
except requests.exceptions.RequestException: # 503
```

`HTTPError` is a *subclass* of `RequestException`, and Python takes the first matching clause. Put
the general one first and a model that was never pulled — Ollama answers 404, `raise_for_status()`
raises `HTTPError` — is reported as "the model backend is unreachable". You then spend twenty
minutes checking Tailscale and firewall rules for a problem that `ollama pull` fixes. The test
`test_transport_failures_carry_the_right_status` exists to keep that ordering honest, because
nothing about the code *looks* wrong.

## 2.7 `app.py` — grounding, which is the actual product

The HTTP layer is thin. What matters is three pure functions and the contract they defend.

### Numbered context blocks

```xml
<context>
<chunk id="1" source="oomkilled-pod.md" chunk_index="0">...</chunk>
<chunk id="2" source="postmortem-2026-06-checkout-oom-outage.md" chunk_index="1">...</chunk>
</context>
<question>why do my pods get OOMKilled after a deploy</question>
```

XML tags rather than markdown headings, following the Day 2 prompt work: they delimit
unambiguously, so a chunk containing `## Symptom` can't be mistaken for prompt structure. The
`id` is what makes citation possible at all — the model is told to cite ids, never filenames,
because `[2]` resolves to exactly one chunk of one document while
`postmortem-2026-06-checkout-oom-outage.md` resolves to 3 KB of text.

Numbering is **per-request**, not global. `[2]` means "the second chunk retrieved for *this*
question" and means something different next request. That is why `sources` in the response maps
each marker back to a file and `chunk_index`: the marker is a local handle, and the response has
to translate it before it leaves the process.

### Validating citations

```python
valid = set(range(1, len(hits) + 1))
cited = extract_markers(raw_answer)
unresolvable = {marker for marker in cited if marker not in valid}
```

Three chunks in the prompt means `{1, 2, 3}` are the only legal markers. A `[7]` is the model
inventing a source — the failure from §1.6 (2) — and it is stripped from the answer text,
`grounded` goes false, and a WARNING is logged. Note what does *not* happen: no 502. A
partially-cited answer still helps someone at 2am, whereas a response that failed schema
validation has nothing salvageable in it. The log analyzer maps malformed output to 502 because
there is no partial credit in a schema; here there is.

Finding the markers is a regex, and it took two bugs to get right:

```python
MARKER_RE = re.compile(r"(?<!\w)\[(\d+)\]")
```

`\[(\d+)\]` alone matches `argv[1]` and `${nodes[0]}` inside a shell snippet the model quoted
from a runbook. Those aren't citations; treating them as invented ones ungrounds a good answer.
The negative lookbehind fixes that — a marker can't be preceded by a word character.

The second bug was the fix for the first. Excluding a preceding `]` as well, `(?<![\w\]])`, looks
harmless and breaks the most common citation style there is: models write consecutive references
as `[1][2]`, and the second was then never extracted. Which meant it stayed in the prose, never
appeared in `sources`, and — because it was never *seen* — never counted as unresolvable, so
`grounded` still reported **true** about an answer containing a citation the response could not
resolve. A cosmetic fix reintroduced the exact failure the module exists to prevent, and only a
test spelling out `[1][2]` catches it.

### `grounded` is three conditions

```python
grounded = bool(hits) and bool(sources) and not unresolvable
```

Chunks cleared the floor, **and** the model cited at least one, **and** every marker it emitted
resolved. Anything else is false.

The temptation is to make `grounded` mean "we answered", because in the happy path they coincide.
They are different facts, and the gap between them is where every RAG failure lives — an answer
with no citations, an answer citing a source that doesn't exist, a refusal. Collapsing them into
one boolean produces a service that reports success whenever it produced text.

### Why `answer_source` exists

There are two ways to get `grounded: false, sources: []`, and they mean opposite things:

| | `answer` | `grounded` | `sources` | `answer_source` |
|---|---|---|---|---|
| Nothing cleared the floor | `Not covered in the runbooks.` | `false` | `[]` | `"none"` |
| Chunks found, model cited none | prose | `false` | `[]` | `"runbooks"` |

Without the field, a caller distinguishes "we have no idea" from "here is an answer, uncited" by
string-matching the refusal sentence — which breaks the first time the wording changes. The field
is the contract; the sentence is for the human. A `"model_knowledge"` value was designed for an
`allow_general` flag that would answer from pretraining when the runbooks don't cover something,
and deliberately not built: an honest refusal is the better default, and general knowledge about
*this* cluster's conventions is precisely where a 7B local model invents things.

### The refusal makes no model call

```python
if not hits:
    return AskResponse(answer=NOT_COVERED, sources=[], grounded=False, answer_source="none")
```

Cheaper, faster, and — the real point — impossible to accidentally answer from pretraining if the
model is never asked. A test asserts `spy.calls == 0` here, because "we skipped the work" is a
claim about something *not* happening: the response looks identical whether or not a model was
called and its output discarded. You have to instrument the collaborator, the same trick as
`FakeProvider.embed_calls` in §2.4.

### Why the endpoint is `def`, not `async def`

`requests` and the `google-genai` client are blocking. Declared `async def`, a 195-second model
call would stall FastAPI's event loop and freeze every other request — health checks included.
Plain `def` makes Starlette run the handler in a threadpool instead, so the service stays
responsive while the model thinks. This is the single most common FastAPI performance bug, and it
gets worse the slower the upstream is, which §3's latency finding makes very concrete.

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

**Day 10: the answer took 195 seconds.** One grounded question, `k=4`, warm model — and the first
attempt returned a 504 because the timeout was 120s. The model isn't the problem; where it runs
is. `/api/ps` reports `size_vram: 0`, so Ollama on appsrv serves from CPU, and a two-word prompt
to that same warm model returns in 6.5s. The difference is **prompt eval**: the model has to read
~2,000 characters of retrieved context before it writes a single token, and on CPU that read is
the entire cost.

Which reframes something §1.3 treated as free. Chunk size and `k` were discussed as a
precision-versus-context tradeoff; they are also the latency dial, and on CPU it is the dominant
one. Doubling `k` to improve recall roughly doubles time-to-answer. Retrieval quality and
response time are the same knob, so Day 11's eval has to measure both or it will happily
recommend a configuration nobody can wait for.

---

# Part 4 — Retrieval quality

## 4.1 The opening claim, revisited

This document opens with `grep` failing. The runbook says "restart on a loop" and "Exit Code:
137"; the question says "pods keep dying after deploy"; no characters match, so `grep` returns
nothing. The conclusion drawn was that you need something matching **meaning** rather than
characters.

Day 11 measured that claim, and it needs qualifying. Ranking the same twelve questions by BM25 —
which is `grep` with statistics bolted on, still matching nothing but characters — scored **higher
than semantic search on three of the four quality metrics**.

The premise was not wrong. Matching meaning is *necessary*: the paraphrased CoreDNS question
("one microservice cannot find another by its short name") shares almost no vocabulary with its
runbook, and only the embedding finds it. But necessary is not sufficient, and the reason is
visible in the failures. Consider what the corpus actually contains:

- `137` — an exit code. Three characters carrying more diagnostic information than any sentence
  around them.
- `too many clients already` — a Postgres error string, quoted verbatim from a log.
- `x509`, `NET::ERR_CERT_DATE_INVALID`, `ImagePullBackOff`, `DiskPressure`.

These are **identifiers**, not prose. An embedding's whole job is to map surface forms onto
nearby positions in a space — which is exactly wrong for a token whose value is that it is
*precisely itself*. `137` gets averaged into a 768-dimensional summary of the sentence it appeared
in, and the distinction between "the chunk containing 137" and "a chunk about pods dying" softens
into a distinction the vector can barely express.

Keyword search doesn't blur, because it doesn't generalise. Both are correct about different
questions, which is the entire argument for hybrid search: you are not picking a better retriever,
you are refusing to pick.

## 4.2 BM25 from first principles

BM25 scores a document against a query by summing a contribution per shared term. Three ideas,
each fixing a failure of the idea before it.

**Idea one: count the term.** A chunk mentioning `137` five times is more about `137` than one
mentioning it once. So score by term frequency, `tf`.

This fails immediately. A chunk saying `137` twenty times is not twenty times more relevant than
one saying it once — after two or three mentions you already know the chunk is about it. Raw `tf`
lets one repetitive chunk dominate everything. So BM25 **saturates**:

```
tf * (k1 + 1) / (tf + k1)
```

As `tf` grows, this approaches `k1 + 1` and stops. The first occurrence buys the most, the second
less, the tenth almost nothing. `k1 = 1.5` sets how fast the saturation bites.

**Idea two: weight rare terms higher.** `pod` appears in nearly every chunk of a Kubernetes runbook
corpus, so a chunk containing it has told you nothing. `137` appears in four documents out of
eleven, so a chunk containing it has told you a great deal. That is inverse document frequency:

```
idf = log(1 + (n - df + 0.5) / (df + 0.5))
```

where `df` is how many chunks contain the term and `n` is how many chunks exist. Rare term → small
`df` → large ratio → large `idf`. On this corpus a term in all 68 chunks scores `idf = 0.13`; a
term in one scores `0.98`, about seven times more. The eval's exact-token queries live entirely
off this multiplier.

**The trap here is worth dwelling on.** The classical form of that formula is:

```
log((n - df + 0.5) / (df + 0.5))          # not what the code uses
```

When a term appears in more than half the chunks, the numerator drops below the denominator, the
ratio drops below 1, and the log goes **negative**. A negative `idf` means a chunk is *penalised*
for containing a query term — asking about pods makes chunks mentioning pods score worse. On a
corpus of eleven documents that all discuss pods, this is not a corner case, it is Tuesday. The
`1 +` inside the log floors every contribution at zero. `tests/test_hybrid.py` asserts that a term
present in every chunk still scores above zero, which is the assertion that separates the two
formulas.

**Idea three: correct for length.** A 900-character chunk has more room to contain your term than a
200-character one, purely by being longer. Left alone, BM25 quietly prefers long chunks. So the
saturation denominator is scaled by how long this chunk is relative to the corpus average:

```
tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
```

`dl / avgdl` is above 1 for a long chunk, which inflates the denominator and shrinks the score.
`b = 0.75` applies three-quarters of that correction — full correction (`b = 1`) over-punishes
genuinely detailed chunks, none (`b = 0`) lets long chunks win on bulk alone.

Put together, that is fifteen lines of `Counter` and `log`. No dependency was added for it, and
writing it out is the only reason the negative-`idf` trap was ever noticed.

## 4.3 Fusing two rankings you cannot add

Now there are two ranked lists and one answer is needed. The obvious move — add the scores — does
not survive contact with the numbers:

| Ranker | Score of a good match | Score of a bad match | Range |
|---|---|---|---|
| Cosine | 0.76 | 0.59 | bounded, narrow, all positive |
| BM25 | 8.4 | 0.0 | unbounded, depends on corpus size and query length |

Adding `0.76` to `8.4` produces a number that means nothing. BM25 would swamp cosine entirely, and
worse, the weighting would silently change as the corpus grew — `idf` depends on `n`. Normalising
each to 0–1 first is the usual reflex, but min-max normalisation over a 15-item candidate list is
unstable: one outlier compresses everything else, and the "best" score becomes 1.0 by definition
whether it was good or not.

**Reciprocal Rank Fusion** sidesteps the problem by discarding scores and keeping only positions:

```python
fused[doc_id] += 1 / (k + rank)          # k = 60
```

A chunk ranked 1st by cosine and 3rd by BM25 gets `1/61 + 1/63`. A chunk ranked 2nd by both gets
`2/62`. Ranks are comparable across rankers in a way scores never are, because a rank is a
statement about the ranker's own list rather than about a magnitude.

`k = 60` is from the original TREC work, and its job is to flatten the top. Without it, first place
(`1/1`) would be worth twice second place (`1/2`), and a single confident ranker would dictate the
outcome. With it, `1/61` versus `1/62` is a 1.6% difference — so agreement between rankers matters
more than confidence within one. That is the behaviour you want from a fusion rule.

One consequence to be deliberate about: **the fused set is a union, not an intersection.** A chunk
only BM25 found still appears. Restricting to chunks both rankers returned would reduce hybrid
search to "reorder the dense results", and the entire `137` case — a chunk cosine ranked twentieth
— becomes unreachable. Hybrid search that cannot rescue is not hybrid search.

Which raises a safety question. If BM25 can inject a chunk on the strength of a keyword alone,
what stops a chunk about something else entirely from reaching the model because it happened to
contain `137`? The similarity floor, still applied — and applied to **cosine**, not to the fused
score. Fusion decides ordering and nothing else; a rescued chunk has its real cosine computed from
its stored embedding and must clear 0.65 like everything else. Two reasons this matters. RRF scores
sit around 0.016–0.033, so a 0.65 threshold against them would admit literally everything. And
§2.7's refusal behaviour — the endpoint that says *"Not covered in the runbooks"* and makes no model
call — depends on `floor` continuing to mean *similarity to the question*. Changing what the number
means would have broken grounding silently, while every test still passed.

## 4.4 MMR, and a measurement that killed it

Fusion decides which chunks are best. It has nothing to say about whether they're **redundant**.

The failure mode is concrete. Ask about resource limits and the top four chunks come back as
`reference-pod-resource-limits:0`, `oomkilled-pod:5`, `oomkilled-pod:3`,
`postmortem:2` — with `k = 4`, three slots spent on documents already represented. The
diagnosis you needed from a fourth document never arrives, and `recall@k` is capped by
duplication rather than by ranking quality.

**Maximal Marginal Relevance** picks greedily, penalising similarity to what it already holds:

```
score = lam * relevance - (1 - lam) * max(similarity to anything already picked)
```

At `lam = 1.0` it is pure relevance — the input order, unchanged, which makes it a free control
condition. Lower `lam` trades rank position for variety.

**One subtlety cost a rewrite.** The natural implementation passes the query vector and computes
`relevance` as cosine. Do that in hybrid mode and MMR re-sorts the candidates by raw cosine —
throwing away the fusion that just happened. MMR must inherit relevance from *whatever ranked the
candidates*, so `mmr()` takes `(id, relevance, vector)` triples and `rank_relevance()` derives
relevance linearly from position. This also puts relevance on the same 0–1 scale as the cosine
redundancy term, without which `lam` has no consistent meaning.

Then it was measured, and **it changed nothing.** Identical output on all twelve queries, in every
mode. That looks like a dead code path, so the embedding space itself got measured — all 2,278
chunk pairs:

| Pairs | n | mean cosine |
|---|---|---|
| chunks of the *same* document | 185 | 0.787 |
| chunks of *different* documents | 2,093 | 0.699 |

**Separation: 0.088.** Now the threshold. Over a 15-candidate pool, one rank position is worth
`lam / 15` of relevance; a redundancy gap is worth `(1 - lam)` times its size. For MMR to demote a
near-duplicate by even one position at `lam = 0.7`:

```
0.3 * gap > 0.7/15   →   gap > 0.156
```

The signal available is 0.088. The knob needs 0.156. MMR is not being unhelpful — it is
**structurally unable to act**, by roughly a factor of two.

This is §1.4 returning with a bill. That section explained that unrelated text still scores 0.59
because embeddings occupy a narrow cone rather than the full sphere, and treated it as a
calibration nuisance — the reason the floor is 0.65 instead of a textbook 0.4. The same
anisotropy means *everything is similar to everything*, so "too similar to what I already picked"
is a distinction this space can barely draw. A property that looked like a threshold-tuning
detail turned out to determine whether an entire technique can function.

Solving `(1 - lam) * 0.088 > lam / 15` gives `lam < 0.57`, so rather than concluding from theory,
lower values were run. At `lam = 0.3` MMR finally reorders — and `chunk_hit` drops from 10/11 to
9/11 while MRR slips 0.79 to 0.78. Inert above 0.57, mildly harmful below it. So MMR does not
ship, `DEFAULT_LAM` stays at 1.0, and the code stays in `hybrid.py` because the measurement is
the deliverable.

The upgrade path is not a better `lam`. It is an embedding model with more spread, or a
cross-encoder that scores query-document pairs directly instead of comparing precomputed vectors
— which needs a GPU this project does not have.

## 4.5 Measuring retrieval at all

Every conclusion above depends on the eval set being able to tell configurations apart. Day 8's
could not: 5/5 on hit@1 for all three chunk sizes. That is not a finding about chunking, it is a
finding about a measurement with no resolution left, and the cause was one runbook per topic —
picking the right document out of eleven unrelated ones is too easy to discriminate.

So the twelve queries are composed to be hard: three where OOMKilled, CrashLoopBackOff and the
resource-limits reference genuinely compete; two postmortem-versus-runbook pairs; two exact-token
queries; two paraphrases with near-zero shared vocabulary; two straightforward; and one the corpus
cannot answer. Dense-only scores 8/12 — the set has room to move.

**Relevance is a set, not a label.** Day 10 scored the exit-137 query as a miss for returning
`crashloopbackoff.md`, which is wrong: an OOMKilled pod *presents* as CrashLoopBackOff and the two
runbooks cross-reference each other. Binary single-label scoring cannot express "also correct", so:

```json
{
  "primary": "oomkilled-pod.md",
  "acceptable": ["crashloopbackoff.md", "reference-pod-resource-limits.md"],
  "must_contain": "137"
}
```

`primary` drives `hit@1`. `primary ∪ acceptable` drives `soft_hit@1` and `recall@k`. `MRR` — the
reciprocal of the rank where the first relevant document appears — captures *how far down* an
answer was, which a hit/miss count throws away. And `must_contain` is the **chunk-level** check
the day asks for: not "did the right document appear" but "did a chunk carrying the actual answer
appear", for the cost of one `in` test rather than labelling all 68 chunks.

`primary: null` marks a question with no answer in the corpus, where the only correct behaviour is
refusal. Day 10 asked for these explicitly: the 0.65 floor was calibrated against positives only,
and a threshold set without negatives is a guess.

**Why not nDCG.** The textbook metric grades each document 0–2 and discounts by `log2(rank + 1)`.
It is the right tool at scale. Here it would mean hand-scoring ~36 document-query cells, and on
twelve queries it orders the candidate configurations identically to MRR. It buys precision the
decision does not consume.

## 4.6 What the eval decided, including against its author

| Config | hit@1 | soft@1 | recall@k | MRR | p50 |
|---|---|---|---|---|---|
| dense (Day 10 baseline) | 8/12 | 8/12 | 0.85 | 0.79 | 3ms |
| lexical (BM25 ranking) | 8/12 | **10/12** | **0.88** | **0.88** | 4ms |
| **hybrid (shipped)** | **9/12** | 9/12 | 0.85 | 0.83 | 5ms |
| hybrid + metadata filter | 9/12 | 9/12 | 0.81 | 0.83 | 5ms |

Read that table honestly and lexical wins. It was not shipped, and the reasoning has to be stated
rather than buried: the differences are one to two queries out of twelve, which this eval cannot
resolve, and BM25-only ranking has a structural failure mode — it requires shared vocabulary —
that the eval has exactly two paraphrase queries to observe. Choosing hybrid is a bet that the
under-sampled failure matters more than a one-query lead, ahead of a chat interface where
paraphrase is the norm. The way to settle it is more paraphrase queries, not more argument.

**Metadata filters cost recall and bought nothing.** `where={"doc_type": "postmortem"}` moved
`recall@k` from 0.85 to 0.81 and moved nothing else. The mechanism is obvious afterwards: filtering
to postmortems removes `oomkilled-pod.md`, which is in the query's `acceptable` set. The filter did
exactly what it was told; the instruction was wrong. Both queries already scored 2/2 unfiltered,
because *"what broke in the June checkout outage"* is already specific enough that there was no
ambiguity left to resolve. A filter earns its keep when a query is genuinely ambiguous across doc
types — which Day 12's live alert data is far likelier to produce than eleven hand-written runbooks.

**The floor costs a real answer, by seven thousandths.** *"builds sit queued forever and nothing
ever picks them up"* returns nothing in every mode: best score **0.643** against a floor of 0.65.
The Kafka question — genuinely unanswerable — scores 0.588. That gap, 0.643 against 0.588, is the
entire margin separating "the threshold is slightly wrong" from "the corpus cannot answer this",
and it is the reason `retrieval.py` logs the best score alongside a refusal. Same empty list, two
completely different bugs. Day 10 called 0.65 a guess and asked for calibration; the answer is that
it is about a hundredth too high, and the fix is more negatives to place it against rather than
nudging it until this one query passes.

**And the eval set repeated the mistake it was built to fix.** On the exit-137 query,
`postmortem-2026-06-checkout-oom-outage:1` wins at 0.796, beating `oomkilled-pod:0` at 0.758. That
chunk reads `Code 137). Pod restarts; RESTARTS count begins climbing.` — it is a defensible answer
to the question. It is not in `acceptable`, so it scores as a miss.

This is Day 10's lesson recurring one level up. The fix for binary single-label scoring was
applied, and the replacement label set was still drawn too tightly: a *postmortem of exactly this
failure* was not considered an acceptable answer to a question about exactly this failure. Adding
it would take hybrid to 11/12 on soft@1, which is precisely why it has not been added yet —
changing the labels after seeing the scores is how an eval stops measuring anything. It is a
judgement about what a good answer is, and it belongs in a separate decision from the run that
exposed it.

---

# Part 5 — Live data

## 5.1 What actually changes when the index stops being a folder

Everything up to here was built over eleven markdown files, and those files have three properties
the design leaned on without ever saying so:

1. They change only when a human changes them.
2. They never disappear.
3. "The corpus" and "the collection" are the same set.

Alerts break all three. They appear at 03:00 without asking, they vanish when the condition clears,
and they share a collection with documents they do not own. None of that is a *feature* problem —
the retriever, the chunker, and the grounding contract all work fine on an alert. It is an
*invariant* problem, and the interesting part of this day is that two pieces of already-shipped,
already-tested code were quietly resting on invariants that stopped being true.

Say that more precisely, because it is the transferable lesson: **a cache key and a
reconciliation scope are both statements about what can change.** Write them for data that only
you edit and they are correct. Point the same code at data that edits itself and they are wrong —
not slowly wrong, wrong on the first poll.

## 5.2 The bug that deletes your corpus

Day 9 built `plan_reconcile`, and its contract is simple: given what the index holds and what it
*should* hold, produce adds, updates, and deletes. The deletes come from one line —

```python
plan.to_delete = [cid for cid in existing if cid not in desired_ids]
```

— and `existing` came from `existing_hashes(collection)`, which read **the entire collection**.

That is exactly right while one source owns everything. `load_corpus()` returns every document
there is, so anything in the index and not in that list was deleted from disk, and deleting it from
Chroma is the correct response.

Now add a second source. An alert sync's desired set is three alerts. It contains no runbooks —
not because the runbooks are gone, but because runbooks are not this source's business.
`plan_reconcile` cannot tell those apart. It sees 68 ids it was not asked for and deletes all of
them.

The fix is one parameter:

```python
def existing_hashes(collection, where: dict | None = None) -> dict[str, str]:
    stored = collection.get(include=["metadatas"], where=where or None)
```

threaded through `ingest()`, so the corpus source reconciles against everything and the alert
source reconciles against `{"doc_type": "alert"}`.

What matters is *where* that fix went. The tempting version is a guard inside the alert connector —
"don't delete anything that isn't an alert" — and it would work, for alerts. But the bug is not
"alerts delete runbooks". The bug is that **reconciliation assumed a single owner**, and the next
source to arrive — K8s events, Jenkins builds, PagerDuty incidents — walks into the identical trap
with no guard of its own. Fixing it at the shared function is both the smaller diff and the only
one that covers code nobody has written yet.

## 5.3 Resolution is something you infer, not something you're told

Alertmanager's `/api/v2/alerts` returns alerts that are *currently known*. When a condition clears,
Prometheus sends a resolve, Alertmanager holds the alert for `resolve_timeout` — five minutes by
default, and five minutes on appsrv — and then drops it entirely.

So there is no "what resolved?" endpoint. There is no resolved-alert record to fetch. The only
signal available is **the alert you had is no longer being mentioned**.

That inverts the usual direction of a sync. Normally you ask the source what exists and write it
down. Here you have to diff:

```
desired = live alerts from the API
        + indexed alerts absent from the API, marked resolved, if under 24h old
```

Two consequences follow, and both are load-bearing.

**First: retention needs no code.** An alert older than the window simply isn't in `desired`, and
`plan_reconcile` already deletes what isn't desired. The retention policy is the *absence* of a
document, not a deletion routine, a TTL field, or a cleanup job. This is what reusing Day 9's
machinery bought — the entire 24-hour policy is one `continue` statement.

**Second: an empty response is now dangerous.** If `merge` receives `[]`, it concludes that every
indexed alert resolved simultaneously. For a genuinely quiet cluster that is exactly right. For an
Alertmanager that just 500'd, timed out, or restarted, it is catastrophic and silent.

A failed fetch and a quiet cluster produce *the same input*. The only thing distinguishing them is
whether the request succeeded — a fact that exists at the moment of the call and nowhere
afterwards. So it has to be preserved as control flow rather than flattened into a value:

```python
    except requests.exceptions.RequestException as e:
        raise AlertmanagerError(f"Alertmanager at {url} did not answer: {e}") from e
```

`sync_alerts` deliberately does not catch that. The poll loop does, logs it, and tries again in
sixty seconds having written nothing. This is why `fetch_alerts` returning `[]` on error would be
a data-loss bug rather than a style preference, and why it has a test that asserts the collection
is untouched.

## 5.4 Why the same text has to be rendered twice

A resolved alert cannot be re-fetched, so it is re-rendered from what the index already holds. That
sounds like a detail and produced two of the day's bugs.

**Why the status is in the text at all.** `/ask-runbook` builds its prompt from chunk *text*.
Metadata never reaches the model. An alert whose metadata says `status: resolved` but whose text
still reads `Status: firing` will be reported as an ongoing incident — fluently, in operational
prose, with a citation marker that resolves correctly to a chunk that really was retrieved. Every
grounding check from Day 10 passes. The answer is still false. So `Status:` and `Resolved:` are
part of the document body, and the cost is one re-embed per resolution.

**Why every timestamp is absolute.** The natural way to write an alert is `firing for 47 minutes`.
Do that and the text changes on every poll → `content_hash` changes → `plan_reconcile` classifies
every alert as an update → the entire alert set re-embeds every sixty seconds, forever, against a
CPU-only Ollama. The live payload made the same point about `updatedAt`: it had drifted thirteen
minutes past `startsAt` on an alert that had not otherwise changed. Duration is a display concern.
The document stores the instant; the reader does the subtraction.

**Why metadata is rebuilt from a list rather than copied.** When Chroma hands back a resolved
alert's metadata, that dict contains `content_hash` and `indexed_at`, which `enrich()` stamped on
after the hash was computed. Copy it wholesale into the next document and you are folding a hash
into the input of its own successor. It never settles: every poll sees a change, every poll
re-embeds. So `rebuild()` names the keys it carries — `META_KEYS`, `ANNOTATION_KEYS` — and takes
nothing else.

All three are one mistake wearing three hats: **something that varies leaked into the identity of
something that didn't change.** It is worth learning as a shape rather than three rules, because
the fourth instance will not look like any of them.

## 5.5 The two bugs that only real data found

The unit suite was green — 110 tests, including tests written specifically for churn and
retention. Both of these appeared within twenty minutes of pointing the service at appsrv.

**A resolved alert lost the only sentence worth reading.** `rebuild()` reconstructs the document
from metadata, and annotations weren't stored, so they couldn't be restored. The shipped version
carried a `ponytail:` comment saying so and judging it low-cost. Then a real alert resolved, and
the line that disappeared was:

```
Summary: root filesystem is 79.3% full on 10.0.0.24:9100
```

That is not a nicety, it is the entire informational content of the document. What remained was an
alertname, a severity, and two timestamps — a husk that would still match "NodeDiskSpaceLow" and
tell a reader nothing. The estimate was wrong because it was made about a *shape* ("annotations")
rather than about the *content* ("the number"). Carrying `summary` and `description` in metadata
fixed it in three lines.

**Chroma's `upsert` merges metadata; it does not replace it.**

```python
upsert(ids=["a"], metadatas=[{"status": "resolved", "resolved_at": "T1"}])
upsert(ids=["a"], metadatas=[{"status": "firing"}])
# → {'resolved_at': 'T1', 'status': 'firing'}
```

A key you *stop sending* survives. The retention code read

```python
resolved_at = meta.get("resolved_at") or now.isoformat()
```

which is correct if the key's presence means "this is resolved". Under merge semantics it means
"this was resolved at some point, possibly long ago, possibly followed by re-firing". So a flapping
alert — resolve, re-fire, resolve again — reads back the *first* resolution's timestamp, and if
that is over 24 hours old the alert is deleted the moment it resolves the second time. A
chronically flapping alert becomes the one alert the index refuses to keep.

The fix is to stop trusting presence and start trusting a field that is rewritten every poll:

```python
already_resolved = meta.get("status") == "resolved" and meta.get("resolved_at")
resolved_at = already_resolved or now.isoformat()
```

Note how it was found. A Prometheus restart pushed `NodeDiskSpaceLow` back to `pending`, which
expired it out of Alertmanager, which resolved it in the index — and then its `for: 5m` re-elapsed
and it fired again. Resolve → expire → re-fire, in about four minutes. No unit test had thought to
write that sequence, because writing it requires having watched a real monitoring stack restart.
The tests were not lacking rigour; they were lacking *imagination about time*, which is the thing
production has and fixtures don't.

## 5.6 What it cost the retriever

The concern was concrete: BM25's `df` and `avgdl` are corpus-wide, so any chunk added anywhere
changes the score of every query — including questions about runbooks that have nothing to do with
alerts.

Measured, corpus-only versus corpus plus three live alerts, nothing moved. Not one of hit@1,
soft@1, recall@4, MRR, or chunk-hit changed in any of the six configurations.

The honest reading is **unmeasurable, not absent**. Three chunks against 68 is 4.4% of the corpus,
and twelve queries cannot resolve a shift that small. The mechanism is entirely intact; it just
needs volume to show itself. Reporting this as "adding alerts is free" would be the same error the
Day 8 eval made when it scored 5/5 on every chunk configuration and called it a result about
chunking.

What the run *did* establish is the thing the day was for:

| Question | Top result |
|---|---|
| "is the disk filling up on the node right now" | the live alert, 0.760, ahead of `node-disk-pressure.md` at 0.729 |
| "why do my pods get OOMKilled after a deploy" | runbook, postmortem, reference — no alert anywhere |

Live state ranks first when the question is about now, and stays out of the way when it isn't. And
the grounded answer cited both a runbook and an alert as separate sources, reporting that the disk
was at 79.3% — a number that appears nowhere in the corpus and was thirty seconds old.

That is the difference between a service that knows what OOMKilled means and one that knows what
your infrastructure is doing.

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
| **Top-k** | The *k* highest-scoring chunks a query returns. `k=4` here; it is both a recall and a latency dial |
| **Similarity floor** | Minimum score a chunk needs to be used as context. Below it, the service refuses instead of answering |
| **Grounded** | The answer's every claim came from retrieved text, and every citation resolves to a chunk that was actually in the prompt |
| **Citation marker** | The `[n]` in an answer, referring to chunk *n* of the retrieved set — a per-request handle, not a document ID |
| **Hallucination** | Fluent, confident output that isn't supported by the source. The `[7]` that cites a chunk which was never in the prompt |
| **System prompt** | Instructions given to the model separately from the user's text — here, the rules about citing and refusing |
| **Temperature** | Randomness in generation. 0.1 for near-deterministic operational prose; Ollama reads it from `options`, not the top level |
| **Prompt eval** | The model reading the prompt before generating anything. On CPU this dominates latency and scales with context size |
| **hit@k** | Fraction of queries where the correct document appears in the top *k* results |
| **Anisotropy** | The tendency of embeddings to occupy a narrow cone rather than the full sphere — why unrelated text still scores 0.59, and why MMR cannot act. §4.4 |
| **Dense retrieval** | Ranking by embedding similarity. "Dense" because every one of the 768 dimensions carries a value |
| **Lexical retrieval** | Ranking by which query words appear in the text. Exact, not semantic — `grep` with statistics |
| **Hybrid search** | Running both and combining the rankings, on the grounds that they fail on different questions. §4.1 |
| **BM25** | The standard lexical scoring function: term frequency, saturated; weighted by rarity; corrected for length. §4.2 |
| **tf** | Term frequency — how often a query term appears in this chunk |
| **df** | Document frequency — how many chunks contain the term at all. Low `df` means the term discriminates |
| **idf** | Inverse document frequency, `log(1 + (n - df + 0.5)/(df + 0.5))`. The `1 +` keeps it from going negative on a common term |
| **Saturation** | Capping the reward for repetition, so the tenth mention of a term adds almost nothing. `k1` controls the rate |
| **Length normalization** | Scaling a chunk's score by `dl / avgdl`, so a long chunk can't win on bulk. `b = 0.75` here |
| **RRF** | Reciprocal Rank Fusion — combine rankings as `1/(k + rank)`, using positions because scores from different rankers aren't comparable. §4.3 |
| **Candidate pool** | The over-fetch before final selection. 15 here against `k = 4`, so fusion and reranking have something to choose among |
| **Rescue** | A chunk surfaced by one ranker that the other never returned. Why the fused set is a union; the `137` case. §4.3 |
| **MMR** | Maximal Marginal Relevance — pick greedily, penalising similarity to what's already picked. Measured inert on this corpus. §4.4 |
| **lambda (λ)** | MMR's dial between relevance and variety. `1.0` = pure relevance, i.e. no reranking at all, which is why it's the control. Below `0.57` on this corpus MMR can finally act, and makes things worse |
| **Bi-encoder** | Embedding query and document *separately*, then comparing vectors. Everything in this service is one — which is what makes precomputed vectors and a vector database possible |
| **Cross-encoder** | Feeding query and document through a model *together* and scoring the pair directly. Far more accurate than comparing two vectors, and unusable as a first-stage retriever: nothing can be precomputed, so every candidate costs an inference. The reranker Day 11 wanted and skipped. §4.4 |
| **MRR** | Mean Reciprocal Rank — average of `1/rank` of the first relevant result. Captures *how far down* an answer was, which hit/miss discards |
| **recall@k** | What fraction of the relevant documents made it into the top *k* at all |
| **precision@k** | What fraction of the top *k* results are relevant. The mirror of recall@k: recall asks what was missed, precision asks what was wasted |
| **soft hit@1** | Top result is the primary answer **or** one explicitly marked acceptable — how partial correctness gets expressed. §4.5 |
| **primary / acceptable** | The eval set's two-tier answer key. `primary` is the single best document and drives `hit@1`; `acceptable` is what a competent engineer would also take, and widens `soft hit@1` and `recall@k`. The distinction exists because Day 10 scored a semantically right answer as a miss. §4.5 |
| **must_contain / chunk hit** | A substring that has to appear in some retrieved chunk's text. Doc-level metrics only prove the right *file* was found; this is the cheap way to ask whether the chunk carrying the actual answer came back. §4.5 |
| **nDCG** | The graded-relevance metric with logarithmic rank discounting. Correct at scale; deliberately skipped at twelve queries. §4.5 |
| **Alertmanager** | The component that receives fired alerts from Prometheus and handles grouping, silencing, inhibition, and routing. The authority on alert *state* as an operator experiences it, which is why the connector reads it rather than Prometheus. §5.3 |
| **Fingerprint** | Alertmanager's stable hash over an alert's label set. Two polls of the same alert produce the same fingerprint, which makes it the document id and the whole reason re-syncing embeds nothing. §5.3 |
| **resolve_timeout** | How long Alertmanager keeps an alert after it stops firing — five minutes by default. After that it vanishes from the API entirely, which is why resolution has to be inferred from absence. §5.3 |
| **Silenced / suppressed** | An alert muted at Alertmanager. Still firing; someone chose not to be notified. Rendered as `silenced`, never as `resolved` — the distinction is the difference between "fixed" and "ignored" |
| **Scoped reconcile** | Restricting `plan_reconcile`'s read-back with a metadata filter so a source deletes only documents it owns. The fix for reconciliation having assumed a single owner. §5.2 |
| **Retention window** | The 24 hours a resolved alert stays in the index, so a question at 09:00 still finds what fired at 03:00. Implemented as absence from the desired set, not as a deletion routine. §5.3 |
| **Resolution by absence** | Concluding an alert resolved because a *successful* fetch did not mention it. Requires that a failed fetch be impossible to confuse with an empty one. §5.3 |
| **Metadata merge** | Chroma's `upsert` folds new metadata keys into existing ones rather than replacing the dict, so a key you stop sending survives. The cause of the flapping-alert expiry bug. §5.5 |
| **Alert flapping** | An alert that resolves and re-fires repeatedly. The sequence that no fixture had imagined and that production produced in four minutes. §5.5 |
