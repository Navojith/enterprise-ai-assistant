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
degrading to pure RRF ordering. `cohere-rerank-v3.5` is excluded by allowlist because it bills on the
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

---

## Known limitations

- **Latency.** Expect 60–90 seconds per question. This is dominated by local inference on a 4 GB GPU,
  not by the architecture.
- **Answer quality is model-bound**, not design-bound. See trade-off 1.
- **Synthetic corpus** means retrieval quality is not validated against real enterprise documents.
- **No evaluation harness.** There is no automated answer-quality benchmark; correctness is verified
  by the acceptance criteria in `docs/DELIVERY_PLAN.md` rather than by scored evaluation.
- **Single region.** Pinecone Starter is limited to AWS `us-east-1`.
