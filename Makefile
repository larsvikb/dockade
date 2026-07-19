# dockade — dev/ops entrypoints. Run `make` (or `make help`) to list targets.
#
# Two jobs:
#   - `make check`  static checks: linters (when installed) + repo consistency
#                   guards that always run. CI-friendly: non-zero on any failure.
#   - compose/*     thin wrappers over the shared egress-proxy infrastructure in
#                   docker-compose.yml. Sandboxes themselves are NOT compose
#                   services (they are ephemeral + plural); launch them with
#                   `make sandbox`, which calls run-claude-sandbox.sh.
#
# The linters (shellcheck, hadolint, ruff) run from the HOST/CI, not the sandbox
# image; missing ones are skipped with a note so `make check` still runs the
# intrinsic consistency guards locally.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

COMPOSE := docker compose
SANDBOX ?= claude-sandbox
WORKSPACE ?= $(PWD)

# Shell scripts (bash -n + shellcheck) and Dockerfiles (hadolint).
SCRIPTS := run-claude-sandbox.sh \
           claude-sandbox/init-firewall.sh \
           claude-sandbox/entrypoint.sh \
           claude-sandbox/boundary-check.sh \
           claude-sandbox/statusline.sh
DOCKERFILES := claude-sandbox/Dockerfile proxies/egress/Dockerfile
YAMLFILES := docker-compose.yml .hadolint.yaml .yamllint
JSONFILES := $(shell git ls-files '*.json' 2>/dev/null)

# Files referenced by Dockerfile COPY / entrypoint — existence is asserted so a
# rename can't silently break the build.
REFFILES := $(SCRIPTS) \
            claude-sandbox/user-settings.json \
            claude-sandbox/dotfiles/.bashrc \
            claude-sandbox/dotfiles/.vimrc \
            claude-sandbox/dotfiles/.inputrc \
            claude-sandbox/dotfiles/.gitconfig \
            proxies/egress/addon.py \
            proxies/egress/allowlist.txt

.PHONY: help check lint consistency \
        up down down-v build rebuild ps logs audit \
        sandbox sandbox-rebuild boundary

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ── static checks ───────────────────────────────────────────────────────────

check: lint consistency ## Run all static checks (linters + consistency guards)
	@echo "== all checks passed =="

lint: ## Run linters (shellcheck, hadolint, ruff, yamllint) — skipped if not installed
	@fail=0
	if command -v shellcheck >/dev/null 2>&1; then
	  echo "== shellcheck =="; shellcheck $(SCRIPTS) || fail=1
	else echo "SKIP shellcheck (not installed)"; fi
	if command -v hadolint >/dev/null 2>&1; then
	  echo "== hadolint =="; hadolint $(DOCKERFILES) || fail=1
	else echo "SKIP hadolint (not installed)"; fi
	if command -v ruff >/dev/null 2>&1; then
	  echo "== ruff =="; ruff check proxies/egress/addon.py || fail=1
	else echo "SKIP ruff (not installed)"; fi
	if command -v yamllint >/dev/null 2>&1; then
	  echo "== yamllint =="; yamllint $(YAMLFILES) || fail=1
	else echo "SKIP yamllint (not installed)"; fi
	exit $$fail

consistency: ## Repo consistency guards (syntax, allowlist drift, file refs)
	@echo "== bash -n (shell syntax) =="
	for f in $(SCRIPTS); do bash -n "$$f"; echo "  ok $$f"; done
	echo "== python compile (addon) =="
	python3 -m py_compile proxies/egress/addon.py && echo "  ok addon.py"
	echo "== json validity =="
	for f in $(JSONFILES); do
	  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$$f" && echo "  ok $$f" \
	    || { echo "  BAD $$f"; exit 1; }
	done
	echo "== domain allowlist drift (firewall ⊆ proxy allowlist) =="
	fw=$$(awk '/ALLOWED_DOMAINS=\(/{f=1;next} f&&/^[[:space:]]*\)/{f=0} f' \
	         claude-sandbox/init-firewall.sh | grep -oE '"[a-z0-9.]+"' | tr -d '"' | sort -u)
	al=$$(grep -vE '^[[:space:]]*#|^[[:space:]]*$$' proxies/egress/allowlist.txt \
	         | sed 's/^\.//' | sort -u)
	missing=$$(comm -23 <(printf '%s\n' "$$fw") <(printf '%s\n' "$$al"))
	if [ -n "$$missing" ]; then
	  echo "  FAIL: firewall allows hosts the proxy allowlist does not:"
	  printf '%s\n' "$$missing" | sed 's/^/    /'; exit 1
	fi
	echo "  ok — every firewall host is covered by the proxy allowlist"
	echo "== referenced files exist (Dockerfile COPY / entrypoint) =="
	for f in $(REFFILES); do
	  if [ -f "$$f" ]; then echo "  ok $$f"; else echo "  MISSING $$f"; exit 1; fi
	done

# ── shared infrastructure (docker-compose.yml) ──────────────────────────────

up: ## Bring up the shared infra (egress proxy), building if needed
	$(COMPOSE) up -d --build

down: ## Stop the shared infra (keeps the egress-audit volume)
	$(COMPOSE) down

down-v: ## Stop infra AND delete the egress-audit volume (destructive)
	$(COMPOSE) down -v

build: ## Build the egress-proxy image
	$(COMPOSE) build

rebuild: ## Rebuild the egress-proxy image from scratch (no cache)
	$(COMPOSE) build --no-cache

ps: ## Show infra container status
	$(COMPOSE) ps

logs: ## Follow the egress-proxy log — the live per-connection audit stream
	$(COMPOSE) logs -f egress-proxy

audit: logs ## Alias for `logs` (the egress audit trail)

# ── sandbox lifecycle (run-claude-sandbox.sh) ───────────────────────────────

sandbox: ## Launch a sandbox against the infra (WORKSPACE=/path, default $$PWD)
	./run-claude-sandbox.sh "$(WORKSPACE)"

sandbox-rebuild: ## Rebuild the sandbox image, then launch
	./run-claude-sandbox.sh "$(WORKSPACE)" --rebuild

boundary: ## Run boundary-check.sh inside a running sandbox (SANDBOX=name)
	docker exec -it "$(SANDBOX)" boundary-check.sh
