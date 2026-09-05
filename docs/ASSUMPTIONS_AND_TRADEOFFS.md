# Assumptions and Trade-offs

A required deliverable (`ASSESSMENT.md` → Deliverables §5), maintained continuously rather than
reconstructed at the end. Each entry states what was assumed or traded away, why, and what it cost.

---

## Assumptions

**The assessment brief permits reasonable assumptions.** `ASSESSMENT.md` states the candidate may
"make assumptions on any question by picking the best path". Every assumption below is recorded here
rather than left implicit, so the reasoning can be examined and challenged.

1. **Corpus is synthetic.** The brief permits generated mock data. ~60 documents across six types
   with realistic metadata — enough for hybrid retrieval and RLM batching to be meaningful, small
   enough to re-ingest cheaply. A real corpus would be thousands of documents; the architecture is
   unchanged by that, only the index size.

2. **Single-tenant, single-node deployment.** No horizontal scaling, no distributed rate limiting, no
   session affinity concerns. Appropriate for an assessment; a production deployment would move rate
   limiting to Redis and the checkpointer to a managed Postgres.

3. **"Session" memory means per-conversation-thread.** Memory survives multiple turns within a
   session, as required. Long-term cross-session memory was a bonus item and is out of scope.

4. **`access_level` is a simple ordinal clearance.** Documents carry one level; roles map to a maximum
   readable level. Real enterprise document security is usually attribute-based and far more complex.

5. **Dependency versions are exact pins to the latest release available on PyPI as of
   2026-09-05** (`requirements.txt`), not loose ranges. The stack leans on several fast-moving
   libraries — LangGraph, LangChain, the Pinecone client — whose behavior has changed across
   minor versions; a pin makes the build reproducible and any future upgrade a deliberate,
   reviewable change rather than something that happens silently on a clean install.

6. **One Postgres driver, `psycopg` (v3, async), for both the LangGraph checkpointer and the
   app's own tables**, rather than adding `asyncpg` as a second driver. `sqlalchemy`'s
   `postgresql+psycopg` async dialect covers the app's own tables (rate-limit state, the
   rerank-budget counter); `langgraph-checkpoint-postgres` already depends on `psycopg`
   directly. This held up under the Windows event-loop finding in trade-off 8 below — see
   `backend/app/core/loop.py`.

7. **The MCP server (Cycle 4) is built on the official `mcp` SDK's built-in FastMCP**
   (`mcp.server.fastmcp.FastMCP`), not the standalone third-party `fastmcp` package that
   `docs/ARCHITECTURE.md`'s shorthand name could also mean. `requirements.txt` pins to `mcp`
   specifically so this is a Cycle-0 commitment, not something Cycle 4 has to decide later.

---

## Trade-offs

### 1. Local 4B model instead of a frontier cloud model

**Chosen because:** the project operates under a hard zero-cost constraint, and local inference has no
API key, no quota and no bill.

**Cost:** this is the single most consequential trade-off in the build. A 4B model produces weaker
final prose, less reliable multi-step reasoning, and noticeably rougher Python search plans than a
frontier model would. Answer quality is therefore *not* representative of what this architecture
would produce in production.

**Mitigations:** JSON-schema-constrained decoding for every routing, planning and validation decision,
so structural correctness does not depend on the model's instruction-following; a tight `rlm` API
surface with few-shot examples; a deterministic fallback plan when generated code fails validation.

**What would change in production:** swap the provider — the `LLMProvider` abstraction exists precisely
so this is a configuration change, not a rewrite.

### 2. One model for every node, rather than tiered routing

**Chosen because:** the development GPU has 4 GB of VRAM. Two model tiers cannot co-reside; Ollama
would evict and reload between graph nodes at 10–30 s per swap, making a "tiered" design slower than a
single model.

**Cost:** the Supervisor's cheap classification and the Response node's demanding composition run on
the same model. Ideally the former would use something smaller and faster, the latter something larger.

**Note:** the tiered-routing abstraction is still implemented and tested — only the configuration is
single-tier. On a larger GPU this becomes a config change.

### 3. Hardcoded users and JWT instead of Keycloak

**Chosen because:** RBAC carries 5% of the grade while Agent Architecture, RAG, LangGraph and RLM
together carry 60%. Keycloak would consume setup time from the higher-value work and add an infra
dependency that can fail during a live demo.

**Cost:** no real identity provider, no OIDC flows, no token refresh, no user management.

**Retained:** genuine JWT issuance and verification, hashed passwords, and role claims. Only the user
*store* is static — the authorization mechanism itself is real.

### 4. Reranking is budget-gated and off by default in development

**Chosen because:** Pinecone's Starter tier includes 500 rerank requests per month, the tightest limit
in the stack.

**Cost:** development and testing run without reranking, so its quality contribution is only observed
during demo runs.

**Guard:** a persisted monthly counter disables reranking *before* the cap rather than erroring at it,
degrading to pure RRF ordering. `cohere-rerank-3.5` is excluded by allowlist because it bills on the
first call despite sharing the same API.

### 5. Simplified RLM

**Chosen because:** the brief explicitly permits it — *"the candidate may implement a simplified
version but should demonstrate the concept."*

**What is implemented:** genuine Python plan generation, AST-validated sandboxed execution, task
decomposition, batching, recursive sub-agent calls and aggregation.

**What is simplified:** recursion depth and fan-out are hard-capped, and the `rlm` API surface is
deliberately narrow. An unbounded implementation would be both a latency and a safety problem on this
hardware.

### 6. Docker Compose runs Postgres only

**Chosen because:** full containerization was a bonus item, and containerizing the backend, frontend
and MCP server would consume time without affecting any graded criterion other than the bonus itself.

**Cost:** the demo requires four processes started manually rather than one `docker compose up`.

### 7. Bonus items deliberately not built

Human-in-the-loop approval, long-term memory, and an answer-quality feedback loop were all scoped out
against the 2–3 day budget. Each is architecturally accommodated — HITL maps to a LangGraph interrupt
node, long-term memory to a second store behind the existing memory interface — but none is built.

### 8. A custom uvicorn event-loop factory, forced explicitly rather than left to a flag's side effect

**Found while verifying Cycle 0's readiness check against real Postgres:** psycopg's async mode —
required by the readiness check, and by `langgraph-checkpoint-postgres`'s `AsyncPostgresSaver` from
Cycle 3 onward — cannot run on Windows' default `ProactorEventLoop`. Uvicorn 0.52 only switches to a
compatible loop when `use_subprocess` is true, which it derives from `--reload` or `--workers > 1`
being passed, not from anything about the driver actually in use.

**The trap this would have left:** the documented dev command already includes `--reload`, so the bug
was invisible in normal development — it would only have appeared the first time someone ran the
server without `--reload` (a demo recording, a production-style smoke test), at exactly the moment
that's most costly to debug.

**Fix:** `backend/app/core/loop.py` provides an explicit `--loop backend.app.core.loop:selector_loop_factory`
that forces `SelectorEventLoop` on Windows regardless of other flags, and is a no-op on Linux/macOS.
`docs/SETUP.md`'s run command and `CLAUDE.md`'s dev commands were updated to always pass it.

**The same bug, a second time, in a different shape:** `scripts/ingest.py` (Cycle 1) hit the identical
`ProactorEventLoop` error the first time it touched Postgres — but a plain script calling `asyncio.run()`
has no `--loop` flag to reach for, and (unlike uvicorn, which bypasses it) `asyncio.run()` *does* build
its loop through the event-loop *policy*. `backend/app/core/loop.py` therefore provides a second,
complementary function, `install_selector_event_loop_policy()`, that every future Postgres-touching
script must call before `asyncio.run(...)` — documented together in one module so the next script
doesn't have to rediscover which of the two fixes applies to which entry point.

**Cost:** `SelectorEventLoop` cannot spawn subprocesses on Windows. Nothing in this application does.

### 9. Docker's Postgres container publishes on host port 5433, not 5432

**Found the same way:** this development machine already runs a native Postgres service on the
default port 5432. On Windows, that service silently wins the loopback bind ahead of Docker Desktop's
own port-forward, so connections to `localhost:5432` reached the *wrong* database — authenticating as
`postgres`/`postgres` failed against it, which looks identical to a genuine credential error and would
have cost real debugging time without the container's healthcheck output pointing anywhere useful.

**Fix:** `docker-compose.yml` publishes the project's Postgres on host port **5433**; `DB_PORT` in
`.env.example` and the default in `backend/app/core/config.py` both point at it, and
`docker-compose.yml` reads the same `.env` variable so the two can't drift apart. The existing
native service was left untouched — it may serve some other purpose on this machine, and moving our
own container off the contested port is non-destructive either way.

### 10. Postgres connection settings are five granular fields, not one DSN

**Changed on request:** `DATABASE_URL` was replaced with `DB_HOST` / `DB_PORT` / `DB_NAME` /
`DB_USER` / `DB_PASSWORD`, and `Settings.database_url` became a computed property that assembles
the DSN (with the user and password percent-encoded, since either could contain a `:` or `@` that
would otherwise be parsed as a DSN delimiter). `docker-compose.yml` now provisions the container's
credentials from these same names via Compose's automatic `.env` substitution, so the container and
the app can no longer disagree about what database they mean.

**Consequence surfaced while wiring it up:** pydantic-settings treats a variable that is *present
but blank* in `.env` (`DB_HOST=`) as an explicit empty-string value, not as "unset" — which would
have silently produced a broken DSN (and, separately, `SecretStr("")` instead of `None` for the
already-blank `PINECONE_API_KEY`/`LANGSMITH_API_KEY`/`JWT_SECRET_KEY` fields from Cycle 0). Fixed by
setting `env_ignore_empty=True` on `Settings.model_config`, so a blank value falls back to the
field's default exactly like an absent one does.

### 11. Pinecone's Python SDK (v10) was verified by introspecting the installed package, not from training recall

**Why:** `requirements.txt` pins `pinecone==10.0.0` — a major-version SDK with a materially different
surface from the client most training data (and most public tutorials) describe, including an entire
"Documents/Assistant" API absent from earlier versions. Writing `retrieval/pinecone_store.py` against
a remembered API shape would have produced code that imports cleanly, passes `mypy`, and fails only at
the first real call.

**What was actually done:** every method used — `create_index_for_model`, `has_index`, `IndexAsyncio`,
`search`, `upsert_records`, `describe_index_stats`, `inference.rerank` — was checked against the
installed package's real signatures and docstrings (`inspect.signature`, reading the source directly)
*and* exercised against a live Starter account before being treated as correct. That process caught
three mistakes a docs-only reading would not have: `search()`'s `inputs`/`top_k`/`filter` are top-level
keyword arguments, not nested under a `query=` parameter as one plausible reading of the REST API
suggests; a hit's id and score are `.id`/`.score` attributes, not `"_id"`/`"_score"` dict keys; and the
billed rerank model's exact string is `"cohere-rerank-3.5"`, not `"cohere-rerank-v3.5"` as
`docs/DECISIONS.md` originally (and incorrectly) recorded it — an allowlist built against the wrong
string would silently fail to block the model it exists to block.

### 12. `create_all_tables()` imports every ORM-model module itself, rather than trusting caller order

**Found while testing the reranker's budget counter end to end:** `RerankUsage`'s table was never
created, even though `create_all_tables()` had already run successfully — because it ran from
`scripts/ingest.py`, which never imports `retrieval/reranker.py`, and SQLAlchemy's declarative
`Base.metadata` only knows about a model class once its module has been imported. The failure surfaced
as `psycopg.errors.UndefinedTable` at the first rerank call, not at startup.

**Fix:** `core/db.py`'s `create_all_tables()` now imports every module that defines a table itself,
before calling `Base.metadata.create_all` — so callers (an ingestion script, the app's own startup)
never need to know which modules define what, and a new table added in a later cycle only needs one
line added to that one function, not a new import scattered into every entry point that might run
before it.

### 13. Ollama's defaults silently undermined two of `docs/DECISIONS.md`'s hardware assumptions

**Found while verifying the Cycle 3 prerequisite** (a first `ollama run qwen3:4b "Reply with OK"` took
~1 minute — orders of magnitude slower than §3's ~60–90 s *per question*, for a two-token reply).
Measured live against `ollama` 0.33.3 rather than assumed from the earlier hardware analysis:

**Finding A — partial GPU offload by default.** `ollama ps` showed `33%/67% CPU/GPU` even though
`qwen3:4b` (Q4_K_M, 3.5 GB) fits entirely inside the 4 GB card. Ollama's automatic layer-placement
heuristic reserves headroom conservatively and does not maximize GPU residency on its own. Measured
throughput at that split: **~18 tok/s**. Forcing `options.num_gpu: 99` (request all layers onto GPU)
produced `100% GPU`, 3.1/4.0 GB VRAM used, and **~57 tok/s** — matching §3's original estimate exactly.
**Fix:** every call from `llm/ollama_provider.py` sets `num_gpu: 99` explicitly; this is not left to
the default.

**Finding B — `think: false` is unreliable without `format`.** `qwen3:4b` is a hybrid-reasoning model
that opens every reply with a `<think>...</think>` block unless told otherwise. Tested across
`/api/generate` and `/api/chat`, with and without a JSON schema:

| `format` set? | `think` value | Result |
| --- | --- | --- |
| No | `false` | **Broken** — the `<think>` block is left concatenated into `content` unsplit; the caller cannot tell reasoning from answer. |
| No | unset (default) / `true` | Clean — `content` is just the answer, reasoning arrives separately in `message.thinking`. |
| Yes | unset (default) | Clean on `/api/chat` — `content` is valid JSON *and* `message.thinking` is populated (both, at the cost of the extra reasoning tokens). On `/api/generate` this combination is worse than broken: the JSON lands entirely in a `thinking` field and `response` comes back **empty**. |
| Yes | `false` | Clean and fastest — `content` is valid JSON, no `thinking` field, fewest tokens. |

**Consequence for the design:** `docs/DECISIONS.md` §5 already commits every routing/validation
decision to schema-constrained decoding, which is exactly the row that behaves correctly. The
implementation rule is therefore: **`think: false` is only ever sent alongside `format`**, on
`/api/chat` (never `/api/generate`, whose format+think interaction was verified broken). The
free-text Response node does not fight thinking mode at all — it leaves `think` at its default and
forwards the separated `message.thinking` to the Agent Activity Panel as visible reasoning, turning a
model quirk into a demo strength rather than suppressing it unreliably.

**Cost:** none of this changed the model choice or the architecture — §3's "keep `qwen3:4b`" holds.
The cost was purely investigative time, and the risk it removed was real: shipping `format` alone (the
naive reading of "use JSON-schema-constrained decoding") against `/api/generate` would have silently
returned empty responses from the Supervisor and Validator the first time either ran.

### 14. Pydantic field order is load-bearing under JSON-schema-constrained decoding

**Found while verifying Cycle 3's Supervisor live**, on the exact example question
ASSESSMENT.md's own RLM scenario uses (*"What are the recurring root causes of payment failure
incidents?"*): the Supervisor's `RoutingDecision` schema originally declared `route` before
`reasoning`. Under grammar-constrained decoding, Ollama emits JSON fields in the schema's
declared order and the model commits to each field as it is produced — with `reasoning=False`
(the Supervisor and Validator's setting, per §5) there is no thinking-mode scratch space either,
so `route` was being chosen with **zero deliberation**, and the `reasoning` field generated
immediately afterward would sometimes argue for the opposite route from the one already locked
in. Live evidence: the payment-failures question was classified `route="direct"` while its own
`reasoning` field started *"This question requires [internal document search]..."* — an answer
directly contradicting its own field two positions later.

**Fix:** `agents/nodes/supervisor.py`'s `RoutingDecision` declares `reasoning` *before* `route`,
forcing one sentence of deliberation to happen before the choice it justifies rather than after.
Reordering the two fields alone was sufficient — verified live, same question, both turns after
the fix routed correctly to retrieval.

**Why this matters beyond the one bug:** every schema-constrained decision in this codebase (the
Supervisor's routing, the Validator's future guardrail verdict in Cycle 6, the RLM planner's
future plan in Cycle 5) needs its rationale field ordered *before* its decision field whenever
`reasoning=False` is used, or the constraint that makes a 4B model reliable (§5) can just as
easily make it reliably wrong, silently, with no error to notice. This is now a checked-by-eye
rule for every new `BaseModel` schema passed to `astructured()`, not something the framework
enforces — worth a lint rule or schema convention if a future cycle adds many more of these.

---

## Known limitations

- **Latency.** Expect 60–90 seconds per question, now measured plausible rather than assumed — see
  trade-off 13. Still dominated by local inference on a 4 GB GPU, not by the architecture.
- **Answer quality is model-bound**, not design-bound. See trade-off 1.
- **Synthetic corpus** means retrieval quality is not validated against real enterprise documents.
- **No evaluation harness.** There is no automated answer-quality benchmark; correctness is verified
  by the acceptance criteria in `docs/DELIVERY_PLAN.md` rather than by scored evaluation.
- **Single region.** Pinecone Starter is limited to AWS `us-east-1`.
