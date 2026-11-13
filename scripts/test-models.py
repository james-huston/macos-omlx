#!/usr/bin/env python3
"""
test-models.py

Cycle through every model an OpenAI-compatible endpoint advertises, send a real
request to each (chat or embeddings), and report pass/fail/warn.

    ./scripts/test-models.py                 # test oMLX directly
    ./scripts/test-models.py --via litellm   # test through the LiteLLM proxy
    ./scripts/test-models.py qwen3-coder-30b # a subset

The model list is fetched from {base}/v1/models unless --via litellm and not
--all: then only LiteLLM entries with model_info.managed_by == owner are tested.

Configuration (CLI flag > real env var > .env in repo root > default):
  --via {upstream,litellm}        which endpoint to test (default upstream)
  --base URL        explicit base URL, overrides --via
  --api-key KEY     explicit api key, overrides --via
  --owner OWNER     managed_by filter for --via litellm (default from manifest)
  --all             with --via litellm: test every /v1/models entry (no owner filter)
  --max-tokens N    response cap (default 256)
  --timeout SECS    per-request timeout, covers cold start (default 120)
  --prompt TEXT     the user prompt (default a trivial "reply OK")

  OMLX_URL / UPSTREAM_URL  (default http://localhost:8000)  oMLX OpenAI base
  OMLX_API_KEY / MODEL_API_KEY                            key for upstream
  LITELLM_URL   (default http://localhost:4000)   LiteLLM proxy base
  LITELLM_ADMIN_KEY / LITELLM_API_KEY / LITELLM_MASTER_KEY   key when --via litellm
  LITELLM_OWNER                                   owner tag (else models.yaml)

Exit code is non-zero if any model FAILS (a WARN — loaded but produced no
visible text, usually a thinking model hitting the token cap — does not fail).
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")
MODELS_YAML = os.path.join(REPO_ROOT, "models.yaml")

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"

DEFAULT_OWNER = "macos-omlx"


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


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


def setting(cli_value, env_name, default):
    if cli_value is not None:
        return cli_value
    if os.environ.get(env_name):
        return os.environ[env_name]
    if DOTENV.get(env_name):
        return DOTENV[env_name]
    return default


def setting_url(default_port_path):
    """OMLX_URL or UPSTREAM_URL."""
    if os.environ.get("OMLX_URL"):
        return os.environ["OMLX_URL"]
    if DOTENV.get("OMLX_URL"):
        return DOTENV["OMLX_URL"]
    return setting(None, "UPSTREAM_URL", default_port_path)


def resolve_upstream_key(cli_key):
    if cli_key is not None:
        return cli_key
    for name in ("MODEL_API_KEY", "OMLX_API_KEY"):
        if os.environ.get(name):
            return os.environ[name]
        if DOTENV.get(name):
            return DOTENV[name]
    return None


def openai_base(url):
    """Normalize a base URL so we can append /v1/... regardless of trailing /v1."""
    u = url.rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u


def _load_manifest():
    if not os.path.isfile(MODELS_YAML):
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    try:
        return yaml.safe_load(open(MODELS_YAML, encoding="utf-8")) or {}
    except Exception:
        return {}


def yaml_owner(manifest):
    o = manifest.get("owner")
    return str(o) if o is not None else None


def resolve_owner(cli_owner):
    o = setting(cli_owner, "LITELLM_OWNER", None)
    if o is not None:
        return o
    yo = yaml_owner(_load_manifest())
    if yo is not None:
        return yo
    return DEFAULT_OWNER


def model_manifest_index():
    """Map advertised name / alias -> manifest entry fields."""
    manifest = _load_manifest()
    index = {}
    for m in manifest.get("models") or []:
        name = m.get("name")
        if not name:
            continue
        index[name] = m
        alias = m.get("alias")
        if alias:
            index[str(alias)] = m
        for a in m.get("aliases") or []:
            if a:
                index[str(a)] = m
    return index


def is_embedding_model(model_id):
    idx = model_manifest_index()
    m = idx.get(model_id)
    if not m:
        return False
    if (m.get("type") or "").lower() == "embedding":
        return True
    litellm = m.get("litellm") or {}
    return (litellm.get("mode") or "").lower() == "embedding"


def skip_chat_models():
    """Names to skip in bulk chat runs (image generation, rerankers, etc.)."""
    manifest = _load_manifest()
    out = set()
    for m in manifest.get("models") or []:
        name = m.get("name")
        if not name:
            continue
        litellm = m.get("litellm") or {}
        mtype = (m.get("type") or "").lower()
        if litellm.get("mode") == "image_generation" or mtype in ("reranker",):
            out.add(name)
            alias = m.get("alias")
            if alias:
                out.add(str(alias))
            for a in m.get("aliases") or []:
                if a:
                    out.add(str(a))
    return out


def request(method, url, key, body, timeout):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return resp.status, (json.loads(raw) if raw.strip() else None)


def fetch_models(base, key, timeout):
    url = f"{base}/v1/models"
    try:
        status, payload = request("GET", url, key, None, timeout=min(timeout, 20))
    except urllib.error.HTTPError as exc:
        die(f"GET {url} -> HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        die(f"cannot reach {url}: {getattr(exc, 'reason', exc)}")
    if status != 200 or not isinstance(payload, dict):
        die(f"GET {url} returned {status}: {payload}")
    ids = [m["id"] for m in payload.get("data", []) if m.get("id")]
    if not ids:
        die(f"no models advertised by {url}")
    return ids


def fetch_litellm_owned(litellm_base, admin_key, owner, timeout):
    """Return model_name values managed by owner from GET /model/info."""
    url = f"{litellm_base.rstrip('/')}/model/info"
    try:
        status, payload = request("GET", url, admin_key, None, timeout=min(timeout, 30))
    except urllib.error.HTTPError as exc:
        die(f"GET {url} -> HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        die(f"cannot reach {url}: {getattr(exc, 'reason', exc)}")
    if status == 401:
        die("LiteLLM rejected the admin key (401). Check LITELLM_ADMIN_KEY / LITELLM_API_KEY / LITELLM_MASTER_KEY.")
    if status != 200 or not isinstance(payload, dict):
        die(f"GET {url} returned {status}: {payload}")
    names = []
    for item in payload.get("data", []):
        info = item.get("model_info") or {}
        if info.get("managed_by") != owner:
            continue
        model_name = item.get("model_name")
        if model_name:
            names.append(model_name)
    if not names:
        die(f"no LiteLLM models with managed_by={owner!r} — run make sync-litellm?")
    return sorted(set(names))


def short_error(exc):
    """Pull a human-readable message out of an HTTPError body."""
    body = exc.read().decode(errors="replace")
    try:
        obj = json.loads(body)
        err = obj.get("error", obj)
        msg = err.get("message") if isinstance(err, dict) else err
        body = msg or body
    except Exception:
        pass
    return " ".join(str(body).split())[:300]


def test_chat_one(base, key, model, prompt, max_tokens, timeout):
    """Return (verdict, seconds, detail). verdict in {ok, warn, fail}."""
    url = f"{base}/v1/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    start = time.monotonic()
    try:
        status, payload = request("POST", url, key, body, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return "fail", time.monotonic() - start, f"HTTP {exc.code}: {short_error(exc)}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return "fail", time.monotonic() - start, f"connection: {getattr(exc, 'reason', exc)}"
    elapsed = time.monotonic() - start

    if status != 200 or not isinstance(payload, dict):
        return "fail", elapsed, f"HTTP {status}: {str(payload)[:300]}"

    choices = payload.get("choices") or []
    if not choices:
        return "fail", elapsed, "200 but no choices in response"
    msg = choices[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    finish = choices[0].get("finish_reason")

    if content:
        return "ok", elapsed, f'reply="{" ".join(content.split())[:60]}"'
    if reasoning:
        return "ok", elapsed, (
            f"thinking only ({len(reasoning)} chars); bump --max-tokens for a final answer"
        )
    return "warn", elapsed, (
        f"empty content (finish_reason={finish}); likely a thinking model — raise --max-tokens"
    )


def test_embedding_one(base, key, model, prompt, timeout):
    """Return (verdict, seconds, detail)."""
    url = f"{base}/v1/embeddings"
    body = {"model": model, "input": prompt or "hello"}
    start = time.monotonic()
    try:
        status, payload = request("POST", url, key, body, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return "fail", time.monotonic() - start, f"HTTP {exc.code}: {short_error(exc)}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return "fail", time.monotonic() - start, f"connection: {getattr(exc, 'reason', exc)}"
    elapsed = time.monotonic() - start

    if status != 200 or not isinstance(payload, dict):
        return "fail", elapsed, f"HTTP {status}: {str(payload)[:300]}"

    data = payload.get("data") or []
    if not data:
        return "fail", elapsed, "200 but no data in embeddings response"
    emb = data[0].get("embedding")
    if not isinstance(emb, list) or not emb:
        return "fail", elapsed, "missing or empty embedding vector"
    return "ok", elapsed, f"dims={len(emb)}"


def test_one(base, key, model, prompt, max_tokens, timeout):
    if is_embedding_model(model):
        return test_embedding_one(base, key, model, prompt, timeout)
    return test_chat_one(base, key, model, prompt, max_tokens, timeout)


def parse_args():
    p = argparse.ArgumentParser(
        description="Smoke-test every model an endpoint serves.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("models", nargs="*", help="restrict to these model names (default: all)")
    p.add_argument("--via", choices=["upstream", "litellm"], default="upstream",
                   help="which endpoint to test (default upstream = oMLX)")
    p.add_argument("--base", help="explicit base URL (overrides --via)")
    p.add_argument("--api-key", dest="api_key", help="explicit api key (overrides --via)")
    p.add_argument("--owner", help="LiteLLM managed_by owner (default models.yaml / LITELLM_OWNER)")
    p.add_argument("--all", action="store_true",
                   help="with --via litellm: test all /v1/models (no ownership filter)")
    p.add_argument("--max-tokens", dest="max_tokens", type=int, default=256)
    p.add_argument("--timeout", type=float, default=120.0, help="per-request timeout (cold start)")
    p.add_argument("--prompt", default="Reply with exactly the word: OK")
    return p.parse_args()


def main():
    args = parse_args()
    owner = resolve_owner(args.owner)

    if args.base:
        base_url, key = args.base, args.api_key
    elif args.via == "litellm":
        base_url = setting(None, "LITELLM_URL", "http://localhost:4000")
        key = args.api_key or (
            os.environ.get("LITELLM_ADMIN_KEY")
            or os.environ.get("LITELLM_API_KEY")
            or os.environ.get("LITELLM_MASTER_KEY")
            or DOTENV.get("LITELLM_ADMIN_KEY")
            or DOTENV.get("LITELLM_API_KEY")
            or DOTENV.get("LITELLM_MASTER_KEY")
        )
        if not key:
            die("--via litellm needs LITELLM_ADMIN_KEY (or LITELLM_API_KEY / "
                "LITELLM_MASTER_KEY) in env or .env.")
    else:
        base_url = setting_url("http://localhost:8000")
        key = resolve_upstream_key(args.api_key)

    base = openai_base(base_url)

    print(f"target : {base}/v1   (--via {args.via})")
    if args.via == "litellm" and not args.all:
        print(f"filter : managed_by={owner!r}  (--all skips this)")
    print(f"params : max_tokens={args.max_tokens}  timeout={args.timeout:g}s  prompt={args.prompt!r}")

    if args.via == "litellm" and not args.all:
        models = fetch_litellm_owned(base, key, owner, args.timeout)
        # Still intersect with what's advertised for clearer errors on stale DB rows.
        try:
            advertised = set(fetch_models(base, key, args.timeout))
            stale = [m for m in models if m not in advertised]
            if stale:
                print(f"{YELLOW}note{RESET}: owned but not in /v1/models: {stale}")
            models = [m for m in models if m in advertised]
            if not models:
                die("none of the owned LiteLLM models are advertised by /v1/models.")
        except SystemExit:
            raise
        except Exception:
            pass
    else:
        models = fetch_models(base, key, args.timeout)

    if args.models:
        wanted = set(args.models)
        missing = wanted - set(models)
        if missing:
            print(f"{YELLOW}note{RESET}: not in test set: {sorted(missing)}")
        models = [m for m in models if m in wanted]
        if not models:
            die("none of the requested models are in the test set.")

    if not args.models:
        skip = skip_chat_models()
        hidden = [m for m in models if m in skip]
        if hidden:
            print(f"{DIM}skipping non-chat models: {', '.join(hidden)}{RESET}")
        models = [m for m in models if m not in skip]

    print(f"testing {len(models)} model(s)\n")
    sys.stdout.flush()

    results = []
    width = max((len(m) for m in models), default=8)
    for i, model in enumerate(models, 1):
        kind = "emb" if is_embedding_model(model) else "chat"
        print(f"[{i}/{len(models)}] {model:<{width}}  ({kind}) ... ", end="", flush=True)
        verdict, secs, detail = test_one(
            base, key, model, args.prompt, args.max_tokens, args.timeout,
        )
        color = {"ok": GREEN, "warn": YELLOW, "fail": RED}[verdict]
        print(f"{color}{verdict.upper():4}{RESET}  {secs:5.1f}s  {DIM}{detail}{RESET}")
        results.append((model, verdict, detail))

    oks = [m for m, v, _ in results if v == "ok"]
    warns = [(m, d) for m, v, d in results if v == "warn"]
    fails = [(m, d) for m, v, d in results if v == "fail"]

    print(f"\n{'-' * 60}")
    print(f"{len(results)} tested: {GREEN}{len(oks)} ok{RESET}, "
          f"{YELLOW}{len(warns)} warn{RESET}, {RED}{len(fails)} fail{RESET}")
    for m, d in warns:
        print(f"  {YELLOW}warn{RESET} {m}: {d}")
    for m, d in fails:
        print(f"  {RED}fail{RESET} {m}: {d}")

    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
