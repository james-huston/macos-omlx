#!/usr/bin/env python3
"""
bench.py — measure oMLX models through the OpenAI-compatible streaming API.

    make bench
    make bench MODELS='gpt-oss-120b' QUICK=1
    make bench LONG=1 POWER=1 UPDATE_MANIFEST=1

Scenarios (skipped with a note when a model is not advertised):
  A) single-stream — TTFT, prefill tok/s, decode tok/s at 1k/8k/32k prompts (64k with --long)
  B) concurrency sweep — parallel streams at 1/2/4 (8 with --long)
  C) prefix-cache — same long prefix twice; TTFT reduction on the second request
  D) mixed — brain + worker together when both roles exist and are loaded
  E) optional average watts during decode (--power)

Results append to benchmarks/results/<YYYY-MM-DD>-<hostname>.jsonl and a human summary is printed.

Configuration (CLI > env > repo .env > default):
  OMLX_URL / UPSTREAM_URL   default http://localhost:8000
  OMLX_API_KEY / MODEL_API_KEY

Never prints a full API key.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")
MODELS_YAML = os.path.join(REPO_ROOT, "models.yaml")
RESULTS_DIR = os.path.join(REPO_ROOT, "benchmarks", "results")
SETTINGS_JSON = os.path.expanduser("~/.omlx/settings.json")

FILLER_WORD = " benchmark "
DEFAULT_DECODE_TOKENS = 64
CONCURRENCY_MIN_SPEEDUP = 1.3

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def note(msg: str) -> None:
    print(f"{YELLOW}note{RESET}: {msg}")


def load_dotenv(path: str) -> dict[str, str]:
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


DOTENV = load_dotenv(ENV_FILE)


def setting_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
        if DOTENV.get(name):
            return DOTENV[name]
    return default


def base_url() -> str:
    url = setting_env("OMLX_URL", "UPSTREAM_URL", default="http://localhost:8000")
    return (url or "http://localhost:8000").rstrip("/")


def resolve_api_key(cli_key: str | None) -> str | None:
    if cli_key is not None:
        return cli_key
    return setting_env("OMLX_API_KEY", "MODEL_API_KEY")


def mask_key(key: str | None) -> str:
    if not key:
        return "(none)"
    if len(key) <= 8:
        return "****"
    return f"...{key[-4:]}"


def openai_base(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u


def run_cmd(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def _coerce_scalar(raw: str) -> Any:
    raw = raw.strip()
    if not raw or raw in ("|", ">"):
        return raw
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    if re.match(r"^-?\d+$", raw):
        return int(raw)
    if re.match(r"^-?\d+\.\d+$", raw):
        return float(raw)
    return raw.strip("'\"")


def parse_models_yaml(path: str) -> list[dict[str, Any]]:
    """Minimal models.yaml reader (stdlib only): models list entries."""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()

    in_models = False
    entries: list[dict[str, Any]] = []
    entry: dict[str, Any] | None = None
    litellm: dict[str, Any] | None = None

    for line in lines:
        if not in_models:
            if re.match(r"^models:\s*$", line):
                in_models = True
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m_name = re.match(r"^(\s*)-\s+name:\s*(.+)$", line)
        if m_name:
            if entry and entry.get("enabled", True) is not False:
                entries.append(entry)
            name = _coerce_scalar(m_name.group(2).split("#")[0])
            entry = {"name": str(name)}
            litellm = None
            continue
        if entry is None:
            continue
        m4 = re.match(r"^    (\w+):\s*(.*)$", line)
        if m4:
            key, raw = m4.group(1), m4.group(2).split("#", 1)[0].rstrip()
            if key == "litellm" and not raw.strip():
                litellm = {}
                entry["litellm"] = litellm
                continue
            entry[key] = _coerce_scalar(raw)
            litellm = None
            continue
        m6 = re.match(r"^      (\w+):\s*(.*)$", line)
        if m6 and litellm is not None:
            k, raw = m6.group(1), m6.group(2).split("#", 1)[0].rstrip()
            litellm[k] = _coerce_scalar(raw)

    if entry and entry.get("enabled", True) is not False:
        entries.append(entry)
    return entries


def is_embedding_entry(entry: dict[str, Any]) -> bool:
    if (entry.get("type") or "").lower() == "embedding":
        return True
    litellm = entry.get("litellm") or {}
    return (litellm.get("mode") or "").lower() == "embedding"


def manifest_index(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for m in entries:
        name = m.get("name")
        if not name:
            continue
        idx[str(name)] = m
        alias = m.get("alias")
        if alias:
            idx[str(alias)] = m
        for a in m.get("aliases") or []:
            if a:
                idx[str(a)] = m
    return idx


def default_bench_models(advertised: set[str], entries: list[dict[str, Any]]) -> list[str]:
    """Enabled non-embedding manifest names that appear in /v1/models."""
    names: list[str] = []
    seen: set[str] = set()
    for m in entries:
        if m.get("enabled") is False:
            continue
        if is_embedding_entry(m):
            continue
        name = str(m.get("name"))
        if not name:
            continue
        aliases = {name}
        if m.get("alias"):
            aliases.add(str(m["alias"]))
        for a in m.get("aliases") or []:
            if a:
                aliases.add(str(a))
        if aliases & advertised:
            if name not in seen:
                names.append(name)
                seen.add(name)
    return names


def http_json(method: str, url: str, key: str | None, body: dict | None, timeout: float):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else None


def fetch_advertised(base: str, key: str | None, timeout: float) -> list[str]:
    url = f"{base}/v1/models"
    try:
        payload = http_json("GET", url, key, None, timeout=min(timeout, 30))
    except urllib.error.HTTPError as exc:
        die(f"GET {url} -> HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        die(f"cannot reach {url}: {getattr(exc, 'reason', exc)}")
    if not isinstance(payload, dict):
        die(f"GET {url} returned unexpected payload")
    return [m["id"] for m in payload.get("data", []) if m.get("id")]


def probe_version(base: str) -> str | None:
    for path in ("/health", "/api/status", "/admin/api/status", "/v1/health"):
        url = f"{base}{path}"
        try:
            payload = http_json("GET", url, None, None, 8)
        except Exception:
            continue
        if isinstance(payload, dict):
            for k in ("version", "omlx_version", "server_version", "build"):
                if payload.get(k):
                    return str(payload[k])
        if isinstance(payload, str) and payload.strip():
            return payload.strip()[:120]
    return None


def host_metadata() -> dict[str, Any]:
    meta: dict[str, Any] = {}
    sw = run_cmd(["sw_vers", "-productVersion"], timeout=5)
    if sw.returncode == 0:
        meta["macos_version"] = sw.stdout.strip()
    chip = run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5)
    if chip.returncode == 0 and chip.stdout.strip():
        meta["chip"] = chip.stdout.strip()
    else:
        hw = run_cmd(["sysctl", "-n", "hw.model"], timeout=5)
        if hw.returncode == 0:
            meta["chip"] = hw.stdout.strip()
    mem = run_cmd(["sysctl", "-n", "hw.memsize"], timeout=5)
    if mem.returncode == 0 and mem.stdout.strip().isdigit():
        meta["ram_gb"] = round(int(mem.stdout.strip()) / (1024 ** 3))
    meta["hostname"] = socket.gethostname().split(".")[0]
    return meta


def load_omlx_settings() -> dict[str, Any]:
    if not os.path.isfile(SETTINGS_JSON):
        return {}
    try:
        with open(SETTINGS_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        out: dict[str, Any] = {}
        for k in ("max_concurrent_requests", "paged_ssd_cache_dir", "paged_ssd_cache_max_size"):
            if k in data:
                out[k] = data[k]
        return out
    except Exception:
        return {}


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def prompt_for_tokens(target_tokens: int) -> str:
    unit = (FILLER_WORD * 8).strip() + " "
    need_chars = target_tokens * 4
    reps = max(1, need_chars // len(unit) + 1)
    text = (unit * reps).strip()
    while len(text) < need_chars:
        text += unit
    return text[:need_chars]


class PowerSampler:
    """Best-effort average watts via sudo powermetrics during a window."""

    def __init__(self) -> None:
        self._samples_mw: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._err: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=35)
        if not self._samples_mw:
            return {"available": False, "error": self._err or "no samples"}
        avg_w = sum(self._samples_mw) / len(self._samples_mw) / 1000.0
        return {"available": True, "avg_watts": round(avg_w, 2), "samples": len(self._samples_mw)}

    def _run(self) -> None:
        while not self._stop.is_set():
            r = run_cmd(
                ["sudo", "powermetrics", "--samplers", "gpu_power,cpu_power", "-n", "1", "-i", "500"],
                timeout=25,
            )
            if r.returncode != 0:
                self._err = (r.stderr or r.stdout or "powermetrics failed").strip()[:200]
                return
            text = r.stdout or ""
            total = 0.0
            found = False
            for pat in (
                r"GPU Power:\s*(\d+(?:\.\d+)?)\s*mW",
                r"CPU Power:\s*(\d+(?:\.\d+)?)\s*mW",
                r"Package Power:\s*(\d+(?:\.\d+)?)\s*mW",
            ):
                m = re.search(pat, text, re.I)
                if m:
                    total += float(m.group(1))
                    found = True
            if found:
                self._samples_mw.append(total)


def parse_sse_stream(resp) -> dict[str, Any]:
    """Read an OpenAI-style SSE body from an open urllib response."""
    first_token_at: float | None = None
    start = time.monotonic()
    output_chars = 0
    completion_tokens: int | None = None
    raw_lines: list[str] = []

    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            continue
        raw_lines.append(line)
        if not line.startswith("data:"):
            continue
        data = line[5:].lstrip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
            completion_tokens = int(usage["completion_tokens"])
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = (delta.get("content") or "") + (delta.get("reasoning_content") or "")
        if piece and first_token_at is None:
            first_token_at = time.monotonic()
        output_chars += len(piece)

    end = time.monotonic()
    out_tokens = completion_tokens if completion_tokens is not None else approx_tokens(" " * output_chars)
    if first_token_at is None:
        first_token_at = end
    return {
        "ttft_s": first_token_at - start,
        "total_s": end - start,
        "decode_s": max(1e-6, end - first_token_at),
        "output_tokens": out_tokens,
        "output_chars": output_chars,
    }


def stream_chat(
    base: str,
    key: str | None,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base}/v1/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    prompt_text = " ".join(
        (m.get("content") or "") for m in messages if isinstance(m.get("content"), str)
    )
    prompt_tokens = approx_tokens(prompt_text)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            stats = parse_sse_stream(resp)
    except urllib.error.HTTPError as exc:
        err = exc.read().decode(errors="replace")[:300]
        return {"error": f"HTTP {exc.code}: {err}", "prompt_tokens": prompt_tokens}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"error": str(getattr(exc, "reason", exc)), "prompt_tokens": prompt_tokens}

    prefill_s = stats["ttft_s"]
    decode_s = stats["decode_s"]
    out_tok = stats["output_tokens"]
    metrics = {
        "ttft_s": round(stats["ttft_s"], 4),
        "prefill_s": round(prefill_s, 4),
        "decode_s": round(decode_s, 4),
        "prompt_tokens_est": prompt_tokens,
        "output_tokens": out_tok,
        "prefill_tps": round(prompt_tokens / max(prefill_s, 1e-6), 2),
        "decode_tps": round(out_tok / max(decode_s, 1e-6), 2),
        "total_s": round(stats["total_s"], 4),
    }
    return {"ok": True, "metrics": metrics, "prompt_tokens": prompt_tokens}


class JsonlWriter:
    def __init__(self, path: str, ctx: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.ctx = ctx

    def write(self, scenario: str, model: str, metrics: dict[str, Any], extra: dict | None = None) -> dict:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scenario": scenario,
            "model": model,
            **self.ctx.get("model_fields", {}).get(model, {}),
            "omlx_version": self.ctx.get("omlx_version"),
            "macos_version": self.ctx.get("macos_version"),
            "chip": self.ctx.get("chip"),
            "ram_gb": self.ctx.get("ram_gb"),
            "omlx": self.ctx.get("omlx_settings") or {},
            "metrics": metrics,
        }
        if extra:
            row.update(extra)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        return row


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def scenario_single_stream(
    base: str,
    key: str | None,
    model: str,
    sizes: list[int],
    timeout: float,
    writer: JsonlWriter,
    power: bool,
    summary: list[str],
    manifest_updates: dict[str, dict[str, Any]],
) -> None:
    tail = " Reply with a short paragraph of plausible text."
    for size in sizes:
        user = prompt_for_tokens(size) + tail
        sampler: PowerSampler | None = None
        if power and size == (sizes[1] if len(sizes) > 1 else sizes[0]):
            sampler = PowerSampler()
            sampler.start()
        result = stream_chat(
            base, key, model,
            [{"role": "user", "content": user}],
            DEFAULT_DECODE_TOKENS,
            timeout,
        )
        power_metrics: dict[str, Any] | None = None
        if sampler:
            power_metrics = sampler.stop()
        if result.get("error"):
            note(f"{model} single-stream {size}k: {result['error']}")
            continue
        m = dict(result["metrics"])
        m["prompt_size_k"] = size
        if power_metrics:
            m["power"] = power_metrics
            writer.write("power", model, power_metrics, extra={"during": f"single-stream-{size}k"})
        writer.write("single-stream", model, m)
        summary.append(
            f"  {model} @ {size}k: TTFT {m['ttft_s']:.2f}s  "
            f"prefill {m['prefill_tps']:.0f} tok/s  decode {m['decode_tps']:.0f} tok/s"
        )
        rep_size = 8 if 8 in sizes else sizes[0]
        if size == rep_size:
            mu = manifest_updates.setdefault(model, {})
            mu["prefill_tps"] = m["prefill_tps"]
            mu["decode_tps"] = m["decode_tps"]


def scenario_concurrency(
    base: str,
    key: str | None,
    model: str,
    levels: list[int],
    timeout: float,
    writer: JsonlWriter,
    summary: list[str],
    manifest_updates: dict[str, dict[str, Any]],
) -> None:
    prompt = prompt_for_tokens(1024) + " Say hello in one sentence."
    per_level: dict[int, float] = {}

    for n in levels:
        results: list[dict[str, Any]] = []

        def one_stream() -> dict[str, Any]:
            return stream_chat(
                base, key, model,
                [{"role": "user", "content": prompt}],
                DEFAULT_DECODE_TOKENS,
                timeout,
            )

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=n) as ex:
            futs = [ex.submit(one_stream) for _ in range(n)]
            for fut in as_completed(futs):
                results.append(fut.result())
        wall = time.monotonic() - t0

        oks = [r for r in results if r.get("ok")]
        if len(oks) < n:
            note(f"{model} concurrency {n}: {n - len(oks)} stream(s) failed")
        if not oks:
            continue
        decode_rates = [r["metrics"]["decode_tps"] for r in oks]
        ttfts = [r["metrics"]["ttft_s"] for r in oks]
        total_out = sum(r["metrics"]["output_tokens"] for r in oks)
        aggregate_tps = total_out / max(wall, 1e-6)
        per_level[n] = aggregate_tps
        metrics = {
            "concurrency": n,
            "per_stream_decode_tps": [round(x, 2) for x in decode_rates],
            "per_stream_decode_tps_avg": round(sum(decode_rates) / len(decode_rates), 2),
            "aggregate_tps": round(aggregate_tps, 2),
            "ttft_p50_s": round(percentile(ttfts, 0.5), 4),
            "ttft_p95_s": round(percentile(ttfts, 0.95), 4),
            "wall_s": round(wall, 4),
        }
        writer.write("concurrency", model, metrics)
        summary.append(
            f"  {model} x{n}: aggregate {metrics['aggregate_tps']:.1f} tok/s  "
            f"per-stream avg {metrics['per_stream_decode_tps_avg']:.1f}  "
            f"TTFT p50 {metrics['ttft_p50_s']:.2f}s"
        )

    if 1 in per_level and 2 in per_level:
        ratio = per_level[2] / max(per_level[1], 1e-6)
        if ratio < CONCURRENCY_MIN_SPEEDUP:
            note(
                f"{model}: aggregate@2 is only {ratio:.2f}x aggregate@1 "
                f"(expected clearly > {CONCURRENCY_MIN_SPEEDUP}x)"
            )
    if per_level:
        manifest_updates.setdefault(model, {})["concurrency"] = max(per_level.keys())


def scenario_prefix_cache(
    base: str,
    key: str | None,
    model: str,
    timeout: float,
    writer: JsonlWriter,
    summary: list[str],
) -> None:
    prefix = prompt_for_tokens(8192)
    msgs = [
        {"role": "user", "content": prefix + "\n\nNow answer: what is 2+2? One word."},
    ]
    first = stream_chat(base, key, model, msgs, 16, timeout)
    second = stream_chat(base, key, model, msgs, 16, timeout)
    if not first.get("ok") or not second.get("ok"):
        note(f"{model} prefix-cache: {first.get('error') or second.get('error')}")
        return
    t1 = first["metrics"]["ttft_s"]
    t2 = second["metrics"]["ttft_s"]
    reduction = (t1 - t2) / t1 if t1 > 0 else 0.0
    metrics = {
        "ttft_first_s": round(t1, 4),
        "ttft_second_s": round(t2, 4),
        "ttft_reduction_ratio": round(reduction, 4),
        "ttft_reduction_pct": round(reduction * 100, 1),
    }
    writer.write("prefix-cache", model, metrics)
    summary.append(
        f"  {model} prefix-cache: TTFT {t1:.2f}s -> {t2:.2f}s ({metrics['ttft_reduction_pct']:.0f}% faster)"
    )


def find_role_models(entries: list[dict[str, Any]], role: str, advertised: set[str]) -> str | None:
    for m in entries:
        if (m.get("role") or "").lower() != role.lower():
            continue
        if m.get("enabled") is False or is_embedding_entry(m):
            continue
        name = str(m.get("name"))
        aliases = {name, str(m.get("alias") or "")}
        if aliases & advertised:
            return name
    return None


def scenario_mixed(
    base: str,
    key: str | None,
    entries: list[dict[str, Any]],
    advertised: set[str],
    timeout: float,
    writer: JsonlWriter,
    summary: list[str],
    solo_baselines: dict[str, float],
) -> None:
    brain = find_role_models(entries, "brain", advertised)
    worker = find_role_models(entries, "worker", advertised)
    if not brain or not worker:
        return
    if brain not in advertised or worker not in advertised:
        note(f"mixed: brain={brain!r} worker={worker!r} not both advertised")
        return

    prompt_b = prompt_for_tokens(512) + " Brief greeting."
    prompt_w = prompt_for_tokens(512) + " Brief greeting."

    def run_pair() -> tuple[dict, dict]:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fb = ex.submit(
                stream_chat, base, key, brain, [{"role": "user", "content": prompt_b}],
                DEFAULT_DECODE_TOKENS, timeout,
            )
            fw = ex.submit(
                stream_chat, base, key, worker, [{"role": "user", "content": prompt_w}],
                DEFAULT_DECODE_TOKENS, timeout,
            )
            return fb.result(), fw.result()

    rb, rw = run_pair()
    if not rb.get("ok") or not rw.get("ok"):
        note(f"mixed {brain}+{worker}: {rb.get('error') or rw.get('error')}")
        return
    mb, mw = rb["metrics"], rw["metrics"]
    metrics = {
        "brain": brain,
        "worker": worker,
        "brain_decode_tps": mb["decode_tps"],
        "worker_decode_tps": mw["decode_tps"],
        "brain_solo_baseline_tps": solo_baselines.get(brain),
        "worker_solo_baseline_tps": solo_baselines.get(worker),
    }
    writer.write("mixed", f"{brain}+{worker}", metrics)
    line = (
        f"  mixed {brain} ({mb['decode_tps']:.0f} tok/s) + "
        f"{worker} ({mw['decode_tps']:.0f} tok/s)"
    )
    if brain in solo_baselines:
        line += f"  brain solo was {solo_baselines[brain]:.0f}"
    if worker in solo_baselines:
        line += f"  worker solo was {solo_baselines[worker]:.0f}"
    summary.append(line)


def update_manifest(updates: dict[str, dict[str, Any]], manifest_path: str) -> None:
    try:
        from ruamel.yaml import YAML
    except ImportError:
        die("ruamel.yaml is required for --update-manifest. Install: pip install ruamel.yaml")

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    with open(manifest_path, encoding="utf-8") as fh:
        data = yaml.load(fh)
    if not data or "models" not in data:
        die("models.yaml has no models list")
    by_name = {str(m.get("name")): m for m in data["models"] if m.get("name")}
    changed = 0
    for model, vals in updates.items():
        entry = by_name.get(model)
        if not entry:
            continue
        for key in ("decode_tps", "prefill_tps", "concurrency"):
            if key in vals and vals[key] is not None:
                entry[key] = round(float(vals[key]), 2) if key != "concurrency" else int(vals[key])
                changed += 1
    if not changed:
        note("no manifest fields updated (no matching models / metrics)")
        return
    with open(manifest_path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    print(f"{GREEN}updated{RESET} {manifest_path} ({changed} field write(s))")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Benchmark oMLX models via streaming chat completions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--models", action="append", default=None,
        help="subset of model names (repeatable; each arg may be space-separated)",
    )
    p.add_argument("--quick", action="store_true", help="smaller prompts / fewer concurrency points")
    p.add_argument("--long", action="store_true", help="include 64k prompt and concurrency 8")
    p.add_argument(
        "--power", action="store_true",
        help="sample powermetrics during decode (best-effort; may need sudo)",
    )
    p.add_argument(
        "--update-manifest", action="store_true",
        help="write decode_tps/prefill_tps/concurrency into models.yaml (ruamel round-trip)",
    )
    p.add_argument("--timeout", type=float, default=600.0, help="per-stream timeout seconds")
    p.add_argument("--api-key", dest="api_key", default=None, help="Bearer token (else env/.env)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    base = openai_base(base_url())
    key = resolve_api_key(args.api_key)

    entries = parse_models_yaml(MODELS_YAML)
    idx = manifest_index(entries)

    print("macos-omlx bench")
    print(f"  endpoint : {base}")
    print(f"  api key  : {mask_key(key)}")

    try:
        advertised_list = fetch_advertised(base, key, args.timeout)
    except SystemExit:
        raise
    advertised = set(advertised_list)

    if args.models:
        wanted: list[str] = []
        for chunk in args.models:
            wanted.extend(chunk.split())
        models = []
        for name in wanted:
            if name in advertised or name in idx:
                models.append(idx.get(name, {}).get("name", name))
            else:
                note(f"model {name!r} not advertised — skipping")
        models = list(dict.fromkeys(models))
    else:
        models = default_bench_models(advertised, entries)

    models = [m for m in models if m in advertised]
    if not models:
        die("no models to benchmark (none advertised / enabled)")

    prompt_sizes = [1, 8, 32]
    concurrency_levels = [1, 2, 4]
    if args.quick:
        prompt_sizes = [1]
        concurrency_levels = [1, 2]
    if args.long:
        prompt_sizes.append(64)
        concurrency_levels.append(8)

    host = host_metadata()
    omlx_ver = probe_version(base)
    omlx_settings = load_omlx_settings()
    model_fields: dict[str, dict[str, Any]] = {}
    for m in models:
        ent = idx.get(m) or {}
        model_fields[m] = {
            "repo": ent.get("repo"),
            "revision": ent.get("revision"),
        }

    date = datetime.now().strftime("%Y-%m-%d")
    jsonl_path = os.path.join(RESULTS_DIR, f"{date}-{host.get('hostname', 'host')}.jsonl")
    ctx = {
        **host,
        "omlx_version": omlx_ver,
        "omlx_settings": omlx_settings,
        "model_fields": model_fields,
    }
    writer = JsonlWriter(jsonl_path, ctx)

    print(f"  models   : {', '.join(models)}")
    print(f"  jsonl    : {jsonl_path}")
    print(f"  prompts  : {prompt_sizes}k tokens  concurrency: {concurrency_levels}\n")

    summary_lines: list[str] = []
    manifest_updates: dict[str, dict[str, Any]] = {}
    solo_baselines: dict[str, float] = {}

    for model in models:
        print(f"=== {model} ===")
        scenario_single_stream(
            base, key, model, prompt_sizes, args.timeout, writer, args.power,
            summary_lines, manifest_updates,
        )
        rep = manifest_updates.get(model)
        if rep and "decode_tps" in rep:
            solo_baselines[model] = float(rep["decode_tps"])

        scenario_concurrency(
            base, key, model, concurrency_levels, args.timeout, writer,
            summary_lines, manifest_updates,
        )
        scenario_prefix_cache(base, key, model, args.timeout, writer, summary_lines)
        print()

    scenario_mixed(
        base, key, entries, advertised, args.timeout, writer, summary_lines, solo_baselines,
    )

    print(f"{GREEN}summary{RESET}")
    for line in summary_lines:
        print(line)
    print(f"\n{DIM}results appended to {jsonl_path}{RESET}")

    if args.update_manifest and manifest_updates:
        update_manifest(manifest_updates, MODELS_YAML)


if __name__ == "__main__":
    main()
