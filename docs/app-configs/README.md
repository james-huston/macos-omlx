# App configs

Reference client configurations for apps that talk to this host (usually through
LiteLLM). **No secrets** — keys come from environment variables.

## Keep in sync with `models.yaml`

When you add, rename, disable, or remove a model:

1. Update [`models.yaml`](../../models.yaml) and run `make models-apply`.
2. Run `make sync-litellm` (owner-scoped) so LiteLLM matches.
3. Mirror the change into every file in this folder that lists model ids
   (Hermes brain/worker names, static LiteLLM snippets, etc.).
4. Re-run `make test-models` and `./tests/tool_use/run.sh` for affected models.
5. Update capability flags in the model's `litellm:` block to match what the
   tool-use suite actually passed.

## Files

| File | Purpose |
|------|---------|
| [`hermes-config.yaml`](hermes-config.yaml) | Hermes Agent `~/.hermes/config.yaml` template |
| [`litellm-static-example.yaml`](litellm-static-example.yaml) | Optional static LiteLLM config (invisible to DB sync) |
