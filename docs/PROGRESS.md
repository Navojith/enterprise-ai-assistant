# Progress

> **This file is the resume point.** If you are a session with no prior context, read this first,
> then `docs/DECISIONS.md` for *why*, then the document matching the work you are picking up.
>
> **Maintenance rule:** update this file in the same pass that completes the work — never later.
> A stale progress file is worse than no progress file, because it actively misleads the next
> session into redoing finished work or skipping unfinished work.

---

## Current state

**Cycles 0 through 5 are done.** Cycle 0's scaffold, config, logging, errors, app factory and
health checks are built and verified against real Postgres. Cycle 1's corpus (64 documents, 224
chunks), hybrid retrieval, and reranking are built and verified against a **real, live Pinecone
Starter account**. Cycle 2's static user store, JWT issue/verify, the `Principal`/role→permission
matrix, FastAPI dependency wiring, and the Postgres-backed token-bucket rate limiter are built and
verified against a real, live Postgres container end to end. See each cycle's checklist below and
the session log for what verification found.

Cycle 3 — the LangGraph core — is built and verified live end to end against real Ollama,
Pinecone, and Postgres: Supervisor → {Retrieval → Response, Response} → Validator, with a bounded
retry loop, an `AsyncPostgresSaver` checkpointer (multi-turn memory confirmed by resuming the same
`thread_id` across turns), rolling-summary memory, the `LLMProvider`/`OllamaProvider`/
`FallbackChain` stack, and an SSE endpoint streaming typed `ActivityEvent`s. Two real findings from
that verification are recorded where a future session would otherwise rediscover them the hard
way: `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 13 (Ollama's default GPU offload and thinking-
mode quirks) and trade-off 14 below (a schema field-order bug that silently mis-routed the RLM
demo's own example question). **LangSmith tracing itself is not wired yet** — the key was verified
to authenticate, but `observability/langsmith.py` is a Cycle 7 deliverable; Cycle 3's graph was
verified via its own `ActivityEvent` stream, not via LangSmith traces.

Cycle 4 — tools and RBAC enforcement — is built and verified live end to end against the real
MCP server, real Ollama, real Pinecone, and real Postgres, all four running at once: the
two-layer `ToolRegistry` (bind-time filtering + an independent execution-boundary re-check),
`knowledge_search` (wrapping Cycle 1's hybrid search unchanged), `python_analysis` on a new
AST-allowlisted sandbox (`rlm/sandbox.py`, built a cycle early — `docs/DELIVERY_PLAN.md`'s risk
register already anticipated this), a real `mcp.server.mcpserver.MCPServer` exposing the
employee directory / service catalog / incident records over Streamable HTTP, an async
`MCPClient`, and a new Supervisor `"tools"` route feeding a Tools node into the graph. Live
verification (not just unit tests) confirmed both RBAC layers end to end: an Analyst's
`"Use the employee directory tool..."` request routed to `"tools"`, chose `employee_directory`,
filled its arguments, called the real MCP server, and produced a correctly-cited answer; the
identical request from a Viewer still routed to `"tools"` but the Tools node's own LLM call
could not choose `employee_directory` at all — bind-time filtering had only ever offered it
`knowledge_search` — and it used that instead, exactly as designed. Three real, non-obvious SDK
findings surfaced building this and are recorded so a future session doesn't rediscover them:
`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 15 (the `mcp` 2.x SDK renamed `FastMCP` to
`MCPServer` and vendors its own `httpx2`; `typing.TypedDict` fails at runtime on Python 3.11
against pydantic v2's schema generation) and trade-off 16 (`asyncio.wait_for` around an
`AsyncExitStack`-tracked context manager broke `anyio`'s cancel scopes — found, root-caused, and
fixed before it ever shipped).

Cycle 5 — the RLM research agent — is built and verified live end to end against real Ollama,
Pinecone, and Postgres, as an Analyst asking the spec's own example question ("Summarize all
outage reports related to payment failures during the last year and identify recurring root
causes"): the Supervisor's new `"research"` route (offered only to a principal holding
`Permission.ANALYTICS_TOOLS` — a Viewer asking the identical question was confirmed live to
route to `"retrieval"` instead, never `"research"`), `rlm/planner.py`'s schema-constrained plan
generation with AST-validation retry and a deterministic fallback plan, and `rlm/executor.py` +
`rlm/api.py`'s recursive executor exposing `search`/`filter`/`batch`/`sub_agent`/`sub_agents`/
`aggregate` to generated code via a worker-thread-to-event-loop sync/async bridge
(`asyncio` `Task.create_task(..., context=...)`, preserving the stream-writer's and — from
Cycle 7 — LangSmith's contextvar-based state across the bridge). Three real findings from live
verification are recorded so a future session doesn't rediscover them the hard way: the highest-
impact one, `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 17 (concurrent sub-agent fan-out at
the originally planned `max_depth=2`/`max_concurrent_sub_agents=4` self-DoSed the one local
`qwen3:4b` instance — Ollama serializes concurrent requests rather than parallelizing them,
and the resulting timeouts tripped the fallback chain's circuit breaker and failed the whole
turn; both settings now default to 1, recorded as a load-bearing choice in `docs/DECISIONS.md`
§9, alongside a narrower fix to a fallback-plan budget-sharing bug the same investigation
surfaced), trade-off 18 (`agents/nodes/response.py`'s "no evidence" instruction was
unconditional and made the model discard real research findings on a `"research"` turn — fixed
and pinned with a new unit test, `tests/agents/nodes/test_response.py`), and the deterministic
fallback plan itself firing on both live demo runs (the 4B model's own generated Python failed
the AST allowlist both attempts each time) — expected per `docs/DELIVERY_PLAN.md`'s risk
register, and the fallback produced a correct, evidence-grounded final answer both times. 32 new
tests (263 total, up from 213); `ruff`, `ruff format`, `mypy --strict` all pass clean.

Cycle 6 — guardrails and validation — is built and verified live end to end against real Ollama,
Pinecone, Postgres, and the MCP server: a new `agents/nodes/guardrail.py`, the graph's entry
point ahead of the Supervisor, screens every message with a deterministic heuristic filter
(`guardrails/injection.py`) for the three attack shapes ASSESSMENT.md names, escalating to one
schema-constrained `qwen3:4b` classifier call only when the heuristics are genuinely inconclusive
— a design decision the user was asked about explicitly and chose over "classify every turn" or
"heuristics only," recorded in full in `docs/DECISIONS.md` §10. `agents/nodes/validator.py`'s
prior structural-only check is replaced by `_validate_answer`, layering real citation
verification (`guardrails/citations.py` — does every bracketed `[Title]` match a chunk, tool
result, or research finding actually present this turn?) and a brand/persona guardrail
(`guardrails/brand.py` — persona breaks and system-prompt leaks) on top of the same structural
checks. Retrieved evidence, tool output, and research findings are all framed as untrusted data
(`guardrails/injection.py::frame_untrusted_content`, applied in both `agents/nodes/response.py`
and `rlm/api.py`'s sub-agent prompts) — the second, independent injection channel a chat-endpoint
heuristic cannot see at all, since a compromised document never passes through the endpoint.
`guardrails/validators.py` adds shape validation for chat input (checked in `api/v1/chat.py`
before the SSE stream opens) and content screening for tool arguments
(`tools/registry.py::execute`, after the existing Pydantic shape check). Live verification
confirmed: the acceptance test's literal injection attempt
(`docs/DELIVERY_PLAN.md` criterion 5) is blocked for both an Analyst and a Viewer, visible in the
Agent Activity Panel's event stream as a `guardrail` node block rather than silently dropped; an
ambiguous message ("What is our password reset policy for new employees?") correctly escalated
to the classifier, which returned "safe," and the turn proceeded through Supervisor → Retrieval
→ Response → Validator exactly as before, with the new citation/brand checks passing cleanly on
a real, correctly-cited answer; and a whitespace-only message was rejected with a clean 422
before the graph ever ran. 47 new tests (310 total); `ruff`, `ruff format`, `mypy --strict` all
pass clean.

**Next action:** start **Cycle 7 — frontend, observability, docs** (Streamlit chat UI with the
Agent Activity Panel consuming SSE, wiring `observability/langsmith.py`, and finishing
`README.md` / the architecture diagram / `docs/ASSUMPTIONS_AND_TRADEOFFS.md`). No external
prerequisites are blocking Cycle 7. **Deferred, not blocking:** re-confirm live, through the
Streamlit UI once it exists, that a Viewer's identical spec-example research question still
routes to `"retrieval"` (already confirmed twice via curl — Cycle 5's own live verification and
again during Cycle 6 — see the session log) — a nice-to-have the user asked for, not a reason to
reorder anything.

---

## Blocked on — external prerequisites

These are user-side actions. Full instructions in `docs/SETUP.md`.

| Prerequisite | Needed by | Status |
| --- | --- | --- |
| Ollama installed + `ollama pull qwen3:4b` | Cycle 3 | ✅ done — see `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 13 for the throughput/thinking-mode findings this surfaced |
| Pinecone Starter account, API key, **no payment method attached** | Cycle 1 | ✅ done |
| LangSmith Developer account, API key, **no credit card attached** | Cycle 3 | ✅ done (key verified live against `api.smith.langchain.com`) |
| `.env` populated from `.env.example` | Cycle 1 | ✅ done (Pinecone key set; DB_* left at defaults) |
| `JWT_SECRET_KEY` generated (`openssl rand -hex 32`) | Cycle 2 | ✅ done |

---

## Cycle status

| # | Cycle | Est. | Status |
| --- | --- | --- | --- |
| 0 | Foundation scaffold | 2h | ✅ done |
| 1 | Corpus and hybrid retrieval | 4h | ✅ done |
| 2 | Auth, RBAC, rate limiting | 2.5h | ✅ done |
| 3 | LangGraph core, memory, streaming | 5h | ✅ done |
| 4 | Tools and RBAC enforcement | 3h | ✅ done |
| 5 | RLM research agent | 4h | ✅ done |
| 6 | Guardrails and validation | 2.5h | ✅ done |
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

### Cycle 3 — LangGraph core, memory, streaming ✅

- [x] `agents/state.py` — typed `AgentState`; `merge_retrieved_chunks` reducer dedupes by
      `chunk_id` keeping the higher score, ready for Cycle 5's concurrent sub-agent writes
- [x] Supervisor / Retrieval / Response / Validator nodes (`agents/nodes/`) — Supervisor does
      schema-constrained routing (`"retrieval"` / `"direct"`) plus rolling-summary upkeep;
      Retrieval reuses Cycle 1's `hybrid_search`/`rerank_chunks` unchanged; Response streams
      both the answer and `qwen3`'s separated reasoning to the activity panel; Validator is a
      structural check this cycle (non-empty, cited-if-evidenced) — Cycle 6 replaces the check,
      not the node's shape
- [x] Conditional edges + bounded Validator→Response retry loop (`agents/graph.py`) — the
      retry bound is a `build_graph(..., max_validator_retries=...)` closure over
      `Settings.max_validator_retries`, not a `Runtime` lookup, so it's a plain unit-testable
      `state -> str` function; exhausting the budget still appends the last draft with a
      caveat rather than discarding it
- [x] `AsyncPostgresSaver` checkpointer (`main.py`'s lifespan owns an `AsyncConnectionPool`,
      `agents/graph.py` accepts it as a parameter) — multi-turn memory verified live: a second
      turn on the same `thread_id` sees the first turn's exchange with no application code
      re-supplying it
- [x] Rolling-summary session memory (`memory/summarizer.py`, `memory/session.py`) —
      `needs_summarization`/`summarize_oldest` split pure trigger from the LLM call, folding
      the oldest messages into `state["summary"]` and dropping them via `RemoveMessage`
- [x] `llm/provider.py` (`LLMProvider` Protocol) + `llm/ollama_provider.py` (`ChatOllama`-backed,
      forces `num_gpu=99`, pairs `think: false` only with `format` — see trade-off 13) — verified
      live: 57 tok/s fully GPU-resident, clean structured routing on the RLM demo's own example
      question after fixing trade-off 14's field-order bug, clean reasoning/content separation
      on free-text streaming
- [x] `llm/chain.py` — `FallbackChain` + `CircuitBreaker` (pure transition logic split out,
      same pattern as `rate_limit.py`'s refill math); one tier configured today per
      `docs/DECISIONS.md` §7
- [x] SSE endpoint (`api/v1/chat.py`, `POST /api/v1/chat/stream`) streaming typed `ActivityEvent`s
      via `stream_mode="custom"` — the panel observes the graph's own `StreamWriter` calls
      directly, not a parallel narration
- [x] `tests/agents/`, `tests/memory/`, `tests/llm/`, `tests/api/test_chat.py` — 43 new tests
      (134 total) covering the chunk-merge reducer, both conditional-edge functions, the
      Validator's structural check, the summarizer's trigger boundary and message-folding, the
      circuit breaker's state transitions (`freezegun`), `FallbackChain`'s fallback/breaker/
      partial-stream behavior, and the chat endpoint's auth/wiring/graceful-503 paths — plus a
      full live run against real Ollama, Pinecone and Postgres (see above and the session log).
      `ruff`, `ruff format`, `mypy --strict` all pass clean.

Two fixes landed as part of this cycle's own verification, not deferred: `main.py`'s lifespan
degrades `pinecone_store`/`app.state.graph` to `None` rather than crashing the process when
Pinecone or the checkpointer is unreachable at startup (`GraphContext.pinecone_store: PineconeStore
| None`, a new `GraphUnavailableError`) — without this, every existing test using `TestClient(
create_app())` would have started failing the moment this cycle's lifespan additions landed,
since the test environment has no `PINECONE_API_KEY`.

### Cycle 4 — Tools and RBAC enforcement ✅

- [x] `tools/registry.py` — `ToolRegistry.available_to` (bind-time filter) **and**
      `.execute` (independent execution-boundary re-check, verified live and in
      `tests/tools/test_registry.py` with a Viewer principal handed straight to `execute(...)`,
      no LLM or graph involved)
- [x] `knowledge_search` tool — wraps Cycle 1's `hybrid_search`/`rerank_chunks` unchanged
- [x] `python_analysis` tool, on a new `rlm/sandbox.py` (AST allowlist + stripped builtins +
      wall-clock timeout via a worker thread — built this cycle, a cycle ahead of the RLM
      planner that will reuse it, per `docs/DELIVERY_PLAN.md`'s risk register)
- [x] `mcp_server/` — `mcp.server.mcpserver.MCPServer` (see `docs/ASSUMPTIONS_AND_TRADEOFFS.md`
      trade-off 15 for why not `mcp.server.fastmcp.FastMCP`) over Streamable HTTP: employee
      directory, service catalog, incident records, six tools total, two per dataset
- [x] Async `MCPClient` with connect/call timeouts (`MCPUnavailableError`/`ToolTimeoutError`) —
      a real `asyncio.wait_for`/`anyio` cancel-scope bug was found and fixed building this
      (trade-off 16), verified live: connect, call, and clean shutdown against a real running
      `mcp_server` process
- [x] Supervisor `"tools"` route + a new Tools node (`agents/nodes/tools.py`) wiring the
      registry into the graph — two schema-constrained calls (choose a tool, then fill its
      parameters), verified live end to end for both an Analyst (reached a real MCP tool) and a
      Viewer (bind-time filtering left it unable to choose that tool at all, and it used
      `knowledge_search` instead — no execution-boundary denial even needed for this path)
- [x] `tests/rlm/test_sandbox.py`, `tests/tools/` (registry, knowledge_search, python_analysis,
      mcp_client, mcp_tools, factory), `tests/mcp_server/test_server.py`,
      `tests/agents/nodes/test_tools.py`, `tests/agents/nodes/test_supervisor.py`, plus two new
      `test_graph.py` cases for the `"tools"` route — 79 new tests (213 total); `ruff`,
      `ruff format`, `mypy --strict` all pass clean.

### Cycle 5 — RLM research agent ✅

- [x] `rlm/sandbox.py` — AST allowlist, stripped builtins, no imports/dunders/IO, wall-clock
      timeout — **built in Cycle 4** (`python_analysis` needed it); nothing left to do here
- [x] Re-verified `rlm/sandbox.py` fits recursive sub-agent fan-out unchanged — its
      `injected_globals` contract needed no changes; the new work was entirely in what gets
      injected (`rlm/api.py`), not the sandbox itself
- [x] `rlm/api.py` — `search` / `filter` / `batch` / `sub_agent` / `sub_agents` / `aggregate`,
      each a plain-looking synchronous function from the sandboxed code's point of view that
      bridges to the real event loop via `loop.create_task(coro, context=...)` — verified live
      that the stdlib's own `asyncio.run_coroutine_threadsafe` does *not* preserve the calling
      context, which would have silently detached every sandboxed LLM call from the stream
      writer and (Cycle 7) LangSmith tracing
- [x] `rlm/planner.py` — schema-constrained Python plan generation (`ResearchPlan`), one retry
      with the specific AST violations fed back as feedback, then a deterministic fallback
- [x] `rlm/executor.py` — recursion depth + fan-out caps via one `RLMBudget` shared by
      reference across the whole recursive tree, bounded semaphore for concurrent fan-out
- [x] Result aggregator — `aggregate(findings, question)`, one LLM call synthesizing every
      sub-agent finding into `{"summary": ..., "recurring_themes": [...]}`
- [x] Deterministic fallback plan when generated code fails validation *or* fails at runtime
      after validating — `rlm/planner.py::deterministic_fallback_plan`, given a **fresh**
      `RLMBudget` at depth 0 so a failed generated attempt can't leave the fallback with
      nothing to spend (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 17)
- [x] Supervisor `"research"` route, gated to `Permission.ANALYTICS_TOOLS` the same way
      `"tools"` is gated per tool name (`agents/nodes/supervisor.py::_build_routing_schema`)
- [x] Research node (`agents/nodes/research.py`) wiring `rlm/executor.py` into the graph,
      folding its result into a new `research_output` state field
- [x] `tests/rlm/` (api, planner, executor — 32 new tests, including a real
      `run_sandboxed`-through-the-bridge integration test, not just mocks) plus
      `tests/agents/nodes/test_research.py`, `test_response.py`, and extensions to
      `test_supervisor.py`/`test_graph.py` for the new route — 263 total; `ruff`,
      `ruff format`, `mypy --strict` all pass clean
- [x] Live end to end against real Ollama/Pinecone/Postgres: an Analyst's exact
      spec-example question routed to `"research"`, ran the deterministic fallback plan
      (the 4B model's own generated plan failed AST validation both attempts, both live
      runs — expected per `docs/DELIVERY_PLAN.md`'s risk register), completed four
      sequential sub-agent analyses, aggregated them, and produced a correct,
      evidence-grounded final answer that passed validation; the identical question from
      a Viewer routed to `"retrieval"` instead, confirming the RBAC gate holds

### Cycle 6 — Guardrails and validation ✅

- [x] Prompt-injection detection (heuristics + classifier) — `guardrails/injection.py`'s
      deterministic `heuristic_screen` runs on every message; a genuinely ambiguous result (a
      watchlist word, no confident pattern match) escalates to one schema-constrained `qwen3:4b`
      classifier call in `agents/nodes/guardrail.py`, the graph's new entry point. A confident
      match never reaches the LLM at all — the design choice (escalate-only-when-ambiguous, over
      classifying every turn or skipping the classifier entirely) was put to the user explicitly
      and is recorded in `docs/DECISIONS.md` §10
- [x] Untrusted-data framing for retrieved content — `guardrails/injection.py::
      frame_untrusted_content` wraps every evidence section (`agents/nodes/response.py`'s
      Evidence/Tool result/Research findings, `rlm/api.py`'s sub-agent evidence) in explicit
      `<untrusted_data>` delimiters, paired with `UNTRUSTED_CONTENT_INSTRUCTION` in the system
      prompt — the defense against a compromised *document* carrying injection-shaped text,
      which a chat-endpoint heuristic can never see
- [x] Input + tool-parameter validation — `guardrails/validators.py::validate_user_message`
      (whitespace-only, control characters, pathological repetition; checked in
      `api/v1/chat.py` before the SSE stream opens) and `validate_tool_arguments` (recursive
      injection screen over every string value in a tool call's arguments, checked in
      `tools/registry.py::execute` right after the existing Pydantic shape validation)
- [x] Citation verification against retrieved chunk IDs — `guardrails/citations.py::
      verify_citations` flags any bracketed `[Title]` that names neither a retrieved chunk's
      title nor a tool/research evidence channel actually present that turn, replacing the
      "cited if evidenced" structural-only check `agents/nodes/validator.py` shipped in Cycle 3
- [x] Commercial-bank brand/persona guardrail — `guardrails/brand.py::check_brand_violation`
      catches a generic "as an AI language model" persona break and a verbatim system-prompt
      echo, both via plain pattern/string matching rather than a second LLM judgment call
- [x] Bounded validator retry loop with feedback — unchanged in shape from Cycle 3
      (`agents/graph.py::_make_after_validator`); `_validate_answer`'s new checks simply feed
      richer feedback into the same loop
- [x] `tests/guardrails/` (injection, validators, citations, brand — 4 new modules),
      `tests/agents/nodes/test_validator.py` rewritten for `_validate_answer`, a new
      `TestBuildGraph` case in `tests/agents/test_graph.py` pinning the guardrail node's position
      in the compiled topology, and a new registry test for the tool-argument content screen —
      47 new tests (310 total); `ruff`, `ruff format`, `mypy --strict` all pass clean
- [x] Live end to end against real Ollama/Pinecone/Postgres/MCP: the acceptance test's literal
      injection attempt blocked for both an Analyst and a Viewer, visible in the Agent Activity
      Panel's event stream as a `guardrail` node block; an ambiguous message correctly escalated
      to the classifier (returned "safe") and completed a normal `"retrieval"` turn whose answer
      passed the new citation/brand checks cleanly; a whitespace-only message rejected with a
      clean 422 before the graph ran at all

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

- **2026-09-06** — Built and verified Cycle 6 (guardrails and validation) live end to end against
  real Ollama, Pinecone, Postgres, and the MCP server (all four restarted fresh this session —
  Docker Desktop needed a manual start first). Before building, asked the user to choose how the
  spec's "heuristics + classifier" injection detection should actually run, since this
  hardware already treats one extra `qwen3:4b` call per turn as a real, non-trivial cost
  (`docs/DECISIONS.md` §3, §9): the user chose escalating to the classifier only when the
  deterministic heuristic filter is genuinely inconclusive, over classifying every turn or
  skipping the classifier entirely — recorded as a load-bearing decision in a new
  `docs/DECISIONS.md` §10 rather than assumed. Built `agents/nodes/guardrail.py` as the graph's
  new entry point (ahead of the Supervisor) specifically so a block is visible in a LangSmith
  trace and the Agent Activity Panel — `docs/DELIVERY_PLAN.md` criterion 5 requires an injection
  attempt to be "blocked *and traced*," which a check outside `graph.astream()` could never
  satisfy. Replaced `agents/nodes/validator.py`'s Cycle 3 structural-only check with
  `_validate_answer`, adding real citation verification (`guardrails/citations.py`) and a
  brand/persona guardrail (`guardrails/brand.py`) as plain pattern/string checks rather than a
  second LLM judgment call, consistent with this project's running skepticism of the 4B model as
  a reliable judge of anything nuanced. Applied `frame_untrusted_content`'s untrusted-data
  delimiter to every evidence channel in both `agents/nodes/response.py` and `rlm/api.py`'s
  sub-agent prompts — the second, independent injection channel (a compromised retrieved
  document) that no chat-endpoint heuristic can ever see, since the injected text never passes
  through the endpoint at all. Live verification confirmed all three layers work together
  without regressing the existing graph: the acceptance test's literal injection attempt
  (`docs/DELIVERY_PLAN.md` criterion 5) was blocked for both an Analyst and a Viewer, each
  visible in the SSE event stream as a `guardrail` node block; an ambiguous message ("What is our
  password reset policy for new employees?") correctly escalated to the classifier (~5.5s), which
  returned "safe," and the turn then completed a normal Supervisor → Retrieval → Response →
  Validator run whose real, correctly-cited answer passed the new citation and brand checks
  cleanly on the first attempt (no retry needed); and a whitespace-only message was rejected with
  a clean 422 before the graph ever ran, distinct from the graph-level injection block. No
  regressions found in the 263 pre-existing tests. 47 new tests (310 total); `ruff`,
  `ruff format`, `mypy --strict` all pass clean.
- **2026-09-05** — Built and verified Cycle 5 (RLM research agent) live end to end against
  real Ollama, Pinecone, and Postgres. Two consequential, non-obvious findings surfaced by
  live verification (not caught by 32 new passing unit tests, `ruff`, or `mypy --strict` —
  all of which passed clean the whole time) and fixed before being called done, both recorded
  in full in `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-offs 17–18: first, the originally
  planned `rlm_max_depth=2`/`rlm_max_concurrent_sub_agents=4` made a single research turn fire
  four concurrent LLM calls at one local `qwen3:4b` instance, which does not serve them in
  parallel — Ollama serializes them — so three queued past `llm_request_timeout_seconds` and
  tripped the fallback chain's circuit breaker, failing the *entire* turn including the
  unrelated Response node; fixed by defaulting both settings to 1 (sequential, one level of
  recursion) and raising `rlm_plan_timeout_seconds` from 90s to 180s to match the real
  sequential cost, recorded as a load-bearing choice in a new `docs/DECISIONS.md` §9 rather
  than left as an unexplained config value. A second, narrower bug the same investigation
  found: `rlm/executor.py`'s runtime-failure fallback path reused the failed attempt's
  already-spent `RLMBudget`, leaving the deterministic fallback plan — the one guarantee
  `docs/DELIVERY_PLAN.md` asks for — with no sub-agent budget of its own; fixed by giving a
  depth-0 fallback a fresh budget while a nested one still shares the whole-tree cap. Second,
  once a research turn could complete at all, its answer flatly denied any evidence had been
  found despite real aggregated findings sitting one paragraph above it in the same prompt —
  traced to `agents/nodes/response.py`'s "no evidence" instruction being unconditional since
  Cycle 3, correct for `"retrieval"`/`"direct"` turns but actively misleading once Cycle 4's
  `tool_output` and this cycle's `research_output` gave a turn other ways to have real
  evidence; fixed by conditioning the instruction on all three fields being empty together,
  extracted into a pure `_build_system_prompt` function specifically so
  `tests/agents/nodes/test_response.py` can pin the regression. Verified the fixes, not just
  the original build, by rerunning the spec's own example question live after each change —
  each prior run's exact failure mode was gone and the next one appeared, rather than
  re-testing the same failure twice. Final confirmed run: an Analyst's request routed to
  `"research"`, ran the deterministic fallback (the 4B model's own generated Python failed
  AST validation on both attempts, both live runs — an accepted, mitigated risk per
  `docs/DELIVERY_PLAN.md`'s risk register, not a bug), completed four real sequential
  sub-agent analyses over live-retrieved incident chunks, aggregated them, and produced a
  correct, evidence-grounded, validation-passing answer; the identical question from a Viewer
  routed to `"retrieval"` instead, confirming `_build_routing_schema`'s bind-time RBAC gate
  holds for the new route exactly as it does for `"tools"`. 263 tests total (32 new); `ruff`,
  `ruff format`, `mypy --strict` all pass clean.
- **2026-09-05** — Built and verified Cycle 4 (tools and RBAC enforcement) live end to end
  against a real `mcp_server` process, real Ollama, real Pinecone, and real Postgres running
  together. Verified the `mcp==2.1.1` SDK by importing the installed package rather than
  assumed from its 1.x shape (same discipline as Cycle 1's Pinecone v10 verification) — the
  server class is `mcp.server.mcpserver.MCPServer`, not `mcp.server.fastmcp.FastMCP`, which
  raises on import in 2.x with a message pointing at a migration guide; the SDK also vendors its
  own HTTP client as a separate `httpx2` package, distinct from this project's own pinned
  `httpx`. Found two further issues only by running real code, not by `ruff`/`mypy` (both passed
  clean the whole time): `mcp_server/data.py`'s `TypedDict`s needed to come from
  `typing_extensions`, not `typing`, because pydantic v2's schema generation for a `TypedDict`
  return annotation requires that on Python 3.11; and `MCPClient.connect()` wrapping
  `AsyncExitStack.enter_async_context(streamable_http_client(...))` in `asyncio.wait_for` broke
  `anyio`'s cancel-scope task affinity, raising `RuntimeError` the first time `aclose()` ran
  afterward — root-caused to `wait_for` wrapping its argument in a new child `Task`, fixed by
  never wrapping a stack-tracked context-manager entry in `wait_for` when its exit happens later
  elsewhere (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-offs 15–16 record both in full). Built
  `rlm/sandbox.py` a cycle early, since `python_analysis` needed it and `docs/DELIVERY_PLAN.md`'s
  risk register already named the sandbox as "the thing to build first" if Cycle 5 slipped.
  Wired a new Supervisor `"tools"` route and Tools node into the Cycle 3 graph (two
  schema-constrained calls: choose a tool from what this principal's role offers, then fill its
  parameters) rather than leaving the registry unreachable from the graph until Cycle 5 — this is
  what let `docs/DELIVERY_PLAN.md`'s acceptance criterion 3 be verified as a real conversation,
  not only a direct `registry.execute()` unit test: an Analyst's "Use the employee directory
  tool..." request routed to `"tools"`, called the real MCP server, and produced a correctly
  cited answer (one run of this also hit `LLM_REQUEST_TIMEOUT_SECONDS`'s 30s default on the
  argument-filling call and degraded to a clean typed error event rather than hanging or
  crashing — an immediate retry completed in ~20s, treated as expected hardware variance, not a
  bug); the identical request from a Viewer still routed to `"tools"` but its own tool-choice
  call could not select `employee_directory` at all, since bind-time filtering had only ever
  offered it `knowledge_search` — proving the two-layer authorization model
  (`tools/registry.py`'s module docstring) holds at the bind-time layer without ever reaching
  the execution-boundary check for this path. 79 new tests (213 total); `ruff`, `ruff format`,
  `mypy --strict` all pass clean.
- **2026-09-05** — Built and verified Cycle 3 (LangGraph core, memory, streaming) end to end
  against real Ollama, Pinecone, and Postgres. Before starting, verified the two external
  prerequisites live rather than assuming success from installation alone: the user's first
  `ollama run qwen3:4b` took ~1 minute for a two-token reply, which turned into its own
  investigation (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 13) — Ollama's default layer
  placement left 33% of the model on CPU despite it fitting fully in 4 GB VRAM (~18 tok/s;
  forcing `num_gpu: 99` fixed it to 100% GPU at ~57 tok/s, matching `docs/DECISIONS.md` §3's
  original estimate), and `think: false` silently corrupts output on `/api/chat` unless paired
  with `format` in the same call — encoded as a hard rule in `llm/ollama_provider.py` rather than
  left as tribal knowledge. Chose `ChatOllama` (already a pinned dependency) over the raw `ollama`
  client for the provider implementation once source inspection confirmed it implements exactly
  these request shapes and gets LangSmith/`astream_events` integration for free. A second,
  independent bug surfaced during live verification of the Supervisor: `RoutingDecision`'s field
  order (`route` before `reasoning`) let the model commit to a route with zero deliberation under
  schema-constrained decoding, misrouting the RLM demo's own example question
  (*"recurring root causes of payment failure incidents"*) to `"direct"` — fixed by reordering the
  fields (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 14) and reverified live. Also found, while
  wiring the new lifespan additions, that they would have broken every existing `TestClient`-based
  test (no `PINECONE_API_KEY` in the test environment) — fixed by making `GraphContext.
  pinecone_store` and `app.state.graph` degrade to `None` on startup failure instead of crashing,
  with a new `GraphUnavailableError` surfaced as a clean 503. Full pipeline exercised live:
  multi-turn memory (a second turn on the same `thread_id` saw the first turn's exchange with no
  application code re-supplying it), RBAC-filtered retrieval returning 8 cited chunks for the
  payment-failures question, and the Validator passing a correctly-cited answer. 43 new tests (134
  total); `ruff`, `ruff format`, `mypy --strict` all pass clean.
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
