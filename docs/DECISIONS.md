# Decision Record

Every load-bearing choice in this project, with the reasoning behind it. The purpose is that no
decision gets silently relitigated, and that a reader (or a future session) can tell the difference
between a deliberate trade-off and an accident.

---

## 1. Constraints that drove everything else

Three hard constraints shaped the architecture more than any preference did:

1. **Zero cost.** No component may incur a charge. Free-tier status must be **verified against live
   provider documentation**, never assumed or recalled. If a component would bill, it is replaced.
2. **2–3 day build budget.** Cycles are ordered so that stopping after any one of them still leaves
   a coherent, demoable system.
3. **Measured local hardware** — see §3. 4 GB of VRAM is the single most restrictive input to the design.

Grading weight also steered scope. Agent Architecture (20%), RAG Design (15%), LangGraph (15%) and
RLM (10%) are **60% of the grade in four areas**, so those receive disproportionate effort; RBAC and
Code Quality at 5% each receive correct-but-lean implementations.

---

## 2. Stack

| Area | Decision | Rationale |
| --- | --- | --- |
| LLM | **Ollama, single local model — `qwen3:4b`** for every graph node | Zero cost and fully offline. A single pinned model avoids VRAM eviction thrash (§3). |
| Embeddings | **Pinecone integrated inference** — `llama-text-embed-v2` (dense) + `pinecone-sparse-english-v0` (sparse) | Free tier, server-side, so it consumes **no** local VRAM — leaving all 4 GB to the LLM. Delivers genuine dense **and** sparse hybrid retrieval from one dependency. |
| Vector DB | **Pinecone serverless (Starter)** | Mandated by `ASSESSMENT.md`. |
| Reranker | **`bge-reranker-v2-m3`**, allowlisted | Free-tier eligible. See the billing trap in §4. |
| Persistence | **Postgres via Docker Compose** | Backs the LangGraph checkpointer, rate-limit state, and the rerank budget counter. |
| Auth | **Hardcoded users + real JWT** (spec Option A) | RBAC is 5% of the grade; Keycloak would consume time better spent on the 60%. Real JWT issuance, hashed passwords and role claims — only the user store is static. |
| Packaging | **pip + `requirements.txt`** | User preference. |
| RLM | **Sandboxed Python execution** | The spec asks for "Python-based search plans" — this implements that literally rather than approximating it. |
| Backend | Python 3.11 · FastAPI · async throughout | Mandated by spec. |
| Frontend | Streamlit consuming SSE | Mandated by spec. |
| Observability | LangSmith (Developer tier) | Mandated by spec. |

**In scope beyond the required minimum:** MCP server, reranking layer, full containerization
(§7 below — picked back up after initially being scoped out).
**Explicitly out of scope:** human-in-the-loop approval, long-term memory, answer-quality feedback
loop. These are bonus items sacrificed to the time budget.

---

## 3. Hardware, and why the model is small

Measured on the development machine:

| Component | Spec |
| --- | --- |
| CPU | Intel i5-12500H — 12 cores / 16 threads |
| RAM | 15.7 GB |
| **GPU** | **NVIDIA RTX 3050 Laptop — 4 GB VRAM** |
| Python | 3.11.5 |
| Docker | 29.6.1 |

**The reasoning chain:**

At Q4 quantization, a 3–4B model occupies ~2.0–2.6 GB and runs fully GPU-resident at roughly
40–55 tok/s. A 7–8B model needs ~4.7–5.2 GB, exceeds 4 GB, and spills layers to CPU at ~8–15 tok/s.
14B and above is effectively CPU-bound at ~3–6 tok/s.

The graph issues **8–15 LLM calls per user question** (supervisor routing, retrieval, research plus
recursive sub-agents, response, validation). At 8B speeds that is roughly 4–5 minutes per question —
unusable in a 45-minute demo. At 4B speeds it lands near 60–90 seconds, which is workable.

**The non-obvious part:** two model tiers cannot co-reside in 4 GB. Ollama would evict and reload
between nodes at 10–30 s per swap, plausibly making a "tiered" design *slower* than a single model.
Therefore **one model serves every node**. This is a hardware-forced decision, not a simplification.

**The cost of this decision** is answer quality and Python plan-generation quality, which a 4B model
does measurably worse than a frontier model. It is mitigated — not eliminated — by JSON-schema-
constrained decoding (§5).

**Verified live at the start of Cycle 3** (`ollama` 0.33.3, this machine): the assumed 40–55 tok/s
did not hold out of the box. Ollama's default layer-placement heuristic put only 67% of the model's
layers on the GPU (`ollama ps` showed `33%/67% CPU/GPU`) even though the whole model fits in 4 GB —
measured at **~18 tok/s**, which would have roughly tripled every latency estimate in this section.
Forcing full GPU residency with `options.num_gpu: 99` on every request fixed it: **100% GPU, 3.1/4.0 GB
VRAM, ~57 tok/s** — back in line with the number this section originally assumed. `llm/ollama_provider.py`
sets this on every call; it is not optional configuration. See
`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 13 for the full investigation, including a second,
independent finding about `qwen3:4b`'s thinking-mode behavior that changes how §5's schema-constrained
decoding is actually implemented.

---

## 4. Zero-cost verification

Checked against live pricing pages, not from memory.

### Pinecone Starter — $0, no credit card required

| Limit | Starter allowance | Our usage |
| --- | --- | --- |
| Indexes | 5 | 2 (dense + sparse) ✓ |
| Storage | 2 GB | ~60 documents — negligible ✓ |
| Namespaces per index | 100 | ~6 departments ✓ |
| Dense **and sparse** index types | Included | Required for hybrid ✓ |
| Integrated-inference embeddings | **5M tokens/month included** | Ingestion ~0.5M; queries negligible ✓ |
| Reranking | **500 requests/month included** | ⚠️ Tightest limit in the stack |
| Write / read units | 2M / 1M per month | Comfortable ✓ |
| Region | AWS `us-east-1` only | Acceptable |

### LangSmith Developer — $0

1 seat · **5k base traces/month** · **14-day retention**. Personal organizations are hard-capped at
5k until a credit card is added, so with no card on file, overage throttles rather than bills.
Roughly one trace per user turn, so 5k is ample.

### Ollama

Open source, local, no account, no cost.

### The billing trap this uncovered

Pinecone's integrated reranking exposes `cohere-rerank-3.5` through the **same API** as the free
models, but it has **0 free requests on Starter** and would bill on the first call. An earlier draft
of the plan said only "Pinecone hosted reranker", which would have selected a billed model by accident.

### Cost guards this forces into the design

1. **Reranker model is an allowlisted constant**, not a free-form config string. `cohere-rerank-3.5`
   cannot be selected even by misconfiguration.
2. **Rerank runs once per user turn**, on the final fused candidate set — **never per RLM sub-agent**,
   which would exhaust 500 requests in a handful of questions. Off by default in development; enabled
   by config for the demo. A persisted monthly counter disables reranking *before* the cap rather than
   erroring at it, degrading to pure RRF ordering.
3. **Ingestion is idempotent** via content-hashed chunks, so repeated dev runs re-embed only what changed.
4. **No payment method is attached to either account**, making overage structurally impossible rather
   than merely unlikely.
5. **The demo must be recorded within 14 days** of the traced run, or LangSmith retention expires the
   traces that are a required deliverable.

---

## 5. Reliability: schema-constrained decoding

A 4B model is not reliably instruction-following in free text. Every routing decision, search plan,
and validation verdict is therefore produced with **JSON-schema-constrained decoding** (Ollama's
`format` parameter) rather than parsed out of prose.

This is the single highest-leverage choice for making a small local model behave predictably, and it
is what makes a multi-agent graph viable on this hardware at all. `qwen3` thinking mode stays **off**
(`think: false`) for the Supervisor and Validator; the RLM planner (Cycle 5) requests it
(`think: true`/default), where reasoning quality justifies the extra tokens.

**A verified quirk this forces into the implementation:** on Ollama's `/api/chat` endpoint, `think:
false` only behaves correctly when `format` (a JSON schema) is also set in the same call — tested live
against `qwen3:4b`. With both set, `message.content` comes back as clean, schema-conformant JSON and no
`thinking` field is present. `think: false` **without** `format` does not suppress reasoning at all; it
leaves the raw `<think>...</think>` block concatenated directly into `message.content` instead of
splitting it out — worse than doing nothing, since the caller can no longer tell reasoning from answer.
The safe rule `llm/ollama_provider.py` encodes: never send `think: false` unless `format` is also set.
For free-text generation (the Response node), reasoning is left at Ollama's default (on), which *does*
split cleanly into a separate `thinking` field even without `format` — the Response node forwards that
field to the Agent Activity Panel as visible reasoning rather than fighting to suppress it. See
`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 13.

---

## 6. Security boundary model

**RBAC is enforced at the tool-execution boundary and the retrieval filter — never in the prompt.**

The principal is read from request-scoped context, not from anything the model can emit. The registry
filters tools by role at bind time *and* re-checks at execution time. Retrieval injects an
`access_level` filter derived from the caller's role, so documents above their clearance never enter
the context window in the first place.

The consequence is that prompt injection **cannot** escalate privilege, because the model was never
the thing holding the authorization decision. The spec's requirement that "the agent should not be
able to bypass authorization" is satisfied structurally rather than by instruction.

---

## 7. Reconciliations

Two decisions were taken in different rounds and appeared to conflict. Both are resolved deliberately:

1. **"Tiered routing + fallback chain" vs. "single local model."** The `LLMProvider` abstraction and
   its fallback chain are kept as real, tested code — that is what satisfies the spec's *"LLM failures
   / degrade gracefully"* requirement — but only one tier (`qwen3:4b`) is active. A cloud provider can
   be added by configuration alone. Because inference is local there are no rate-limit errors to
   handle, so the chain guards **timeouts, model-load failures and malformed output** instead.

2. **Postgres-only Docker Compose vs. full containerization.** Originally `docker-compose.yml` ran
   **Postgres only**, with the backend, frontend and MCP server running natively — full
   containerization was a bonus item not selected under the initial time budget. That was later
   picked back up: `docker-compose.yml` now also builds and runs the backend, MCP server, and
   frontend, each from its own `Dockerfile` (`backend/Dockerfile`, `mcp_server/Dockerfile`,
   `frontend/Dockerfile`). Three implementation choices, each deliberate:
   - **Ollama stays native**, reached from containers via `http://host.docker.internal:11434`.
     This project's GPU-residency tuning (`num_gpu: 99` in `llm/ollama_provider.py`, forced
     because Ollama's own layer-placement heuristic under-used a 4GB GPU by default — §3 above)
     was already fragile enough to need live measurement natively. Windows Docker Desktop GPU
     passthrough (WSL2 + the NVIDIA Container Toolkit) would add real, undemonstrated setup risk
     on top of that for a bonus item, so "fully dockerized" here means every *application*
     service, with Ollama a documented, reasoned exception rather than an oversight.
   - **Every service Dockerfile builds from the repo root, copying the whole tree**, not just its
     own directory — needed regardless of the point below, since the three services' absolute
     imports are all rooted at the repo root. This trades a larger per-image footprint for zero
     risk of a *file* being missing inside a container.
   - **`frontend/app.py` and `frontend/api_client.py` no longer import `backend.app` at all**
     (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 25 — corrects an earlier version of this same
     bullet, which claimed copying the whole tree made such an import risk-free; that was wrong,
     found live when the containerized frontend actually crashed with `ModuleNotFoundError: No
     module named 'backend'`). The shared `ActivityEvent`/`ActivityEventType` contract (kept in
     one place since Cycle 7 specifically to avoid a hand-maintained duplicate) now lives in a
     new top-level `shared/events.py` with no dependency beyond `pydantic`;
     `backend/app/observability/events.py` re-exports it so every existing backend call site is
     unaffected. `mcp_server/server.py`'s equivalent (but previously unnoticed, because
     `python -m mcp_server` happened to mask it) coupling to `backend.app.core.config` was fixed
     the same way, with its own minimal `mcp_server/config.py`. `frontend/Dockerfile` separately
     gained `ENV PYTHONPATH=/app`, since Streamlit's script runner doesn't put the repo root on
     `sys.path` the way `python -m ...` does — a second, independent problem the import fix alone
     would not have solved.
   - **No application code changed for the networking/config layer.** `mcp_server_host` already
     serves two roles from one setting name — the server's own bind address, and the client's
     connect address — purely through which process's `Settings` instance reads it. Compose
     exploits this directly: the `mcp_server` container sets it to `0.0.0.0` (bind all
     interfaces) while the `backend` container sets the *same variable name* to `mcp_server`
     (the compose service's DNS name), since each container gets its own independent
     environment. Every other host difference between native and containerized runs
     (`DB_HOST`/`DB_PORT`, `OLLAMA_BASE_URL`, `BACKEND_URL`) was already a plain env var with no
     hardcoded fallback in application logic. One application code change *was* needed later,
     for a different reason: live-verifying the containerized deployment (not just building it)
     found `host.docker.internal` intermittently stalling LLM calls to the intentionally-native
     Ollama — `llm/ollama_provider.py::astructured` now calls Ollama's non-streaming API
     directly instead of through `ChatOllama`'s always-streamed internal path, at the user's
     explicit direction to fix this inside the existing provider abstraction rather than around
     it. This is a real, verified improvement, not a complete fix — `docs/ASSUMPTIONS_AND_
     TRADEOFFS.md` trade-off 24 has the full, honest reliability picture, including the
     residual, unresolved intermittency this doesn't eliminate.

---

## 8. Working agreements

- `ASSESSMENT.md` is **read-only**. It is the assignment brief, not a working document.
- Free-tier and pricing claims are **verified against current documentation**, never assumed. If a
  proposed component would incur charges, stop and propose a zero-cost alternative.
- Production-grade code is the bar: typed boundaries, validated schemas, a real exception hierarchy
  with graceful degradation, structured logging, dependency injection, and tests for logic that matters.
- Surface decision points as explicit questions rather than picking a default silently. Where an
  assumption is unavoidable, record it in `docs/ASSUMPTIONS_AND_TRADEOFFS.md`.
- `docs/PROGRESS.md` is updated in the same pass as the work it describes.

---

## 9. RLM recursion depth and sub-agent concurrency default to 1, not the originally planned 2/4

Cycle 5's RLM executor (`rlm/executor.py`, `rlm/api.py`) is fully built to support recursive
plan generation (`sub_agent` recursing into a nested plan) and concurrent sub-agent fan-out
(`sub_agents`, bounded by a semaphore) — both `Settings.rlm_max_depth` and `Settings.
rlm_max_concurrent_sub_agents` are configurable, and the mechanism does not change if either is
raised. The **defaults**, however, are both `1`, not the `2`/`4` originally planned, because
live verification against this project's actual hardware showed the higher defaults self-DoS
the one local model §3 already establishes as the whole graph's shared, non-concurrent
resource: four concurrent `astructured` calls to `qwen3:4b` do not run in parallel on one RTX
3050 — Ollama serializes them — so three of the four queued long enough to exceed
`llm_request_timeout_seconds` and trip `llm/chain.py`'s circuit breaker, failing the entire
turn including the unrelated Response node. `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 17
has the full investigation and the accompanying fallback-budget bug it also surfaced.

This is the same reasoning as §3's "one model serves every node," extended from concurrent
*models* to concurrent *requests* against that one model: a bounded semaphore only protects a
backend from saturation if the bound sits at or below that backend's actual concurrent
capacity, and for one local model on consumer GPU hardware that capacity is 1. Raising either
setting is a configuration change for a deployment with an LLM backend that genuinely serves
concurrent requests (a cloud tier, or multiple resident models) — not a code change — since the
recursive, concurrent-capable mechanism stays fully in place underneath the conservative
default. `Settings.rlm_plan_timeout_seconds` was raised from 90s to 180s to match: sequential
execution of a full research turn (plan generation, search, up to four sequential sub-agent
analyses, aggregation) measured 90–150s live on this hardware.

A later session raised all three of these numbers again — `llm_request_timeout_seconds` 30s ->
90s, `llm_circuit_breaker_failure_threshold` 3 -> 5, and `rlm_plan_timeout_seconds` 180s -> 450s
— after live testing found the 180s figure above was itself measured against calls that
happened to skip `reasoning=True`'s extra generation cost; `rlm/api.py`'s actual sub-agent and
aggregate calls run with reasoning on (so the panel can show their thinking), which the earlier
measurement did not account for, and routinely pushed individual calls well past the 30s
per-call timeout — tripping the circuit breaker and failing the whole turn, the identical
failure shape as the paragraph above, just triggered by request latency instead of concurrent
fan-out. Full investigation, including two further correctness bugs the same session found once
the turn could complete at all, in `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 27.

A third session raised `rlm_max_total_sub_agent_calls` 4 -> 8 and `rlm_plan_timeout_seconds`
450s -> 750s together, after a real quality gap surfaced by comparing the "research" route's
answer against the plain "retrieval" route's answer to the identical question
(`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 28): once that same session's `group_by_document`
fix made batching correctly respect document boundaries, 4 sub-agent calls covers only a
handful of whole documents — well under the seed corpus's real ~10-15 payment incidents for the
spec's own example question — where the previous fixed-size `batch()` had (inaccurately) spread
thinner across more of them. Raising the call count without also raising the wall-clock budget
would have just traded one failure for another (the same shape as the two raises above: more
sequential reasoning-enabled calls need more total time, not just more per-call headroom), so
both moved together, sized off live-measured 55-90s-per-call figures. `frontend/api_client.py`'s
own client-side `stream_chat_turn` timeout was found in the same pass to have already drifted
stale below the backend's *previous* 450s budget (still defaulting to an old 240s), a client-side
truncation risk with the identical shape as a circuit-breaker trip above; raised to 900s and its
rationale comment corrected to cite the current backend figure instead of a two-raises-old one.

A fourth session widened `rlm/api.py::build_search`'s department-scoped merge budget from
`top_k` to `top_k * 2`, after a live reproduction found the narrower budget could make a wrong
department guess *strictly worse than no scoping at all* — a wrong-department search still fills
every slot with irrelevant chunks (`hybrid_search` never returns "no good match" for a namespace,
only its nearest neighbors), and the pre-fix merge kept all of them unconditionally, leaving zero
room for the correct all-department result. This was put to the user as an explicit choice rather
than picked unilaterally, because it trades away something real: a scoped `search()` call can now
return up to twice its requested `top_k` chunks, growing a plan's own `batch`/`group_by_document`
count and `RLMBudget` spend correspondingly — the exact cost this module's merge call had
originally been sized to avoid. The user chose to accept that cost over a narrower,
`top_k`-preserving alternative (a fixed reservation quota for unscoped results), because live
testing showed the narrower option would not have recovered the reported case either — the
genuinely relevant chunks were buried too deep in the plain all-department ranking (rank ~18-38
of a fully-ranked ~224-chunk corpus) for any quota short of "give unscoped its own full budget"
to reach them. `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 29 has the full investigation,
including the RRF-based merge alternative that was tried and measured before being rejected.

---

## 10. Prompt-injection detection: heuristics first, classifier only when ambiguous

`docs/DELIVERY_PLAN.md` scopes Cycle 6's injection detection as "heuristics + classifier."
Decided explicitly, at the user's request, rather than assumed: the deterministic heuristic
filter (`guardrails/injection.py::heuristic_screen`) runs on every message and either confidently
blocks (a known instruction-override, data-exfiltration, or tool-abuse pattern — zero LLM calls,
zero added latency) or confidently allows. Only a message that trips a *weaker* signal — a
sensitive-sounding word with no confident pattern match — escalates to one schema-constrained
`qwen3:4b` classification call (`agents/nodes/guardrail.py`).

**Why not classify every turn:** this hardware already treats the one local model as a scarce,
easily-saturated resource — §3 and §9 above both exist because of it. Adding one more
unconditional `qwen3:4b` call to every single turn, on top of the 8–15 already budgeted per
question, would widen the latency this project already spends real effort bounding, for
coverage the heuristic filter already provides on every attack shape this project's own
acceptance test (`docs/DELIVERY_PLAN.md` criterion 5) actually exercises.

**Why not heuristics alone:** a fixed pattern list cannot catch a paraphrased or novel attack
that never matches a known shape. Reserving the classifier for exactly the messages the
heuristics could not confidently decide — not the confident allows, not the confident blocks —
spends the model's one extra call only where it adds real coverage, the same bound-the-scarce-
resource reasoning §9 already applies to RLM sub-agent concurrency.

**The classifier fails open, not closed**, when the LLM call itself errors (timeout, model
unavailable): the residual risk at that point is a paraphrased attack the classifier might have
caught, not a known one, since the heuristic filter already ran first. Every other non-
authorization dependency in this system (Pinecone, MCP, the rerank budget) degrades the same
way — continuing rather than blocking the user on an infrastructure hiccup — and authorization
itself is never at stake here, since RBAC is enforced structurally (§6), not by this guardrail.

**Where it runs:** as the graph's first node (`agents/nodes/guardrail.py`), not a check before
`graph.astream()` is called, because `docs/DELIVERY_PLAN.md` criterion 5 requires a block to be
"blocked and traced" — a check outside the graph would never appear in a LangSmith trace or the
Agent Activity Panel, and the assessment explicitly wants the evaluator able to observe the
decision, not just receive an HTTP error for it. A block is a raised `GuardrailViolationError`,
reusing `api/v1/chat.py`'s existing mid-stream `AppError` handling rather than adding new
plumbing for a new failure shape.

---

## 11. LangSmith tracing: an explicit callback, not env-var-only global tracing

The obvious implementation — set `LANGSMITH_TRACING=true` / `LANGSMITH_API_KEY` and rely on
LangChain's global, environment-variable-gated tracer to instrument every LLM call automatically
— is what Cycle 3 assumed and what Cycle 7 built first (`observability/langsmith.py::
configure_langsmith`). Live verification (not a passing test suite — 334 tests stayed green
throughout) showed it does not trace a single call made from inside a LangGraph node: a real
chat turn produced zero LangSmith runs, confirmed by querying the LangSmith API directly, even
though the identical global configuration correctly traced a bare `ChatOllama` call made outside
the graph (`main.py`'s own Ollama warm-up). `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 21 has
the full investigation.

**The decision:** attach tracing explicitly rather than keep debugging why the implicit path
doesn't reach a node's calls. `observability/langsmith.py::build_tracing_callbacks` constructs
one `LangChainTracer` (backed by its own `langsmith.Client`) once at startup, stored on
`app.state`; `api/v1/chat.py` passes it as `config["callbacks"]` on every graph invocation,
alongside `metadata`/`tags`/`run_name` for run attribution (thread id, correlation id, principal
username and role — role for filtering a trace explorer by, never for authorization itself, per
§6). LangGraph threads an explicitly-supplied `config["callbacks"]` through every node's
execution — the standard, documented mechanism for attaching a callback to a compiled graph run
— and this is what actually produced a nested trace tree (one `chat_turn` root run with 12 child
runs: guardrail, supervisor, retrieval, response, validator, both LLM calls, both conditional
edges) in live verification.

**`configure_langsmith`'s env-var wiring is kept, not removed**, for two reasons: it costs
nothing to leave in place, and it is still what any LangChain code running outside this
project's own graph invocation — a REPL, a notebook, a future integration that doesn't know
about `app.state.langsmith_callbacks` — would rely on to get traced at all.

**Why this matters beyond one bug fix:** it is the same lesson as §10's classifier-escalation
reasoning and trade-off 20's security pass, applied to observability instead of security or
cost — a component "working" in isolation (the key authenticates, one call traces) is not
evidence that a mandatory requirement ("trace every conversation") is met on the actual path an
evaluator will exercise. The fix was to verify that specific path directly, not to trust that a
generically-correct configuration generalizes to it.

---

## 12. Memory design: two mechanisms, two different jobs

ASSESSMENT.md asks for conversational memory that maintains "user context, previous questions,
and relevant historical interactions" and "survive[s] multiple turns during a session," and asks
for the design to be explained. Two separate, independently-testable mechanisms answer that,
each doing a job the other cannot:

**1. Turn-to-turn persistence — the `AsyncPostgresSaver` checkpointer (Cycle 3).** Every graph
invocation is keyed by the caller's own `thread_id` (`api/v1/chat.py`); LangGraph's checkpointer
resumes that thread's full prior `AgentState` — including every message — with no application
code re-supplying it. This is what makes "memory survives multiple turns" true at all: verified
live by resuming the same `thread_id` across two separate HTTP requests and confirming the
second turn's prompt to the model already contained the first turn's exchange. Choosing Postgres
over an in-memory checkpointer (LangGraph ships both) was deliberate: an in-memory checkpointer
loses every conversation on a backend restart, which is a real failure mode during a multi-hour
development or demo session, not a hypothetical one — the same "durable over convenient" bias as
using Postgres for the rate-limit bucket and the rerank budget counter rather than process
memory.

**2. Context-window bounding — rolling-summary memory (`memory/summarizer.py`,
`memory/session.py`).** The checkpointer alone would let a long thread's raw message list grow
without bound, which a 4B model's limited context window (`docs/DECISIONS.md` §3) cannot
absorb forever. Once a thread's verbatim message count exceeds `memory_max_verbatim_messages`,
`summarize_oldest` folds the oldest `memory_summarize_batch_size` messages into a single rolling
summary string via one schema-constrained LLM call, then drops those exact messages from state
via LangGraph's documented `RemoveMessage` mechanism. `build_context_messages` is the one place
that assembles `(system prompt, rolling summary, recent verbatim messages)` into what the model
actually sees, so the Supervisor and Response nodes cannot each re-derive "how much history to
include" differently. `needs_summarization` is a pure predicate split from the LLM-calling
`summarize_oldest`, specifically so the trigger boundary (`exactly at the threshold`, `one
below`, `one above`) is a plain, fast unit test rather than something that can only be checked
by running a real summarization call.

**What this buys, and what it costs:** a thread can run indefinitely without ever exceeding the
model's context window, and recent exchanges stay available verbatim for a follow-up question
that depends on exact wording (a policy quote, a number). The cost is that anything folded into
the summary is now paraphrased, not verbatim — a follow-up asking about the *exact phrasing* of
something from many turns ago will not get it back. This is an accepted trade-off, not an
oversight: `docs/DECISIONS.md` §2 already scopes long-term memory (a persistent, cross-session
store) out as a bonus item not built, and the two mechanisms above are explicitly *session*
memory — "session" meaning "this `thread_id`," not "this browser session" or "this user across
every conversation they've ever had" (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` assumption 3).

---

## 13. `aggregate`'s citation-completeness check and bounded retry, and why it stops there

`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 28's investigation (first and second addenda)
root-caused the `"research"` route occasionally producing a visibly worse answer than
`"retrieval"` on the identical question to irreducible LLM-sampling variance across the RLM
pipeline's 3–4 sequential generative calls, not a coverage gap or a code defect — every stage,
replayed independently against the live model, was individually correct. The user, asked
explicitly whether to mitigate this or accept it as documented variance, chose a targeted
mitigation over both extremes ("accept as-is" and "no large architectural change"), scoped to
exactly one place: `rlm/api.py::build_aggregate`, the step that reduces several independently-
correct sub-agent findings into the one summary a research turn's answer is actually built from.

**Why `aggregate` specifically, not the whole pipeline.** A self-consistency or retry mechanism
added at every stage (sub-agent findings, `aggregate`, the final Response call) would multiply
latency across an already-expensive pipeline (`docs/DECISIONS.md` §9) for diminishing return —
`aggregate` is the one stage that both *sees everything* (every sub-agent's finding, in one
call) and is the last point before that information either survives into the final answer or is
lost for good. A dropped citation here is unrecoverable downstream; a slightly-imperfect
individual sub-agent finding usually is not, since `aggregate` synthesizes across several of
them.

**The mechanism, deliberately narrow:**

1. **Citation-presence check, not a correctness check.** `_missing_citations` compares every
   bracketed `[Title]` the raw findings actually contained against those the aggregated
   `summary` mentions — checking that the aggregate prompt's own existing instruction ("keep
   their citations intact") was followed, not re-judging whether the *content* is accurate.
   This is deliberately not a fuzzy or semantic check: citations are a small, well-defined
   substring the prompt already asks to preserve verbatim, so checking for their literal
   presence catches real evidence loss without penalizing legitimate paraphrasing of everything
   else — the brittleness the user explicitly asked to avoid would come from diffing prose or
   phrasing, not from checking whether a citation survived at all.
2. **At most one retry**, firing only when the check finds a gap, feeding back exactly which
   citations were dropped (the same "one retry with concrete feedback" shape
   `rlm/planner.py::_retry_messages` already uses for AST validation failures, applied here to
   an incompleteness signal instead of a syntax one). Never unconditional, never loops: an
   `AppError` on the retry call, or a retry that does not actually reduce how many citations are
   missing, both fall back to the original, already-valid (if incomplete) result rather than
   discarding it or trying again.
3. **A lower temperature** (`_AGGREGATE_TEMPERATURE = 0.2`) on both the initial and retry calls
   — threaded through a new optional `temperature: float | None = None` parameter added to
   `LLMProvider.astructured` (and `OllamaProvider`/`FallbackChain`'s implementations), defaulting
   to `None` everywhere else so no existing call site's behavior changes. `aggregate` is a
   one-shot reduction over an already-fixed, already-correct set of findings — synthesis, not
   exploration — unlike the RLM planner, which benefits from the provider's default temperature
   because generating a varied *strategy* is exactly what that step should be free to do.

**What this does and does not claim to fix.** This is explicitly a *completeness* guard, not a
*correctness* one: it can only recover evidence a sub-agent actually returned that aggregation
then dropped. It has no visibility into, and does not claim to address, evidence the search or
planning stages failed to retrieve in the first place (trade-off 28's first addendum's "narrow
plan" problem is a different failure mode this does not touch), nor a sub-agent's own finding
being wrong, nor the aggregated summary reaching an internally inconsistent conclusion despite
complete evidence (observed live in the same verification pass that confirmed the fix working —
see trade-off 28's third addendum). Distinguishing what a fix at this one stage can and cannot
plausibly cover, rather than letting a narrow, verified improvement read as "the variance
problem is now solved," was treated as more important than looking maximally finished.

**Verified live, not just unit-tested — twice, independently.** 5 new tests
(`tests/rlm/test_api.py`'s `TestAggregateCompletenessCheck`, 373 total) cover a clean pass, a
retry that improves, a retry that does not, and a retry call that itself fails — but the
mitigation was also replayed through the *real* `build_aggregate` wiring against live Ollama
twice: once against real sub-agent findings from the original investigation, and once (after
the fix had already shipped) on a completely fresh live draw of the whole pipeline, requested
specifically to rule out the first result being a one-off. Both runs demonstrated the fix
earning its keep, not merely passing tests: both times the initial `aggregate` call's summary
dropped several real citations on its first attempt, and both times the bounded retry fired
automatically and recovered a complete, correctly-cited summary — direct, twice-replicated
evidence that the specific failure mode this was built for is real and recoverable, not a
one-off or merely theoretically possible. The second run also surfaced, live, the concrete
shape of the boundary described above: one sub-agent call failed outright (a genuine Ollama
timeout), losing that batch's evidence before it ever reached `aggregate` — a real instance of
the "evidence lost upstream" case this fix does not and cannot cover, exactly as scoped. Full
narrative of both runs in `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 28's third and fourth
addenda.

---

## 14. The Tools node's choice schema always offers an explicit decline option

A user reported `python_analysis` failing with `NameError: name 'python_analysis' is not
defined` on a question the `"tools"` route has no way to actually serve — it has no retrieval
step, so with nothing real to compute over, the model either echoed the tool's own name back as
literal (non-runnable) code, or fabricated an entirely invented dataset and confidently computed
over it as if real (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 30). Three options were put to
the user rather than one picked unilaterally: sharpen the routing/tool-description prompts alone;
sharpen them plus add an anti-fabrication instruction as a fill-args-level safety net; or a
structural fix, adding an explicit "no suitable tool" option to `agents/nodes/tools.py::
_build_choice_schema`'s `Literal` so the model is never forced to name a tool at all. The user
chose the prompt-level fix first, then — after live re-verification showed the fill-args safety
net alone did not reliably prevent fabrication, and that the choice schema had no way to express
"none of these fit" even when the model's own `reasoning` field said exactly that — explicitly
asked for the structural fix on top.

**Why this is the right layer to fix it at, not just a prompt.** Prompting can make a wrong
choice *less likely*; it cannot make a *forced* choice safe, because the schema itself defines
the space of things the model is even allowed to say. Adding `_NO_SUITABLE_TOOL` to the
`Literal` alongside the real tool names — always, regardless of which tools a role has — removes
the forcing function directly: `tools_node` short-circuits on that choice before the fill-args
call ever runs, so there is no `code`/`data` for the model to fabricate in the first place. This
mirrors §10's classifier-escalation reasoning applied to a different layer: don't rely on
prompting to prevent a model from doing something it structurally *can* still do; change what it
can do instead.

**Live-verified, not assumed to close the gap**: the real choice-stage call, 8 times for the
identical question, now declines via `_NO_SUITABLE_TOOL` 7 of 8 times (vs. 0 of 3 before this
fix existed); the real `tools_node` function itself, 5 times in a row, produced the clean
short-circuit every time — correct event sequence, no fill-args call, no chance to fabricate.
Not claimed as eliminating the risk entirely: the 1-of-8 residual case is recorded, not smoothed
over, in `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 30's addendum.
