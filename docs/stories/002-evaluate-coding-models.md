# Story 002: Evaluate coding-specialist models for Hermes / agent workloads

## Status

Draft

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

- [ ] Confirm HF ids + on-disk / resident sizes for the shortlist; fill `est_mem_gb` (AC: 1, 2)
  - [ ] `Qwen3-Coder-Next-4bit`
  - [ ] `Qwen3-Coder-30B-A3B-Instruct-4bit`
  - [ ] `GLM-4.7-Flash-4bit`
  - [ ] `GLM-4.5-Air-4bit`
- [ ] Add shortlist entries to `models.yaml` (candidates first) (AC: 1)
- [ ] Download one-at-a-time; never pin two brains (AC: 2, 3)
  - [ ] Workers (Flash, Coder-30B) with current pinned stack still up
  - [ ] Brains (Coder-Next, Air): unpin/unload `gpt-oss-120b` before load
- [ ] Smoke + tool-use suite per model; paste results into Completion Notes (AC: 3, 4)
- [ ] Hermes coding task matrix (same prompt/repo for all lanes) (AC: 5)
- [ ] Bench survivors; optional `--update-manifest` (AC: 6)
- [ ] Write promote/drop decision + residency plan (AC: 7)
- [ ] If promoting: enable in yaml, sync LiteLLM, refresh app-config templates (AC: 8, 9)
- [ ] Update this story + `docs/stories/README.md` to Review (AC: 10)

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

## Dev Agent Record

### Completion Notes

### File List

## QA Results
