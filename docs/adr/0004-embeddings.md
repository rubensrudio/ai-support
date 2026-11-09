# ADR 0004: Embeddings with OpenAI and brute-force cosine search in NumPy

- Status: accepted
- Date: 2026-11-09
- Phase: 04

## Context

Phase 03 grounded the answers with PostgreSQL full-text search, and `demo_03` showed its known
gap: lexical search cannot bridge synonyms. "cancelar plano" does not find the document that
only says "encerrar assinatura", and "cancel plan" does not find "end subscription" (RAG-05,
RAG-16). Customers rarely use the exact wording of the knowledge base, so phase 04 has to
retrieve by meaning: every indexed chunk gets an embedding (RAG-07), the vector search ranks
chunks by cosine similarity and must put the `end-subscription` chunk in the top 5 for
"cancelar plano" and "cancel plan" (RAG-08, RAG-17), and a failure of the embedding service
must never look like a grounded answer (RAG-93, RAG-94).

Forces:

- We need numbers, not impressions (RAG-09): `demo_04` measures recall@5 of keyword search vs
  embeddings on `demos/data/synonym_questions.jsonl` (8 pt + 8 en questions whose words never
  appear in the expected document), and the latency of brute-force search at 10k, 100k and
  1M vectors. Both go to the `| 04 |` row of `docs/results.md`.
- The corpus is small (about 40 documents, a few hundred chunks), so this phase is about
  learning what vector search costs before adding an index (HNSW comes in phase 05).
- CI and tests run offline: no key, no network, deterministic results.
- PostgreSQL 16 stays the only store; the `pgvector` extension is already available.

## Decision

1. **Provider** (DA-19, LAC-19): OpenAI `text-embedding-3-small` with **1536 dimensions**,
   configured by `ATLAS_EMBEDDING_PROVIDER` (`openai` or `fake`), `ATLAS_EMBEDDING_MODEL` and
   `ATLAS_EMBEDDING_DIMENSIONS`. `api/rag/embeddings.py` defines the async
   `EmbeddingClient.embed(texts) -> list[list[float]]` protocol (CT-34). The OpenAI client
   sends batches of at most 100 texts (`MAX_BATCH_SIZE`) and turns every provider failure
   into `EmbeddingError`, whose message never carries the key, the provider response or the
   input text.
2. **Storage** (migration `0005`, `api/rag/models.py`): `chunks.embedding` is a nullable
   `vector(1536)` column, with no vector index in this phase. Ingestion
   (`python -m ingest run`) embeds the chunks of each document in one batched call before
   writing them; when the embedder fails the document is marked `failed` and stays
   reprocessable, so the vector search never sees a chunk without embedding (RAG-93).
   `--skip-embeddings` keeps the phase 03 behaviour.
3. **Search** (`api/rag/vector_bruteforce.py`, CT-35): brute force in **NumPy**. The
   embeddings of the indexed chunks in the question language are loaded into a matrix and
   scored against the query vector with one matrix-vector product (cosine similarity),
   best first, ties by lowest chunk id. The retriever registers it as the `vector_bruteforce`
   mode; a missing embedder, an `EmbeddingError` or a search failure becomes
   `RetrievalUnavailable`, and the chat answers that it cannot reach the knowledge base
   (RAG-94).
4. **Offline embedder**: `FakeEmbeddingClient(dim, synonyms)` hashes each lower-cased word
   (SHA-256 seeds a pseudo-random vector) and returns the normalised sum; an optional
   synonym table maps words to a canonical word first. `demo_04 --provider fake` uses it
   with a small synonym table (`FAKE_SYNONYMS`), so the offline run shows the effect of
   embeddings without pretending to measure them.
5. **Benchmark** (`demos/bench.py`, CT-37, `demo_04`): the latency is measured on synthetic
   unit vectors in memory, with the exact top 5 as ground truth, at **dimension 256** by
   default (`--dim`). 1M × 1536 float32 vectors would need about 6 GB of memory; at 256
   dimensions 1M vectors take about 1 GB. The cost of brute force grows linearly with
   `n × dim`, so the 1536-dimension latency is roughly six times the 256 one. The dimension
   is written in the notes of the `| 04 |` row.

## Alternatives considered

- **Voyage or a local model (sentence-transformers) for embeddings.** Voyage is a strong
  retrieval model but adds another vendor account; a local model avoids the per-token cost
  but makes the API image heavier for the PaaS deploy and slower on CPU. OpenAI
  `text-embedding-3-small` reuses the account of the secondary provider, is cheap and has
  configurable dimensions (DA-19). Changing it later means new `ATLAS_EMBEDDING_*` values and
  a re-ingestion with `python -m ingest run --reembed` (LAC-19).
- **pgvector distance operators (`<=>`) without an index.** Also exact, and it avoids moving
  the vectors to Python, but it hides the cost we want to see. NumPy keeps the brute force
  explicit and comparable with the synthetic benchmark; phase 05 moves the search into
  PostgreSQL with HNSW and compares both.
- **An approximate index (HNSW, IVFFlat) right away.** Faster at scale, but with a recall
  trade-off that can only be judged against an exact baseline. Phase 04 is that baseline.
- **A dedicated vector database.** Another service to run and keep in sync for a few hundred
  chunks; `pgvector` keeps one database.
- **Benchmarking at 1536 dimensions.** Faithful to production, but 1M vectors would not fit
  in the memory of a typical laptop or CI runner next to the exact ground truth.

## Consequences

- Synonym questions now find the right document: on the synonym set, recall@5 of embeddings
  is compared with full-text in `docs/results.md`. With `--provider fake` the run is offline
  and only demonstrates the mechanism; **the real numbers are pending an OpenAI key**
  (`uv run python -m demos.demo_04_embeddings --provider real`).
- Every ingestion and every vector query costs an embedding call; a failure of the provider
  makes vector retrieval unavailable instead of silently degrading to an ungrounded answer.
- Brute force loads all chunk vectors of a language per query: fine for hundreds of chunks,
  linear in `n × dim`, and tens of milliseconds per query at 1M vectors in memory, before
  the cost of reading them from the database. This is what phase 05 (HNSW) addresses.
- Vectors from different embedders are not comparable: switching the provider, the model or
  the dimension needs `python -m ingest run --reembed`, which recomputes the embedding of
  every chunk of the manifest. A plain `python -m ingest run` only fills chunks whose
  embedding is null, so the old vectors would survive and mix with the new ones. `demo_04`
  checks that the stored vectors come from its embedder and refuses to measure otherwise
  (exit 2, suggesting the demo's `--reembed` or `python -m ingest run --reembed`).
- Risk: `demo_04 --provider fake` stores fake vectors in the configured database (on an
  empty index, or with `--reembed`), and the chat in a vector mode would then search them.
  Run the fake demo with `ATLAS_DATABASE_URL` pointing to a throwaway database; if the main
  database got fake vectors, restore it with `python -m ingest run --reembed`.
- Full-text search stays available (`fulltext` mode); phase 05 combines both with
  Reciprocal Rank Fusion.
