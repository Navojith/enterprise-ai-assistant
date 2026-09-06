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

**In scope beyond the required minimum:** MCP server, reranking layer.
**Explicitly out of scope:** human-in-the-loop approval, long-term memory, answer-quality feedback
loop, full containerization of application services. These are bonus items sacrificed to the time budget.

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

2. **Postgres in Docker Compose vs. containerization not selected.** `docker-compose.yml` runs
   **Postgres only**. The backend, Streamlit frontend and MCP server run natively against it. Full
   containerization was a bonus item that was not selected.

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
