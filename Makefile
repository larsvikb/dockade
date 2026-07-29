# dockade — dev/ops entrypoints. Run `make` (or `make help`) to list targets.
#
# Two jobs:
#   - `make check`  static checks: linters (when installed) + repo consistency
#                   guards that always run + a build-verification that asserts
#                   every image still builds (skipped when docker is unavailable).
#                   CI-friendly: non-zero on any failure.
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
DOCKERFILES := claude-sandbox/Dockerfile proxies/egress/Dockerfile \
               control-plane/Dockerfile control-plane-ui/Dockerfile
YAMLFILES := docker-compose.yml .hadolint.yaml .yamllint
JSONFILES := $(shell git ls-files '*.json' 2>/dev/null)
PYFILES := proxies/egress/addon.py control-plane/app.py control-plane-ui/app.py
# Dependency-free unit tests for the governance-critical decision logic. Kept
# separate from PYFILES so they can be linted with the app code but discovered
# and run on their own (python -m unittest, no pip installs — see tests/).
TESTFILES := $(shell git ls-files 'tests/*.py' 2>/dev/null)

# Files referenced by Dockerfile COPY / entrypoint — existence is asserted so a
# rename can't silently break the build.
REFFILES := $(SCRIPTS) \
            claude-sandbox/user-settings.json \
            claude-sandbox/dotfiles/.bashrc \
            claude-sandbox/dotfiles/.vimrc \
            claude-sandbox/dotfiles/.inputrc \
            claude-sandbox/dotfiles/.gitconfig \
            proxies/egress/addon.py \
            control-plane/app.py \
            control-plane/requirements.txt \
            control-plane-ui/app.py \
            control-plane-ui/requirements.txt \
            control-plane-ui/index.html \
            policies/egress-allowlist.txt

.PHONY: help check lint consistency test verify-build \
        up down destroy rebuild logs-ep logs-cp \
        sandbox boundary

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ── static checks ───────────────────────────────────────────────────────────

check: lint consistency test verify-build ## Run all static checks (linters + consistency guards + unit tests + build)
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
	  echo "== ruff =="; ruff check $(PYFILES) $(TESTFILES) || fail=1
	else echo "SKIP ruff (not installed)"; fi
	if command -v yamllint >/dev/null 2>&1; then
	  echo "== yamllint =="; yamllint $(YAMLFILES) || fail=1
	else echo "SKIP yamllint (not installed)"; fi
	exit $$fail

consistency: ## Repo consistency guards (syntax, allowlist drift, file refs)
	@echo "== bash -n (shell syntax) =="
	for f in $(SCRIPTS); do bash -n "$$f"; echo "  ok $$f"; done
	echo "== python compile =="
	for f in $(PYFILES) $(TESTFILES); do python3 -m py_compile "$$f" && echo "  ok $$f"; done
	echo "== json validity =="
	for f in $(JSONFILES); do
	  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$$f" && echo "  ok $$f" \
	    || { echo "  BAD $$f"; exit 1; }
	done
	echo "== domain allowlist drift (firewall ⊆ proxy allowlist) =="
	fw=$$(awk '/ALLOWED_DOMAINS=\(/{f=1;next} f&&/^[[:space:]]*\)/{f=0} f' \
	         claude-sandbox/init-firewall.sh | grep -oE '"[a-z0-9.]+"' | tr -d '"' | sort -u)
	al=$$(grep -vE '^[[:space:]]*#|^[[:space:]]*$$' policies/egress-allowlist.txt \
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
	echo "== control-net isolation (sandbox must have no path to the control plane) =="
	if grep -qE 'control-(ui-)?net' run-claude-sandbox.sh; then
	  echo "  FAIL: run-claude-sandbox.sh references a control-plane network — the"
	  echo "        sandbox must NEVER attach to control-net or control-ui-net (the"
	  echo "        agent must have no route to the control plane)"
	  exit 1
	fi
	# The two security-load-bearing nets MUST each be internal: sandbox-net (the
	# agent's only net) and control-net (the shared control path). Check each BY
	# NAME — a bare count of 'internal: true' can't tell that the RIGHT nets are the
	# internal ones (a future edit could flip sandbox-net to non-internal while some
	# other net gained 'internal: true', and a count would still pass). awk isolates
	# each top-level network block (2-space key .. next 2-space key) and asserts
	# 'internal: true' appears inside it.
	for net in sandbox-net control-net; do
	  if ! awk -v net="$$net" '
	        $$0 ~ "^  " net ":" {inb=1; next}
	        inb && /^  [A-Za-z]/ {inb=0}
	        inb && /^[[:space:]]*internal:[[:space:]]*true[[:space:]]*$$/ {ok=1}
	        END {exit(ok?0:1)}' docker-compose.yml; then
	    echo "  FAIL: network '$$net' is not declared 'internal: true' in docker-compose.yml"
	    echo "        — the agent would gain a route off its isolation net / to the control plane"
	    exit 1
	  fi
	done
	echo "  ok — launcher never attaches the sandbox to control-net; sandbox-net and control-net both internal"

test: ## Run the governance unit tests (dependency-free; python -m unittest)
	@echo "== unit tests (python -m unittest) =="
	# -W ignore::ResourceWarning: the app opens a short-lived sqlite connection
	# per call (`with _connect() as conn:`) and relies on prompt finalization to
	# close it — fine in production (ResourceWarning is ignored by default), but
	# unittest un-ignores warnings, so the finalizer's "unclosed database" notice
	# would spam the gate. Not a leak; filtered here, not worked around in the app.
	python3 -W ignore::ResourceWarning -m unittest discover -s tests -t tests -v

verify-build: ## Assert every image still builds (skipped if docker unavailable)
	@if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
	  echo "SKIP build verification (docker unavailable)"; exit 0
	fi
	# Cache-respecting builds: the first run is slow, repeats are near-instant when
	# nothing changed. Covers all four Dockerfiles — the three compose services
	# here, and the sandbox image (not a compose service) via the launcher.
	echo "== docker compose build (egress proxy + control plane + UI) =="
	$(COMPOSE) build
	echo "== sandbox image build (run-claude-sandbox.sh --build-only) =="
	./run-claude-sandbox.sh --build-only

# ── shared infrastructure (docker-compose.yml) ──────────────────────────────

up: ## Bring up the shared infra (egress proxy), building if needed
	$(COMPOSE) up -d --build

down: ## Stop the shared infra (keeps the egress-audit volume)
	$(COMPOSE) down

destroy: ## Stop infra AND delete the egress-audit volume (destructive)
	$(COMPOSE) down -v

rebuild: ## Tear down infra and rebuild every image from scratch — proxy + control plane + UI + sandbox (run `make up` after)
	$(COMPOSE) down
	$(COMPOSE) build --no-cache
	./run-claude-sandbox.sh --build-only --no-cache

logs-ep: ## Follow the egress-proxy log — the live per-connection audit stream
	$(COMPOSE) logs -f egress-proxy

logs-cp: ## Follow the control-plane log (policy seed + decisions)
	$(COMPOSE) logs -f control-plane

# ── sandbox lifecycle (run-claude-sandbox.sh) ───────────────────────────────

sandbox: ## Launch a sandbox against the infra (WORKSPACE=/path, default $$PWD)
	./run-claude-sandbox.sh "$(WORKSPACE)"

boundary: ## Run boundary-check.sh inside a running sandbox (SANDBOX=name)
	docker exec -it "$(SANDBOX)" boundary-check.sh
