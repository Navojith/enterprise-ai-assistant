# Setup

Everything needed to run this project locally. All components are free; see `docs/DECISIONS.md` §4
for the verified free-tier limits and the cost guards that keep it that way.

> ⚠️ **Do not attach a payment method to the Pinecone or LangSmith account.** Both providers hard-cap
> free usage when no card is on file, which makes accidental charges structurally impossible. Adding a
> card converts the cap into pay-as-you-go billing.

---

## Prerequisites

### 1. Ollama + the local model

The assistant runs entirely on a local model — no LLM API key, no cost, no rate limits.

1. Install Ollama from <https://ollama.com/download> (Windows installer).
2. Pull the model:

   ```bash
   ollama pull qwen3:4b
   ```

3. Verify it responds:

   ```bash
   ollama run qwen3:4b "Reply with OK"
   ```

**Why `qwen3:4b`:** the development machine has an RTX 3050 with **4 GB VRAM**. A 4B model at Q4
quantization fits entirely in VRAM (~2.6 GB) and runs at ~40–55 tok/s; a 7–8B model spills to CPU and
drops to ~8–15 tok/s, which pushes a single question past four minutes. Full reasoning in
`docs/DECISIONS.md` §3. **Do not load a second model concurrently** — there is not enough VRAM, and
Ollama will thrash evicting and reloading.

### 2. Pinecone (vector database)

1. Sign up for the free **Starter** plan at <https://www.pinecone.io/> — no credit card required.
2. **Do not add a payment method.**
3. Create an API key and copy it into `.env` as `PINECONE_API_KEY`.

Indexes are created by the ingestion script; you do not need to create them by hand. Starter is
limited to AWS `us-east-1`.

### 3. LangSmith (tracing — mandatory per the spec)

1. Sign up for the free **Developer** plan at <https://smith.langchain.com/>.
2. **Do not add a credit card** — personal organizations are hard-capped at 5k traces/month without one.
3. Copy the API key into `.env` as `LANGSMITH_API_KEY`.

> ⏰ **Free-tier traces are retained for 14 days.** The demo video must show live traces, so record it
> within 14 days of the run you intend to show.

### 4. Docker

Docker Desktop is already installed on the development machine (29.6.1). Postgres, the backend,
the MCP server, and the Streamlit frontend are all containerized (`docker-compose.yml`). Ollama
is the one deliberate exception — see the "Run everything with Docker Compose" section below.

### 5. Python

Python 3.11 (3.11.5 verified on the development machine).

### 6. JWT signing secret (Cycle 2)

No external account — generate a random secret and put it in `.env` as `JWT_SECRET_KEY`:

```bash
openssl rand -hex 32
```

`core/security/jwt.py` raises `ConfigurationError` at first use if this is left blank, rather
than signing tokens with a predictable default.

---

## Environment variables

Copy `.env.example` to `.env` and fill in the keys below. `.env` is gitignored; `.env.example` is
committed and must stay in sync whenever a variable is added.

| Variable | Purpose |
| --- | --- |
| `ENVIRONMENT` | `development` / `production` — read by `core/logging.py` to choose console vs. JSON log rendering |
| `LOG_LEVEL` | Default `INFO` |
| `PINECONE_API_KEY` | Pinecone authentication |
| `PINECONE_DENSE_INDEX` | Dense index name |
| `PINECONE_SPARSE_INDEX` | Sparse index name |
| `LANGSMITH_API_KEY` | LangSmith tracing |
| `LANGSMITH_PROJECT` | Trace project name |
| `LANGSMITH_TRACING` | `true` to enable tracing |
| `OLLAMA_BASE_URL` | Default `http://localhost:11434` |
| `OLLAMA_MODEL` | Default `qwen3:4b` |
| `LLM_REQUEST_TIMEOUT_SECONDS` | Per-call timeout before `llm/chain.py`'s fallback chain gives up on a request; raised twice live (`docs/DECISIONS.md` §9) |
| `LLM_STREAM_STALL_TIMEOUT_SECONDS` | How long a streaming response may go with no new token before it's treated as stalled |
| `LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | Consecutive failures before the circuit breaker opens |
| `LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | How long the breaker stays open before allowing another attempt |
| `MAX_VALIDATOR_RETRIES` | Bound on the Validator → Response retry loop (`agents/graph.py`) |
| `MEMORY_MAX_VERBATIM_MESSAGES` | Verbatim message count that triggers folding the oldest ones into the rolling summary |
| `MEMORY_SUMMARIZE_BATCH_SIZE` | How many of the oldest messages get folded into the summary per trigger |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Postgres connection, assembled into a DSN by `Settings.database_url`. `docker-compose.yml` provisions the container from these same names. |
| `JWT_SECRET_KEY` | Token signing secret |
| `JWT_EXPIRE_MINUTES` | Token lifetime |
| `RERANK_ENABLED` | `false` in development — protects the 500/month budget |
| `RERANK_MONTHLY_BUDGET` | Hard cutoff, default below 500 |
| `RATE_LIMIT_CAPACITY` | Token-bucket capacity per user |
| `RATE_LIMIT_REFILL_PER_SEC` | Token-bucket refill rate |
| `MCP_SERVER_HOST` / `MCP_SERVER_PORT` / `MCP_SERVER_PATH` | Where `python -m mcp_server` binds and where `tools/mcp_client.py` connects — same three settings on both sides |
| `MCP_CONNECT_TIMEOUT_SECONDS` | How long the client waits for the MCP server to become reachable |
| `MCP_CALL_TIMEOUT_SECONDS` | Per-tool-call timeout once connected |
| `SANDBOX_TIMEOUT_SECONDS` | Wall-clock budget for one `python_analysis` sandbox execution |
| `RLM_PLAN_TIMEOUT_SECONDS` | Wall-clock budget for one whole research turn (plan generation, sub-agents, aggregation) — raised twice live, `docs/DECISIONS.md` §9 |
| `RLM_MAX_DEPTH` | How many levels of recursive plan generation a `sub_agent` call may reach — defaults to 1 (sequential-only) for this hardware, `docs/DECISIONS.md` §9 |
| `RLM_MAX_CONCURRENT_SUB_AGENTS` | Bound on concurrent `sub_agents` fan-out — defaults to 1; raising it self-DoSes the one local model, `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 17 |
| `RLM_MAX_TOTAL_SUB_AGENT_CALLS` | Whole-tree cap on `sub_agent`/`sub_agents` calls per research turn, shared by reference across every recursive call |

---

## Install

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

---

## Running

Two ways to run the stack. Both need Ollama running natively first — it is intentionally not
containerized (see below).

### Option A — Docker Compose (one command)

```bash
# Ollama still runs on the host — see "Why Ollama isn't containerized" below.
ollama serve                      # if not already running as a service

# (optional) Regenerate the seed corpus — data/seed/ is already committed, so this is only
# needed after changing scripts/generate_seed_corpus.py. Deterministic: byte-identical output.
python -m scripts.generate_seed_corpus

docker compose up --build -d

# First run only, or after changing seed documents: ingest the corpus into Pinecone (creates
# the indexes on first run, and is idempotent — re-running re-embeds nothing unchanged). Runs
# inside the backend image so it shares its dependencies and doesn't need a local venv.
docker compose run --rm backend python -m scripts.ingest
```

This starts Postgres, the backend, the MCP server, and the frontend together, all reachable at
the same ports as the native run below. `docker compose logs -f backend` shows the same startup
log lines (`langsmith_tracing_enabled`, `mcp_client_connected`, `ollama_warmup_succeeded`) as a
native run.

**Why Ollama isn't containerized:** this project already needed live measurement to get
`qwen3:4b` fully GPU-resident on a 4GB laptop GPU (`num_gpu: 99`, forced in
`backend/app/llm/ollama_provider.py` — see `docs/DECISIONS.md` §3). Windows Docker Desktop GPU
passthrough (WSL2 + the NVIDIA Container Toolkit) is real, undemonstrated setup risk on top of
that already-fragile tuning, for a bonus item — so the backend and MCP server containers reach
the host's native Ollama via `http://host.docker.internal:11434` instead. Full reasoning in
`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 23.

### Option B — native (one terminal per process)

Useful for debugging a single service in isolation.

```bash
# 1. Postgres
docker compose up -d postgres

# (optional) Regenerate the seed corpus — data/seed/ is already committed with 64 generated
# documents, so this is only needed after changing scripts/generate_seed_corpus.py. It is
# deterministic: re-running it produces byte-identical files.
python -m scripts.generate_seed_corpus

# 2. Ingest the corpus (first run only, or after changing seed documents) — creates the
# Pinecone indexes on first run, and is idempotent: re-running re-embeds nothing unchanged.
python -m scripts.ingest

# 3. Backend API
# --loop points at a custom event-loop factory required on Windows — see Troubleshooting.
uvicorn backend.app.main:app --reload --port 8000 --loop backend.app.core.loop:selector_loop_factory

# 4. MCP server
python -m mcp_server

# 5. Streamlit frontend
streamlit run frontend/app.py
```

Either way, the UI ends up at <http://localhost:8501> and the API at <http://localhost:8000>
(docs at `/docs`).

---

## Demo users

Static credentials for the three RBAC roles, defined in `backend/app/core/security/users.py`.
See `docs/DECISIONS.md`'s Auth row for why hardcoded users were chosen over Keycloak. Get a token with:

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "viewer", "password": "ViewerPass123!"}'
```

Send it as `Authorization: Bearer <access_token>` on subsequent requests; `GET /api/v1/auth/me`
returns the resolved `{username, role}` and is a quick way to confirm a token works.

| Username | Password | Role | Allowed |
| --- | --- | --- | --- |
| `viewer` | `ViewerPass123!` | **Viewer** | Chat and search only — no administrative or analytics tools |
| `analyst` | `AnalystPass123!` | **Analyst** | Search, analytics tools, MCP tools |
| `admin` | `AdminPass123!` | **Administrator** | All tools |

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Very slow responses (>3 min/question) | A model larger than 4B is loaded, or a second model is resident. Run `ollama ps` and confirm only `qwen3:4b`. |
| `connection refused` on port 11434 | Ollama is not running. Start it and retry. |
| Pinecone auth errors | `PINECONE_API_KEY` missing or the index is in a non-`us-east-1` region. |
| No traces in LangSmith | `LANGSMITH_TRACING` is not `true`, or the key is missing. |
| Readiness check / any Postgres feature fails with `Psycopg cannot use the 'ProactorEventLoop'` | Windows only. Uvicorn defaults to `ProactorEventLoop`, which psycopg's async mode cannot use. Always launch with `--loop backend.app.core.loop:selector_loop_factory` as shown above — see `backend/app/core/loop.py` for why. |
| Postgres connection succeeds but returns `password authentication failed for user "postgres"` even though `docker compose ps` shows the container healthy | Something else on the machine — commonly a natively-installed Postgres — is already listening on port 5432 and is shadowing the container on `localhost`. This project's compose file deliberately publishes the container on host port **5433** (`DB_PORT` in `.env.example` matches, and `docker-compose.yml` reads the same variable); if you changed it back to 5432, check `docker port enterprise-ai-assistant-postgres` and whatever else owns 5432 before assuming the container is broken. |
| Reranking silently inactive | Expected in development (`RERANK_ENABLED=false`), or the monthly budget guard has tripped. |
| Postgres connection failures | `docker compose ps` — confirm the container is healthy. |
| Containerized backend logs an Ollama warm-up failure / degrades to a fallback error | Ollama isn't running natively on the host, or `host.docker.internal` isn't resolving. Confirm `ollama serve` is up on the host first; on native Linux Docker Engine (not Windows/Mac Docker Desktop) confirm `docker-compose.yml`'s `extra_hosts: host.docker.internal:host-gateway` took effect (`docker exec enterprise-ai-assistant-backend getent hosts host.docker.internal`). |
| Containerized backend can't reach Postgres or the MCP server | Compose-network traffic uses service names and *internal* ports (`postgres:5432`, `mcp_server:8100`), not the host-published ones (`localhost:5433`) — `docker-compose.yml`'s `backend` service overrides `DB_HOST`/`DB_PORT`/`MCP_SERVER_HOST` for exactly this reason. Confirm those overrides are present rather than editing `.env`, which native runs still depend on. |
| A chat turn through the **containerized** backend occasionally fails with `llm_timeout` (an immediate retry succeeds) | A disclosed, unresolved limitation, not a bug to chase — `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 24. Docker Desktop's `host.docker.internal` NAT intermittently stalls calls to the intentionally-native Ollama, in bursts lasting several requests; `llm/ollama_provider.py` already mitigates the most common form of this, but doesn't fully eliminate it. Only affects the Docker Compose deployment — the native run path never crosses this NAT boundary. If a demo segment needs a guaranteed first-attempt success, run that segment natively instead. |
