# ADR 0005: pgvector HNSW index and hybrid search with Reciprocal Rank Fusion

- Status: accepted
- Date: 2026-11-09
- Phase: 05

## Context

Phase 04 retrieved by meaning with an exact brute-force search in NumPy (ADR 0004): every
query loads all chunk vectors of the question language and scores them, which is linear in
`n × dim`. `demo_04` measured tens of milliseconds per query at 1M vectors in memory, before
the cost of reading them from the database. Phase 05 has to make the vector search
sub-linear inside PostgreSQL and show the price in recall (RAG-10, RAG-11).

Embeddings also have a known weakness: they blur exact terms (product names, codes, plan
names) that full-text search matches precisely, while full-text misses synonyms. The phase
adds a hybrid search that combines both rankings (RAG-12, RAG-13). The source document asks
for a "bonus"; by premise LAC-18 that bonus is the hybrid search only, without a reranker.

Forces:

- We need numbers (RAG-11): `demo_05` measures p50/p95 latency and recall@5 of brute force
  vs HNSW over 1M synthetic vectors, and the recall@5 of the hybrid search on the synonym
  questions of `demo_04`. Both go to the `| 05 |` row of `docs/results.md`.
- The HNSW parameters must change by configuration, without code changes (RAG-10).
- Every earlier behaviour stays selectable (DA-16): `ATLAS_RETRIEVAL_MODE` keeps `fulltext`
  and `vector_bruteforce` next to the new modes.
- PostgreSQL 16 stays the only store; the `pgvector/pgvector:pg16` image ships pgvector 0.8.7.

## Decision

1. **Index** (migration `0006`, `api.rag.models.CHUNKS_HNSW_INDEX`): `ix_chunks_embedding_hnsw`
   is `hnsw (embedding vector_cosine_ops) WITH (m = ATLAS_HNSW_M, ef_construction =
   ATLAS_HNSW_EF_CONSTRUCTION)` on `chunks`, defaults `m = 16`, `ef_construction = 64`. The
   values are read when the index is built; `python -m ingest reindex-hnsw`
   (`ingest/reindex.py`) drops and recreates it with the current configuration in one
   transaction, so a failed build keeps the previous index.
2. **Search** (`api/rag/vector_hnsw.py`, CT-35, mode `vector_hnsw`): `ORDER BY embedding <=>
   :q LIMIT k` over the indexed chunks of the question language, score `1 - cosine distance`.
   Each search sets `SET LOCAL hnsw.ef_search = ATLAS_HNSW_EF_SEARCH` (default 40) and
   `hnsw.iterative_scan = strict_order`, so the language and status filters applied after the
   index scan still return `k` rows in exact distance order. Both settings end with the
   transaction and never leak into pooled connections.
3. **Hybrid** (`api/rag/hybrid.py`, CT-36, mode `hybrid`, now the default
   `ATLAS_RETRIEVAL_MODE`): `search_fulltext` and `search_hnsw` each return `2k` candidates,
   fused with **Reciprocal Rank Fusion**: a chunk at rank `r` contributes `1 / (rrf_k + r)`
   and the contributions are summed, with **`rrf_k = 60`** (`ATLAS_RRF_K`). Only positions
   count, so the incomparable scores of `ts_rank` and cosine never need normalising. No
   reranker (LAC-18).
4. **Trade-off of the parameters**: `m` (links per node) and `ef_construction` (candidate list
   while building) raise recall and the index size and build time; `ef_search` (candidate list
   per query) raises recall and query latency, and can be changed at any time without a
   rebuild. `m` and `ef_construction` need `reindex-hnsw` after a change.
5. **Benchmark** (`demo_05`, `demos/bench.py`, CT-37): `bench_vectors(id bigserial, embedding
   vector(256))` from migration `0006`, without index. The demo loads `--size` synthetic unit
   vectors (default 1M) with `COPY`, builds its own cosine HNSW index on that table and times
   the build, and compares NumPy brute force in memory with HNSW through SQL, both against the
   exact top 5. The HNSW queries run in one transaction with `SET LOCAL hnsw.ef_search` and
   `SET LOCAL enable_seqscan = off` (on small tables the planner would pick an exact sort),
   and `EXPLAIN` confirms the index scan. At the end the demo drops its index and truncates
   the table; it never touches `ix_chunks_embedding_hnsw`. The benchmark dimension is
   **256 by decision of the user** (the plan said 1536): 1M × 1536 vectors take about 6 GB
   plus the index, 1M × 256 about 1 GB. Production chunks keep 1536 dimensions.

## Alternatives considered

- **Keep brute force.** Exact and simple, fine for a few hundred chunks, but linear in
  `n × dim` and it moves every vector to Python per query. It stays available as
  `vector_bruteforce` and is the baseline of `demo_05`.
- **IVFFlat.** Smaller and faster to build, but it needs training data before the build
  (lists chosen from existing rows), loses recall as data drifts and needs `probes` tuning.
  HNSW builds incrementally on an empty table and has a better speed/recall curve.
- **A dedicated vector database (Qdrant, Pinecone, Weaviate).** Better at very large scale,
  but another service to deploy and keep in sync with PostgreSQL, and no join with the
  document status or language. pgvector keeps one database and one transaction.
- **Score normalisation (weighted sum of min-max scores) instead of RRF.** Needs weights tuned
  per corpus and is sensitive to outliers of `ts_rank`; RRF needs only `rrf_k`, and 60 is the
  usual value from the original paper.
- **A cross-encoder reranker after the fusion.** Better precision, but another model call per
  question with its latency and cost; out of scope by LAC-18 and left for later.
- **Benchmarking at 1536 dimensions.** Faithful to production, but too large for a laptop or
  CI runner next to the ground truth (see ADR 0004).

## Consequences

- Vector queries no longer scan every chunk: latency grows roughly with `log n` instead of
  `n`, at the price of an approximate recall that `demo_05` reports next to the exact baseline.
  With `--provider fake` the recall on the synonym questions is a demonstration; **the real
  numbers are pending a run with `--provider real`**
  (`uv run python -m demos.demo_05_hnsw --provider real`). The HNSW benchmark does not depend
  on the provider.
- The hybrid search finds both the exact terms (full-text) and the synonyms (embeddings) and
  is now the default mode; every question costs one embedding call plus two queries.
- On the current corpus (a few hundred chunks) the planner may answer the vector query with
  the language index and an exact sort instead of HNSW: the ranking is the same and
  `ef_search` only matters from a few thousand chunks per language.
- The index costs memory and write time on ingestion, and building it over 1M rows is slow
  when the graph does not fit in `maintenance_work_mem` (`demo_05 --maintenance-work-mem`).
- Changing `ATLAS_HNSW_M` or `ATLAS_HNSW_EF_CONSTRUCTION` has no effect until
  `python -m ingest reindex-hnsw`; changing `ATLAS_HNSW_EF_SEARCH` or `ATLAS_RRF_K` takes
  effect on the next query.
- Risk: `demo_05 --provider fake` stores fake vectors in the configured database, like
  `demo_04`; run it against a throwaway database or restore with
  `python -m ingest run --reembed`.
