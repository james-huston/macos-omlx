# Story 002: Evaluate coding-specialist models for Hermes / agent workloads

## Status

InProgress

<!-- Draft -> Approved -> InProgress -> Review -> Done. Keep docs/stories/README.md table in sync. -->

## Story

**As a** developer using Hermes (and other agents) against this Mac Studio through LiteLLM,
**I want** to evaluate a short list of coding-specialist MLX models that we do not already host,
**so that** we can pick one or two to add to `models.yaml` (worker and/or alternate brain) with
measured smoke, tool-use, and real coding-task results — without oversubscribing 128 GB unified memory.

## Context / Why

- Current hosted stack (story 001): pinned `gpt-oss-120b` + `qwen35-35b-a3b` + embedding; unpinned
  alt brain `qwen35-122b-a10b`. Strong general/agent models, but no **coding-specialist** lane.
- Buddy recommendation: GLM family for coding. Full GLM-4.6 / 4.7 / 5.x do **not** fit 128 GB
  (~185 GB+ at 4-bit). Practical GLM picks: **Flash** (worker-sized) and **Air** (brain-sized).
- Separate coding gap: **Qwen3-Coder** line (Coder-Next + Coder-30B-A3B) is repeatedly recommended
  for local coding agents on Apple Silicon and is not in our manifest.
- Explicitly **out of scope for this story’s shortlist:** Devstral Small 2 (strong code, weak tool
  calling — poor Hermes fit), MiniMax M2.5 / full GLM flagships / Qwen3-Coder-480B (too large).

### Shortlist (test these)

| Priority | Manifest name (proposed) | HF repo | Role to evaluate | Rough resident size | Notes |
|----------|--------------------------|---------|------------------|---------------------|-------|
| Must | `qwen3-coder-next` | `mlx-community/Qwen3-Coder-Next-4bit` | coding brain / alt | ~45 GB (verify) | Top MLX coding-agent candidate; optional 6bit later |
| Should | `qwen3-coder-30b` | `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` | coding worker | ~18 GB (verify) | Compare vs pinned `qwen35-35b-a3b` |
| Should | `glm47-flash` | `mlx-community/GLM-4.7-Flash-4bit` | coding worker | ~17–20 GB (verify) | Buddy family; battlemage already has GGUF flash variants |
| Should | `glm45-air` | `mlx-community/GLM-4.5-Air-4bit` | coding brain / alt | ~60 GB (verify) | Compare vs `gpt-oss-120b` / Coder-Next |

Optional follow-up (not required to close this story): `GLM-4.7-Flash-8bit` if 4bit quality is soft
but tool-use passes; `Qwen3-Coder-Next-6bit` if 4bit quality is soft and RAM allows after unload.

## Acceptance Criteria

1. Shortlist models are documented in `models.yaml` as **candidates** (`enabled: false`) or
   temporarily enabled for evaluation, with verified HF `repo` ids, `est_mem_gb`, roles, and notes
   that they must not violate the dual-brain pin rule.
2. Each shortlist model can be downloaded via `make models-apply` (or an explicit one-model apply
   path) without breaking the existing pinned stack’s budget check when left disabled / unpinned.
3. For each downloaded model: smoke via `make test-models MODELS='…'` succeeds when that model is
   loadable (unload conflicting pinned brain first for Air / Coder-Next if needed).
4. For each downloaded LLM: `make tool-use MODELS='…'` (or `./tests/tool_use/run.sh`) records
   agentic-scale + required_tool_choice outcomes in the story Completion Notes (same style as 001).
5. Side-by-side **coding task** notes (Hermes or equivalent) comparing at least:
   - coding worker lane: Flash and/or Coder-30B vs current `qwen35-35b-a3b`
   - coding brain lane: Coder-Next and/or Air vs current `gpt-oss-120b` (one at a time)
6. Bench numbers captured for survivors (`make bench MODELS='…'` after appropriate unload) and
   optionally written back with `UPDATE_MANIFEST=1`.
7. Decision recorded in Completion Notes: **promote / keep candidate / drop** per model, with
   residency plan (pin vs TTL, what to unpin).
8. LiteLLM: only promoted models get a durable sync (`make sync-litellm`); candidates may be synced
   temporarily for Hermes tests then removed or left disabled. Public names use
   `LITELLM_MODEL_PREFIX` (no hostnames hardcoded in tracked files).
9. Hermes / app-config templates updated only if a model is promoted (use
   `${LITELLM_MODEL_PREFIX}…` placeholders; regenerate with `make app-configs`).
10. Story status moves Draft → … → Review with README table updated; no secrets or internal
    hostnames committed.

## Tasks / Subtasks

- [x] Confirm HF ids + on-disk / resident sizes for the shortlist; fill `est_mem_gb` (AC: 1, 2)
  - [x] `Qwen3-Coder-Next-4bit` (~42G disk / ~44 GB est)
  - [x] `Qwen3-Coder-30B-A3B-Instruct-4bit` (~16G disk / ~17 GB est)
  - [x] `GLM-4.7-Flash-4bit` (~16G disk / ~17 GB est)
  - [x] `GLM-4.5-Air-4bit` (~56G disk / ~59 GB est)
- [x] Add shortlist entries to `models.yaml` (enabled, unpinned TTL) (AC: 1)
- [x] Download + Admin reload/settings; never pin two brains (AC: 2, 3)
  - [x] Workers (Flash, Coder-30B) with current pinned stack still up
  - [x] Brains (Coder-Next, Air): unpin/unload `gpt-oss-120b` before load
- [x] Smoke + tool-use suite per model; paste results into Completion Notes (AC: 3, 4)
- [x] Coding challenge matrix (idempotent ledger + hidden pytest) (AC: 5)
- [x] Quick bench survivors (`QUICK=1`); decode_tps noted in yaml (AC: 6)
- [ ] Write final promote/drop decision after Hermes matrix (AC: 7) — provisional below
- [x] Synced all four to LiteLLM for Hermes trials (AC: 8); templates unchanged until promote (AC: 9)
- [ ] Update this story + `docs/stories/README.md` to Review after Hermes matrix (AC: 10)

## Dev Notes

### Memory / residency (128 GB)

From `models.yaml` budget rules:

- Pinned today ≈ 63 + 20 + 1 + reserved/KV headroom — dual **pinned** brains are forbidden.
- Coding **workers** (~18–20 GB) can usually coreside with the current pinned stack.
- Coding **brains** (~45–60 GB) need the primary brain unloaded (same pattern as `qwen35-122b-a10b`).
- Wired limit is already raised on this host; still watch oMLX 507 / dynamic ceiling errors.

### Commands

```bash
# After editing models.yaml
make models-apply DRY_RUN=1
make models-apply

# Smoke (adjust names to manifest `name:`)
make test-models MODELS='glm47-flash qwen3-coder-30b'
# After unpin/unload gpt-oss:
make test-models MODELS='qwen3-coder-next glm45-air'

make tool-use MODELS='…'
make bench MODELS='…'
make sync-litellm DRY_RUN=1 && make sync-litellm   # only when ready
make app-configs
```

Unpin / unload primary brain via oMLX Admin UI or Admin API (`is_pinned: false` then unload)
before loading Air / Coder-Next. Re-pin `gpt-oss-120b` when evaluation is done unless the
decision is to replace it.

### Comparison baselines

| Lane | Baseline already hosted |
|------|-------------------------|
| Worker / fast coding | `qwen35-35b-a3b` |
| Brain / agent coding | `gpt-oss-120b` (and optionally `qwen35-122b-a10b`) |

### Skip list (document, do not download in this story)

- Full `GLM-4.6` / `GLM-4.7` / `GLM-5.x` — do not fit 128 GB
- Devstral Small 2 — tool-calling too weak for Hermes
- Distill / abliterated Flash forks — evaluate only after baseline Flash

### References

- HF: [Qwen3-Coder-Next-4bit](https://huggingface.co/mlx-community/Qwen3-Coder-Next-4bit),
  [Qwen3-Coder-30B-A3B-Instruct-4bit](https://huggingface.co/mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit),
  [GLM-4.7-Flash-4bit](https://huggingface.co/mlx-community/GLM-4.7-Flash-4bit),
  [GLM-4.5-Air-4bit](https://huggingface.co/mlx-community/GLM-4.5-Air-4bit)
- Sibling patterns: story 001 Completion Notes; `models.yaml` dual-brain rule; LiteLLM prefix +
  `make app-configs` templates

### Testing

- Automated: `make test-models`, `./tests/tool_use/run.sh`, `make bench`
- Manual: one fixed Hermes coding task (multi-file edit + tools) per model under test
- Regressions: existing pinned models still smoke OK after evaluation; LiteLLM foreign/unowned
  models untouched

## Change Log

| Date | Version | Description | Author |
|------|---------|-------------|--------|
| 2026-11-16 | 0.1 | Initial draft — shortlist Qwen3-Coder + GLM Flash/Air | James / Auto |
| 2026-11-16 | 0.2 | Downloads, smoke, tool-use, quick bench; LiteLLM sync; provisional lean | Auto |
| 2026-11-16 | 0.3 | Idempotent-ledger coding eval harness + four-model scorecard | Auto |

## Dev Agent Record

### Completion Notes

**2026-11-16 — automated eval on Studio**

Downloads + `/admin/api/reload` → 8 models discovered. Aliases applied via `make models-apply NO_DOWNLOAD=1`.
LiteLLM: added `bingc-studio5-{qwen3-coder-next,qwen3-coder-30b,glm47-flash,glm45-air}`.
Primary `gpt-oss-120b` re-pinned after brain tests.

| Model | Smoke | Agentic-scale | required_tool_choice | parallel | Decode @1k | Notes |
|-------|-------|---------------|----------------------|----------|------------|-------|
| `qwen3-coder-30b` | OK | PASS | FAIL | PASS | ~146 tok/s | Concurrency@2 ~flat (0.95×) |
| `glm47-flash` | OK | PASS | FAIL | PASS | ~110 tok/s | Concurrency@2 ~1.4× |
| `qwen3-coder-next` | OK | PASS | FAIL | PASS | ~106 tok/s | Needs gpt-oss unloaded; prefix-cache −92% TTFT |
| `glm45-air` | OK | PASS | FAIL | PASS | ~59 tok/s | Slower brain; needs gpt-oss unloaded |

`required_tool_choice` FAIL matches existing gpt-oss / qwen35 workers — mark `supports_tool_choice: false`.

**Provisional promote lean (pending Hermes coding matrix):**
- Brain lane: prefer **`qwen3-coder-next`** over Air (speed + coding-specialist).
- Worker lane: **`glm47-flash`** if concurrency matters; **`qwen3-coder-30b`** if Qwen-family consistency / peak single-stream speed.
- Air: keep as optional alt or drop if Hermes matrix doesn’t beat Coder-Next.

**Coding challenge (2026-11-16):** mid-level *idempotent ledger* (`tests/coding_eval/`).
Hidden oracle: 13 pytest cases. Model must emit `ledger.py` only; we score it.

| Model | Result | Notes |
|-------|--------|-------|
| `qwen3-coder-next` | **13/13 PASS** | Clean; ~20s gen |
| `glm45-air` | **13/13 PASS** | Clean; ~78s gen |
| `qwen3-coder-30b` | 9/13 FAIL | Idempotency bugs + typo `_account` vs `_accounts` |
| `glm47-flash` | 4/13 FAIL* | First run: thinking burned 4096 tokens (PARSE_FAIL). Retry w/ thinking off: code emitted but missing `__init__` (`_idempotency_store`) |

\*Flash needs `chat_template_kwargs.enable_thinking=false` (wired into `run_eval.py`).

**Provisional promote lean (updated):**
- Brain: **`qwen3-coder-next`** (speed + perfect challenge) over Air (also perfect, slower).
- Worker: neither Flash nor Coder-30B cleared the ledger bar; keep both unpinned for more tasks or prefer Coder-Next as coding lane and keep `qwen35-35b-a3b` as general worker.
- Still open: final promote/drop + Hermes live smoke if desired (AC 7).

### File List

- `models.yaml` (shortlist enabled + measured sizes/tps)
- `docs/stories/002-evaluate-coding-models.md`, `docs/stories/README.md`
- `tests/coding_eval/` (PROMPT, hidden tests, `run_eval.py`)
- `benchmarks/results/2026-11-16-host.jsonl`

## QA Results
