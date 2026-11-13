#!/usr/bin/env python3
"""
render-app-configs.py

Render docs/app-configs/*.yaml.template → docs/app-configs/generated/*.yaml
using values from the repo .env (and a few safe defaults).

Generated files are gitignored — they may contain host-specific names.

    make app-configs
    ./scripts/render-app-configs.py
    ./scripts/render-app-configs.py --dry-run

Substitution: ${VAR} and ${VAR:-default}. Unresolved vars abort the render.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")
TEMPLATE_DIR = os.path.join(REPO_ROOT, "docs", "app-configs")
OUT_DIR = os.path.join(TEMPLATE_DIR, "generated")

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Keys templates may reference; process env overrides .env for these.
KNOWN_KEYS = (
    "LITELLM_MODEL_PREFIX",
    "LITELLM_URL",
    "LITELLM_OWNER",
    "MODEL_API_BASE",
    "MODEL_MANAGED_HOST",
    "MODEL_API_KEY",
    "OMLX_API_KEY",
    "OMLX_URL",
    "UPSTREAM_URL",
)


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


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


def default_model_prefix() -> str:
    host = socket.gethostname().split(".")[0].strip().lower()
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in host).strip("-_")
    if not safe:
        safe = "omlx"
    return f"{safe}-"


def build_vars(dotenv: dict[str, str]) -> dict[str, str]:
    """Precedence: process env > .env > defaults."""
    out = dict(dotenv)
    for key in KNOWN_KEYS:
        if key in os.environ:
            out[key] = os.environ[key]

    out.setdefault("LITELLM_MODEL_PREFIX", default_model_prefix())
    out.setdefault("LITELLM_URL", "http://localhost:4000")
    out.setdefault("LITELLM_OWNER", "macos-omlx")
    out.setdefault("MODEL_MANAGED_HOST", socket.gethostname().split(".")[0])
    # Templates append "/v1"; normalize so trailing slashes in .env don't double up.
    if "LITELLM_URL" in out:
        out["LITELLM_URL"] = out["LITELLM_URL"].rstrip("/")
    return out


def render(text: str, variables: dict[str, str], path: str) -> str:
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        if name in variables:
            return variables[name]
        if default is not None:
            return default
        missing.append(name)
        return match.group(0)

    rendered = VAR_RE.sub(repl, text)
    if missing:
        uniq = ", ".join(sorted(set(missing)))
        die(f"{path}: unresolved template vars: {uniq} "
            f"(set them in .env or the environment)")
    return rendered


def list_templates() -> list[str]:
    if not os.path.isdir(TEMPLATE_DIR):
        die(f"missing template dir: {TEMPLATE_DIR}")
    names = sorted(
        f for f in os.listdir(TEMPLATE_DIR)
        if f.endswith(".yaml.template") and os.path.isfile(os.path.join(TEMPLATE_DIR, f))
    )
    if not names:
        die(f"no *.yaml.template files in {TEMPLATE_DIR}")
    return names


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="print outputs to stdout; write nothing")
    p.add_argument("--out-dir", default=OUT_DIR,
                   help=f"output directory (default {OUT_DIR})")
    args = p.parse_args()

    dotenv = load_dotenv(ENV_FILE)
    variables = build_vars(dotenv)
    templates = list_templates()

    print(f"templates : {TEMPLATE_DIR}")
    print(f"output    : {args.out_dir}")
    print(f"prefix    : {variables.get('LITELLM_MODEL_PREFIX')!r}")
    if args.dry_run:
        print("mode      : DRY RUN")
    print()

    if not args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)

    for name in templates:
        src = os.path.join(TEMPLATE_DIR, name)
        out_name = name[: -len(".template")]  # foo.yaml.template → foo.yaml
        dst = os.path.join(args.out_dir, out_name)
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        body = render(text, variables, name)
        output = (
            f"# GENERATED — from {name}; run `make app-configs` to refresh.\n"
            f"{body}"
        )

        if args.dry_run:
            print(f"--- {out_name} ---")
            print(output)
            if not output.endswith("\n"):
                print()
        else:
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(output)
                if not output.endswith("\n"):
                    fh.write("\n")
            print(f"wrote {os.path.relpath(dst, REPO_ROOT)}")

    if not args.dry_run:
        print()
        print("done — generated files are gitignored; copy into app config dirs as needed.")


if __name__ == "__main__":
    main()
