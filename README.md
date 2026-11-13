# macos-omlx

Declarative [oMLX](https://github.com/jundot/omlx) host for a **Mac Studio (M5 Max,
128 GB)** — sibling to [`battlemage-llama`](https://github.com/james-huston/battlemage-llama).
`models.yaml` is the source of truth; LiteLLM sync is **owner-scoped** so this
Mac never deletes another host's models or paid API entries.

## Why this exists

Home LLM fleet: Intel Arc B70 (battlemage-llama) + paid APIs behind one LiteLLM
proxy, plus this Mac as an MLX host for concurrent agent workloads (including
[Hermes Agent](https://hermes-agent.nousresearch.com/)).

oMLX (not Ollama) was chosen for continuous batching, multi-model residency with
pin/TTL/LRU + memory guard, and tiered RAM+SSD KV cache. Everything runs
**natively via Homebrew** — Metal is not available inside Docker on macOS.

## Prerequisites

- Apple Silicon, macOS 15+
- Homebrew
- Python 3.11–3.13 (`brew install python@3.12`)
- Optional: full Xcode + Metal toolchain for `omlx --with-custom-kernel` (Qwen3.5)

## Quick start

```bash
git clone https://github.com/james-huston/macos-omlx.git
cd macos-omlx

make setup                         # idempotent; YES=1 for non-interactive
# edit .env — set MODEL_API_BASE to a URL LiteLLM can reach (not localhost)

make models-apply                  # download + pin/TTL/alias via Admin API
make sync-litellm DRY_RUN=1
make sync-litellm                  # owner-scoped; see below
make test-models
make bench
```

## Architecture

```
Clients (Hermes, Open WebUI, scripts, …)
    │
    ▼
 LiteLLM proxy  ──►  battlemage-llama (Arc B70)
    │
    └──►  oMLX :8000  ──►  MLX / Metal  (this Mac)
```

## `models.yaml`

Edit the manifest, then `make models-apply`. Enabled entries download into
`OMLX_MODEL_DIR` as `org/name/`, apply Admin API settings, and run a memory
budget check (pinned weights + KV headroom + OS reserve ≤ 128 GB). Stale
download dirs are reported, never auto-deleted.

Starter plan (128 GB): pinned **gpt-oss-120b** brain + **Qwen3.5-35B-A3B** worker
+ **Qwen3-Embedding-0.6B**; alternate **Qwen3.5-122B-A10B** unpinned with TTL.
Never pin both brains at once.

## Syncing to LiteLLM (ownership)

```bash
make sync-litellm                  # DRY_RUN=1  NO_DELETE=1  RESET=1  ADOPT=1
make sync-litellm SELF_TEST=1      # verify custom model_info keys persist
```

Every model this script creates carries `model_info.managed_by: macos-omlx`
(override with `LITELLM_OWNER`). It **only** re-points or deletes entries with
that owner. Untagged, static (`db_model: false`), and foreign-owned entries are
left alone. Existing rows are keyed by `model_info.id`, not `model_name`.

LiteLLM **dropdown names** get a host prefix by default (short hostname + `-`,
e.g. `bingc-studio5-gpt-oss-120b`) so Hermes / OpenCode can tell this Mac apart
from battlemage. Change with `LITELLM_MODEL_PREFIX` in `.env`,
`make sync-litellm MODEL_PREFIX=…`, or `litellm_model_prefix` in `models.yaml`.
Empty string disables. Upstream oMLX ids stay unprefixed.

### Multiple hosts, one LiteLLM

| Tag | Meaning |
|-----|---------|
| `managed_by: <owner>` | That host may mutate the entry |
| no `managed_by` | Unowned — hands off (unless `--adopt` on matching `api_base`) |
| static config | Invisible to DB sync |

**Do not** use `litellm_params.tags` for ownership — those participate in
tag-based routing.

> **Rollout warning:** until battlemage-llama gets the same owner-scoped sync,
> its `make sync-litellm` without `NO_DELETE=1` **will delete this host's
> models**. Keep battlemage on `NO_DELETE=1` until that companion change lands.

## Testing

```bash
make test-models                   # direct oMLX
make test-models VIA=litellm       # only this owner's LiteLLM entries
make test-models VIA=litellm ALL=1 # everything LiteLLM advertises
make tool-use                      # tool-calling suite (+ agentic-scale probe)
make status                        # health, pins, memory pressure, wired limit
```

## Benchmarking

```bash
make bench                         # QUICK=1 LONG=1 POWER=1 MODELS='…'
make bench-report                  # regenerate benchmarks/README.md from jsonl
make bench UPDATE_MANIFEST=1       # write decode_tps etc. back into models.yaml
```

Results append to `benchmarks/results/<YYYY-MM-DD>-<host>.jsonl` (tracked in git).

## Hermes

See [`docs/app-configs/hermes-config.yaml`](docs/app-configs/hermes-config.yaml):
brain through LiteLLM, one direct paid fallback that bypasses the proxy, worker
for auxiliary tasks. Models need ≥64k context for agentic tool use.

## Docs

| Doc | |
|-----|---|
| [`docs/host-setup.md`](docs/host-setup.md) | Manual setup + undos |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Common failures |
| [`docs/upgrading.md`](docs/upgrading.md) | Bump oMLX, re-test, re-bench |
| [`docs/app-configs/`](docs/app-configs/) | Hermes + LiteLLM static example |
| [`docs/stories/`](docs/stories/) | BMAD stories (canonical) |
| [`docs/improvements/`](docs/improvements/) | Pointer → stories |
| [`docs/investigations/`](docs/investigations/) | Ad-hoc notes |
| [`benchmarks/`](benchmarks/) | Tracked results + report |

## License

MIT — see [`LICENSE`](LICENSE).
