# Progress

> **This file is the resume point.** If you are a session with no prior context, read this first,
> then `docs/DECISIONS.md` for *why*, then the document matching the work you are picking up.
>
> **Maintenance rule:** update this file in the same pass that completes the work — never later.
> A stale progress file is worse than no progress file, because it actively misleads the next
> session into redoing finished work or skipping unfinished work.

---

## Current state

**Cycles 0 and 1 are done.** Cycle 0's scaffold, config, logging, errors, app factory and health
checks are built and verified against real Postgres. Cycle 1's corpus (64 documents, 224 chunks),
hybrid retrieval, and reranking are built and verified against a **real, live Pinecone Starter
account** — indexes created, all 224 chunks ingested into both the dense and sparse indexes across
6 namespaces, idempotency confirmed (a second `python -m scripts.ingest` run re-embeds zero chunks),
RBAC access-level filtering confirmed across roles, and reranking confirmed end to end including its
Postgres-backed budget counter. See the Cycle 1 checklist below and the session log for what that
verification found and fixed.

**Next action:** get a LangSmith Developer API key and install Ollama (`ollama pull qwen3:4b`; no
payment method / credit card on either — see `docs/SETUP.md`), then start **Cycle 2 — Auth, RBAC,
rate limiting**. Cycle 2 itself needs no external accounts; it can also start immediately and the
Ollama/LangSmith prerequisites resolved before Cycle 3.

---

## Blocked on — external prerequisites

These are user-side actions. Full instructions in `docs/SETUP.md`.

| Prerequisite | Needed by | Status |
| --- | --- | --- |
| Ollama installed + `ollama pull qwen3:4b` | Cycle 3 | ⬜ not done |
| Pinecone Starter account, API key, **no payment method attached** | Cycle 1 | ✅ done |
| LangSmith Developer account, API key, **no credit card attached** | Cycle 3 | ⬜ not done |
| `.env` populated from `.env.example` | Cycle 1 | ✅ done (Pinecone key set; DB_* left at defaults) |

---

## Cycle status

| # | Cycle | Est. | Status |
| --- | --- | --- | --- |
| 0 | Foundation scaffold | 2h | ✅ done |
| 1 | Corpus and hybrid retrieval | 4h | ✅ done |
| 2 | Auth, RBAC, rate limiting | 2.5h | ⬜ pending |
| 3 | LangGraph core, memory, streaming | 5h | ⬜ pending |
| 4 | Tools and RBAC enforcement | 3h | ⬜ pending |
| 5 | RLM research agent | 4h | ⬜ pending |
| 6 | Guardrails and validation | 2.5h | ⬜ pending |
| 7 | Frontend, observability, docs | 4h | ⬜ pending |

Legend: ⬜ pending · 🟡 in progress · ✅ done

---

## Component checklists

Tick each component as it lands, so a context-wiped session can tell **how far into** a cycle the
work got — not merely whether the cycle started.

### Cycle 0 — Foundation scaffold ✅

- [x] Directory structure (`backend/app/...`, `mcp_server/`, `frontend/`, `data/`, `scripts/`, `tests/`)
      — future-cycle packages exist as `__init__.py` stubs naming the cycle that fills them in.
- [x] `requirements.txt` (+ `requirements-dev.txt`) — exact pins for the whole build, grouped by cycle.
- [x] `.env.example` with every variable
- [x] `pyproject.toml` — ruff, mypy (strict), pytest configuration
- [x] `backend/app/core/config.py` — pydantic-settings; enforces the rerank-budget cost guard in code
- [x] `backend/app/core/logging.py` — structlog JSON (console in dev) + correlation IDs
- [x] `backend/app/core/errors.py` — exception hierarchy + FastAPI handlers
- [x] `backend/app/main.py` — app factory + lifespan
- [x] `backend/app/api/v1/health.py` — liveness/readiness (readiness pings Postgres)
- [x] `docker-compose.yml` — Postgres only
- [x] `CLAUDE.md` dev-commands section filled in with real commands
- [x] `tests/` — 12 tests covering the rerank-budget validator, the exception→JSON envelope
      mapping, and both readiness branches; `pytest`/`ruff`/`mypy --strict` all pass clean.

### Cycle 1 — Corpus and hybrid retrieval ✅

- [x] Seed corpus generator (64 docs: 18 incidents, 10 runbooks, 9 architecture, 9 product specs,
      9 policies, 9 meeting notes — `scripts/generate_seed_corpus.py`, deterministic/seeded).
      Payment-failure incidents deliberately draw from 5 recurring root causes for the RLM demo.
- [x] Metadata schema: `department`, `document_type`, `access_level`, `created_date`
      (`retrieval/models.py`)
- [x] Structure-aware chunking preserving document + section attribution (`retrieval/chunking.py`;
      224 chunks from 64 documents)
- [x] Content-hashed **idempotent** ingestion (`retrieval/ingest.py` + `ingested_chunks` table) —
      verified live: a second `python -m scripts.ingest` run re-embeds 0 of 224 chunks
- [x] Pinecone dense index (`llama-text-embed-v2`) with namespaces — created and populated live
- [x] Pinecone sparse index (`pinecone-sparse-english-v0`) with namespaces — created and populated live
- [x] `retrieval/hybrid.py` — concurrent dense+sparse fan-out, RRF fusion; verified live against
      the ingested corpus
- [x] `retrieval/reranker.py` — `bge-reranker-v2-m3` allowlisted (blocklist checked against the
      SDK's real enum value, `cohere-rerank-3.5`), Postgres-backed monthly budget guard; verified
      live end to end including the counter
- [x] `access_level` filter derived from the caller's role (`allowed_access_levels()` in
      `retrieval/models.py`); verified live that a viewer query excludes confidential chunks an
      analyst query returns
- [x] `tests/retrieval/` — 30 tests: access-level policy, chunk identity/idempotency, chunking
      (including malformed front matter and long-section splitting), RRF fusion, hybrid-search
      degradation, and the reranker's allowlist + budget guard

### Cycle 2 — Auth, RBAC, rate limiting ⬜

- [ ] Static user store with hashed passwords
- [ ] JWT issue + verify
- [ ] `Principal` model, request-scoped context
- [ ] Role→permission matrix (Viewer / Analyst / Administrator)
- [ ] FastAPI dependencies (`current_principal`)
- [ ] Async per-user token-bucket limiter, configurable thresholds, graceful 429

### Cycle 3 — LangGraph core, memory, streaming ⬜

- [ ] `agents/state.py` — typed `AgentState` with merge reducers
- [ ] Supervisor / Retrieval / Response / Validator nodes
- [ ] Conditional edges + bounded validator→response retry loop
- [ ] `AsyncPostgresSaver` checkpointer, thread per session
- [ ] Rolling-summary session memory
- [ ] `llm/provider.py` protocol + `llm/ollama_provider.py` (JSON-schema-constrained output)
- [ ] `llm/chain.py` — fallback chain + circuit breaker
- [ ] SSE endpoint streaming typed activity events

### Cycle 4 — Tools and RBAC enforcement ⬜

- [ ] `tools/registry.py` — role filtering at bind time **and** re-check at execution boundary
- [ ] `knowledge_search` tool
- [ ] `python_analysis` tool (reuses the Cycle 5 sandbox)
- [ ] `mcp_server/` — FastMCP: employee directory, service catalog, incident records
- [ ] Async MCP client with timeout + failure handling

### Cycle 5 — RLM research agent ⬜

- [ ] `rlm/sandbox.py` — AST allowlist, stripped builtins, no imports/dunders/IO, wall-clock timeout
- [ ] `rlm/api.py` — `search` / `filter` / `batch` / `sub_agent` / `aggregate`
- [ ] `rlm/planner.py` — schema-constrained Python plan generation
- [ ] `rlm/executor.py` — recursion depth + fan-out caps, bounded semaphore
- [ ] Result aggregator
- [ ] Deterministic fallback plan when generated code fails validation

### Cycle 6 — Guardrails and validation ⬜

- [ ] Prompt-injection detection (heuristics + classifier)
- [ ] Untrusted-data framing for retrieved content
- [ ] Input + tool-parameter validation
- [ ] Citation verification against retrieved chunk IDs
- [ ] Commercial-bank brand/persona guardrail
- [ ] Bounded validator retry loop with feedback

### Cycle 7 — Frontend, observability, docs ⬜

- [ ] Streamlit chat UI, multi-turn, streaming
- [ ] Agent Activity Panel consuming SSE events
- [ ] LangSmith tracing wired with run metadata
- [ ] `README.md` finalised
- [ ] Architecture diagram exported
- [ ] `docs/ASSUMPTIONS_AND_TRADEOFFS.md` completed
- [ ] Model selection + memory design rationale written

---

## Deliverables checklist

From `ASSESSMENT.md`. Tracked separately because these are graded independently of the code.

- [ ] Public source repository
- [ ] Architecture diagram
- [ ] Demo video (45 min), public URL
- [ ] LangSmith traces shown in the demo — **record within 14 days** of the traced run (free-tier retention)
- [ ] Assumptions and trade-offs presented in the demo

---

## Session log

Newest first. One line per meaningful change.

- **2026-09-05** — Built and verified Cycle 1 against a real Pinecone Starter account (key added
  to `.env`). Verified Pinecone's v10 SDK by introspecting the installed package rather than
  recalling its API — a major-version client with a materially different surface than older
  versions — which caught three mistakes before they shipped: `search()`'s `inputs`/`top_k`/
  `filter` are top-level kwargs, not nested under `query=`; hits expose `.id`/`.score`, not
  `"_id"`/`"_score"`; and the billed rerank model's real string is `cohere-rerank-3.5`, not
  `cohere-rerank-v3.5` as `docs/DECISIONS.md` had it — corrected everywhere. Also found and fixed
  a second, script-shaped instance of Cycle 0's Windows event-loop bug (`scripts/ingest.py` has no
  `--loop` flag to reach for; added `install_selector_event_loop_policy()` to `core/loop.py` for
  plain scripts), and a real SQLAlchemy gotcha where `RerankUsage`'s table was never created
  because nothing had imported its module yet (`create_all_tables()` now imports every ORM module
  itself). Full pipeline — corpus generation, chunking, idempotent ingestion, hybrid search, RBAC
  filtering, reranking with its budget counter — exercised live end to end, not just unit-tested.
- **2026-09-05** — Changed Postgres connection config from a single `DATABASE_URL` to five
  granular `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` fields at the user's request;
  `docker-compose.yml` now provisions its container from the same `.env` names. Found and fixed a
  related bug: pydantic-settings treated a blank `.env` value as an explicit empty string rather
  than "unset", which would have broken the DSN and silently turned blank secrets into
  `SecretStr("")` instead of `None` — fixed with `env_ignore_empty=True`.

- **2026-09-05** — Built and verified Cycle 0 (scaffold, config, structured logging, exception
  hierarchy, app factory, health endpoints, `docker-compose.yml`, 12 passing tests). Verification
  against a real Postgres container surfaced two environment-specific findings, both fixed and
  recorded in `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-offs 8–9: psycopg's async mode cannot run
  on Windows' default `ProactorEventLoop` (fixed with an explicit uvicorn `--loop` factory in
  `backend/app/core/loop.py`, since the bug was otherwise masked by `--reload` and would have
  resurfaced at the worst time); and a pre-existing native Postgres service on this machine was
  shadowing the Docker container on the default port 5432 (fixed by publishing on 5433 instead).
- **2026-09-05** — Created the documentation set (`PROGRESS`, `DECISIONS`, `ARCHITECTURE`,
  `DELIVERY_PLAN`, `SETUP`, `ASSUMPTIONS_AND_TRADEOFFS`, `README`, `.gitignore`) and rewrote
  `CLAUDE.md` as the context-wipe recovery entry point. No application code yet.
- **2026-09-05** — Verified Pinecone Starter and LangSmith Developer free-tier limits against live
  pricing pages; found that `cohere-rerank-3.5` bills on first call and pinned the reranker to
  `bge-reranker-v2-m3` behind an allowlist.
- **2026-09-05** — Measured hardware (RTX 3050, 4 GB VRAM) and settled on a single local `qwen3:4b`
  model for every graph node to avoid model-eviction thrash.
- **2026-09-05** — Architecture, stack and 8-cycle delivery plan agreed.
