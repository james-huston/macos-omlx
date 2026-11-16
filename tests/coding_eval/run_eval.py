#!/usr/bin/env python3
"""Run the idempotent-ledger coding challenge against one or more oMLX models.

    ./tests/coding_eval/run_eval.py
    ./tests/coding_eval/run_eval.py qwen3-coder-next glm47-flash

For each model: prompt → extract ledger.py → pytest our hidden tests → scorecard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(EVAL_DIR, "PROMPT.md")
TESTS_SRC = os.path.join(EVAL_DIR, "tests", "test_ledger.py")
WORK_ROOT = os.path.join(EVAL_DIR, "work")
ENV_FILE = os.path.join(REPO_ROOT, ".env")

DEFAULT_MODELS = [
    "qwen3-coder-30b",
    "glm47-flash",
    "qwen3-coder-next",
    "glm45-air",
]

BRAIN_MODELS = {"qwen3-coder-next", "glm45-air", "qwen35-122b-a10b", "gpt-oss-120b"}
PRIMARY_BRAIN_ID = "gpt-oss-120b-MXFP4-Q4"

FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_dotenv(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


DOTENV = load_dotenv(ENV_FILE)


def setting(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or DOTENV.get(name) or default


def api(method: str, url: str, key: str | None, body: dict | None = None, timeout: float = 600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            pass
        return exc.code, detail
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def admin_model_id(base: str, key: str, alias_or_name: str) -> str | None:
    status, payload = api("GET", f"{base}/admin/api/models", key, timeout=30)
    if status != 200 or not isinstance(payload, dict):
        return None
    items = payload if isinstance(payload, list) else payload.get("data") or []
    # payload is dict with list values? apply-models saw list-like via iteration
    if isinstance(payload, dict) and not items:
        # openapi returns object map? earlier dump was list in "data" missing — was raw list?
        pass
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        # some versions return {"models": [...]} — also try values that are lists of dicts
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0]:
                items = v
                break
        if not items and all(isinstance(v, dict) for v in payload.values()):
            items = list(payload.values())

    alias_or_name = alias_or_name.lower()
    for m in items or []:
        mid = str(m.get("id") or "")
        alias = str((m.get("settings") or {}).get("model_alias") or m.get("model_alias") or "")
        display = str(m.get("display_name") or m.get("name") or "")
        if alias_or_name in {mid.lower(), alias.lower(), display.lower()}:
            return mid
        if alias_or_name.replace("_", "-") in mid.lower():
            return mid
        # match our short names to HF folder ids
        mapping = {
            "gpt-oss-120b": "gpt-oss-120b-MXFP4-Q4",
            "qwen3-coder-30b": "Qwen3-Coder-30B-A3B-Instruct-4bit",
            "qwen3-coder-next": "Qwen3-Coder-Next-4bit",
            "glm47-flash": "GLM-4.7-Flash-4bit",
            "glm45-air": "GLM-4.5-Air-4bit",
            "qwen35-35b-a3b": "Qwen3.5-35B-A3B-4bit",
        }
        if mapping.get(alias_or_name) == mid:
            return mid
    return mapping_fallback(alias_or_name)


def mapping_fallback(name: str) -> str | None:
    mapping = {
        "gpt-oss-120b": "gpt-oss-120b-MXFP4-Q4",
        "qwen3-coder-30b": "Qwen3-Coder-30B-A3B-Instruct-4bit",
        "qwen3-coder-next": "Qwen3-Coder-Next-4bit",
        "glm47-flash": "GLM-4.7-Flash-4bit",
        "glm45-air": "GLM-4.5-Air-4bit",
    }
    return mapping.get(name)


def ensure_capacity_for(base: str, key: str, model: str) -> None:
    """Unload primary brain before loading another brain-sized model."""
    if model not in BRAIN_MODELS:
        return
    print(f"  prep: unpin/unload {PRIMARY_BRAIN_ID} for brain-sized model")
    api("PUT", f"{base}/admin/api/models/{PRIMARY_BRAIN_ID}/settings", key,
        {"is_pinned": False}, timeout=60)
    api("POST", f"{base}/admin/api/models/{PRIMARY_BRAIN_ID}/unload", key, timeout=120)
    # Also unload other brains that might be resident
    for other in ("Qwen3-Coder-Next-4bit", "GLM-4.5-Air-4bit", "Qwen3.5-122B-A10B-4bit"):
        if other != mapping_fallback(model):
            api("POST", f"{base}/admin/api/models/{other}/unload", key, timeout=60)


def restore_primary_brain(base: str, key: str) -> None:
    print("  restore: pin/load primary gpt-oss brain")
    for other in ("Qwen3-Coder-Next-4bit", "GLM-4.5-Air-4bit", "GLM-4.7-Flash-4bit",
                  "Qwen3-Coder-30B-A3B-Instruct-4bit"):
        api("POST", f"{base}/admin/api/models/{other}/unload", key, timeout=60)
    api("PUT", f"{base}/admin/api/models/{PRIMARY_BRAIN_ID}/settings", key,
        {"is_pinned": True}, timeout=60)
    api("POST", f"{base}/admin/api/models/{PRIMARY_BRAIN_ID}/load", key, timeout=300)


def extract_ledger_source(text: str) -> str | None:
    if not text:
        return None
    matches = FENCE_RE.findall(text)
    if matches:
        # Prefer the longest python fence (full file)
        return max(matches, key=len).strip() + "\n"
    # Fallback: entire reply looks like a module
    if "class Ledger" in text:
        return text.strip() + "\n"
    return None


def ask_model(base: str, key: str, model: str, prompt: str, max_tokens: int, timeout: float):
    user_prompt = prompt
    body: dict = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful Python engineer. Follow the user's specification exactly. "
                    "Do not use chain-of-thought. "
                    "Output only a single Python code fence containing ledger.py."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    # GLM thinking mode can consume the entire max_tokens budget with no code.
    if model.startswith("glm"):
        body["chat_template_kwargs"] = {"enable_thinking": False}
        body["messages"][1]["content"] = user_prompt + "\n\n/nothink"
    t0 = time.time()
    status, resp = api("POST", f"{base}/v1/chat/completions", key, body, timeout=timeout)
    elapsed = time.time() - t0
    return status, resp, elapsed


def run_pytest(work_dir: str) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = work_dir + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", os.path.join(work_dir, "test_ledger.py")],
        cwd=work_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def parse_pytest_counts(output: str) -> tuple[int, int]:
    """Return (passed, failed) from pytest -q summary if present."""
    # e.g. "13 passed", "10 passed, 3 failed"
    passed = failed = 0
    m = re.search(r"(\d+)\s+passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+)\s+failed", output)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+)\s+error", output)
    if m:
        failed += int(m.group(1))
    return passed, failed


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("models", nargs="*", default=DEFAULT_MODELS)
    p.add_argument("--base", default=None, help="oMLX base URL")
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--keep-work", action="store_true", help="do not wipe work/ beforehand")
    p.add_argument("--no-restore-brain", action="store_true")
    args = p.parse_args()

    base = (args.base or setting("OMLX_URL") or setting("UPSTREAM_URL") or "http://localhost:8000")
    base = base.rstrip("/")
    key = setting("OMLX_API_KEY") or setting("MODEL_API_KEY")
    if not key:
        die("set OMLX_API_KEY")

    prompt = open(PROMPT_PATH, encoding="utf-8").read()
    if not os.path.isfile(TESTS_SRC):
        die(f"missing {TESTS_SRC}")

    if not args.keep_work and os.path.isdir(WORK_ROOT):
        shutil.rmtree(WORK_ROOT)
    os.makedirs(WORK_ROOT, exist_ok=True)

    # Ensure pytest is available
    try:
        import pytest  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pytest"])

    results = []
    print(f"coding eval — idempotent ledger")
    print(f"  endpoint : {base}/v1")
    print(f"  models   : {', '.join(args.models)}")
    print(f"  work     : {WORK_ROOT}")
    print()

    try:
        for model in args.models:
            print(f"=== {model} ===")
            ensure_capacity_for(base, key, model)
            work = os.path.join(WORK_ROOT, model.replace("/", "_"))
            os.makedirs(work, exist_ok=True)
            shutil.copy2(TESTS_SRC, os.path.join(work, "test_ledger.py"))

            status, resp, elapsed = ask_model(
                base, key, model, prompt, args.max_tokens, args.timeout
            )
            raw_path = os.path.join(work, "raw_response.json")
            with open(raw_path, "w", encoding="utf-8") as fh:
                json.dump({"status": status, "elapsed_s": elapsed, "response": resp}, fh, indent=2)

            if status != 200 or not isinstance(resp, dict):
                print(f"  generate: FAIL http {status} ({elapsed:.1f}s)")
                results.append((model, "GEN_FAIL", 0, 0, elapsed, str(resp)[:200]))
                continue

            content = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
            with open(os.path.join(work, "raw_content.md"), "w", encoding="utf-8") as fh:
                fh.write(content)

            src = extract_ledger_source(content)
            if not src or "class Ledger" not in src:
                print(f"  generate: FAIL no ledger.py extract ({elapsed:.1f}s)")
                results.append((model, "PARSE_FAIL", 0, 0, elapsed, "no class Ledger"))
                continue

            ledger_path = os.path.join(work, "ledger.py")
            with open(ledger_path, "w", encoding="utf-8") as fh:
                fh.write(src)
            print(f"  generate: OK  {elapsed:.1f}s  ({len(src)} bytes)")

            code, output = run_pytest(work)
            with open(os.path.join(work, "pytest.out"), "w", encoding="utf-8") as fh:
                fh.write(output)
            passed, failed = parse_pytest_counts(output)
            total = passed + failed
            status_label = "PASS" if code == 0 else "FAIL"
            print(f"  pytest  : {status_label}  {passed} passed, {failed} failed"
                  + (f"  (exit {code})" if code else ""))
            if code != 0:
                # show brief failure names
                for line in output.splitlines():
                    if "FAILED" in line or "ERROR" in line:
                        print(f"    {line}")
            results.append((model, status_label, passed, failed, elapsed, ""))
            print()
    finally:
        if not args.no_restore_brain:
            restore_primary_brain(base, key)

    print("=" * 70)
    print(f"{'model':<22} {'status':<10} {'passed':>6} {'failed':>6} {'gen_s':>7}")
    print("-" * 70)
    for model, st, passed, failed, elapsed, _ in results:
        print(f"{model:<22} {st:<10} {passed:>6} {failed:>6} {elapsed:>7.1f}")
    print()
    print(f"artifacts under {WORK_ROOT}/<model>/")


if __name__ == "__main__":
    main()
