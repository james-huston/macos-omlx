#!/usr/bin/env python3
"""status.py — one-shot snapshot of oMLX health, models, and host memory.

Shows:
  - oMLX up/down via GET /v1/models; version from /health or /api/status when present
  - Per-model load state from the Admin API (GET /admin/api/models, else /api/models)
  - macOS memory pressure (memory_pressure, vm_stat), wired limit (iogpu.wired_limit_mb)
  - Optional GPU/CPU power via powermetrics (--power)

Environment (CLI/env/.env): OMLX_URL or UPSTREAM_URL (default http://localhost:8000),
OMLX_API_KEY for admin routes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")


def load_dotenv(path):
    values = {}
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


def setting_env(*names, default=None):
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
        if DOTENV.get(name):
            return DOTENV[name]
    return default


def base_url():
    return (setting_env("OMLX_URL", "UPSTREAM_URL", default="http://localhost:8000")).rstrip("/")


def api_key():
    return setting_env("OMLX_API_KEY", "MODEL_API_KEY")


def http_get(path, key=None, timeout=10):
    url = f"{base_url()}{path}"
    req = urllib.request.Request(url, method="GET")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            if not body.strip():
                return resp.status, body
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except Exception as exc:
        return None, str(exc)


def probe_version():
    for path in ("/health", "/api/status", "/admin/api/status", "/v1/health"):
        status, payload = http_get(path, key=None, timeout=5)
        if status != 200:
            continue
        if isinstance(payload, dict):
            for key in ("version", "omlx_version", "server_version", "build"):
                if payload.get(key):
                    return str(payload[key])
            if payload.get("status") == "ok":
                return "ok"
        if isinstance(payload, str) and payload.strip():
            return payload.strip()[:120]
    return None


def openai_up(key):
    status, payload = http_get("/v1/models", key=key, timeout=8)
    if status == 200 and isinstance(payload, dict):
        n = len(payload.get("data") or [])
        return True, n
    return False, status


def admin_models(key):
    for path in ("/admin/api/models", "/api/models"):
        status, payload = http_get(path, key=key, timeout=15)
        if status == 200 and isinstance(payload, dict):
            models = payload.get("models")
            if isinstance(models, list):
                return models, path
    return None, None


def run_cmd(argv, timeout=15):
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def memory_pressure_summary():
    lines = []
    r = run_cmd(["memory_pressure"], timeout=8)
    if r.stdout:
        for line in r.stdout.splitlines():
            if "System-wide memory" in line or "pressure" in line.lower():
                lines.append(line.strip())
        if not lines and r.stdout.strip():
            lines.append(r.stdout.strip().splitlines()[-1])
    r2 = run_cmd(["vm_stat"], timeout=5)
    wired_pages = swap_used = None
    page_size = 4096
    if r2.stdout:
        m = re.search(r"page size of (\d+)", r2.stdout)
        if m:
            page_size = int(m.group(1))
        wm = re.search(r"Pages wired down:\s+(\d+)", r2.stdout)
        if wm:
            wired_pages = int(wm.group(1))
        for pat in (r"Swapouts:\s+(\d+)", r"swapins:\s+(\d+)", r"swapouts:\s+(\d+)"):
            sm = re.search(pat, r2.stdout, re.I)
            if sm:
                swap_used = int(sm.group(1))
                break
    if wired_pages is not None:
        gib = wired_pages * page_size / (1024 ** 3)
        lines.append(f"wired (vm_stat) : {gib:.1f} GiB")
    if swap_used is not None:
        lines.append(f"swap activity  : swapouts={swap_used}")
    return lines


def wired_limit_mb():
    r = run_cmd(["sysctl", "-n", "iogpu.wired_limit_mb"], timeout=5)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        return int(r.stdout.strip())
    return None


def powermetrics_watts():
    r = run_cmd(
        ["sudo", "powermetrics", "--samplers", "gpu_power,cpu_power", "-n", "1", "-i", "1000"],
        timeout=30,
    )
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "powermetrics failed").strip()[:200]
    text = r.stdout or ""
    gpu_m = re.search(r"GPU Power:\s*(\d+(?:\.\d+)?)\s*mW", text, re.I)
    cpu_m = re.search(r"CPU Power:\s*(\d+(?:\.\d+)?)\s*mW", text, re.I)
    if not gpu_m:
        gpu_m = re.search(r"GPU.*?\s+(\d+(?:\.\d+)?)\s*mW", text, re.I)
    if not cpu_m:
        cpu_m = re.search(r"Package Power:\s*(\d+(?:\.\d+)?)\s*mW", text, re.I)
    parts = []
    if gpu_m:
        parts.append(f"GPU {float(gpu_m.group(1)) / 1000:.1f} W")
    if cpu_m:
        parts.append(f"CPU {float(cpu_m.group(1)) / 1000:.1f} W")
    return ", ".join(parts) if parts else None, None


def fmt_model_row(m):
    name = m.get("display_name") or m.get("id") or "?"
    loaded = m.get("loaded", False)
    loading = m.get("is_loading", False)
    pinned = m.get("pinned", False)
    if loading:
        state = "loading"
    elif loaded:
        state = "loaded"
    else:
        state = "idle"
    pin = "pinned" if pinned else "unpinned"
    mem = m.get("actual_size_formatted") or m.get("estimated_size_formatted")
    if not mem and m.get("actual_size"):
        mem = f"{int(m['actual_size']) / (1024**3):.1f} GB"
    mem_s = f"  mem {mem}" if mem else ""
    return f"  {name:<28} {state:<8} {pin:<10}{mem_s}"


def parse_args():
    p = argparse.ArgumentParser(description="oMLX and host memory snapshot.")
    p.add_argument(
        "--power",
        action="store_true",
        help="sample GPU/CPU power via sudo powermetrics (may prompt for password)",
    )
    return p.parse_args()


def main():
    args = parse_args()
    url = base_url()
    key = api_key()

    print("macos-omlx status")
    print(f"  endpoint : {url}")

    up, detail = openai_up(key)
    if up:
        ver = probe_version()
        ver_s = f"  version  : {ver}" if ver else ""
        print(f"  oMLX     : up ({detail} model(s) advertised){ver_s}")
    else:
        print(f"  oMLX     : down or unreachable (GET /v1/models -> {detail})")

    models, admin_path = admin_models(key)
    if models is None:
        print("  models   : (admin API unavailable — set OMLX_API_KEY?)")
    else:
        print(f"  admin    : {url}{admin_path}")
        loaded = [m for m in models if m.get("loaded")]
        print(f"  library  : {len(models)} discovered, {len(loaded)} loaded")
        for m in sorted(models, key=lambda x: (not x.get("loaded"), x.get("id", ""))):
            print(fmt_model_row(m))

    wl = wired_limit_mb()
    if wl is not None:
        print(f"  GPU wired limit : {wl} MiB (sysctl iogpu.wired_limit_mb)")

    mem_lines = memory_pressure_summary()
    if mem_lines:
        print("  memory:")
        for line in mem_lines:
            print(f"             {line}")

    if args.power:
        watts, err = powermetrics_watts()
        if watts:
            print(f"  power    : {watts}")
        else:
            print(f"  power    : ({err or 'no wattage in powermetrics output'})")


if __name__ == "__main__":
    main()
