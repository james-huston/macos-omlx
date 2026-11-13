#!/usr/bin/env python3
"""
apply-models.py

Make oMLX's on-disk models and Admin API settings a pure function of models.yaml.

    make models-apply
    make models-apply DRY_RUN=1
    make models-apply NO_DOWNLOAD=1 STRICT=1

Workflow: edit models.yaml -> `make models-apply` -> `make sync-litellm` -> `make test-models`.

Configuration (CLI > process env > repo .env > default):
  --manifest       path to models.yaml (default: repo models.yaml)
  OMLX_URL / UPSTREAM_URL   oMLX base URL (default http://localhost:8000)
  OMLX_API_KEY     Bearer token for Admin API (never printed)
  OMLX_MODEL_DIR   local model root (default ~/.omlx/models)

Flags: --dry-run, --no-download, --strict, --help.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML is required. Install it with: pip install pyyaml")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")
DEFAULT_MANIFEST = os.path.join(REPO_ROOT, "models.yaml")
DEFAULT_OMLX_URL = "http://localhost:8000"
DEFAULT_MODEL_DIR = os.path.expanduser("~/.omlx/models")
HTTP_TIMEOUT = 15.0


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


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


def setting(cli_value, env_names, default):
    """Precedence: CLI flag > process env > repo .env > default."""
    if cli_value is not None:
        return cli_value
    if isinstance(env_names, str):
        env_names = (env_names,)
    for name in env_names:
        if os.environ.get(name):
            return os.environ[name]
        if DOTENV.get(name):
            return DOTENV[name]
    return default


def expand_path(raw):
    return os.path.expanduser(os.path.expandvars(raw))


def mask_key(key):
    if not key:
        return "(unset)"
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}…{key[-4:]}"


def truthy(val):
    if val is True:
        return True
    if val is False or val is None:
        return False
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def repo_org_name(repo_id):
    repo_id = str(repo_id).strip()
    if "/" not in repo_id:
        die(f"invalid Hugging Face repo id (expected org/name): {repo_id!r}")
    org, name = repo_id.split("/", 1)
    if not org or not name:
        die(f"invalid Hugging Face repo id: {repo_id!r}")
    return org, name


def repo_local_rel(repo_id):
    org, name = repo_org_name(repo_id)
    return f"{org}/{name}"


def repo_local_dir(model_dir, repo_id):
    org, name = repo_org_name(repo_id)
    return os.path.join(model_dir, org, name)


def model_files_present(local_dir):
    """True only when weights exist — config.json alone is not enough (partial HF pulls)."""
    if not os.path.isdir(local_dir):
        return False
    for pattern in ("*.safetensors", "**/*.safetensors"):
        if glob.glob(os.path.join(local_dir, pattern), recursive=("**" in pattern)):
            return True
    return False


def find_hf_download_cmd():
    if shutil.which("hf"):
        return ["hf", "download"]
    if shutil.which("huggingface-cli"):
        return ["huggingface-cli", "download"]
    return None


def download_repo(entry, local_dir, dry_run):
    repo = entry["repo"]
    if dry_run:
        rev = entry.get("revision")
        rev_s = f" --revision {rev}" if rev else ""
        print(f"  [WOULD DOWNLOAD] {repo} -> {local_dir}{rev_s}")
        return

    cmd_base = find_hf_download_cmd()
    if cmd_base is None:
        die("neither 'hf' nor 'huggingface-cli' found on PATH — install huggingface_hub CLI")

    os.makedirs(local_dir, exist_ok=True)
    cmd = list(cmd_base) + [repo, "--local-dir", local_dir]
    revision = entry.get("revision")
    if revision:
        cmd += ["--revision", str(revision)]
    print(f"  downloading {repo} -> {local_dir}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        die(f"download failed for {repo} (exit {exc.returncode})")
    if not model_files_present(local_dir):
        die(f"download finished but no config.json or *.safetensors in {local_dir}")


def resolve_enabled_paths(entry, model_dir):
    """Return (host_path, relative org/name key for stale-dir tracking)."""
    name = entry["name"]
    if entry.get("repo"):
        rel = repo_local_rel(entry["repo"])
        return repo_local_dir(model_dir, entry["repo"]), rel
    if entry.get("path"):
        raw = str(entry["path"]).strip()
        host = expand_path(raw)
        if not os.path.isabs(host):
            host = os.path.join(model_dir, raw)
        rel = None
        try:
            common = os.path.commonpath([os.path.realpath(host), os.path.realpath(model_dir)])
            if common == os.path.realpath(model_dir):
                rel = os.path.relpath(host, model_dir).replace(os.sep, "/")
        except ValueError:
            pass
        if rel and rel.count("/") >= 1:
            parts = rel.split("/")
            rel = f"{parts[0]}/{parts[1]}"
        elif rel:
            rel = rel.split("/")[0]
        return host, rel
    die(f"model {name!r} needs exactly one of 'repo' or 'path'")


def load_manifest(path):
    if not os.path.isfile(path):
        die(f"manifest not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    models = data.get("models")
    if not models:
        die(f"no models defined in {path}")
    return data


def validate_manifest(manifest):
    enabled = []
    seen_names = set()
    for entry in manifest.get("models") or []:
        name = entry.get("name")
        if not name:
            die("a model entry is missing 'name'")
        if name in seen_names:
            die(f"duplicate model name in manifest: {name}")
        seen_names.add(name)
        if entry.get("enabled", True) is False:
            continue
        has_repo = bool(entry.get("repo"))
        has_path = bool(entry.get("path"))
        if has_repo == has_path:
            die(f"model {name!r} needs exactly one of 'repo' or 'path'")
        enabled.append(entry)
    return enabled


def memory_budget_check(manifest, enabled, strict):
    mem = manifest.get("memory") or {}
    total = mem.get("total_gb")
    reserved = mem.get("reserved_os_gb", 0) or 0
    kv = mem.get("kv_headroom_gb", 0) or 0
    if total is None:
        warn("memory.total_gb not set in manifest — skipping memory budget check")
        return False

    pinned = []
    pinned_sum = 0.0
    for entry in enabled:
        if not truthy(entry.get("pin")):
            continue
        est = entry.get("est_mem_gb")
        if est is None:
            warn(f"pinned model {entry['name']!r} has no est_mem_gb — treated as 0 for budget")
            est = 0
        pinned.append((entry["name"], float(est)))
        pinned_sum += float(est)

    need = pinned_sum + float(reserved) + float(kv)
    ok = need <= float(total)
    print("memory budget:")
    print(f"  total_gb          : {total}")
    print(f"  reserved_os_gb    : {reserved}")
    print(f"  kv_headroom_gb    : {kv}")
    print(f"  pinned weights    : {pinned_sum:.2f} GB  ({len(pinned)} model(s))")
    for pname, gb in pinned:
        print(f"    - {pname}: {gb:.2f} GB")
    print(f"  required          : {need:.2f} GB")
    print(f"  headroom          : {float(total) - need:.2f} GB")
    if ok:
        print("  status            : OK")
        return False
    msg = (
        f"memory budget exceeded: pinned {pinned_sum:.2f} + reserved {reserved} + kv {kv} "
        f"= {need:.2f} GB > total {total} GB"
    )
    if strict:
        die(msg)
    warn(msg)
    return True


def brain_pin_check(enabled, strict):
    brains = [
        entry["name"]
        for entry in enabled
        if (entry.get("role") or "").lower() == "brain" and truthy(entry.get("pin"))
    ]
    if len(brains) < 2:
        return False
    msg = (
        "multiple enabled models have role=brain with pin truthy "
        f"(never pin two brains): {', '.join(brains)}"
    )
    if strict:
        die(msg)
    warn(msg)
    return True


def list_stale_dirs(model_dir, referenced_rels):
    if not os.path.isdir(model_dir):
        return []
    stale = []
    for org in sorted(os.listdir(model_dir)):
        org_path = os.path.join(model_dir, org)
        if not os.path.isdir(org_path) or org.startswith("."):
            continue
        try:
            subs = os.listdir(org_path)
        except OSError:
            continue
        if not subs:
            rel = org
            if rel not in referenced_rels:
                stale.append(rel)
            continue
        for sub in sorted(subs):
            sub_path = os.path.join(org_path, sub)
            if not os.path.isdir(sub_path):
                continue
            rel = f"{org}/{sub}"
            if rel not in referenced_rels:
                stale.append(rel)
    return stale


def api_request(method, url, api_key, body=None, timeout=HTTP_TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = detail
        return exc.code, parsed
    except urllib.error.URLError as exc:
        return None, f"cannot reach {url}: {exc.reason}"
    except TimeoutError:
        return None, f"timed out reaching {url}"


def normalize_models_payload(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "models", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def model_record_id(record):
    for key in ("id", "model_id", "name", "model_name"):
        val = record.get(key)
        if val:
            return str(val)
    return None


def model_record_strings(record):
    """Collect lowercase strings we can match against."""
    out = set()
    mid = model_record_id(record)
    if mid:
        out.add(mid.lower())
    for key in ("path", "model_path", "local_path", "directory", "dir"):
        val = record.get(key)
        if val:
            out.add(str(val).lower().replace("\\", "/"))
    alias = record.get("model_alias") or record.get("alias")
    if alias:
        if isinstance(alias, list):
            out.update(str(a).lower() for a in alias if a)
        else:
            out.add(str(alias).lower())
    for key in ("aliases", "model_aliases"):
        val = record.get(key)
        if isinstance(val, list):
            out.update(str(a).lower() for a in val if a)
    return out


def find_api_model_id(entry, api_models, local_rel):
    name = entry["name"].lower()
    alias = entry.get("alias")
    alias_l = str(alias).lower() if alias else None
    leaf = local_rel.split("/")[-1].lower() if local_rel else ""
    want = {name, local_rel.lower() if local_rel else "", leaf}
    if alias_l:
        want.add(alias_l)
    want.discard("")

    best = None
    for record in api_models:
        strings = model_record_strings(record)
        if want & strings:
            best = model_record_id(record)
            break
        # Match HF leaf dir (oMLX often advertises basename, not org/name)
        for s in strings:
            base = s.rsplit("/", 1)[-1]
            if base in want or (leaf and (s.endswith(leaf) or leaf in s)):
                best = model_record_id(record)
                break
        if best:
            break
    return best


def build_settings_payload(entry):
    body = {}
    if "pin" in entry:
        body["is_pinned"] = truthy(entry.get("pin"))
    if entry.get("ttl") is not None:
        body["ttl_seconds"] = int(entry["ttl"])
    # Advertise the stable manifest `name` (LiteLLM sync keys off /v1/models ids).
    # oMLX only supports a single model_alias; short aliases like "worker" stay in
    # models.yaml for docs / Hermes notes. Clients can also use the on-disk basename.
    if entry.get("name"):
        body["model_alias"] = str(entry["name"])
    if entry.get("type") is not None:
        body["model_type_override"] = str(entry["type"])
    if entry.get("ctx") is not None:
        body["max_context_window"] = int(entry["ctx"])
    if entry.get("temp") is not None:
        body["temperature"] = float(entry["temp"])
    if entry.get("top_p") is not None:
        body["top_p"] = float(entry["top_p"])
    ctk = entry.get("chat_template_kwargs")
    if isinstance(ctk, dict) and ctk:
        body["chat_template_kwargs"] = ctk
    return body


def print_manual_settings_instructions(entry, base_url, api_key, settings_body, reason):
    name = entry["name"]
    base = base_url.rstrip("/")
    print()
    print(f"Could not apply Admin settings for {name!r} ({reason}).")
    print("Configure manually in the oMLX Admin UI, or run:")
    print(f"  # resolve model id first:")
    print("  curl -s -H \"Authorization: Bearer $OMLX_API_KEY\" \\")
    print(f"    {base}/admin/api/models")
    print(f"  # then PUT (replace MODEL_ID):")
    payload = json.dumps(settings_body, indent=2)
    for line in payload.splitlines():
        print(f"  #   {line}")
    print(f"  curl -X PUT -H \"Authorization: Bearer <OMLX_API_KEY>\" \\")
    print(f"    -H \"Content-Type: application/json\" \\")
    print(f"    -d '<payload above>' \\")
    print(f"    {base}/admin/api/models/MODEL_ID/settings")
    print(f"  (API key in use: {mask_key(api_key)})")


def apply_settings(entry, base_url, api_key, api_models, local_rel, dry_run):
    settings_body = build_settings_payload(entry)
    if not settings_body:
        print(f"  [skip settings] {entry['name']} — no Admin fields to apply")
        return True

    model_id = find_api_model_id(entry, api_models, local_rel)
    if dry_run:
        target = model_id or f"(after download: {local_rel or entry['name']})"
        print(f"  [WOULD PUT] {entry['name']} -> {target}  {json.dumps(settings_body, sort_keys=True)}")
        return True

    if not model_id:
        print_manual_settings_instructions(
            entry, base_url, api_key, settings_body,
            "model not found in GET /admin/api/models (try after download / oMLX scan)",
        )
        return False

    url = f"{base_url.rstrip('/')}/admin/api/models/{model_id}/settings"

    status, payload = api_request("PUT", url, api_key, body=settings_body)
    if status in (200, 201, 204):
        print(f"  [settings] {entry['name']} -> {model_id}")
        return True

    reason = f"PUT returned {status}: {payload}"
    print_manual_settings_instructions(entry, base_url, api_key, settings_body, reason)
    return False


def fetch_api_models(base_url, api_key):
    url = f"{base_url.rstrip('/')}/admin/api/models"
    status, payload = api_request("GET", url, api_key)
    if status != 200:
        return None, f"GET /admin/api/models returned {status}: {payload}"
    return normalize_models_payload(payload), None


def parse_args():
    p = argparse.ArgumentParser(
        description="Apply models.yaml to oMLX downloads and Admin API settings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--manifest", default=DEFAULT_MANIFEST, help="path to models.yaml")
    p.add_argument("--dry-run", action="store_true", help="show plan; change nothing")
    p.add_argument("--no-download", action="store_true", help="skip Hugging Face downloads")
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero on memory budget or multi-brain pin violations")
    return p.parse_args()


def main():
    args = parse_args()
    strict_issues = False

    manifest = load_manifest(args.manifest)
    enabled = validate_manifest(manifest)

    base_url = setting(None, ("OMLX_URL", "UPSTREAM_URL"), DEFAULT_OMLX_URL)
    api_key = setting(None, "OMLX_API_KEY", "")
    model_dir = expand_path(setting(None, "OMLX_MODEL_DIR", DEFAULT_MODEL_DIR))

    all_models = manifest.get("models") or []
    disabled = [m["name"] for m in all_models if m.get("enabled", True) is False]

    print(f"manifest   : {args.manifest}")
    print(f"OMLX_URL   : {base_url}")
    print(f"MODEL_DIR  : {model_dir}")
    print(f"API key    : {mask_key(api_key)}")
    print(f"enabled    : {len(enabled)}   disabled : {len(disabled)}"
          + (f"  {disabled}" if disabled else ""))
    if args.dry_run:
        print("mode       : DRY RUN — nothing will be downloaded or sent to the API")
    if args.no_download:
        print("downloads  : skipped (--no-download)")
    print()
    sys.stdout.flush()

    if memory_budget_check(manifest, enabled, args.strict):
        strict_issues = True
    if brain_pin_check(enabled, args.strict):
        strict_issues = True
    print()

    referenced_rels = set()
    hard_fail = False

    for entry in enabled:
        host_path, rel = resolve_enabled_paths(entry, model_dir)
        if rel:
            referenced_rels.add(rel)
        name = entry["name"]
        print(f"{name}:")
        if entry.get("path"):
            if os.path.isdir(host_path):
                print(f"  [path ok] {host_path}")
            elif args.dry_run:
                warn(f"{name}: path not found (dry-run continues): {host_path}")
            else:
                print(f"  [MISSING] path not found: {host_path}", file=sys.stderr)
                hard_fail = True
            continue

        if model_files_present(host_path):
            print(f"  [have] {host_path}")
        elif args.dry_run:
            download_repo(entry, host_path, dry_run=True)
        elif args.no_download:
            print(f"  [missing] {host_path} (--no-download; not fetching)", file=sys.stderr)
            hard_fail = True
        else:
            download_repo(entry, host_path, dry_run=False)

    print()

    stale = list_stale_dirs(model_dir, referenced_rels)
    if stale:
        print("stale model dirs under MODEL_DIR (not referenced by any enabled entry):")
        for rel in stale:
            print(f"  - {rel}/  (remove manually if unwanted)")
        print()

    settings_failures = 0
    api_models = []
    present = [
        entry for entry in enabled
        if model_files_present(resolve_enabled_paths(entry, model_dir)[0])
        or (entry.get("path") and os.path.isdir(expand_path(str(entry["path"]))))
        or args.dry_run
    ]
    if present:
        models, err = fetch_api_models(base_url, api_key)
        if models is None:
            warn(f"could not list models from Admin API: {err}")
            print("Skipping automatic settings apply for all models.")
            for entry in present:
                body = build_settings_payload(entry)
                if body:
                    print_manual_settings_instructions(
                        entry, base_url, api_key, body,
                        "Admin API model list unavailable",
                    )
                    settings_failures += 1
        else:
            api_models = models
            print(f"Admin API : {len(api_models)} model(s) reported")
            for entry in present:
                _, rel = resolve_enabled_paths(entry, model_dir)
                if rel is None and entry.get("repo"):
                    rel = repo_local_rel(entry["repo"])
                elif rel is None:
                    rel = entry["name"]
                if not apply_settings(entry, base_url, api_key, api_models, rel, args.dry_run):
                    settings_failures += 1
            print()

    if settings_failures:
        warn(f"{settings_failures} model(s) need manual Admin configuration (see above)")

    if hard_fail:
        die("one or more enabled models are missing on disk")

    if args.strict and strict_issues:
        sys.exit(1)

    if not args.dry_run:
        print("Next: `make sync-litellm` to mirror into LiteLLM, then `make test-models`.")


if __name__ == "__main__":
    main()
