# App configs

Tracked files here are **templates only** — no hostnames, LAN names, or secrets.
Real values come from the repo `.env` via `make app-configs`.

## Generate local configs

```bash
# Ensure .env has LITELLM_MODEL_PREFIX (make setup writes a hostname default)
make app-configs
# → docs/app-configs/generated/*.yaml   (gitignored)
```

Copy/merge `generated/hermes-config.yaml` into `~/.hermes/config.yaml` as needed.
Regenerate after changing `LITELLM_MODEL_PREFIX` or model roles in the templates.

## Keep in sync with `models.yaml`

When you add, rename, disable, or remove a model:

1. Update [`models.yaml`](../../models.yaml) and run `make models-apply`.
2. Run `make sync-litellm` (owner-scoped) so LiteLLM matches. Public names use
   `LITELLM_MODEL_PREFIX` from `.env` (default: short hostname + `-`).
3. Update the **templates** in this folder (use `${LITELLM_MODEL_PREFIX}…`),
   then `make app-configs` and refresh any installed client configs.
4. Re-run `make test-models` and `./tests/tool_use/run.sh` for affected models.
5. Update capability flags in the model's `litellm:` block to match what the
   tool-use suite actually passed.

## Files

| File | Purpose |
|------|---------|
| [`hermes-config.yaml.template`](hermes-config.yaml.template) | Hermes Agent template → `generated/hermes-config.yaml` |
| [`litellm-static.yaml.template`](litellm-static.yaml.template) | Optional static LiteLLM config template |
| `generated/` | Rendered configs (gitignored; may contain host prefix) |
