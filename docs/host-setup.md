# Host setup (Mac Studio / Apple Silicon)

Manual equivalent of `scripts/setup.sh` / `make setup`. Prefer the script; use this
page to understand each step and how to undo it.

## Prerequisites

- Apple Silicon (`uname -m` → `arm64`)
- macOS **15.0+** (oMLX requirement)
- [Homebrew](https://brew.sh)
- Python **3.11–3.13** (system 3.9 is not enough — `brew install python@3.12`)
- Full **Xcode** + Metal toolchain only if you want `brew install … --with-custom-kernel`
  (recommended for Qwen3.5). Without Xcode, plain brew install works but some models
  fall back to slower generic kernels.

## Install oMLX (Homebrew)

```bash
brew tap jundot/omlx https://github.com/jundot/omlx
# With full Xcode:
brew install jundot/omlx/omlx --with-custom-kernel
# Without Xcode:
brew install jundot/omlx/omlx
```

Persistent config lives in `~/.omlx/settings.json` (nested schema as of oMLX 0.8;
or env vars `OMLX_MODEL_DIR`, `OMLX_PORT`, `OMLX_API_KEY`). **Homebrew's service
runs bare `omlx serve`**, so one-shot CLI flags do **not** stick — write
settings.json (or set env for the service) and `omlx restart`.

`make setup` merges these sections into settings.json:

```json
{
  "version": "1.0",
  "server": { "host": "0.0.0.0", "port": 8000 },
  "auth": { "api_key": "…", "skip_api_key_verification": false,
            "allow_unauthenticated_inference": false },
  "model": { "model_dirs": ["~/.omlx/models"] },
  "scheduler": { "max_concurrent_requests": 8 },
  "memory": { "memory_guard_tier": "safe" },
  "cache": { "ssd_cache_dir": "~/.omlx/ssd-cache", "ssd_cache_max_size": "100GB" }
}
```

| Setting | Value |
|---------|--------|
| `server.host` | `0.0.0.0` (LiteLLM on another machine must reach it) |
| `server.port` | `8000` |
| `model.model_dirs` | `~/.omlx/models` |
| `auth.api_key` | strong secret (required for non-loopback) |
| `scheduler.max_concurrent_requests` | `8` (raise after `make bench` concurrency sweep) |
| `memory.memory_guard_tier` | `safe` / `balanced` / `aggressive` |
| `cache.ssd_cache_*` | under `~/.omlx/ssd-cache` |

Start / stop:

```bash
omlx start                 # or: brew services start omlx
omlx stop
omlx restart
brew services info omlx
```

Logs: `$(brew --prefix)/var/log/omlx.log`, `~/.omlx/logs/server.log`.

Verify:

```bash
curl -s -H "Authorization: Bearer $OMLX_API_KEY" http://127.0.0.1:8000/v1/models | jq
```

From another machine, use the Mac's LAN IP, `.local` name, or Tailscale name —
**not** `localhost`.

## Network

- Prefer **wired 10GbE** over Wi-Fi for LiteLLM ↔ oMLX traffic.
- First inbound connection to port 8000 triggers the **macOS firewall** prompt —
  allow it for oMLX / Python.
- Optional [Tailscale](https://tailscale.com): use the MagicDNS name in
  `MODEL_API_BASE` so LiteLLM can reach the Mac off-LAN.

## Auto-login / headless

oMLX via `brew services` starts at login/boot for the installing user. For a true
headless Mac Studio:

1. System Settings → Users & Groups → automatic login (or a LaunchDaemon user
   session strategy you already trust).
2. Confirm `brew services list` shows `omlx` as started after reboot.
3. Keep the machine awake (see sleep tuning below) if agents hit it overnight.

## GPU wired-memory limit

macOS caps Metal's GPU working set. `0` means "use the default fraction of RAM":

```bash
sysctl iogpu.wired_limit_mb
```

To raise it (keeps ≥8–12 GB for macOS), `make setup` can install
`scripts/launchdaemons/com.macos-omlx.wired-limit.plist` as
`/Library/LaunchDaemons/com.macos-omlx.wired-limit.plist`.

**Undo:**

```bash
sudo launchctl bootout system/com.macos-omlx.wired-limit
sudo rm -f /Library/LaunchDaemons/com.macos-omlx.wired-limit.plist
sudo sysctl iogpu.wired_limit_mb=0
```

## Disable system sleep (opt-in)

```bash
sudo pmset -a sleep 0 disablesleep 1
```

**Undo:**

```bash
sudo pmset -a sleep 10 disablesleep 0
# or restore your preferred values from `pmset -g`
```

## Helper tooling

```bash
brew install uv python@3.12
uv tool install 'huggingface_hub[cli]'   # provides `hf`
uv pip install --python python3.12 pyyaml 'ruamel.yaml'
```

## Re-run safety

`./scripts/setup.sh` is idempotent. A second run with the same options should
report **No changes — already configured.**
