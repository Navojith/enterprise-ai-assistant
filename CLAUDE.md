# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start here

**Read `docs/PROGRESS.md` first** (auto-loaded below). It records what is built, what is next, and the
single next action. Conversation history is volatile; **the documents in `docs/` are the source of
truth.** When the two disagree, trust the files.

Then read the document matching the work:

| Document | Read it when |
| --- | --- |
| `docs/DECISIONS.md` | Before questioning or changing any technical choice — it records the reasoning |
| `docs/ARCHITECTURE.md` | Before writing code — folder structure, request lifecycle, design per grading criterion |
| `docs/DELIVERY_PLAN.md` | When picking up a cycle — scope, cut order, risks, acceptance criteria |
| `docs/SETUP.md` | For prerequisites, environment variables, run commands |
| `docs/ASSUMPTIONS_AND_TRADEOFFS.md` | When recording a new assumption — append, do not rewrite |
| `docs/DEMO_SCRIPT.md` | Before recording the deliverable demo video — minute-by-minute, mapped to grading criteria |

## Standing rules

- **`ASSESSMENT.md` is read-only.** It is the assignment brief, not a working document. Never edit,
  reformat or reorganize it. Notes and documentation go in `docs/`.
- **Zero cost is a hard constraint.** Every component must be free. **Verify free-tier claims against
  current provider documentation — never assume or recall them.** If something would incur charges,
  stop and propose a zero-cost alternative. See `docs/DECISIONS.md` §4 for the verified limits and the
  cost guards, including why the reranker model is allowlisted.
- **Production-grade code is the bar.** This is a Lead Technical assessment graded on explainability
  and readability: typed boundaries, validated schemas, a real exception hierarchy with graceful
  degradation, structured logging, dependency injection, and tests for logic that matters.
- **Ask rather than assume.** Surface decision points as explicit questions with a recommendation.
  Where an assumption is unavoidable, record it in `docs/ASSUMPTIONS_AND_TRADEOFFS.md`.
- **Update `docs/PROGRESS.md` in the same pass as the work.** A stale progress file misleads the next
  session into redoing or skipping work.
- **Commit per cycle.** The brief requires a maintained git commit history.

## Architecture invariants

Do not violate these without an explicit decision recorded in `docs/DECISIONS.md`:

- **Authorization never lives in a prompt.** RBAC is enforced at the tool-execution boundary and the
  retrieval `access_level` filter, reading the principal from request-scoped context.
- **One local model, `qwen3:4b`, for every graph node.** The dev GPU has 4 GB VRAM; a second resident
  model causes eviction thrash. Never load two concurrently.
- **Routing, planning and validation use JSON-schema-constrained decoding**, never free-text parsing.
  This is what makes a 4B model reliable enough for a multi-agent graph.
- **Reranking runs at most once per user turn**, never per RLM sub-agent — the free tier allows only
  500 requests per month.

## Development commands

Install (see `docs/SETUP.md` for prerequisites):

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements-dev.txt   # includes requirements.txt
```

Lint, format, type-check:

```bash
ruff check .
ruff format .
mypy
```

Test:

```bash
pytest
```

Run the backend (see `docs/SETUP.md` for the full multi-process order, required accounts, and
why `--loop` is required on Windows):

```bash
docker compose up -d postgres
uvicorn backend.app.main:app --reload --port 8000 --loop backend.app.core.loop:selector_loop_factory
```

Health check once it is up: `curl http://localhost:8000/api/v1/health/live`.

@ASSESSMENT.md

@docs/PROGRESS.md
