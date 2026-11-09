# ADR 0003: Retrieval-augmented answers with PostgreSQL full-text search

- Status: accepted
- Date: 2026-11-09
- Phase: 03

## Context

Up to phase 02 Atlas answered from the model's own knowledge plus the conversation history.
For questions about Assinaflow (plans, refunds, invoices, ending a subscription) the model has
no reliable source, so it either guesses or refuses. Phase 03 has to ground the answers in a
bilingual knowledge base (`corpus/en/`, `corpus/pt/`, Markdown and PDF, about 20 documents
per language) and make every grounded statement traceable to its source (RAG-01 to RAG-04,
RAG-14, RAG-15).

Forces:

- The product is bilingual (pt and en): a Portuguese question must be matched with
  Portuguese stemming and stop words, and must not pull English chunks (RAG-15).
- PostgreSQL 16 is already the system of record; phase 03 should not add a new store.
- The answer must say where each statement comes from, so a customer or a reviewer can check
  it, and a question the corpus does not cover must get "I don't know" (RAG-04, RAG-90).
- We need a number, not an impression: `demo_03` measures the hallucination rate of 30
  reference questions (15 pt + 15 en) with and without retrieval, using an LLM judge and a
  manual review sample (RAG-06, RAG-18, RAG-19). The rates per language are in the `| 03 |`
  row of `docs/results.md`, and the sample is in `docs/review/phase03_sample.csv`.

## Decision

1. **Ingestion** (`python -m ingest run`, `ingest/`): Markdown is split by headings (the
   heading becomes the chunk `section`) and PDF by page (`page`). The chunker builds windows
   of about **500 tokens with an 80-token overlap** (named constants in `ingest/chunking.py`,
   tokens counted with `api.llm.tokens`) and never splits a section that fits whole. Each
   document × language is ingested in its own transaction, so one unreadable file is reported
   as `failed` without stopping the others, and re-ingesting replaces the chunks of a
   `(doc_key, language)` instead of duplicating them. The run writes an `IngestReport`.
2. **Storage** (`api/rag/models.py`, migration `0004`): `documents` and `chunks` tables.
   `chunks.ts_config` is a `regconfig` (`english` or `portuguese`, from the chunk language)
   and `chunks.tsv` is a **generated `tsvector` column**, `to_tsvector(ts_config, content)`,
   with one GIN index (DA-13). The linguistic analysis follows the language of each chunk
   with a single column and a single index.
3. **Search** (`api/rag/fulltext.py`): `websearch_to_tsquery(<config of the question
   language>, query)` ranked with `ts_rank_cd`, filtered to chunks of the question language
   from `indexed` documents.
4. **Retriever facade** (`api/rag/retriever.py`): `Retriever.retrieve(query, language, k,
   mode)` dispatches on `ATLAS_RETRIEVAL_MODE` through a mode registry (`none` and `fulltext`
   now; vector and hybrid modes register themselves in later phases). Any search failure
   becomes `RetrievalUnavailable`, which the chat turns into the fixed
   `KNOWLEDGE_UNAVAILABLE` reply without calling the LLM. An empty index returns no chunks.
5. **Prompt and citations** (`api/chat/prompt_builder.py`, `api/rag/citations.py`): the
   top `ATLAS_RETRIEVAL_TOP_K` chunks go to the model numbered `[1]..[k]` with their title
   and section or page. `prompts/system.md` asks for a **citation marker `[n]`** after each
   grounded statement and for "I don't know" when the excerpts do not support an answer.
   `extract_citations` maps the markers back to `Citation(index, document, section, page)`,
   dropping indices that were not given, and the chat response carries them.
6. **Measurement** (`evals/judge.py`, `prompts/judge_faithfulness.md`, `demo_03`): an
   LLM-as-judge with a faithfulness rubric (every factual claim must be supported by an
   excerpt) and a structured verdict `{faithful, score, rationale}`. Invalid judge output is
   retried once and then counts as unfaithful. The judge is also the `faithfulness` grader
   of the eval runner.

## Alternatives considered

- **Vector search with embeddings first.** It handles synonyms, but it adds an embedding
  provider, a vector column and a cost per document and per query. Starting with full-text
  gives a baseline and shows the problem embeddings solve: `demo_03` proves that
  "cancelar plano" does not find the chunk that only says "encerrar assinatura", and that
  "cancel plan" does not find "end subscription" (RAG-05, RAG-16). Embeddings come in
  phase 04 and are measured against this baseline.
- **Two `tsvector` columns (one per language) or one config for everything.** Two columns
  double the index and force every query to pick a column; a single `simple` or `english`
  config would break Portuguese stemming ("assinaturas" vs "assinatura"). The generated
  column with a per-row `regconfig` keeps one index and correct analysis for both languages.
- **An external search engine (Elasticsearch, OpenSearch, Meilisearch).** Better ranking
  and analyzers, but another service to run, secure and keep in sync, for a corpus of about
  40 documents. PostgreSQL full-text is enough at this size and keeps one database.
- **Fixed-size chunks with no overlap, or whole documents.** No overlap cuts answers in half
  at chunk borders; whole documents waste the context window and dilute ranking. About 500
  tokens with 80 of overlap keeps a policy rule and its exceptions together in most chunks.
- **Citations as a JSON field produced by the model (tool or structured output).** It would
  make parsing trivial, but it changes the streaming contract of the chat and couples the
  answer text to a schema. Inline `[n]` markers stream naturally, read well in the UI, and
  unknown indices are simply dropped.
- **Exact-match or keyword overlap to measure hallucination.** Cheap and deterministic, but
  it cannot tell a paraphrase from an invented rule. An LLM judge with a written rubric,
  checked by a manual review sample, measures what we care about.

## Consequences

- Answers about Assinaflow are grounded and cite their sources; questions outside the corpus
  get "I don't know" instead of a guess. The hallucination rates before and after retrieval
  are tracked per language in `docs/results.md`.
- Lexical search misses synonyms and paraphrases ("cancel plan" vs "end subscription"),
  in both languages. This is the known gap that phase 04 addresses with embeddings.
- `websearch_to_tsquery` combines terms with AND, so long or chatty questions can miss
  chunks that a shorter query would find; ranking quality depends on wording.
- The judge is itself an LLM: its verdicts cost tokens and can be wrong. The manual review
  sample (`docs/review/phase03_sample.csv`) records how far the judge agrees with a human,
  and the sample size is reported next to the rates.
- Ingestion must be rerun when the corpus changes (`python -m ingest run`); there is no
  automatic sync. Prompt-injection hardening of excerpts is deferred to phase 10, and the
  prompt order is not yet optimized for caching (phase 11).
