# Tool-use test suite

Black-box validator that probes the oMLX OpenAI-compatible endpoint to confirm
whether each served model implements tool / function calling. The suite hits
`/v1/chat/completions` directly (no SDK) so what you see is exactly what the
server emits.

## Running

```bash
# All models, default endpoint (http://localhost:8000)
./tests/tool_use/run.sh

# A single model
./tests/tool_use/run.sh qwen35-35b-a3b

# A different endpoint
OMLX_URL=http://some-host:8000 OMLX_API_KEY=... ./tests/tool_use/run.sh
```

`run.sh` handles venv creation and pip install on first run.

### Environment knobs

| Variable | Default | Purpose |
|---|---|---|
| `OMLX_URL` / `UPSTREAM_URL` / `LLAMA_SWAP_URL` | `http://localhost:8000` | Base URL of the OpenAI-compatible endpoint |
| `OMLX_API_KEY` | (from `.env`) | `Authorization: Bearer` for secured binds |
| `COLD_TIMEOUT_SECS` | `120` | Timeout for the first request to a model (cold-start) |
| `WARM_TIMEOUT_SECS` | `60` | Timeout for subsequent requests to the same model |
| `TOOL_TEST_MAX_TOKENS` | `256` | `max_tokens` cap on tool-call requests |
| `TOOL_TEST_FINAL_TOKENS` | `128` | `max_tokens` cap on final-answer requests |

Exit code is non-zero if any **required** test fails. `parallel_tool_calls`
is advisory-only.

## What each test checks

| Test | What it sends | What we expect |
|---|---|---|
| `basic_tool_call` | "What's the weather in Paris?" + `get_weather` tool | Structured `get_weather(city=Paris)` |
| `tool_result_roundtrip` | Replays the tool call and feeds back `role: tool` content `"sunny, 20C"` | Final assistant message that references the tool result |
| `multi_tool_selection` | Two tools (`get_weather`, `get_time`); asks only for the time | Model picks `get_time` |
| `no_tool_path` | Tools provided but asks "What is 2+2?" | Plain text answer containing `4`, no `tool_calls` |
| `required_tool_choice` | `tool_choice: "required"`, ambiguous prompt | Model emits a tool call anyway |
| `streaming_tool_call` | Same as `basic_tool_call` but `stream: true` | Streamed deltas assemble to `get_weather(city=Paris)` |
| `agentic_scale_tool_call` | ~20k-token system prompt + 15 tool schemas; weather in Paris | Still emits correct `get_weather` tool call (**required**) |
| `parallel_tool_calls` | "Weather in Paris AND Tokyo?" | Two tool calls in one response. **Advisory** |

## Interpreting failures

- **`basic_tool_call` returns XML/JSON in `content` instead of `tool_calls`** — chat template or server tool parsing is misconfigured for that model in oMLX.
- **`agentic_scale_tool_call` fails while `basic_tool_call` passes** — context or prompt budget handling under long system prompts; check max context and tool schema limits.
- **`tool_result_roundtrip` fails but `basic_tool_call` passes** — `role: tool` messages may not be rendered back into the prompt.
- **`streaming_tool_call` fails but `basic_tool_call` passes** — streaming tool-call assembly differs from non-streaming.
- **`parallel_tool_calls` warns** — usually fine; many models serialize tool calls.

## Files

- `run.sh` — bootstrap venv, install deps, run `test_tools.py` (macOS `shasum` fallback)
- `test_tools.py` — the actual test runner
- `requirements.txt` — pinned dependencies (`httpx`)
- `.venv/` — created by `run.sh`, gitignored

## Notes

- We deliberately do **not** use the OpenAI SDK; the suite surfaces raw server behavior.
- `temperature` is pinned to `0.0` for reproducibility.
- The first request to any given model uses the cold timeout (default 120s).
