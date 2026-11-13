#!/usr/bin/env python3
"""
sync-litellm.py

Owner-scoped sync: register only this host's oMLX models in a shared LiteLLM proxy.

    LITELLM_API_KEY=sk-... ./scripts/sync-litellm.py

What it does, in one pass:
  * Reads source model ids from GET {upstream}/v1/models (oMLX).
  * Reads LiteLLM's current models via GET {litellm}/model/info (keyed by model_info.id).
  * Adds missing models owned by this host (POST /model/new).
  * Re-points or deletes only entries where model_info.managed_by == OWNER.
  * Leaves static config models, foreign-owned DB models, and unowned DB models alone
    (unless --adopt claims unowned entries on this host's api_base).

Configuration (CLI flag > process env > .env in repo root > default):
  --upstream       UPSTREAM_URL       http://localhost:8000
  --litellm        LITELLM_URL        http://localhost:4000
  --api-base       MODEL_API_BASE     {upstream}/v1
  --model-api-key  MODEL_API_KEY      OMLX_API_KEY from env/.env, else 'dummy'
  --provider       MODEL_PROVIDER     openai
  --owner          LITELLM_OWNER      models.yaml owner, else macos-omlx
  --timeout        (HTTP client timeout per request, default 15s)

Per-model litellm_params timeout/num_retries default to 900 / 0; override with
MODEL_TIMEOUT and MODEL_NUM_RETRIES in the environment or .env.

The LiteLLM admin key is read from LITELLM_ADMIN_KEY, LITELLM_API_KEY, or
LITELLM_MASTER_KEY (env or .env). It is never printed.

Flags: --dry-run, --no-delete, --reset, --adopt, --allow-shared-names, --self-test.
"""

import argparse
import json
import math
import os
import socket
import sys
import urllib.error
import urllib.request
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")
MODELS_YAML = os.path.join(REPO_ROOT, "models.yaml")

PROBE_MODEL_NAME = "__omlx_probe__"
PROBE_INFO_KEY = "__omlx_sync_probe__"

DEFAULT_OWNER = "macos-omlx"


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_dotenv(path):
    """Minimal KEY=VALUE reader for the repo .env (ignores comments/blank/quotes)."""
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            values[key.strip()] = val
    return values


DOTENV = load_dotenv(ENV_FILE)


def setting(cli_value, env_name, default):
    """Precedence: CLI flag > process env > repo .env > default."""
    if cli_value is not None:
        return cli_value
    if os.environ.get(env_name):
        return os.environ[env_name]
    if DOTENV.get(env_name):
        return DOTENV[env_name]
    return default


def setting_int(cli_value, env_name, default):
    raw = setting(cli_value, env_name, None)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        die(f"invalid integer for {env_name}: {raw!r}")


def api(method, url, key, body=None, timeout=15):
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
        return exc.code, detail
    except urllib.error.URLError as exc:
        die(f"cannot reach {url}: {exc.reason}")
    except TimeoutError:
        die(f"timed out reaching {url}")


def _load_yaml_manifest():
    if not os.path.isfile(MODELS_YAML):
        return None
    try:
        import yaml
    except ImportError:
        print("note: PyYAML not installed — models.yaml metadata ignored.", file=sys.stderr)
        return None
    with open(MODELS_YAML, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def yaml_owner(manifest):
    if not manifest:
        return None
    owner = manifest.get("owner")
    if owner is None:
        return None
    return str(owner).strip() or None


def enabled_sync_names(manifest):
    """Enabled model names and aliases from models.yaml."""
    names = set()
    if not manifest:
        return names
    for m in manifest.get("models") or []:
        if m.get("enabled") is False:
            continue
        name = m.get("name")
        if name:
            names.add(name)
        for alias in m.get("aliases") or []:
            if alias:
                names.add(str(alias))
    return names


def load_model_infos(manifest):
    """Map model name -> LiteLLM model_info dict (without managed_by / managed_host)."""
    if not manifest:
        return {}
    cost = manifest.get("cost") or {}
    watts = cost.get("watts")
    rate = cost.get("usd_per_kwh")
    amort = cost.get("amortization", 1)
    input_factor = cost.get("input_factor", 1)

    out = {}
    for m in manifest.get("models") or []:
        if m.get("enabled") is False:
            continue
        name = m.get("name")
        if not name:
            continue
        info = {}
        block = m.get("litellm")
        if isinstance(block, dict):
            info.update({k: (" ".join(v.split()) if isinstance(v, str) else v)
                         for k, v in block.items()})
        mtype = (m.get("type") or "").lower()
        if mtype == "embedding":
            info["mode"] = "embedding"
        else:
            info.setdefault("mode", "chat")
        if mtype == "vlm":
            info["supports_vision"] = True
        else:
            info.setdefault("supports_vision", False)
        if m.get("ctx"):
            info.setdefault("max_input_tokens", int(m["ctx"]))
        tps = m.get("decode_tps")
        if tps and watts and rate:
            out_cost = (watts / 1000.0) * (1.0 / float(tps) / 3600.0) * float(rate) * float(amort)
            info["output_cost_per_token"] = round(out_cost, 12)
            info["input_cost_per_token"] = round(out_cost / float(input_factor), 12)
        out[name] = info
        for alias in m.get("aliases") or []:
            if alias and alias not in out:
                out[str(alias)] = dict(info)
    return out


def resolve_owner(cli_owner, manifest):
    o = setting(cli_owner, "LITELLM_OWNER", None)
    if o is not None:
        return o
    yo = yaml_owner(manifest)
    if yo is not None:
        return yo
    return DEFAULT_OWNER


def resolve_model_api_key(cli_key):
    if cli_key is not None:
        return cli_key
    if os.environ.get("MODEL_API_KEY"):
        return os.environ["MODEL_API_KEY"]
    if DOTENV.get("MODEL_API_KEY"):
        return DOTENV["MODEL_API_KEY"]
    if os.environ.get("OMLX_API_KEY"):
        return os.environ["OMLX_API_KEY"]
    if DOTENV.get("OMLX_API_KEY"):
        return DOTENV["OMLX_API_KEY"]
    return "dummy"


def classify_entry(item, owner):
    info = item.get("model_info") or {}
    if not info.get("db_model"):
        return "static"
    managed_by = info.get("managed_by")
    if managed_by == owner:
        return "owned"
    if managed_by:
        return "foreign"
    return "unowned"


def fetch_source_models(upstream, timeout, api_key=None):
    url = f"{upstream.rstrip('/')}/v1/models"
    # oMLX requires a Bearer key on non-loopback binds; always send when present.
    status, payload = api("GET", url, key=api_key, timeout=timeout)
    if status == 401:
        die(f"GET {url} returned 401 — set OMLX_API_KEY / MODEL_API_KEY for upstream auth")
    if status != 200 or not isinstance(payload, dict):
        die(f"GET {url} returned {status}: {payload}")
    ids = sorted({m["id"] for m in payload.get("data", []) if m.get("id")})
    return ids


def compute_desired_source_ids(upstream_ids, manifest):
    """Only sync models oMLX currently advertises (intersect enabled manifest names).

    Do not register yaml-enabled names that are not served yet — that leaves
    LiteLLM pointing at missing upstream ids.
    """
    if manifest is None:
        if not upstream_ids:
            die("no models reported by upstream — is oMLX running?")
        return upstream_ids

    enabled = enabled_sync_names(manifest)
    if not enabled:
        return sorted(upstream_ids)

    desired = sorted(mid for mid in upstream_ids if mid in enabled)
    skipped = sorted(mid for mid in upstream_ids if mid not in enabled)
    missing = sorted(name for name in enabled if name not in set(upstream_ids))
    if skipped:
        print(f"note: upstream ids not in enabled models.yaml (ignored): {skipped}")
    if missing:
        print(f"note: enabled in models.yaml but not yet served by oMLX (skipped): {missing}")
    if not desired and not upstream_ids:
        die("no models to sync (empty upstream and no enabled entries in models.yaml)")
    if not desired:
        die("no overlap between oMLX /v1/models and enabled models.yaml names "
            "(apply aliases via make models-apply after downloads)")
    return desired


def fetch_litellm_models(litellm, key, owner, timeout):
    url = f"{litellm.rstrip('/')}/model/info"
    status, payload = api("GET", url, key=key, timeout=timeout)
    if status == 401:
        die("LiteLLM rejected the admin key (401). Check LITELLM_ADMIN_KEY / LITELLM_API_KEY / LITELLM_MASTER_KEY.")
    if status != 200 or not isinstance(payload, dict):
        die(f"GET {url} returned {status}: {payload}")

    by_id = {}
    for item in payload.get("data", []):
        info = item.get("model_info") or {}
        model_id = info.get("id")
        if not model_id:
            continue
        params = item.get("litellm_params") or {}
        kind = classify_entry(item, owner)
        by_id[model_id] = {
            "id": model_id,
            "model_name": item.get("model_name"),
            "db_model": bool(info.get("db_model")),
            "managed_by": info.get("managed_by"),
            "kind": kind,
            "model": params.get("model"),
            "api_base": params.get("api_base"),
            "num_retries": params.get("num_retries"),
            "timeout": params.get("timeout"),
            "info": info,
        }
    return by_id


def add_model(litellm, key, name, params, timeout, model_info=None):
    url = f"{litellm.rstrip('/')}/model/new"
    body = {"model_name": name, "litellm_params": params}
    if model_info is not None:
        body["model_info"] = model_info
    status, payload = api("POST", url, key=key, body=body, timeout=timeout)
    if status not in (200, 201):
        die(f"failed to add '{name}' ({status}): {payload}")


def delete_model(litellm, key, model_id, timeout):
    url = f"{litellm.rstrip('/')}/model/delete"
    status, payload = api("POST", url, key=key, body={"id": model_id}, timeout=timeout)
    if status not in (200, 201):
        die(f"failed to delete id={model_id} ({status}): {payload}")


def name_has_foreign_or_unowned(model_name, by_id, allow_shared, exclude_id=None):
    if allow_shared:
        return False
    for entry in by_id.values():
        if exclude_id and entry.get("id") == exclude_id:
            continue
        if entry.get("model_name") != model_name:
            continue
        if entry["kind"] in ("foreign", "unowned"):
            return True
    return False


def owned_entries_for_name(model_name, by_id):
    return [e for e in by_id.values() if e.get("model_name") == model_name and e["kind"] == "owned"]


def info_drifted(model_name, cur_info, want_info):
    have = cur_info or {}
    for k, v in want_info.items():
        hv = have.get(k)
        if isinstance(v, float) and isinstance(hv, (int, float)):
            if not math.isclose(float(hv), v, rel_tol=1e-6, abs_tol=1e-15):
                return True
        elif hv != v:
            return True
    return False


def params_drifted(entry, want_params):
    return (
        entry.get("model") != want_params.get("model")
        or entry.get("api_base") != want_params.get("api_base")
        or entry.get("num_retries") != want_params.get("num_retries")
        or entry.get("timeout") != want_params.get("timeout")
    )


def run_self_test(litellm, admin_key, owner, managed_host, api_base, provider,
                  model_api_key, model_num_retries, model_timeout, http_timeout, dry_run):
    probe_params = {
        "model": f"{provider}/{PROBE_MODEL_NAME}",
        "api_base": api_base,
        "api_key": model_api_key,
        "num_retries": model_num_retries,
        "timeout": model_timeout,
    }
    probe_info = {
        PROBE_INFO_KEY: "persist-check",
        "managed_by": owner,
        "managed_host": managed_host,
    }

    existing = fetch_litellm_models(litellm, admin_key, owner, http_timeout)
    for entry in list(existing.values()):
        if entry.get("model_name") == PROBE_MODEL_NAME and entry["kind"] == "owned":
            print(f"self-test: removing prior owned probe {entry['id']}")
            if not dry_run:
                delete_model(litellm, admin_key, entry["id"], http_timeout)

    print(f"self-test: add {PROBE_MODEL_NAME}")
    if dry_run:
        print("self-test: dry run — skipping live probe")
        return

    add_model(litellm, admin_key, PROBE_MODEL_NAME, probe_params, http_timeout,
              model_info=probe_info)

    refreshed = fetch_litellm_models(litellm, admin_key, owner, http_timeout)
    probe = None
    for entry in refreshed.values():
        if entry.get("model_name") == PROBE_MODEL_NAME and entry["kind"] == "owned":
            probe = entry
            break

    if probe is None:
        die("self-test: probe model not found after add")

    got = (probe.get("info") or {}).get(PROBE_INFO_KEY)
    if got != "persist-check":
        die(f"self-test: custom key {PROBE_INFO_KEY!r} did not persist (got {got!r})")

    print(f"self-test: {PROBE_INFO_KEY} persisted on id={probe['id']}")
    delete_model(litellm, admin_key, probe["id"], http_timeout)
    print("self-test: ok")


def parse_args():
    p = argparse.ArgumentParser(
        description="Owner-scoped sync of oMLX models into a LiteLLM proxy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--upstream", help="oMLX base URL (default http://localhost:8000)")
    p.add_argument("--litellm", help="LiteLLM proxy base URL (default http://localhost:4000)")
    p.add_argument("--api-base", dest="api_base",
                   help="api_base stored in LiteLLM for each model (default {upstream}/v1)")
    p.add_argument("--model-api-key", dest="model_api_key",
                   help="api_key stored with each model (default OMLX_API_KEY or dummy)")
    p.add_argument("--provider", help="LiteLLM provider prefix (default openai)")
    p.add_argument("--owner", help=f"ownership tag managed_by (default {DEFAULT_OWNER})")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="HTTP client timeout in seconds for each API call (default 15)")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("--no-delete", action="store_true", help="never delete owned models, only add/update")
    p.add_argument("--reset", action="store_true",
                   help="delete all DB models owned by this host, then re-add from source")
    p.add_argument("--adopt", action="store_true",
                   help="claim unowned DB models on this api_base by re-adding with managed_by")
    p.add_argument("--allow-shared-names", action="store_true",
                   help="allow model_name collisions with foreign/unowned entries")
    p.add_argument("--self-test", action="store_true",
                   help="probe that custom model_info keys persist via /model/info, then exit")
    return p.parse_args()


def main():
    args = parse_args()

    manifest = _load_yaml_manifest()
    owner = resolve_owner(args.owner, manifest)
    managed_host = setting(None, "MODEL_MANAGED_HOST", socket.gethostname())

    upstream = setting(args.upstream, "UPSTREAM_URL", "http://localhost:8000")
    litellm = setting(args.litellm, "LITELLM_URL", "http://localhost:4000")
    api_base = setting(args.api_base, "MODEL_API_BASE", f"{upstream.rstrip('/')}/v1")
    model_api_key = resolve_model_api_key(args.model_api_key)
    provider = setting(args.provider, "MODEL_PROVIDER", "openai")
    model_num_retries = setting_int(None, "MODEL_NUM_RETRIES", 0)
    model_timeout = setting_int(None, "MODEL_TIMEOUT", 900)

    admin_key = (
        os.environ.get("LITELLM_ADMIN_KEY")
        or os.environ.get("LITELLM_API_KEY")
        or os.environ.get("LITELLM_MASTER_KEY")
        or DOTENV.get("LITELLM_ADMIN_KEY")
        or DOTENV.get("LITELLM_API_KEY")
        or DOTENV.get("LITELLM_MASTER_KEY")
    )
    if not admin_key:
        die("no LiteLLM admin key. Set LITELLM_ADMIN_KEY (or LITELLM_API_KEY / "
            "LITELLM_MASTER_KEY) in the environment or in .env.")

    print(f"source  : {upstream.rstrip('/')}/v1/models")
    print(f"litellm : {litellm.rstrip('/')}/model/*")
    print(f"api_base: {api_base}  (provider: {provider})")
    print(f"owner   : {owner}")
    print(f"managed_host: {managed_host}")
    if args.dry_run:
        print("mode    : DRY RUN — no changes will be made")
    print()
    sys.stdout.flush()

    if args.self_test:
        run_self_test(litellm, admin_key, owner, managed_host, api_base, provider,
                      model_api_key, model_num_retries, model_timeout, args.timeout,
                      args.dry_run)
        return

    # Prefer MODEL_API_KEY / OMLX_API_KEY for reading upstream /v1/models.
    upstream_key = (
        os.environ.get("OMLX_API_KEY")
        or DOTENV.get("OMLX_API_KEY")
        or model_api_key
    )
    upstream_ids = fetch_source_models(upstream, args.timeout, api_key=upstream_key)
    source_ids = compute_desired_source_ids(upstream_ids, manifest)
    model_infos = load_model_infos(manifest)
    by_id = fetch_litellm_models(litellm, admin_key, owner, args.timeout)

    foreign = sorted(
        {f"{e['model_name']} (id={e['id']}, managed_by={e['managed_by']})"
         for e in by_id.values() if e["kind"] == "foreign"},
        key=str.lower,
    )
    unowned = sorted(
        {f"{e['model_name']} (id={e['id']})" for e in by_id.values() if e["kind"] == "unowned"},
        key=str.lower,
    )

    def desired_params(model_id):
        return {
            "model": f"{provider}/{model_id}",
            "api_base": api_base,
            "api_key": model_api_key,
            "num_retries": model_num_retries,
            "timeout": model_timeout,
        }

    def desired_info(model_id):
        info = dict(model_infos.get(model_id) or {})
        info["managed_by"] = owner
        info["managed_host"] = managed_host
        return info

    if args.reset:
        purge = [e for e in by_id.values() if e["kind"] == "owned"]
        verb = "would delete" if args.dry_run else "deleting"
        print(f"reset: {verb} {len(purge)} owned model(s) before re-adding")
        for entry in purge:
            print(f"  - {entry['model_name']} (id={entry['id']})")
            if not args.dry_run:
                delete_model(litellm, admin_key, entry["id"], args.timeout)
        static = [e for e in by_id.values() if e["kind"] == "static"]
        for entry in static:
            print(f"  skip {entry['model_name']} (config-defined, not deletable via API)")
        by_id = {i: e for i, e in by_id.items() if e["kind"] != "owned"}
        print()

    to_add, to_update, to_delete, to_adopt, skipped = [], [], [], [], []

    for model_id in source_ids:
        want_params = desired_params(model_id)
        want_info = desired_info(model_id)
        owned = owned_entries_for_name(model_id, by_id)

        if not owned:
            if name_has_foreign_or_unowned(model_id, by_id, args.allow_shared_names):
                skipped.append((model_id, "model_name collides with foreign/unowned entry"))
                continue
            to_add.append(model_id)
            continue

        for entry in owned:
            drift = params_drifted(entry, want_params) or info_drifted(model_id, entry.get("info"), want_info)
            if drift:
                to_update.append((model_id, entry["id"]))

    if args.adopt:
        for entry in by_id.values():
            if entry["kind"] != "unowned":
                continue
            if entry.get("api_base") != api_base:
                continue
            name = entry.get("model_name")
            if not name:
                continue
            if name_has_foreign_or_unowned(name, by_id, args.allow_shared_names,
                                            exclude_id=entry["id"]):
                skipped.append((name, "adopt skipped — name collides with foreign/unowned"))
                continue
            if any(a[0] == name for a in to_adopt):
                continue
            to_adopt.append((name, entry["id"]))

    owned_names_in_desired = {n for n in source_ids}
    for entry in by_id.values():
        if entry["kind"] != "owned":
            continue
        name = entry.get("model_name")
        if not name or name in owned_names_in_desired:
            continue
        if args.no_delete:
            skipped.append((name, "stale owned entry, but --no-delete set"))
        else:
            to_delete.append((name, entry["id"]))

    print(f"source models : {len(source_ids)}")
    print(f"  to add      : {len(to_add)}  {to_add if to_add else ''}")
    print(f"  to re-point : {len(to_update)}  {to_update if to_update else ''}")
    print(f"  to delete   : {len(to_delete)}  {[n for n, _ in to_delete] if to_delete else ''}")
    print(f"  to adopt    : {len(to_adopt)}  {to_adopt if to_adopt else ''}")
    if skipped:
        for name, why in skipped:
            print(f"  skip        : {name} ({why})")
    if foreign:
        print(f"  foreign     : {len(foreign)}")
        for line in foreign:
            print(f"    - {line}")
    if unowned:
        print(f"  unowned     : {len(unowned)}")
        for line in unowned:
            print(f"    - {line}")
    print()

    if args.dry_run:
        print("dry run complete — nothing changed.")
        return

    for model_id in to_add:
        print(f"+ add    {model_id}")
        add_model(litellm, admin_key, model_id, desired_params(model_id), args.timeout,
                  model_info=desired_info(model_id))

    for model_id, mid in to_update:
        print(f"~ repoint {model_id} (id={mid})")
        delete_model(litellm, admin_key, mid, args.timeout)
        add_model(litellm, admin_key, model_id, desired_params(model_id), args.timeout,
                  model_info=desired_info(model_id))

    for name, mid in to_adopt:
        print(f"* adopt  {name} (id={mid})")
        delete_model(litellm, admin_key, mid, args.timeout)
        add_model(litellm, admin_key, name, desired_params(name), args.timeout,
                  model_info=desired_info(name))

    for name, mid in to_delete:
        print(f"- delete {name} (id={mid})")
        delete_model(litellm, admin_key, mid, args.timeout)

    print()
    print(f"done: +{len(to_add)} added, ~{len(to_update)} re-pointed, "
          f"*{len(to_adopt)} adopted, -{len(to_delete)} deleted.")


if __name__ == "__main__":
    main()
