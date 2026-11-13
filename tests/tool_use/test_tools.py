#!/usr/bin/env python3
"""Black-box validator for OpenAI-style tool/function calling.

Runs a battery of /v1/chat/completions probes against an oMLX (OpenAI-compatible)
endpoint and reports per-model pass/fail.

Usage:
    python test_tools.py [model ...]

If no model is specified, models are auto-discovered via /v1/models.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_FILE = os.path.join(REPO_ROOT, ".env")


def _load_dotenv(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


_DOTENV = _load_dotenv(ENV_FILE)


def _env_get(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    return _DOTENV.get(name)


def _default_base_url() -> str:
    for name in ("OMLX_URL", "UPSTREAM_URL", "LLAMA_SWAP_URL"):
        v = _env_get(name)
        if v:
            return v.rstrip("/")
    return "http://localhost:8000"


def _default_api_key() -> str | None:
    return _env_get("OMLX_API_KEY") or _env_get("MODEL_API_KEY")


DEFAULT_URL = _default_base_url()
COLD_TIMEOUT = float(os.environ.get("COLD_TIMEOUT_SECS", "120"))
WARM_TIMEOUT = float(os.environ.get("WARM_TIMEOUT_SECS", "60"))
TOOL_MAX_TOKENS = int(os.environ.get("TOOL_TEST_MAX_TOKENS", "256"))
FINAL_MAX_TOKENS = int(os.environ.get("TOOL_TEST_FINAL_TOKENS", "128"))

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Name of the city, e.g. 'Paris'",
                },
            },
            "required": ["city"],
        },
    },
}

TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the current local time in a timezone.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone, e.g. 'Europe/Paris'",
                },
            },
            "required": ["timezone"],
        },
    },
}


def _extra_tool(name: str, description: str, prop: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    prop: {"type": "string", "description": f"{prop} value"},
                },
                "required": [prop],
            },
        },
    }


AGENTIC_TOOLS: list[dict] = [
    WEATHER_TOOL,
    TIME_TOOL,
    _extra_tool("search_web", "Search the web.", "query"),
    _extra_tool("translate_text", "Translate text.", "text"),
    _extra_tool("send_email", "Send an email.", "body"),
    _extra_tool("create_calendar_event", "Create a calendar event.", "title"),
    _extra_tool("lookup_stock", "Look up a stock ticker.", "symbol"),
    _extra_tool("run_sql", "Run a read-only SQL query.", "sql"),
    _extra_tool("fetch_url", "Fetch a URL.", "url"),
    _extra_tool("grep_files", "Search files for a pattern.", "pattern"),
    _extra_tool("summarize_document", "Summarize a document.", "doc_id"),
    _extra_tool("calculate", "Evaluate a math expression.", "expression"),
    _extra_tool("list_directory", "List a directory path.", "path"),
    _extra_tool("read_file", "Read a file path.", "path"),
    _extra_tool("set_reminder", "Set a reminder.", "message"),
]


def build_agentic_system_prompt(target_tokens: int = 20_000) -> str:
    """Pad a system prompt to roughly target_tokens (heuristic: ~4 chars/token)."""
    prefix = (
        "You are an agent with many tools. Use tools when appropriate. "
        "Never invent data — call functions with valid JSON arguments. "
    )
    chunk = (
        "Policy: follow user instructions, respect tool schemas, prefer structured "
        "tool_calls over free text when a tool applies. "
    )
    target_chars = target_tokens * 4
    reps = max(1, (target_chars - len(prefix)) // len(chunk))
    return prefix + chunk * reps


@dataclass
class TestResult:
    name: str
    passed: bool
    advisory: bool = False
    detail: str = ""
    raw: Any = None


@dataclass
class ModelReport:
    model: str
    results: list[TestResult] = field(default_factory=list)

    @property
    def required_failed(self) -> int:
        return sum(1 for r in self.results if not r.passed and not r.advisory)

    @property
    def advisory_failed(self) -> int:
        return sum(1 for r in self.results if not r.passed and r.advisory)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)


class EndpointClient:
    """Thin wrapper around /v1/chat/completions with cold/warm timeouts."""

    def __init__(self, base_url: str, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self._warm: set[str] = set()
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            timeout=httpx.Timeout(COLD_TIMEOUT),
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def _timeout_for(self, model: str) -> float:
        return WARM_TIMEOUT if model in self._warm else COLD_TIMEOUT

    def list_models(self) -> list[str]:
        r = self._client.get(f"{self.base_url}/v1/models", timeout=WARM_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return [m["id"] for m in data.get("data", [])]

    def chat(self, model: str, payload: dict) -> tuple[int, dict | str]:
        body = dict(payload)
        body["model"] = model
        timeout = self._timeout_for(model)
        try:
            r = self._client.post(
                f"{self.base_url}/v1/chat/completions",
                json=body,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            return 0, f"transport error: {e!r}"
        self._warm.add(model)
        ct = r.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                return r.status_code, r.json()
            except json.JSONDecodeError:
                return r.status_code, r.text
        return r.status_code, r.text

    def chat_stream(self, model: str, payload: dict) -> tuple[int, list[dict], str]:
        body = dict(payload)
        body["model"] = model
        body["stream"] = True
        timeout = self._timeout_for(model)
        chunks: list[dict] = []
        raw_lines: list[str] = []
        try:
            with self._client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=body,
                timeout=timeout,
            ) as r:
                self._warm.add(model)
                if r.status_code != 200:
                    text = r.read().decode("utf-8", errors="replace")
                    return r.status_code, [], text
                for line in r.iter_lines():
                    if not line:
                        continue
                    raw_lines.append(line)
                    if line.startswith("data: "):
                        data = line[len("data: ") :]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            chunks.append(json.loads(data))
                        except json.JSONDecodeError:
                            pass
                return 200, chunks, "\n".join(raw_lines)
        except httpx.HTTPError as e:
            return 0, [], f"transport error: {e!r}"


def _first_choice_message(resp: dict) -> dict | None:
    if not isinstance(resp, dict):
        return None
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    msg = choices[0].get("message")
    return msg if isinstance(msg, dict) else None


def _tool_calls(resp: dict) -> list[dict]:
    msg = _first_choice_message(resp)
    if not msg:
        return []
    tcs = msg.get("tool_calls")
    return tcs if isinstance(tcs, list) else []


def _parse_args(tc: dict) -> dict | None:
    fn = tc.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args)
        except json.JSONDecodeError:
            return None
    return None


def _short(raw: Any, limit: int = 1500) -> str:
    if isinstance(raw, (dict, list)):
        text = json.dumps(raw, ensure_ascii=False)
    else:
        text = str(raw)
    if len(text) > limit:
        return text[:limit] + f"... <truncated, total {len(text)} chars>"
    return text


def test_basic_tool_call(client: EndpointClient, model: str) -> tuple[TestResult, dict | None]:
    payload = {
        "messages": [
            {"role": "user", "content": "What's the weather in Paris right now?"},
        ],
        "tools": [WEATHER_TOOL],
        "max_tokens": TOOL_MAX_TOKENS,
        "temperature": 0.0,
    }
    status, resp = client.chat(model, payload)
    if status != 200 or not isinstance(resp, dict):
        return TestResult(
            name="basic_tool_call",
            passed=False,
            detail=f"http {status}",
            raw=resp,
        ), None

    tcs = _tool_calls(resp)
    if not tcs:
        msg = _first_choice_message(resp) or {}
        return TestResult(
            name="basic_tool_call",
            passed=False,
            detail=(
                "no tool_calls in response; "
                f"finish_reason={resp.get('choices',[{}])[0].get('finish_reason')!r}; "
                f"content={msg.get('content')!r}"
            ),
            raw=resp,
        ), None

    first = tcs[0]
    fn_name = (first.get("function") or {}).get("name")
    if fn_name != "get_weather":
        return TestResult(
            name="basic_tool_call",
            passed=False,
            detail=f"expected function get_weather, got {fn_name!r}",
            raw=resp,
        ), None

    args = _parse_args(first)
    if args is None:
        return TestResult(
            name="basic_tool_call",
            passed=False,
            detail="function.arguments did not parse as JSON",
            raw=resp,
        ), None

    city = args.get("city")
    if not isinstance(city, str) or city.strip().lower() != "paris":
        return TestResult(
            name="basic_tool_call",
            passed=False,
            detail=f"expected city=Paris (case-insensitive), got {city!r}",
            raw=resp,
        ), None

    return TestResult(name="basic_tool_call", passed=True), resp


def test_agentic_scale_tool_call(client: EndpointClient, model: str) -> TestResult:
    """Large system prompt (~20k tokens) and 15 tools; still must call get_weather for Paris."""
    system = build_agentic_system_prompt(20_000)
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "What's the weather in Paris right now? "
                    "You must use the get_weather tool."
                ),
            },
        ],
        "tools": AGENTIC_TOOLS,
        "max_tokens": TOOL_MAX_TOKENS,
        "temperature": 0.0,
    }
    status, resp = client.chat(model, payload)
    if status != 200 or not isinstance(resp, dict):
        return TestResult(
            name="agentic_scale_tool_call",
            passed=False,
            detail=f"http {status}",
            raw=resp,
        )

    tcs = _tool_calls(resp)
    if not tcs:
        return TestResult(
            name="agentic_scale_tool_call",
            passed=False,
            detail="no tool_calls under large system prompt",
            raw=resp,
        )

    first = tcs[0]
    fn_name = (first.get("function") or {}).get("name")
    if fn_name != "get_weather":
        return TestResult(
            name="agentic_scale_tool_call",
            passed=False,
            detail=f"expected get_weather, got {fn_name!r}",
            raw=resp,
        )

    args = _parse_args(first)
    if args is None:
        return TestResult(
            name="agentic_scale_tool_call",
            passed=False,
            detail="arguments did not parse as JSON",
            raw=resp,
        )

    city = args.get("city")
    if not isinstance(city, str) or city.strip().lower() != "paris":
        return TestResult(
            name="agentic_scale_tool_call",
            passed=False,
            detail=f"expected city=Paris, got {city!r}",
            raw=resp,
        )

    return TestResult(name="agentic_scale_tool_call", passed=True)


def test_tool_result_roundtrip(
    client: EndpointClient,
    model: str,
    prior_response: dict | None,
) -> TestResult:
    if prior_response is None:
        return TestResult(
            name="tool_result_roundtrip",
            passed=False,
            detail="skipped: basic_tool_call did not produce a usable response",
        )

    msg = _first_choice_message(prior_response) or {}
    tcs = msg.get("tool_calls") or []
    if not tcs:
        return TestResult(
            name="tool_result_roundtrip",
            passed=False,
            detail="prior response lacked tool_calls",
            raw=prior_response,
        )
    call_id = tcs[0].get("id") or "call_0"

    assistant_msg = {
        "role": "assistant",
        "content": msg.get("content") or "",
        "tool_calls": tcs,
    }
    payload = {
        "messages": [
            {"role": "user", "content": "What's the weather in Paris right now?"},
            assistant_msg,
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "get_weather",
                "content": "sunny, 20C",
            },
        ],
        "tools": [WEATHER_TOOL],
        "max_tokens": FINAL_MAX_TOKENS,
        "temperature": 0.0,
    }
    status, resp = client.chat(model, payload)
    if status != 200 or not isinstance(resp, dict):
        return TestResult(
            name="tool_result_roundtrip",
            passed=False,
            detail=f"http {status}",
            raw=resp,
        )

    final_msg = _first_choice_message(resp) or {}
    content = final_msg.get("content") or ""
    if final_msg.get("tool_calls"):
        return TestResult(
            name="tool_result_roundtrip",
            passed=False,
            detail="model emitted another tool_call instead of using the tool result",
            raw=resp,
        )
    if not content.strip():
        return TestResult(
            name="tool_result_roundtrip",
            passed=False,
            detail="final assistant content is empty",
            raw=resp,
        )

    lower = content.lower()
    signals = ["sunny", "20", "warm", "clear", "weather"]
    if not any(s in lower for s in signals):
        return TestResult(
            name="tool_result_roundtrip",
            passed=False,
            detail=(
                "final answer does not appear to incorporate the tool result; "
                f"content={content!r}"
            ),
            raw=resp,
        )

    return TestResult(name="tool_result_roundtrip", passed=True)


def test_multi_tool_selection(client: EndpointClient, model: str) -> TestResult:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": "What time is it in Tokyo right now? Use the appropriate tool.",
            },
        ],
        "tools": [WEATHER_TOOL, TIME_TOOL],
        "max_tokens": TOOL_MAX_TOKENS,
        "temperature": 0.0,
    }
    status, resp = client.chat(model, payload)
    if status != 200 or not isinstance(resp, dict):
        return TestResult(
            name="multi_tool_selection",
            passed=False,
            detail=f"http {status}",
            raw=resp,
        )

    tcs = _tool_calls(resp)
    if not tcs:
        return TestResult(
            name="multi_tool_selection",
            passed=False,
            detail="no tool_calls emitted",
            raw=resp,
        )
    name = (tcs[0].get("function") or {}).get("name")
    if name != "get_time":
        return TestResult(
            name="multi_tool_selection",
            passed=False,
            detail=f"expected get_time, got {name!r}",
            raw=resp,
        )
    return TestResult(name="multi_tool_selection", passed=True)


def test_no_tool_path(client: EndpointClient, model: str) -> TestResult:
    payload = {
        "messages": [
            {"role": "user", "content": "What is 2+2? Just answer with the number."},
        ],
        "tools": [WEATHER_TOOL, TIME_TOOL],
        "max_tokens": FINAL_MAX_TOKENS,
        "temperature": 0.0,
    }
    status, resp = client.chat(model, payload)
    if status != 200 or not isinstance(resp, dict):
        return TestResult(
            name="no_tool_path",
            passed=False,
            detail=f"http {status}",
            raw=resp,
        )

    msg = _first_choice_message(resp) or {}
    if msg.get("tool_calls"):
        return TestResult(
            name="no_tool_path",
            passed=False,
            detail="model invoked a tool when none was needed",
            raw=resp,
        )
    content = (msg.get("content") or "").strip()
    if not content:
        return TestResult(
            name="no_tool_path",
            passed=False,
            detail="empty content and no tool_calls",
            raw=resp,
        )
    if "4" not in content:
        return TestResult(
            name="no_tool_path",
            passed=False,
            detail=f"answer did not contain '4'; content={content!r}",
            raw=resp,
        )
    return TestResult(name="no_tool_path", passed=True)


def test_required_tool_choice(client: EndpointClient, model: str) -> TestResult:
    payload = {
        "messages": [
            {"role": "user", "content": "Hello, please look up something useful for me."},
        ],
        "tools": [WEATHER_TOOL],
        "tool_choice": "required",
        "max_tokens": TOOL_MAX_TOKENS,
        "temperature": 0.0,
    }
    status, resp = client.chat(model, payload)
    if status != 200 or not isinstance(resp, dict):
        return TestResult(
            name="required_tool_choice",
            passed=False,
            detail=f"http {status}",
            raw=resp,
        )
    tcs = _tool_calls(resp)
    if not tcs:
        return TestResult(
            name="required_tool_choice",
            passed=False,
            detail="tool_choice=required did not produce a tool_call",
            raw=resp,
        )
    name = (tcs[0].get("function") or {}).get("name")
    if name != "get_weather":
        return TestResult(
            name="required_tool_choice",
            passed=False,
            detail=f"expected get_weather (only tool offered), got {name!r}",
            raw=resp,
        )
    return TestResult(name="required_tool_choice", passed=True)


def test_streaming_tool_call(client: EndpointClient, model: str) -> TestResult:
    payload = {
        "messages": [
            {"role": "user", "content": "What's the weather in Paris?"},
        ],
        "tools": [WEATHER_TOOL],
        "max_tokens": TOOL_MAX_TOKENS,
        "temperature": 0.0,
    }
    status, chunks, raw = client.chat_stream(model, payload)
    if status != 200:
        return TestResult(
            name="streaming_tool_call",
            passed=False,
            detail=f"http {status}",
            raw=raw,
        )

    assembled: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    for chunk in chunks:
        choices = chunk.get("choices") or []
        if not choices:
            continue
        ch = choices[0]
        delta = ch.get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = assembled.setdefault(idx, {"name": "", "args": "", "id": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]
        if ch.get("finish_reason"):
            finish_reason = ch["finish_reason"]

    if not assembled:
        return TestResult(
            name="streaming_tool_call",
            passed=False,
            detail=(
                "no tool_call deltas observed in stream; "
                f"finish_reason={finish_reason!r}; chunks={len(chunks)}"
            ),
            raw=raw,
        )

    slot = assembled[min(assembled.keys())]
    if slot["name"] != "get_weather":
        return TestResult(
            name="streaming_tool_call",
            passed=False,
            detail=f"streamed function name was {slot['name']!r}",
            raw=raw,
        )
    try:
        args = json.loads(slot["args"]) if slot["args"] else {}
    except json.JSONDecodeError:
        return TestResult(
            name="streaming_tool_call",
            passed=False,
            detail=f"streamed arguments did not parse: {slot['args']!r}",
            raw=raw,
        )
    city = args.get("city")
    if not isinstance(city, str) or city.strip().lower() != "paris":
        return TestResult(
            name="streaming_tool_call",
            passed=False,
            detail=f"streamed city was {city!r}",
            raw=raw,
        )
    return TestResult(name="streaming_tool_call", passed=True)


def test_parallel_tool_calls(client: EndpointClient, model: str) -> TestResult:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "What's the weather in Paris AND Tokyo? "
                    "Call the tool once for each city."
                ),
            },
        ],
        "tools": [WEATHER_TOOL],
        "max_tokens": TOOL_MAX_TOKENS,
        "temperature": 0.0,
    }
    status, resp = client.chat(model, payload)
    if status != 200 or not isinstance(resp, dict):
        return TestResult(
            name="parallel_tool_calls",
            passed=False,
            advisory=True,
            detail=f"http {status}",
            raw=resp,
        )
    tcs = _tool_calls(resp)
    if len(tcs) < 2:
        return TestResult(
            name="parallel_tool_calls",
            passed=False,
            advisory=True,
            detail=(
                f"model emitted {len(tcs)} tool_call(s); "
                "expected 2 (advisory only — serial round-tripping is acceptable)"
            ),
            raw=resp,
        )
    cities = []
    for tc in tcs:
        args = _parse_args(tc) or {}
        c = args.get("city")
        if isinstance(c, str):
            cities.append(c.strip().lower())
    if "paris" in cities and "tokyo" in cities:
        return TestResult(name="parallel_tool_calls", passed=True, advisory=True)
    return TestResult(
        name="parallel_tool_calls",
        passed=False,
        advisory=True,
        detail=f"two tool_calls emitted but cities were {cities!r}",
        raw=resp,
    )


def run_for_model(client: EndpointClient, model: str) -> ModelReport:
    print(f"\n=== {model} ===", flush=True)
    report = ModelReport(model=model)

    def record(result: TestResult) -> None:
        flag = "PASS" if result.passed else ("WARN" if result.advisory else "FAIL")
        suffix = f" — {result.detail}" if result.detail else ""
        print(f"  [{flag}] {result.name}{suffix}", flush=True)
        report.results.append(result)

    t0 = time.time()
    basic, basic_resp = test_basic_tool_call(client, model)
    record(basic)
    print(f"        (cold/first-request elapsed {time.time()-t0:.1f}s)", flush=True)

    record(test_tool_result_roundtrip(client, model, basic_resp))
    record(test_multi_tool_selection(client, model))
    record(test_no_tool_path(client, model))
    record(test_required_tool_choice(client, model))
    record(test_streaming_tool_call(client, model))
    record(test_agentic_scale_tool_call(client, model))
    record(test_parallel_tool_calls(client, model))

    return report


def print_summary(reports: list[ModelReport]) -> None:
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)

    test_names = [
        "basic_tool_call",
        "tool_result_roundtrip",
        "multi_tool_selection",
        "no_tool_path",
        "required_tool_choice",
        "streaming_tool_call",
        "agentic_scale_tool_call",
        "parallel_tool_calls",
    ]

    name_col = max(8, max((len(r.model) for r in reports), default=8))
    header = f"{'model'.ljust(name_col)}  " + "  ".join(n[:5].ljust(5) for n in test_names)
    print(header)
    print("-" * len(header))

    for rep in reports:
        cells = []
        result_by_name = {r.name: r for r in rep.results}
        for n in test_names:
            r = result_by_name.get(n)
            if r is None:
                cells.append("-".ljust(5))
            elif r.passed:
                cells.append("PASS ")
            elif r.advisory:
                cells.append("warn ")
            else:
                cells.append("FAIL ")
        print(f"{rep.model.ljust(name_col)}  " + "  ".join(cells))

    print()
    print("Legend: PASS ok | FAIL required test failed | warn advisory failure")
    print("Advisory tests: parallel_tool_calls")
    print()

    any_diag = False
    for rep in reports:
        for r in rep.results:
            if r.passed:
                continue
            if not any_diag:
                print("=" * 78)
                print("FAILURE DETAILS")
                print("=" * 78)
                any_diag = True
            kind = "advisory" if r.advisory else "required"
            print(f"\n[{rep.model}] {r.name} ({kind})")
            print(f"  detail: {r.detail}")
            if r.raw is not None:
                print(f"  raw   : {_short(r.raw)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Tool-use validator for oMLX")
    parser.add_argument(
        "models",
        nargs="*",
        help="Model name(s) to test. If omitted, all models from /v1/models are used.",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Base URL for the OpenAI-compatible endpoint (default {DEFAULT_URL})",
    )
    parser.add_argument(
        "--api-key",
        default=_default_api_key(),
        help="Bearer API key (default OMLX_API_KEY / MODEL_API_KEY / .env)",
    )
    args = parser.parse_args()

    client = EndpointClient(args.url, api_key=args.api_key)
    try:
        if args.models:
            models = args.models
        else:
            try:
                models = client.list_models()
            except httpx.HTTPError as e:
                print(f"error: failed to list models from {args.url}: {e!r}", file=sys.stderr)
                return 2
            if not models:
                print(f"error: no models discovered at {args.url}", file=sys.stderr)
                return 2

        print(f"endpoint   : {args.url}")
        print(f"models     : {', '.join(models)}")
        print(f"timeouts   : cold={COLD_TIMEOUT}s warm={WARM_TIMEOUT}s")
        print(f"max_tokens : tool={TOOL_MAX_TOKENS} final={FINAL_MAX_TOKENS}")

        reports = []
        for m in models:
            try:
                reports.append(run_for_model(client, m))
            except Exception as e:
                print(f"  [FAIL] runner crash on {m}: {e!r}", flush=True)
                rep = ModelReport(model=m)
                rep.results.append(
                    TestResult(name="runner", passed=False, detail=f"crash: {e!r}")
                )
                reports.append(rep)

        print_summary(reports)

        any_required_failed = any(rep.required_failed > 0 for rep in reports)
        return 1 if any_required_failed else 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
