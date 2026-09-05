# Progress

> **This file is the resume point.** If you are a session with no prior context, read this first,
> then `docs/DECISIONS.md` for *why*, then the document matching the work you are picking up.
>
> **Maintenance rule:** update this file in the same pass that completes the work — never later.
> A stale progress file is worse than no progress file, because it actively misleads the next
> session into redoing finished work or skipping unfinished work.

---

## Current state

**Cycle 0 is done.** The scaffold, config, structured logging, the exception hierarchy, the app
factory, and the liveness/readiness health check are built, and were verified end to end against a
real Postgres container (not just linted) — see the Cycle 0 checklist below and the session log for
what that verification found. Cycle 1 needs Pinecone credentials before it can start; see "Blocked on"
below.

**Next action:** get a Pinecone Starter API key (no payment method attached; see `docs/SETUP.md`),
then start **Cycle 1 — Corpus and hybrid retrieval**.

---

## Blocked on — external prerequisites

These are user-side actions. Cycle 0 does not need them; Cycle 1 and Cycle 3 do.
Full instructions in `docs/SETUP.md`.

| Prerequisite | Needed by | Status |
| --- | --- | --- |
| Ollama installed + `ollama pull qwen3:4b` | Cycle 3 | ⬜ not done |
| Pinecone Starter account, API key, **no payment method attached** | Cycle 1 | ⬜ not done |
| LangSmith Developer account, API key, **no credit card attached** | Cycle 3 | ⬜ not done |
| `.env` populated from `.env.example` | Cycle 1 | ⬜ not done |

---

## Cycle status

| # | Cycle | Est. | Status |
| --- | --- | --- | --- |
| 0 | Foundation scaffold | 2h | ✅ done |
| 1 | Corpus and hybrid retrieval | 4h | ⬜ pending |
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

### Cycle 1 — Corpus and hybrid retrieval ⬜

- [ ] Seed corpus generator (~60 docs: incidents, runbooks, architecture, product specs, policies, meeting notes)
- [ ] Metadata schema: `department`, `document_type`, `access_level`, `created_date`
- [ ] Structure-aware chunking preserving document + section attribution
- [ ] Content-hashed **idempotent** ingestion (protects the 5M-token embedding allowance)
- [ ] Pinecone dense index (`llama-text-embed-v2`) with namespaces
- [ ] Pinecone sparse index (`pinecone-sparse-english-v0`)
- [ ] `retrieval/hybrid.py` — concurrent dense+sparse fan-out, RRF fusion
- [ ] `retrieval/reranker.py` — `bge-reranker-v2-m3`, allowlisted, monthly budget guard
- [ ] `access_level` filter derived from the caller's role

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
  pricing pages; found that `cohere-rerank-v3.5` bills on first call and pinned the reranker to
  `bge-reranker-v2-m3` behind an allowlist.
- **2026-09-05** — Measured hardware (RTX 3050, 4 GB VRAM) and settled on a single local `qwen3:4b`
  model for every graph node to avoid model-eviction thrash.
- **2026-09-05** — Architecture, stack and 8-cycle delivery plan agreed.
