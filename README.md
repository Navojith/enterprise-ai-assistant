# Enterprise AI Assistant

A conversational assistant that answers questions from internal organizational documents — policies,
architecture docs, runbooks, incident reports, product specs and meeting notes — using a LangGraph
multi-agent system over hybrid dense+sparse retrieval, with role-based access control enforced
outside the model and full execution tracing.

Built for the AI Lead Technical Assessment (`ASSESSMENT.md`).

> **Status: all 8 delivery cycles built and live-verified.** See [`docs/PROGRESS.md`](docs/PROGRESS.md)
> for exactly what was verified, and [`docs/ASSUMPTIONS_AND_TRADEOFFS.md`](docs/ASSUMPTIONS_AND_TRADEOFFS.md)
> for real findings live verification surfaced along the way. Outstanding: recording the demo
> video and publishing the repository publicly (`ASSESSMENT.md`'s deliverables).

---

## What it does

- **Multi-agent orchestration** — a Supervisor routes to specialized Retrieval, Research and Response
  agents, with a Validator gating every answer.
- **Recursive Language Model (RLM)** — instead of loading whole documents into context, the Research
  agent writes **Python search plans**, executes them in an AST-validated sandbox, decomposes work into
  batches, calls sub-agents recursively, and aggregates the results.
- **Hybrid retrieval** — dense and sparse Pinecone queries run concurrently and are fused with
  Reciprocal Rank Fusion, with optional reranking and full document attribution.
- **Transparent execution** — a real-time Agent Activity Panel shows the active graph node, tool calls,
  retrieval status, memory updates and validation results as they happen.
- **Security by construction** — RBAC is enforced at the tool-execution boundary and the retrieval
  filter, never in the prompt, so prompt injection cannot escalate privilege.
- **Fully local inference** — runs on Ollama with no LLM API key and no cost.

---

## Architecture

```mermaid
graph LR
    UI[Streamlit UI] --> API[FastAPI + JWT + rate limit]
    API --> SUP[Supervisor]
    SUP --> RET[Retrieval agent]
    SUP --> RES[Research agent · RLM]
    RET --> RSP[Response agent]
    RES --> RSP
    RSP --> VAL[Validator]
    VAL -->|retry| RSP
    VAL -->|pass| UI
    RET --> PC[(Pinecone<br/>dense + sparse)]
    RES --> PC
    SUP -.-> OLL[Ollama qwen3:4b]
    API -.-> PG[(Postgres)]
    SUP -.-> LS[LangSmith]
```

Full detail, including the request lifecycle and the folder structure, in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Stack

| Layer | Technology |
| --- | --- |
| Frontend | Streamlit (SSE streaming) |
| Backend | Python 3.11 · FastAPI · async throughout |
| Orchestration | LangGraph |
| LLM | Ollama · `qwen3:4b` (local, free) |
| Vector DB | Pinecone serverless — dense + sparse indexes |
| Embeddings | Pinecone integrated inference |
| Persistence | Postgres (Docker) |
| Observability | LangSmith |
| Tools | Knowledge search · Python analysis · MCP server |

---

## Quickstart

Full instructions — including free-tier account setup and the cost guards — in
[`docs/SETUP.md`](docs/SETUP.md).

```bash
# Prerequisites: Ollama + qwen3:4b, Pinecone key, LangSmith key
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then fill in the three API keys

docker compose up -d postgres
python -m scripts.ingest
uvicorn backend.app.main:app --reload --port 8000
python -m mcp_server
streamlit run frontend/app.py
```

---

## Roles

| Role | Allowed |
| --- | --- |
| **Viewer** | Chat and search |
| **Analyst** | Search, analytics tools, MCP tools |
| **Administrator** | All tools |

Tool execution respects these permissions at the execution boundary — the agent cannot bypass them.

---

## Documentation

| Document | Read it for |
| --- | --- |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | Current state, what is done, what is next |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Every technical decision and why it was made |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System design, folder structure, request lifecycle |
| [`docs/DELIVERY_PLAN.md`](docs/DELIVERY_PLAN.md) | Build cycles, risk register, acceptance criteria |
| [`docs/SETUP.md`](docs/SETUP.md) | Prerequisites, environment variables, running locally |
| [`docs/ASSUMPTIONS_AND_TRADEOFFS.md`](docs/ASSUMPTIONS_AND_TRADEOFFS.md) | What was assumed, traded away, and why |

---

## Cost

Every component runs on a free tier or locally, and no payment method is attached to any account.
Verified limits and the guards that enforce this are documented in
[`docs/DECISIONS.md`](docs/DECISIONS.md) §4.
