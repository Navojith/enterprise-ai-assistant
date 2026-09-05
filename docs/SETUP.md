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

### 4. Docker (Postgres)

Docker Desktop is already installed on the development machine (29.6.1). Only Postgres is
containerized; the backend, frontend and MCP server run natively.

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
| `PINECONE_API_KEY` | Pinecone authentication |
| `PINECONE_DENSE_INDEX` | Dense index name |
| `PINECONE_SPARSE_INDEX` | Sparse index name |
| `LANGSMITH_API_KEY` | LangSmith tracing |
| `LANGSMITH_PROJECT` | Trace project name |
| `LANGSMITH_TRACING` | `true` to enable tracing |
| `OLLAMA_BASE_URL` | Default `http://localhost:11434` |
| `OLLAMA_MODEL` | Default `qwen3:4b` |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Postgres connection, assembled into a DSN by `Settings.database_url`. `docker-compose.yml` provisions the container from these same names. |
| `JWT_SECRET_KEY` | Token signing secret |
| `JWT_EXPIRE_MINUTES` | Token lifetime |
| `RERANK_ENABLED` | `false` in development — protects the 500/month budget |
| `RERANK_MONTHLY_BUDGET` | Hard cutoff, default below 500 |
| `RATE_LIMIT_CAPACITY` | Token-bucket capacity per user |
| `RATE_LIMIT_REFILL_PER_SEC` | Token-bucket refill rate |
| `LOG_LEVEL` | Default `INFO` |

---

## Install

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

---

## Running

Start each in its own terminal, in this order:

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

The UI is then at <http://localhost:8501> and the API at <http://localhost:8000>
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
