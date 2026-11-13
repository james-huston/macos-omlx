#!/usr/bin/env bash
# setup.sh — idempotent bootstrap of oMLX + tooling on Apple Silicon.
#
#   ./scripts/setup.sh            # interactive prompts for opt-in host tuning
#   ./scripts/setup.sh --yes      # non-interactive (skip opt-in tuning unless flagged)
#   ./scripts/setup.sh --dry-run  # print every action, change nothing
#
# Opt-in tuning (each reversible; see docs/host-setup.md):
#   --tune-sleep     disable system sleep via pmset
#   --tune-wired     raise GPU wired-memory limit via LaunchDaemon
#   --wired-mb N     absolute MB for iogpu.wired_limit_mb (default: RAM-12GB)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"
ENV_FILE="${REPO_ROOT}/.env"
SETTINGS_JSON="${HOME}/.omlx/settings.json"
DAEMON_SRC="${REPO_ROOT}/scripts/launchdaemons/com.macos-omlx.wired-limit.plist"
DAEMON_DST="/Library/LaunchDaemons/com.macos-omlx.wired-limit.plist"
DEFAULT_PORT=8000
DEFAULT_HOST="0.0.0.0"
DEFAULT_MODEL_DIR="${HOME}/.omlx/models"
DEFAULT_SSD_CACHE="${HOME}/.omlx/ssd-cache"
DEFAULT_MAX_CONCURRENT=8
DEFAULT_MEMORY_GUARD="safe"

DRY_RUN=0
YES=0
TUNE_SLEEP=0
TUNE_WIRED=0
WIRED_MB=""
CHANGED=0

log()  { printf '%s\n' "$*"; }
info() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

mark_changed() { CHANGED=1; }

mask_key() {
  local k="$1"
  local n=${#k}
  if [[ $n -le 8 ]]; then
    printf '********'
  else
    printf '%s…%s' "${k:0:4}" "${k: -4}"
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

ask_yes() {
  local prompt="$1"
  if [[ "${YES}" -eq 1 ]]; then
    return 1  # --yes alone does not opt into tuning
  fi
  local reply
  read -r -p "${prompt} [y/N] " reply || true
  [[ "${reply}" =~ ^[Yy]$ ]]
}

usage() {
  cat <<'EOF'
Usage: setup.sh [--dry-run] [--yes] [--tune-sleep] [--tune-wired] [--wired-mb N]

Bootstrap oMLX and helper tooling on this Mac. Idempotent: a second run with
the same options reports no changes.

  --dry-run       Print actions without changing the system
  --yes           Non-interactive; skip prompts (does not enable opt-in tuning)
  --tune-sleep    Disable system sleep (pmset); reversible
  --tune-wired    Install LaunchDaemon raising iogpu.wired_limit_mb
  --wired-mb N    Wired limit in MB (default: total RAM MB minus 12288)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --yes|-y) YES=1; shift ;;
    --tune-sleep) TUNE_SLEEP=1; shift ;;
    --tune-wired) TUNE_WIRED=1; shift ;;
    --wired-mb) WIRED_MB="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown flag: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
info "Preflight checks"

ARCH="$(uname -m)"
[[ "${ARCH}" == "arm64" ]] || die "Apple Silicon (arm64) required; found ${ARCH}"

OS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[[ "${OS_MAJOR}" -ge 15 ]] || die "macOS 15.0+ required; found $(sw_vers -productVersion)"
note "arch=${ARCH}  macOS=$(sw_vers -productVersion)"

if ! have brew; then
  if [[ "${YES}" -eq 1 ]] || ask_yes "Homebrew not found. Install it now?"; then
    info "Installing Homebrew"
    run /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # shellcheck disable=SC2016
    note 'Ensure brew is on PATH: eval "$(/opt/homebrew/bin/brew shellenv)"'
    have brew || die "brew still not on PATH after install"
    mark_changed
  else
    die "Homebrew is required"
  fi
else
  note "Homebrew: $(brew --prefix)"
fi

# ---------------------------------------------------------------------------
# Python 3.11–3.13
# ---------------------------------------------------------------------------
info "Python 3.11–3.13"
pick_python() {
  local c
  for c in python3.13 python3.12 python3.11; do
    if have "$c"; then
      local ver
      ver="$("$c" -c 'import sys; print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
      case "${ver}" in
        3.11|3.12|3.13) echo "$c"; return 0 ;;
      esac
    fi
  done
  return 1
}

PY_BIN="$(pick_python || true)"
if [[ -z "${PY_BIN}" ]]; then
  info "Installing python@3.12 via Homebrew"
  run brew install python@3.12
  mark_changed
  PY_BIN="$(pick_python || true)"
  if [[ -z "${PY_BIN}" ]]; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      PY_BIN="python3.12"
      note "[dry-run] assuming ${PY_BIN} after brew install"
    else
      die "could not find Python 3.11–3.13 after brew install"
    fi
  fi
fi
if [[ "${DRY_RUN}" -eq 1 ]] && ! have "${PY_BIN}"; then
  note "Python: ${PY_BIN} (not on PATH yet — dry-run)"
else
  note "Python: ${PY_BIN} ($("${PY_BIN}" --version 2>&1))"
fi

# ---------------------------------------------------------------------------
# Helper tooling
# ---------------------------------------------------------------------------
info "Helper tooling (uv, huggingface-cli / hf, PyYAML, ruamel.yaml)"

if ! have uv; then
  run brew install uv
  mark_changed
else
  note "uv present"
fi

# Hugging Face CLI: prefer `hf` (huggingface_hub) via uv tool
if ! have hf && ! have huggingface-cli; then
  info "Installing Hugging Face CLI (uv tool install huggingface_hub[cli])"
  run uv tool install 'huggingface_hub[cli]'
  mark_changed
else
  note "hf/huggingface-cli present"
fi

# Repo-local venv so we don't fight Homebrew's externally-managed Python.
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PY="${VENV_DIR}/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
  info "Creating repo venv at .venv"
  if have uv; then
    run uv venv --python "${PY_BIN}" "${VENV_DIR}"
  else
    run "${PY_BIN}" -m venv "${VENV_DIR}"
  fi
  mark_changed
else
  note "venv present: ${VENV_DIR}"
fi
# Prefer the venv interpreter for remaining Python work when it exists
if [[ -x "${VENV_PY}" ]] || [[ "${DRY_RUN}" -eq 1 ]]; then
  PY_BIN="${VENV_PY}"
fi

py_import_ok() {
  local mod="$1"
  [[ -x "${PY_BIN}" ]] || return 1
  "${PY_BIN}" -c "import ${mod}" 2>/dev/null
}

if py_import_ok yaml && py_import_ok "ruamel.yaml"; then
  note "PyYAML + ruamel.yaml present in venv"
else
  info "Installing PyYAML + ruamel.yaml into .venv"
  if have uv; then
    run uv pip install --python "${PY_BIN}" pyyaml 'ruamel.yaml'
  else
    run "${PY_BIN}" -m pip install pyyaml 'ruamel.yaml'
  fi
  mark_changed
fi

# ---------------------------------------------------------------------------
# oMLX via Homebrew
# ---------------------------------------------------------------------------
info "oMLX (Homebrew)"

if ! brew tap-info jundot/omlx >/dev/null 2>&1; then
  run brew tap jundot/omlx https://github.com/jundot/omlx
  mark_changed
else
  note "tap jundot/omlx present"
fi

has_xcode=0
if [[ -d /Applications/Xcode.app ]] && xcodebuild -version >/dev/null 2>&1; then
  has_xcode=1
fi

if have omlx; then
  note "omlx present: $(omlx --version 2>/dev/null || echo installed)"
else
  if [[ "${has_xcode}" -eq 1 ]]; then
    info "Installing omlx with --with-custom-kernel (Xcode detected)"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      note "[dry-run] brew install jundot/omlx/omlx --with-custom-kernel"
    else
      if ! brew install jundot/omlx/omlx --with-custom-kernel; then
        warn "custom-kernel install failed; falling back to plain brew install"
        brew install jundot/omlx/omlx
      fi
      mark_changed
    fi
  else
    info "Installing omlx (no full Xcode — skipping custom kernels)"
    warn "Qwen3.5 and some models are slower without native kernels. Later: install Xcode + Metal toolchain, then: brew reinstall jundot/omlx/omlx --with-custom-kernel"
    run brew install jundot/omlx/omlx
    mark_changed
  fi
fi

# ---------------------------------------------------------------------------
# .env + API key
# ---------------------------------------------------------------------------
info ".env and OMLX_API_KEY"

if [[ ! -f "${ENV_FILE}" ]]; then
  if [[ -f "${ENV_EXAMPLE}" ]]; then
    info "Creating .env from .env.example"
    run cp "${ENV_EXAMPLE}" "${ENV_FILE}"
    mark_changed
  else
    run touch "${ENV_FILE}"
    mark_changed
  fi
else
  note ".env present"
fi

read_env_key() {
  local key="$1"
  if [[ -f "${ENV_FILE}" ]]; then
    local line
    line="$(grep -E "^${key}=" "${ENV_FILE}" 2>/dev/null | tail -1 || true)"
    if [[ -n "${line}" ]]; then
      printf '%s' "${line#*=}" | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
    fi
  fi
}

set_env_key() {
  local key="$1" val="$2"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    note "[dry-run] set ${key}=*** in .env"
    return
  fi
  if grep -qE "^#?${key}=" "${ENV_FILE}" 2>/dev/null; then
    # Replace existing (commented or not) assignment line
    local tmp
    tmp="$(mktemp)"
    awk -v k="${key}" -v v="${val}" '
      BEGIN{done=0}
      $0 ~ "^#?"k"=" && !done { print k"="v; done=1; next }
      { print }
      END { if (!done) print k"="v }
    ' "${ENV_FILE}" > "${tmp}"
    mv "${tmp}" "${ENV_FILE}"
  else
    printf '\n%s=%s\n' "${key}" "${val}" >> "${ENV_FILE}"
  fi
}

API_KEY="$(read_env_key OMLX_API_KEY)"
if [[ -z "${API_KEY}" && -n "${OMLX_API_KEY:-}" ]]; then
  API_KEY="${OMLX_API_KEY}"
fi
if [[ -z "${API_KEY}" ]]; then
  info "Generating OMLX_API_KEY"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    API_KEY="dryrun_placeholder_key_xxxxxxxx"
    note "[dry-run] would generate key (masked: $(mask_key "${API_KEY}"))"
  else
    API_KEY="$(openssl rand -hex 32)"
    set_env_key OMLX_API_KEY "${API_KEY}"
    mark_changed
    note "OMLX_API_KEY generated: $(mask_key "${API_KEY}")"
  fi
else
  note "OMLX_API_KEY present: $(mask_key "${API_KEY}")"
  # Ensure it is written into .env if only in process env
  if [[ -z "$(read_env_key OMLX_API_KEY)" ]]; then
    set_env_key OMLX_API_KEY "${API_KEY}"
    mark_changed
  fi
fi

# Sensible defaults into .env if missing
[[ -n "$(read_env_key UPSTREAM_URL)" ]] || { set_env_key UPSTREAM_URL "http://localhost:${DEFAULT_PORT}"; mark_changed; }
[[ -n "$(read_env_key OMLX_URL)" ]] || { set_env_key OMLX_URL "http://localhost:${DEFAULT_PORT}"; mark_changed; }
[[ -n "$(read_env_key LITELLM_OWNER)" ]] || { set_env_key LITELLM_OWNER "macos-omlx"; mark_changed; }
# LiteLLM / Hermes dropdown prefix (short hostname + "-"); override in .env anytime.
if [[ -z "$(read_env_key LITELLM_MODEL_PREFIX)" ]]; then
  _prefix_host="$(hostname -s 2>/dev/null || hostname | cut -d. -f1)"
  _prefix_host="$(printf '%s' "${_prefix_host}" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-')"
  _prefix_host="${_prefix_host#-}" ; _prefix_host="${_prefix_host%-}"
  [[ -n "${_prefix_host}" ]] || _prefix_host="omlx"
  set_env_key LITELLM_MODEL_PREFIX "${_prefix_host}-"
  mark_changed
  unset _prefix_host
fi

PORT="$(read_env_key OMLX_PORT)"; PORT="${PORT:-$DEFAULT_PORT}"
HOST_BIND="$(read_env_key OMLX_HOST)"; HOST_BIND="${HOST_BIND:-$DEFAULT_HOST}"
MODEL_DIR="$(read_env_key OMLX_MODEL_DIR)"; MODEL_DIR="${MODEL_DIR:-$DEFAULT_MODEL_DIR}"
MODEL_DIR="${MODEL_DIR/#\~/$HOME}"
SSD_CACHE="$(read_env_key OMLX_SSD_CACHE_DIR)"; SSD_CACHE="${SSD_CACHE:-$DEFAULT_SSD_CACHE}"
SSD_CACHE="${SSD_CACHE/#\~/$HOME}"
MAX_CONC="$(read_env_key OMLX_MAX_CONCURRENT)"; MAX_CONC="${MAX_CONC:-$DEFAULT_MAX_CONCURRENT}"
MEM_GUARD="$(read_env_key OMLX_MEMORY_GUARD)"; MEM_GUARD="${MEM_GUARD:-$DEFAULT_MEMORY_GUARD}"

# ---------------------------------------------------------------------------
# Directories + persistent oMLX settings
# ---------------------------------------------------------------------------
info "oMLX directories and settings"

for d in "${MODEL_DIR}" "${SSD_CACHE}" "${HOME}/.omlx/logs"; do
  if [[ ! -d "${d}" ]]; then
    run mkdir -p "${d}"
    mark_changed
  else
    note "dir exists: ${d}"
  fi
done

# oMLX 0.8+ uses a nested settings.json (server/auth/model/scheduler/memory/cache).
# Merge our desired values into any existing file so we don't wipe unrelated keys.
SETTINGS_CHANGED=0
write_settings() {
  local py="${PY_BIN}"
  if [[ ! -x "${py}" ]]; then
    py="$(command -v python3.12 || command -v python3 || true)"
  fi
  if [[ -z "${py}" ]]; then
    die "no Python available to write nested settings.json"
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    note "[dry-run] would merge host=${HOST_BIND} port=${PORT} auth+dirs into settings.json"
    return 0
  fi
  mkdir -p "$(dirname "${SETTINGS_JSON}")"
  RESULT="$("${py}" - <<PY
import json, os, pathlib
path = pathlib.Path(os.environ["SETTINGS_JSON"])
desired = {
    "version": "1.0",
    "server": {"host": os.environ["HOST_BIND"], "port": int(os.environ["PORT"])},
    "auth": {
        "api_key": os.environ["API_KEY"],
        "skip_api_key_verification": False,
        "allow_unauthenticated_inference": False,
    },
    "model": {"model_dirs": [os.environ["MODEL_DIR"]], "model_dir": os.environ["MODEL_DIR"]},
    "scheduler": {"max_concurrent_requests": int(os.environ["MAX_CONC"])},
    "memory": {"prefill_memory_guard": True, "memory_guard_tier": os.environ["MEM_GUARD"]},
    "cache": {
        "enabled": True,
        "ssd_cache_dir": os.environ["SSD_CACHE"],
        "ssd_cache_max_size": os.environ.get("SSD_CACHE_MAX", "100GB"),
    },
}
data = {}
if path.is_file():
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
# Drop legacy flat keys if present from an earlier mistaken write
for k in ("host", "port", "model_dir", "api_key", "max_concurrent_requests",
          "memory_guard", "paged_ssd_cache_dir", "paged_ssd_cache_max_size"):
    data.pop(k, None)
changed = False
data.setdefault("version", "1.0")
for section, values in desired.items():
    if section == "version":
        if data.get("version") != values:
            data["version"] = values
            changed = True
        continue
    cur = data.get(section)
    if not isinstance(cur, dict):
        data[section] = dict(values)
        changed = True
        continue
    for k, v in values.items():
        if cur.get(k) != v:
            cur[k] = v
            changed = True
if changed or not path.is_file():
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    print("changed")
else:
    print("unchanged")
PY
)" || die "failed to write settings.json"
  if [[ "${RESULT}" == "changed" ]]; then
    info "Updated ${SETTINGS_JSON} (nested oMLX schema)"
    SETTINGS_CHANGED=1
    mark_changed
  else
    note "settings.json already up to date"
  fi
}

export SETTINGS_JSON HOST_BIND PORT API_KEY MODEL_DIR MAX_CONC MEM_GUARD SSD_CACHE
SSD_CACHE_MAX="$(read_env_key OMLX_SSD_CACHE_MAX_SIZE)"; export SSD_CACHE_MAX="${SSD_CACHE_MAX:-100GB}"
write_settings

# ---------------------------------------------------------------------------
# Start service + readiness
# ---------------------------------------------------------------------------
info "oMLX service"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  note "[dry-run] omlx start / brew services start omlx"
else
  if [[ "${SETTINGS_CHANGED}" -eq 1 ]]; then
    info "Restarting omlx to apply settings.json"
    if have omlx; then
      omlx restart 2>/dev/null || brew services restart omlx || true
    else
      brew services restart omlx || true
    fi
  elif have omlx; then
    if ! omlx start 2>/dev/null; then
      brew services start omlx || true
    fi
  else
    brew services start omlx || true
  fi
fi

wait_ready() {
  local url="http://127.0.0.1:${PORT}/v1/models"
  local i
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    note "[dry-run] would wait for GET ${url}"
    return 0
  fi
  info "Waiting for ${url}"
  for i in $(seq 1 60); do
    if curl -sf -H "Authorization: Bearer ${API_KEY}" "${url}" >/dev/null 2>&1 \
       || curl -sf "${url}" >/dev/null 2>&1; then
      note "ready after ~${i}s"
      return 0
    fi
    sleep 1
  done
  warn "oMLX did not answer ${url} within 60s — check $(brew --prefix)/var/log/omlx.log"
  return 1
}
wait_ready || true

# ---------------------------------------------------------------------------
# Opt-in host tuning
# ---------------------------------------------------------------------------
info "Opt-in host tuning"

if [[ "${TUNE_SLEEP}" -eq 1 ]] || { [[ "${YES}" -eq 0 ]] && ask_yes "Disable system sleep (pmset -a sleep 0 / disablesleep 1)?"; }; then
  info "Disabling system sleep (undo: sudo pmset -a sleep 10 disablesleep 0)"
  run sudo pmset -a sleep 0 disablesleep 1
  mark_changed
else
  note "leaving sleep settings unchanged"
fi

calc_wired_mb() {
  local ram_bytes ram_mb
  ram_bytes="$(sysctl -n hw.memsize)"
  ram_mb=$(( ram_bytes / 1024 / 1024 ))
  # Keep at least 12 GB for macOS
  echo $(( ram_mb - 12288 ))
}

if [[ "${TUNE_WIRED}" -eq 1 ]] || { [[ "${YES}" -eq 0 ]] && ask_yes "Raise GPU wired-memory limit via LaunchDaemon (persists across reboot)?"; }; then
  local_wired="${WIRED_MB:-$(calc_wired_mb)}"
  [[ "${local_wired}" -gt 0 ]] || die "computed wired limit is non-positive"
  info "Setting iogpu.wired_limit_mb=${local_wired} (undo: see docs/host-setup.md)"
  if [[ ! -f "${DAEMON_SRC}" ]]; then
    die "missing ${DAEMON_SRC}"
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    note "[dry-run] install LaunchDaemon with wired_mb=${local_wired}"
  else
    tmp_plist="$(mktemp)"
    sed "s/__WIRED_LIMIT_MB__/${local_wired}/g" "${DAEMON_SRC}" > "${tmp_plist}"
    sudo cp "${tmp_plist}" "${DAEMON_DST}"
    rm -f "${tmp_plist}"
    sudo chmod 644 "${DAEMON_DST}"
    sudo chown root:wheel "${DAEMON_DST}"
    sudo launchctl bootout system/com.macos-omlx.wired-limit 2>/dev/null || true
    sudo launchctl bootstrap system "${DAEMON_DST}"
    sudo launchctl enable system/com.macos-omlx.wired-limit
    sudo launchctl kickstart -k system/com.macos-omlx.wired-limit
    note "current: $(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo unknown) MB"
    mark_changed
  fi
else
  note "leaving iogpu.wired_limit_mb unchanged (0 = macOS default)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
info "Summary"

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
LOCAL_NAME="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
TS_NAME=""
if have tailscale; then
  TS_NAME="$(tailscale status --json 2>/dev/null | "${PY_BIN}" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
fi

note "oMLX port     : ${PORT}"
note "API key       : $(mask_key "${API_KEY}")"
note "model dir     : ${MODEL_DIR}"
note "settings      : ${SETTINGS_JSON}"
[[ -n "${LAN_IP}" ]] && note "LAN URL       : http://${LAN_IP}:${PORT}/v1"
note ".local URL    : http://${LOCAL_NAME}.local:${PORT}/v1"
[[ -n "${TS_NAME}" ]] && note "Tailscale URL : http://${TS_NAME}:${PORT}/v1"
note "loopback URL  : http://127.0.0.1:${PORT}/v1"

echo
if [[ "${CHANGED}" -eq 0 ]]; then
  log "No changes — already configured."
else
  log "Setup applied changes."
fi

cat <<EOF

Next steps:
  1. Set MODEL_API_BASE in .env to a URL LiteLLM can reach (LAN / .local / Tailscale), not localhost.
  2. make models-apply          # download + pin models from models.yaml
  3. make sync-litellm DRY_RUN=1 && make sync-litellm
  4. make app-configs           # render Hermes / LiteLLM templates from .env (gitignored)
  5. make test-models && make bench

Until battlemage-llama gets owner-scoped sync, run battlemage with NO_DELETE=1
so it does not delete this host's LiteLLM entries.
EOF
