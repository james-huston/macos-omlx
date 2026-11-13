# macos-omlx — convenience targets for oMLX on Apple Silicon.
#
# Quick start (see `make help`):
#   make setup
#   make models-apply
#   make sync-litellm
#   make test-models
#   make bench

SHELL   := /usr/bin/env bash
SCRIPTS := ./scripts

# Prefer the repo venv created by `make setup` (Python 3.11–3.13 + PyYAML).
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; \
	elif command -v python3.12 >/dev/null; then echo python3.12; \
	elif command -v python3.13 >/dev/null; then echo python3.13; \
	elif command -v python3.11 >/dev/null; then echo python3.11; \
	else echo python3; fi)

SETUP  := $(SCRIPTS)/setup.sh
APPLY  := $(PYTHON) $(SCRIPTS)/apply-models.py
SYNC   := $(PYTHON) $(SCRIPTS)/sync-litellm.py
RENDER := $(PYTHON) $(SCRIPTS)/render-app-configs.py
TEST   := $(PYTHON) $(SCRIPTS)/test-models.py
STATUS := $(PYTHON) $(SCRIPTS)/status.py
BENCH  := $(PYTHON) $(SCRIPTS)/bench.py
REPORT := $(PYTHON) $(SCRIPTS)/bench-report.py

.DEFAULT_GOAL := help
.PHONY: help setup models-apply sync-litellm app-configs test-models status bench bench-report tool-use

help: ## Show this help
	@echo "macos-omlx — make targets:"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Common flags (Make vars → script flags):"
	@echo "  DRY_RUN=1  NO_DOWNLOAD=1  NO_DELETE=1  RESET=1  ADOPT=1  MODEL_PREFIX=…"
	@echo "  VIA=upstream|litellm  MODELS='name1 name2'  QUICK=1  POWER=1"
	@echo
	@echo "Example:"
	@echo "  make setup"
	@echo "  make models-apply DRY_RUN=1"
	@echo "  make sync-litellm DRY_RUN=1"
	@echo "  make test-models VIA=litellm"

setup: ## Bootstrap oMLX + tooling on this Mac (idempotent; YES=1 non-interactive)
	@$(SETUP) \
		$(if $(DRY_RUN),--dry-run) \
		$(if $(YES),--yes) \
		$(if $(TUNE_SLEEP),--tune-sleep) \
		$(if $(TUNE_WIRED),--tune-wired) \
		$(if $(WIRED_MB),--wired-mb $(WIRED_MB))

models-apply: ## Download enabled models from models.yaml and apply oMLX settings
	@$(APPLY) \
		$(if $(MANIFEST),--manifest $(MANIFEST)) \
		$(if $(DRY_RUN),--dry-run) \
		$(if $(NO_DOWNLOAD),--no-download) \
		$(if $(STRICT),--strict)

sync-litellm: ## Mirror this host's oMLX models into LiteLLM (owner-scoped)
	@$(SYNC) \
		$(if $(UPSTREAM),--upstream $(UPSTREAM)) \
		$(if $(LITELLM),--litellm $(LITELLM)) \
		$(if $(API_BASE),--api-base $(API_BASE)) \
		$(if $(MODEL_API_KEY),--model-api-key $(MODEL_API_KEY)) \
		$(if $(PROVIDER),--provider $(PROVIDER)) \
		$(if $(OWNER),--owner $(OWNER)) \
		$(if $(MODEL_PREFIX),--model-prefix $(MODEL_PREFIX)) \
		$(if $(DRY_RUN),--dry-run) \
		$(if $(NO_DELETE),--no-delete) \
		$(if $(RESET),--reset) \
		$(if $(ADOPT),--adopt) \
		$(if $(ALLOW_SHARED_NAMES),--allow-shared-names) \
		$(if $(SELF_TEST),--self-test)

app-configs: ## Render docs/app-configs/*.yaml.template → generated/ (from .env)
	@$(RENDER) $(if $(DRY_RUN),--dry-run)

test-models: ## Smoke-test served models (VIA=upstream|litellm, MODELS=subset)
	@$(TEST) \
		$(if $(VIA),--via $(VIA)) \
		$(if $(BASE),--base $(BASE)) \
		$(if $(API_KEY),--api-key $(API_KEY)) \
		$(if $(MAX_TOKENS),--max-tokens $(MAX_TOKENS)) \
		$(if $(TIMEOUT),--timeout $(TIMEOUT)) \
		$(if $(PROMPT),--prompt "$(PROMPT)") \
		$(if $(ALL),--all) \
		$(MODELS)

status: ## Show oMLX health, loaded models, memory pressure, wired limit
	@$(STATUS) \
		$(if $(POWER),--power)

bench: ## Benchmark models (MODELS=subset, QUICK=1, LONG=1, POWER=1)
	@$(BENCH) \
		$(if $(MODELS),--models "$(MODELS)") \
		$(if $(QUICK),--quick) \
		$(if $(LONG),--long) \
		$(if $(POWER),--power) \
		$(if $(UPDATE_MANIFEST),--update-manifest)

bench-report: ## Regenerate benchmarks/README.md from jsonl results
	@$(REPORT)

tool-use: ## Run tool-calling probe suite (tests/tool_use/run.sh)
	@./tests/tool_use/run.sh $(MODELS)
