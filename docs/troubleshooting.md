# Troubleshooting

## Auth / connectivity

- **`401` from oMLX:** LiteLLM's per-model `api_key` is missing/wrong, or
  `OMLX_API_KEY` in `.env` does not match `~/.omlx/settings.json`. Keys are
  never printed in full by our scripts — compare masked forms or re-set both.
- **LiteLLM can't reach the Mac:** `MODEL_API_BASE` must be the Mac's LAN IP,
  `.local` hostname, or Tailscale name — **not** `localhost`. Check the macOS
  firewall prompt for port 8000. Prefer wired 10GbE.
- **oMLX won't bind `0.0.0.0`:** it refuses non-loopback without a main API key.
  Set `OMLX_API_KEY` and rewrite settings (`make setup`).

## Performance / memory

- **Requests serialize:** check `--max_concurrent_requests` / settings.json.
  Confirm with `make bench` concurrency sweep (aggregate@2 should beat @1).
- **Swap or memory-pressure spikes:** too many pinned models or too much KV.
  Run `make status`, lower context, unpin, or raise the wired limit
  (see [`host-setup.md`](host-setup.md)).
- **Model evicted unexpectedly:** LRU under the memory guard. Pin it, or reduce
  other resident models / KV headroom.
- **Qwen3.5 unexpectedly slow:** custom kernels missing. Install full Xcode +
  Metal toolchain, then `brew reinstall jundot/omlx/omlx --with-custom-kernel`.

## Model / generation quirks

- **Thinking model returns empty content:** token cap hit (same WARN semantics
  as battlemage). Raise `--max-tokens` in `make test-models`.
- **Raw tool-call JSON instead of tools:** chat template / tool parsing issue.
  Gate with `./tests/tool_use/run.sh` before blaming the client.
- **LiteLLM name collision skipped:** another host or unowned entry already uses
  that `model_name`. Rename, or deliberately share with
  `ALLOW_SHARED_NAMES=1` (load-balancing).

## Sync safety

- **Battlemage deleted Mac models:** until battlemage gets owner-scoped sync,
  always run its sync with `NO_DELETE=1`, or sync Mac first only after that
  companion change lands.
- **`--self-test` failed:** custom `model_info` keys did not persist on your
  LiteLLM version. Stop and record the finding — do not invent a fallback.
