# Progress

> **This file is the resume point.** If you are a session with no prior context, read this first,
> then `docs/DECISIONS.md` for *why*, then the document matching the work you are picking up.
>
> **Maintenance rule:** update this file in the same pass that completes the work — never later.
> A stale progress file is worse than no progress file, because it actively misleads the next
> session into redoing finished work or skipping unfinished work.

---

## Current state

**Cycles 0, 1 and 2 are done.** Cycle 0's scaffold, config, logging, errors, app factory and health
checks are built and verified against real Postgres. Cycle 1's corpus (64 documents, 224 chunks),
hybrid retrieval, and reranking are built and verified against a **real, live Pinecone Starter
account** — indexes created, all 224 chunks ingested into both the dense and sparse indexes across
6 namespaces, idempotency confirmed (a second `python -m scripts.ingest` run re-embeds zero chunks),
RBAC access-level filtering confirmed across roles, and reranking confirmed end to end including its
Postgres-backed budget counter. Cycle 2's static user store, JWT issue/verify, the
`Principal`/role→permission matrix, FastAPI dependency wiring, and the Postgres-backed token-bucket
rate limiter are built and verified against a real, live Postgres container end to end — login for
all three roles, `GET /api/v1/auth/me` resolving a bearer token back to its principal, a
`require_permission`-gated route returning 403 for a viewer and 200 for an administrator, and 20
real requests against a `RATE_LIMIT_CAPACITY=20` bucket followed by two real 429s with a correct
`retry_after_seconds`, confirmed by reading `rate_limit_buckets` directly in Postgres. See the
Cycle 2 checklist below and the session log for what that verification found.

**Next action:** get a LangSmith Developer API key and install Ollama (`ollama pull qwen3:4b`; no
payment method / credit card on either — see `docs/SETUP.md`), then start **Cycle 3 — LangGraph
core, memory, streaming**. It is the largest cycle and needs Ollama, `qwen3:4b`, a LangSmith key,
and Postgres running — resolve the two remaining external prerequisites before starting it.

---

## Blocked on — external prerequisites

These are user-side actions. Full instructions in `docs/SETUP.md`.

| Prerequisite | Needed by | Status |
| --- | --- | --- |
| Ollama installed + `ollama pull qwen3:4b` | Cycle 3 | ⬜ not done |
| Pinecone Starter account, API key, **no payment method attached** | Cycle 1 | ✅ done |
| LangSmith Developer account, API key, **no credit card attached** | Cycle 3 | ⬜ not done |
| `.env` populated from `.env.example` | Cycle 1 | ✅ done (Pinecone key set; DB_* left at defaults) |
| `JWT_SECRET_KEY` generated (`openssl rand -hex 32`) | Cycle 2 | ✅ done |

---

## Cycle status

| # | Cycle | Est. | Status |
| --- | --- | --- | --- |
| 0 | Foundation scaffold | 2h | ✅ done |
| 1 | Corpus and hybrid retrieval | 4h | ✅ done |
| 2 | Auth, RBAC, rate limiting | 2.5h | ✅ done |
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

### Cycle 2 — Auth, RBAC, rate limiting ✅

- [x] Static user store with hashed passwords (`core/security/users.py` — bcrypt, one account
      per role: `viewer` / `analyst` / `admin`, credentials in `docs/SETUP.md`)
- [x] JWT issue + verify (`core/security/jwt.py` — HS256, `ConfigurationError` if
      `JWT_SECRET_KEY` is unset, every verification failure collapses to one
      `AuthenticationError` so a caller can't distinguish bad-signature from expired from
      malformed)
- [x] `Principal` model, request-scoped context (`core/security/rbac.py`; constructed in
      exactly one place, `api/deps.py::current_principal`, from a verified JWT claim)
- [x] Role→permission matrix (Viewer / Analyst / Administrator) — `core/security/rbac.py`,
      Administrator defined as `frozenset(Permission)` so it is structurally "every
      permission" rather than an enumerated list that could drift
- [x] FastAPI dependencies (`api/deps.py`: `current_principal` -> `enforce_rate_limit` ->
      `require_permission(...)`, composed in that fixed order) and `api/v1/auth.py`
      (`POST /auth/login`, `GET /auth/me`)
- [x] Async per-user token-bucket limiter, configurable thresholds, graceful 429
      (`core/security/rate_limit.py` — Postgres-backed so a bucket survives an app restart;
      pure refill math split out from the async DB shell for unit testing, same pattern as
      `retrieval/reranker.py`'s budget guard)
- [x] `tests/core/security/` (rbac, users, jwt, rate_limit) + `tests/api/` (auth, deps) —
      46 new tests; verified live end to end against a real Postgres container: login for all
      three roles, `/auth/me` round-tripping a token back to its principal, a
      `require_permission`-gated route returning 403 for a viewer, and a real
      `RATE_LIMIT_CAPACITY=20` bucket allowing 20 requests then returning two real 429s with
      `retry_after_seconds`, confirmed by reading `rate_limit_buckets` directly in Postgres.

`docs/ARCHITECTURE.md`'s folder structure lists `api/v1/admin.py` for "admin-only operations",
but nothing admin-only exists to expose yet — no tools, no graph, no MCP client. Deferred to
whichever later cycle first has real admin-only functionality (a Cycle 4 tool-registry
inspection endpoint is the likely candidate), rather than shipping an empty placeholder file
now. `require_permission(Permission.ADMIN_TOOLS)` is already built and tested in `api/deps.py`
so that cycle only needs to write the route, not the authorization plumbing under it.

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

- **2026-09-05** — Built and verified Cycle 2 (auth, RBAC, rate limiting). `bcrypt`, `pyjwt` and
  `freezegun` were pinned in `requirements.txt`/`requirements-dev.txt` since Cycle 0 but not yet
  installed in the venv — installing them surfaced no code issues, just confirmed the pins were
  already correct. `RateLimiter` splits a pure, exhaustively-unit-tested refill function from a
  thin async Postgres shell (row-locked via `SELECT ... FOR UPDATE` in the same transaction as
  the write, seeded via `ON CONFLICT DO NOTHING` so a brand-new user's first two concurrent
  requests can't race each other into inserting the same primary key), mirroring
  `retrieval/reranker.py`'s split between atomic DB primitive and business logic. Generated a
  real `JWT_SECRET_KEY` (`openssl rand -hex 32`) and ran the whole stack against the live
  Postgres container: login for all three demo roles, `/auth/me` resolving a bearer token,
  a `require_permission`-gated test route returning 403 for a viewer and 200 for an
  administrator, and 20 real requests against `RATE_LIMIT_CAPACITY=20` followed by two genuine
  429s with correct `retry_after_seconds` — confirmed by reading `rate_limit_buckets` directly
  in Postgres, then cleared that row so the demo user isn't left pre-throttled. `ruff`, `ruff
  format`, `mypy --strict` and all 91 tests (46 new) pass clean.
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
