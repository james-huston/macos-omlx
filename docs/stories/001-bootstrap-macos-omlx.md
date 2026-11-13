# Story 001: Bootstrap macos-omlx — oMLX model host for Mac Studio M5 Max, owner-scoped LiteLLM sync, benchmarking

## Status

Review

<!-- Draft -> Approved -> InProgress -> Review -> Done. Keep docs/stories/README.md table in sync. -->

## Story

**As a** developer running a home LLM fleet behind one LiteLLM proxy (an Intel Arc B70 box served by
[`battlemage-llama`](https://github.com/james-huston/battlemage-llama), plus paid API models),
**I want** a `james-huston/macos-omlx` repo that sets up my **Mac Studio (M5 Max, 128 GB unified memory, 614 GB/s)** as an
[oMLX](https://github.com/jundot/omlx) model host. The repo should be managed declaratively from `models.yaml`. It should
sync **only the models this host owns** into LiteLLM, and it should include smoke tests, tool-calling tests and a
benchmark/tracking workflow like the one in battlemage-llama,
**so that** I can run several local MLX models at once (agent workloads, including
[Hermes Agent](https://hermes-agent.nousresearch.com/)) and reach them from any app through LiteLLM. Each host must be
able to add and remove its own models without ever touching another host's models or my paid models.

## Context / Why

- **Reference repo:** `~/github.com/james-huston/battlemage-llama` (local) / https://github.com/james-huston/battlemage-llama.
  Sibling ergonomics: same Makefile targets, `models.yaml`, `.env` precedence, stdlib-first Python, BMAD under
  `docs/stories/` (with a thin `docs/improvements/` pointer).
- **Why oMLX** (decided): MLX engine, continuous batching, multi-model residency, tiered RAM+SSD KV cache, OpenAI/Anthropic APIs.
- **Differs from battlemage:** no Docker (Metal); native Homebrew; manage memory budget + concurrency, not VRAM swapping.

## Acceptance Criteria

### Setup & host

1. `scripts/setup.sh` (`make setup`) is idempotent; supports `--dry-run` and `--yes`. Preflight arm64 / macOS 15+ /
   Homebrew / Python 3.11–3.13; installs oMLX via brew (+ helpers); creates `.env` with generated masked `OMLX_API_KEY`;
   persists oMLX settings; starts brew services; opt-in pmset + wired-limit LaunchDaemon; prints reachable URLs.
2. `docs/host-setup.md` covers the manual equivalent and undos.

### Declarative models

3. `models.yaml` source of truth with `owner`, `memory`, `cost`, and per-model fields as specified.
4. `scripts/apply-models.py` downloads, Admin API settings, memory-budget check, stale-dir report.
5. Starter 128 GB manifest with verified HF ids (brain / alt brain / worker / embedding / optional VLM).

### LiteLLM — owner-scoped sync

6. `scripts/sync-litellm.py` owner-scoped; key by `model_info.id`; collision skip; `--reset` / `--adopt` / `--allow-shared-names`.
7. Ownership via `model_info.managed_by`; `--self-test` probe; document multi-host contract.
8. `test-models.py --via litellm` filters owned entries by default (`--all` restores everything).

### Testing, status, benchmarks

9. `scripts/test-models.py` smoke tests including embeddings.
10. `tests/tool_use/` with seven probes + agentic-scale probe.
11. `scripts/status.py` oMLX + memory + optional power.
12. `scripts/bench.py` single-stream, concurrency, prefix-cache, mixed, optional power.
13. Benchmark jsonl tracked; `make bench-report`; `--update-manifest` via ruamel.

### Clients & docs

14. `docs/app-configs/` Hermes + LiteLLM static example.
15. README + troubleshooting + upgrading + stories/investigations scaffolding + LICENSE + `.gitignore`.
16. Scripts have `--help`, non-zero on failure, never print secrets; `make help` lists targets.

## Tasks / Subtasks

- [x] Study the reference repo (AC: all)
- [x] Scaffold the repo: layout, `LICENSE`, `.gitignore`, `.env.example`, `Makefile` with `help` (AC: 15, 16)
- [x] `scripts/setup.sh` + `docs/host-setup.md` (AC: 1, 2)
- [x] `models.yaml` schema + starter manifest with verified HF repo ids and sizes (AC: 3, 5)
- [x] `scripts/apply-models.py` with downloads, Admin API settings and budget check (AC: 4)
- [x] `scripts/sync-litellm.py`, owner-scoped (AC: 6, 7)
- [x] `scripts/test-models.py`, ported, owner-filtered `--via litellm`, embeddings path (AC: 8, 9)
- [x] `tests/tool_use/`, ported + agentic-scale probe (AC: 10)
- [x] `scripts/status.py` (AC: 11)
- [x] `scripts/bench.py` + `make bench` / `bench-report` + jsonl + `--update-manifest` (AC: 12, 13)
- [x] `docs/app-configs/` (Hermes, LiteLLM static example, README checklist) (AC: 14)
- [x] README, troubleshooting, upgrading, improvements/investigations scaffolding (AC: 15)
- [x] End-to-end validation on the real Mac; record results in Completion Notes
- [x] Update `docs/stories/README.md` status to Review

## Dev Notes

### Decisions (2026-11-13)

- BMAD canonical path: `docs/stories/`; thin pointer at `docs/improvements/`.
- Installs: Homebrew-first; try custom kernels when Xcode present, else plain brew + document upgrade path.
- Reference clone: `/Users/hustonjs/github.com/james-huston/battlemage-llama`.

### Verified HF starter ids

| Role | Repo | ~size |
|------|------|------:|
| brain (pinned) | `mlx-community/gpt-oss-120b-MXFP4-Q4` | ~62 GB |
| alt brain | `mlx-community/Qwen3.5-122B-A10B-4bit` | ~70 GB |
| worker | `mlx-community/Qwen3.5-35B-A3B-4bit` | ~20 GB |
| embedding | `mlx-community/Qwen3-Embedding-0.6B-8bit` | ~0.65 GB |

### oMLX Admin API (notes)

- Prefix is **`/admin/api/...`** (not `/api/...`): `GET/PUT /admin/api/models/{id}/settings`.
- Nested `~/.omlx/settings.json` (version 1.0): `server`, `auth`, `model`, `scheduler`, `memory`, `cache`.
- Brew service runs bare `omlx serve` — rewrite settings.json then `omlx restart`.
- Auth: Bearer main API key required on non-loopback (401 without key).

### Testing

See Completion Notes for on-box results.

## Change Log

| Date | Version | Description | Author |
|------|---------|-------------|--------|
| 2026-11-13 | 0.1 | Initial story | James Huston (drafted with Claude) |
| 2026-11-13 | 0.2 | Status → InProgress; docs/stories path; brew-first + verified HF ids | Auto (implementing) |
| 2026-11-13 | 0.3 | Implemented + on-box validated; Status → Review | Auto |

## Dev Agent Record

### Completion Notes

**Host / setup**
- `make setup` / `./scripts/setup.sh --yes`: idempotent second run reports no changes.
- oMLX `0.8.0rc1` via Homebrew (no Xcode → no custom kernels; documented).
- Binds `0.0.0.0:8000`; `Authorization: Bearer` required (401 without).
- Reachable over LAN, `.local`, and Tailscale (URLs kept in local `.env` only).
- `memory_guard_tier=balanced` required for brain+worker residency (`safe` was too tight).
- **Wired-limit LaunchDaemon not installed** — sudo password unavailable in agent session. Run:
  `./scripts/setup.sh --yes --tune-wired` (or see `docs/host-setup.md`). Still `iogpu.wired_limit_mb=0`.

**Models (on disk under `~/.omlx/models/mlx-community/`)**
| Manifest name | Disk | Notes |
|---|---:|---|
| gpt-oss-120b | 58 GB | pinned; smoke OK with worker+embedding |
| qwen35-35b-a3b | 19 GB | pinned; smoke OK |
| qwen3-embedding-0.6b | 619 MB | pinned; embeddings OK |
| qwen35-122b-a10b | 65 GB | unpinned+TTL; smoke OK only after unloading primary brain |

**LiteLLM**
- `--self-test` passed (`model_info.managed_by` persists).
- Owner-scoped sync: 28 unowned battlemage/UI models untouched; only `managed_by=macos-omlx` mutated.
- Accepts `LITELLM_ADMIN_KEY` (from `~/.zshrc`).
- `make test-models VIA=litellm`: gpt-oss / worker / embedding OK; alt brain 507 while primary resident (expected).

**Tool use**
- Worker + brain: agentic-scale **PASS**; `required_tool_choice` **FAIL** on both → `supports_tool_choice: false` in yaml.
- Brain parallel_tool_calls advisory WARN.

**Bench (quick, worker)**
- Decode ~145 tok/s @1k; concurrency x2 aggregate 1.27× (flagged <1.3× threshold).
- Prefix-cache TTFT 0.96s → 0.10s (−90%).
- Results: `benchmarks/results/2026-11-13-host.jsonl`.

**Deferred / follow-ups**
- User: install wired-limit daemon (`--tune-wired`) and optionally Xcode+custom kernels.
- Hermes live task + `hermes fallback check --live` not run.
- Full `make bench` (32k/64k, mixed, power) and `--update-manifest` not run.
- Battlemage companion owner-scoped sync still required before battlemage sync without `NO_DELETE=1`.

### File List

- `LICENSE`, `Makefile`, `README.md`, `.gitignore`, `.env.example`, `models.yaml`
- `scripts/setup.sh`, `scripts/apply-models.py`, `scripts/sync-litellm.py`, `scripts/render-app-configs.py`, `scripts/test-models.py`, `scripts/status.py`, `scripts/bench.py`, `scripts/bench-report.py`
- `scripts/launchdaemons/com.macos-omlx.wired-limit.plist`
- `tests/tool_use/` (`run.sh`, `test_tools.py`, `requirements.txt`, `README.md`, `.gitignore`)
- `docs/stories/` (`README.md`, `_TEMPLATE.md`, `001-bootstrap-macos-omlx.md`)
- `docs/improvements/README.md` (pointer)
- `docs/host-setup.md`, `docs/troubleshooting.md`, `docs/upgrading.md`, `docs/investigations/README.md`
- `docs/app-configs/` (`README.md`, `*.yaml.template`; render via `make app-configs`)
- `benchmarks/README.md`, `benchmarks/results/.gitkeep`, `benchmarks/results/2026-11-13-host.jsonl`

## QA Results
