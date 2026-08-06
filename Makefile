# SPDX-License-Identifier: Apache-2.0
# dockade — dev/ops entrypoints. Run `make` (or `make help`) to list targets.
#
# Two jobs:
#   - `make check`  static checks: linters (when installed) + repo consistency
#                   guards that always run + a build-verification that asserts
#                   every image still builds (skipped when docker is unavailable).
#                   CI-friendly: non-zero on any failure.
#   - compose/*     thin wrappers over the shared infrastructure (egress proxy +
#                   control plane + UI, plus the optional llm-* profiles) in
#                   docker-compose.yml. Sandboxes themselves are NOT compose
#                   services (they are ephemeral + plural); launch them with
#                   `make claude` / `make opencode`, one per agent tier.
#
# The linters (shellcheck, hadolint, ruff, yamllint) run wherever `make check`
# runs — host, CI, or inside the sandbox image, which bakes them; missing ones
# are skipped with a note so the intrinsic consistency guards still run.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

COMPOSE := docker compose
SANDBOX ?= claude-sandbox
WORKSPACE ?= $(PWD)

# How far the inference server's context window must exceed the window the CLIENT
# believes it has. Not a safety margin picked by taste — it is sized to a measured
# overshoot; see the context-window headroom check in `consistency`.
CTX_HEADROOM := 1.33

# Strict mode. Every stage of `check` degrades to a SKIP when its tool is absent —
# right on a dev machine, where running the checks you CAN run beats running none.
# In CI it is a trap: a runner image without hadolint would print `SKIP hadolint`
# and pass GREEN, verifying less than the badge claims, and nothing would ever say
# so. Set DOCKADE_REQUIRE_TOOLS=1 (as `check-strict` and the CI workflow do) to turn
# every such skip into a failure. Same fail-closed reasoning as the LAUNCHERS glob
# guard below: silently checking nothing is the outcome worth refusing.
#
# Read from the environment too (make imports env vars as variables), so both
# `DOCKADE_REQUIRE_TOOLS=1 make check` and `make check DOCKADE_REQUIRE_TOOLS=1` work.
REQUIRE_TOOLS ?= $(DOCKADE_REQUIRE_TOOLS)

# Shell scripts (bash -n + shellcheck) and Dockerfiles (hadolint).
# Every sandbox launcher. A GLOB, not a list, so a new tier's launcher is covered
# by the lint/syntax/control-net guards automatically — forgetting to register one
# would leave it unchecked, and the control-net guard is security-load-bearing.
LAUNCHERS := $(wildcard run-*-sandbox.sh)

SCRIPTS := $(LAUNCHERS) \
           sandbox-lib.sh \
           sandbox-common/init-firewall.sh \
           sandbox-common/entrypoint.sh \
           sandbox-common/boundary-check.sh \
           claude-sandbox/tier-setup.sh \
           opencode-sandbox/tier-setup.sh \
           claude-sandbox/statusline.sh
DOCKERFILES := claude-sandbox/Dockerfile opencode-sandbox/Dockerfile \
               proxies/egress/Dockerfile \
               control-plane/Dockerfile control-plane-ui/Dockerfile
# A GLOB for the workflows, not a list, so a second workflow is linted without
# anyone remembering to register it — same reasoning as LAUNCHERS above.
YAMLFILES := docker-compose.yml .hadolint.yaml .yamllint \
             $(wildcard .github/workflows/*.yml)
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
            opencode-sandbox/opencode.json \
            sandbox-common/dotfiles/.bashrc \
            sandbox-common/dotfiles/.vimrc \
            sandbox-common/dotfiles/.inputrc \
            sandbox-common/dotfiles/.gitconfig \
            claude-sandbox/dotfiles/.bashrc.tier \
            opencode-sandbox/dotfiles/.bashrc.tier \
            proxies/egress/addon.py \
            control-plane/app.py \
            control-plane/requirements.txt \
            control-plane-ui/app.py \
            control-plane-ui/requirements.txt \
            control-plane-ui/index.html \
            control-plane-ui/app.js \
            policies/egress-allowlist.txt

.PHONY: help check check-strict lint consistency test verify-build \
        up down destroy rebuild logs-ep logs-cp \
        claude opencode boundary

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ── static checks ───────────────────────────────────────────────────────────

check: lint consistency test verify-build ## Run all static checks (linters + consistency guards + unit tests + build)
	@echo "== all checks passed =="

check-strict: ## Like check, but a MISSING TOOL is a failure, not a skip — what CI runs
	@$(MAKE) --no-print-directory check DOCKADE_REQUIRE_TOOLS=1

lint: ## Run linters (shellcheck, hadolint, ruff, yamllint) — skipped if not installed
	@fail=0
	# A missing tool SKIPS by default and FAILS under DOCKADE_REQUIRE_TOOLS (see
	# REQUIRE_TOOLS above for why the two behaviours differ).
	miss() {
	  if [ -n "$(REQUIRE_TOOLS)" ]; then
	    echo "  FAIL: $$1 is not installed and DOCKADE_REQUIRE_TOOLS is set —"
	    echo "        refusing to report success for a check that never ran."
	    return 1
	  fi
	  echo "SKIP $$1 (not installed)"
	}
	if command -v shellcheck >/dev/null 2>&1; then
	  echo "== shellcheck =="; shellcheck $(SCRIPTS) || fail=1
	else miss shellcheck || fail=1; fi
	if command -v hadolint >/dev/null 2>&1; then
	  echo "== hadolint =="; hadolint $(DOCKERFILES) || fail=1
	else miss hadolint || fail=1; fi
	if command -v ruff >/dev/null 2>&1; then
	  echo "== ruff =="; ruff check $(PYFILES) $(TESTFILES) || fail=1
	else miss ruff || fail=1; fi
	if command -v yamllint >/dev/null 2>&1; then
	  echo "== yamllint =="; yamllint $(YAMLFILES) || fail=1
	else miss yamllint || fail=1; fi
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
	echo "== domain allowlist drift (firewall ⊆ control-plane policy seed) =="
	fw=$$(awk '/ALLOWED_DOMAINS=\(/{f=1;next} f&&/^[[:space:]]*\)/{f=0} f' \
	         sandbox-common/init-firewall.sh | grep -oE '"[a-z0-9.]+"' | tr -d '"' | sort -u)
	al=$$(grep -vE '^[[:space:]]*#|^[[:space:]]*$$' policies/egress-allowlist.txt \
	         | sed 's/^\.//' | sort -u)
	missing=$$(comm -23 <(printf '%s\n' "$$fw") <(printf '%s\n' "$$al"))
	if [ -n "$$missing" ]; then
	  echo "  FAIL: firewall allows hosts the control-plane policy seed does not:"
	  printf '%s\n' "$$missing" | sed 's/^/    /'; exit 1
	fi
	echo "  ok — every firewall host is covered by the control-plane policy seed"
	echo "== context-window headroom (server -c >= opencode limit.context x $(CTX_HEADROOM)) =="
	# Two numbers in two files, and the relationship between them is NOT equality.
	#
	# It used to be. That check passed while three agent runs died anyway: the
	# server refused requests of 40840, 37943 and 35980 tokens against a 32768
	# window, with both files declaring 32768 and agreeing perfectly. So a client
	# told the true window still overshoots it — opencode's own accounting is
	# approximate (it does not tokenize with the server's tokenizer, and tool
	# output lands in the conversation after it has budgeted for the turn). Making
	# the numbers equal leaves the client no room to be wrong in the one direction
	# it is actually wrong in.
	#
	# So the invariant is HEADROOM, not agreement: the server's window must exceed
	# what the client believes by enough to absorb the client's undercount. The
	# worst overshoot observed was 1.25x, hence the 1.33x floor. Costs nothing in
	# memory — the server's KV allocation is set by -c, which does not move; the
	# client simply compacts earlier and the overflow never reaches the server.
	#
	# Still a real check in the other direction: too MUCH headroom means the tier
	# pays KV for a window the client will never fill, so this fails on a client
	# limit under half the server's. Both failure modes stay invisible until an
	# agent run dies mid-task, which is why they are worth a build-time guard.
	srv=$$(grep -oE '\-c \$$\{DOCKADE_LLM_CTX:-[0-9]+\}' docker-compose.yml \
	         | grep -oE '[0-9]+' | sort -u)
	oc=$$(python3 -c "import json; print(json.load(open('opencode-sandbox/opencode.json'))['provider']['local']['models']['local']['limit']['context'])")
	if [ "$$(printf '%s' "$$srv" | wc -l)" != "0" ]; then
	  echo "  FAIL: accelerator variants disagree on the default context size:"
	  printf '%s\n' "$$srv" | sed 's/^/    /'; exit 1
	fi
	python3 -c "import sys; srv, oc, h = $$srv, $$oc, $(CTX_HEADROOM); \
	  sys.exit(0) if srv >= oc * h else (print(f'  FAIL: server -c is {srv} but opencode.json limit.context is {oc} —'), \
	  print(f'        only {srv/oc:.2f}x headroom, and {h}x is required. A client told the'), \
	  print('        exact window still overshoots it; see the comment above. Lower'), \
	  print('        limit.context, or raise -c and pay the KV for it.'), sys.exit(1))"
	python3 -c "import sys; srv, oc = $$srv, $$oc; \
	  sys.exit(0) if oc * 2 >= srv else (print(f'  FAIL: server -c is {srv} but opencode.json limit.context is only {oc} —'), \
	  print('        the tier pays KV cache for a window the client will never fill.'), sys.exit(1))"
	echo "  ok — server $$srv, client $$oc ($$(python3 -c "print(f'{$$srv/$$oc:.2f}')")x headroom)"
	echo "== launchers are executable IN THE GIT INDEX =="
	# `make claude` / `make opencode` / verify-build all invoke these as
	# ./run-*-sandbox.sh, so the exec bit is load-bearing for anyone who CLONES.
	#
	# It must be read from the INDEX, not from disk, and that is the whole point of
	# this guard: this repo is routinely worked on through a bind mount where git
	# sets core.fileMode=false, so the on-disk bit is ignored and a 100644 in the
	# index goes unnoticed indefinitely. That is exactly how run-opencode-sandbox.sh
	# shipped non-executable — 755 on disk, 644 in the index, working perfectly on
	# the machine that wrote it and dying with exit 126 on the first fresh clone
	# (CI). Only the launchers need this: sandbox-lib.sh is sourced, and the scripts
	# copied into images are chmod'ed by their Dockerfile.
	if git rev-parse --git-dir >/dev/null 2>&1; then
	  for launcher in $(LAUNCHERS); do
	    mode=$$(git ls-files -s -- "$$launcher" | awk '{print $$1}')
	    if [ "$$mode" != "100755" ]; then
	      echo "  FAIL: $$launcher is $${mode:-untracked} in the git index, not 100755"
	      echo "        — a fresh clone could not execute it. Fix with:"
	      echo "          git update-index --chmod=+x $$launcher"
	      exit 1
	    fi
	    echo "  ok $$launcher (100755)"
	  done
	else
	  echo "  SKIP (not a git checkout — nothing to read the index from)"
	fi
	echo "== every tracked source file carries an SPDX header =="
	# CONTRIBUTING.md tells contributors to add one, and a documented convention with
	# nothing enforcing it is the kind that holds at 100% until it quietly does not.
	# The reason is concrete: `SPDX-License-Identifier` in the file is what lets a
	# licence scanner answer correctly without parsing LICENSE.
	#
	# SOURCE only, and the boundary is not fussiness — it is what can carry a comment
	# and what a scanner cares about. JSON has no comment syntax at all, so
	# user-settings.json and opencode.json could not comply if asked. Markdown, the
	# linter configs, requirements.txt, the policy allowlist and the baked dotfiles are
	# settings and data rather than works, and none of them carries a header today; a
	# guard demanding one would be inventing a convention rather than holding an
	# existing one. The glob below is exactly the set where it IS held.
	#
	# Read from the INDEX (git ls-files) rather than a find, so a new file is covered
	# the moment it is staged and untracked scratch files never fail the gate. The
	# header must be in the FIRST THREE lines: below that it is prose, not a header,
	# and tools that look for it stop reading.
	if git rev-parse --git-dir >/dev/null 2>&1; then
	  spdx_files=$$(git ls-files '*.py' '*.sh' '*.js' '*.html' \
	                             'Makefile' 'docker-compose.yml' \
	                             '*Dockerfile' '.github/workflows/*.yml')
	  if [ -z "$$spdx_files" ]; then
	    echo "  FAIL: the SPDX glob matched nothing — it would check silently. Renamed?"
	    exit 1
	  fi
	  missing=0
	  for f in $$spdx_files; do
	    if ! head -3 "$$f" | grep -q 'SPDX-License-Identifier'; then
	      echo "  FAIL: $$f has no SPDX-License-Identifier in its first 3 lines"
	      missing=1
	    fi
	  done
	  [ "$$missing" = 0 ] || { echo "        add: SPDX-License-Identifier: Apache-2.0"; exit 1; }
	  echo "  ok — $$(echo "$$spdx_files" | wc -w) source files"
	else
	  echo "  SKIP (not a git checkout — nothing to read the index from)"
	fi
	echo "== referenced files exist (Dockerfile COPY / entrypoint) =="
	for f in $(REFFILES); do
	  if [ -f "$$f" ]; then echo "  ok $$f"; else echo "  MISSING $$f"; exit 1; fi
	done
	echo "== control-net isolation (sandbox must have no path to the control plane) =="
	# Checked for EVERY launcher via the LAUNCHERS glob, not one hardcoded name: a
	# new tier's launcher must not be able to attach to the control plane merely
	# because nobody remembered to add it to this guard. Empty glob = fail closed.
	if [ -z "$(LAUNCHERS)" ]; then
	  echo "  FAIL: no run-*-sandbox.sh launcher found — the control-net guard would"
	  echo "        silently check nothing. Did a launcher get renamed?"
	  exit 1
	fi
	for launcher in $(LAUNCHERS); do
	  if grep -qE 'control-(ui-)?net' "$$launcher"; then
	    echo "  FAIL: $$launcher references a control-plane network — no sandbox tier"
	    echo "        may EVER attach to control-net or control-ui-net (the agent must"
	    echo "        have no route to the control plane)"
	    exit 1
	  fi
	done
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
	#
	# DOCKADE_REQUIRE_TOOLS is passed through EXPLICITLY rather than exported: the
	# app.js tests need `node` and skip without it, which strict mode must turn into
	# a failure (see tests/test_control_plane_ui_js.py). An empty value is falsy on
	# the Python side, so the default stays "skip".
	DOCKADE_REQUIRE_TOOLS=$(REQUIRE_TOOLS) \
	  python3 -W ignore::ResourceWarning -m unittest discover -s tests -t tests -v

verify-build: ## Assert every image still builds (skipped if docker unavailable)
	@if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
	  if [ -n "$(REQUIRE_TOOLS)" ]; then
	    echo "  FAIL: docker is unavailable and DOCKADE_REQUIRE_TOOLS is set —"
	    echo "        the build verification is the ONLY check that a Dockerfile edit"
	    echo "        (or a COPY of a file that does not exist) still builds, so"
	    echo "        skipping it silently is exactly what strict mode exists to stop."
	    exit 1
	  fi
	  echo "SKIP build verification (docker unavailable)"; exit 0
	fi
	# Cache-respecting builds: the first run is slow, repeats are near-instant when
	# nothing changed. Covers every Dockerfile — the three compose services here,
	# and BOTH sandbox tiers (not compose services) via their launchers.
	echo "== docker compose build (egress proxy + control plane + UI) =="
	$(COMPOSE) build
	for launcher in $(LAUNCHERS); do
	  echo "== sandbox image build ($$launcher --build-only) =="
	  "./$$launcher" --build-only
	done

# ── shared infrastructure (docker-compose.yml) ──────────────────────────────

up: ## Bring up the shared infra (egress proxy + control plane + UI), building if needed
	# --wait: return when the services are HEALTHY, not merely created, so this
	# target's success means the infra can actually serve. Only the three infra
	# services are STARTED — the LLM is profile-gated and is brought up by its own
	# `docker compose --profile ... up`, so its multi-minute model load never
	# counts against the timeout here. A timeout is therefore a real failure.
	#
	# Profile-gating keeps the llm-* services from RUNNING, but NOT from being
	# interpolated: compose expands variables across the whole file before it selects
	# services, so a required-variable (`:?`) reference inside a profile-gated
	# service aborts this target too. Verified the hard way. Hence the llm-*
	# services use bogus defaults instead — see docker-compose.yml.
	$(COMPOSE) up -d --build --wait --wait-timeout 120

down: ## Stop the shared infra (keeps the named volumes)
	$(COMPOSE) down

destroy: ## Stop infra AND delete BOTH volumes: egress audit log + control-plane policy/audit store (destructive)
	$(COMPOSE) down -v

rebuild: ## Rebuild every image from scratch — proxy + control plane + UI + both sandbox tiers — then recreate the infra
	# Deliberately does NOT `down` first. A build touches no running container, so
	# taking the governance plane offline for the whole --no-cache build bought
	# nothing and cost real downtime; the only unavoidable interruption is the
	# container recreate at the end, which `up -d` does in seconds. It also avoids
	# a trap: `compose down` acts on the whole project and can stop profile-gated
	# services (the local LLM), which the following `up -d` would NOT restart,
	# because their profile is not active.
	#
	# Sandbox images are rebuilt but not relaunched — they are ephemeral
	# (`docker run --rm`) and per-workspace, so a running session keeps the image
	# it started with and the next `make claude` / `make opencode` picks up the new
	# one. Nothing to recreate.
	$(COMPOSE) build --no-cache
	for launcher in $(LAUNCHERS); do "./$$launcher" --build-only --no-cache; done
	$(COMPOSE) up -d --wait --wait-timeout 120

logs-ep: ## Follow the egress-proxy log — the live per-connection audit stream
	$(COMPOSE) logs -f egress-proxy

logs-cp: ## Follow the control-plane log (policy seed + decisions)
	$(COMPOSE) logs -f control-plane

# ── sandbox lifecycle (run-*-sandbox.sh) ────────────────────────────────────

claude: ## Launch a tier-1 (Claude, governed egress) sandbox (WORKSPACE=/path, default $$PWD)
	./run-claude-sandbox.sh "$(WORKSPACE)"

opencode: ## Launch a tier-2 (opencode + local LLM, no egress) sandbox (WORKSPACE=/path)
	./run-opencode-sandbox.sh "$(WORKSPACE)"

boundary: ## Run boundary-check.sh in a running sandbox (SANDBOX=claude-sandbox|opencode-sandbox)
	docker exec -it --user sandbox "$(SANDBOX)" boundary-check.sh
