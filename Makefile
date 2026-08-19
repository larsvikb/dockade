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

# Both files, always. docker-compose.yml owns the topology; mcp-servers.yml owns
# the MCP server catalogue (see its header). Merged with -f rather than run as a
# second project so `depends_on: egress-proxy / condition: service_healthy` still
# works and `mcp-net` needs no `external: true`. Every target inherits this, so a
# bare `docker compose ...` typed by hand is the only way to get a partial view.
COMPOSE := docker compose -f docker-compose.yml -f mcp-servers.yml
# Where MCP client credentials live: one JSON file per server, OUTSIDE this repo,
# because a sandbox launched with dockade as its workspace bind-mounts this tree
# read-write (DESIGN.md, "Credentials" — which also fixes the schema and the
# derive-the-path-from-the-server-name rule). Mounted read-only into the gateway once
# it exists; until then, read by hand when probing a server:
#   tok=$$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["token"])' \
#            $(MCP_SECRETS)/mcp-github.json)
# No container is given a credential today: see mcp-servers.yml.
MCP_SECRETS ?= $(HOME)/.config/dockade/secrets
SANDBOX ?= claude-sandbox
WORKSPACE ?= $(PWD)

# The throwaway container `check-boundary` runs the check in. Named apart from
# the default so it is self-evident in `docker ps` while it runs, and so it never
# takes the numbered suffix the launcher would otherwise allocate next to a live
# agent session.
BOUNDARY_SANDBOX ?= boundary-check-sandbox

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
YAMLFILES := docker-compose.yml mcp-servers.yml .hadolint.yaml .yamllint \
             $(wildcard .github/workflows/*.yml)
JSONFILES := $(shell git ls-files '*.json' 2>/dev/null)
PYFILES := proxies/egress/addon.py control-plane-ui/app.py \
           control-plane/app.py control-plane/store.py control-plane/policy.py \
           control-plane/holds.py control-plane/ingest.py
# Dependency-free unit tests for the governance-critical decision logic. Kept
# separate from PYFILES so they can be linted with the app code but discovered
# and run on their own (python -m unittest, no pip installs — see tests/).
TESTFILES := $(shell git ls-files 'tests/*.py' 2>/dev/null)

# Files referenced by Dockerfile COPY / entrypoint — existence is asserted so a
# rename can't silently break the build.
REFFILES := $(SCRIPTS) \
            claude-sandbox/user-settings.json \
            claude-sandbox/CLAUDE.md \
            opencode-sandbox/opencode.json \
            opencode-sandbox/AGENTS.md \
            sandbox-common/dotfiles/.bashrc \
            sandbox-common/dotfiles/.vimrc \
            sandbox-common/dotfiles/.inputrc \
            sandbox-common/dotfiles/.gitconfig \
            claude-sandbox/dotfiles/.bashrc.tier \
            opencode-sandbox/dotfiles/.bashrc.tier \
            proxies/egress/addon.py \
            control-plane/app.py \
            control-plane/store.py \
            control-plane/policy.py \
            control-plane/holds.py \
            control-plane/ingest.py \
            control-plane/requirements.txt \
            control-plane-ui/app.py \
            control-plane-ui/requirements.txt \
            control-plane-ui/index.html \
            control-plane-ui/app.js \
            policies/egress-allowlist.txt

.PHONY: help check check-strict lint consistency test verify-build \
        up down destroy audit-prune rebuild logs-ep logs-cp \
        claude opencode boundary check-boundary split-check

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
	         sandbox-common/init-firewall.sh | grep -oE '"[a-z0-9.-]+"' | tr -d '"' | sort -u)
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
	echo "== proxy env vars are set in BOTH cases (curl reads http_proxy lowercase only) =="
	# Not style. curl honours HTTPS_PROXY and NO_PROXY in either case but reads
	# `http_proxy` in LOWER CASE ONLY — deliberately, because under CGI a
	# client-supplied `Proxy:` header lands in the environment as HTTP_PROXY
	# (httpoxy, CVE-2016-5385). With only the uppercase set, plaintext HTTP from the
	# agent bypassed the governed proxy entirely and died at DNS: no hold, and no
	# audit record, because it never reached the control plane.
	#
	# Checked per LAUNCHER via the glob, and only for launchers that set any proxy
	# env at all — tier 2 deliberately sets none (it has no egress to govern).
	# `[^"]` after the `=`: an empty `-e "http_proxy="` would satisfy a bare-prefix
	# match while meaning NO PROXY, which is the very state being guarded against.
	checked=0
	for launcher in $(LAUNCHERS); do
	  if ! grep -qE '^\s+-e "HTTPS_PROXY=[^"]' "$$launcher"; then
	    echo "  skip $$launcher (sets no proxy env — tier with no governed egress)"
	    continue
	  fi
	  for var in http_proxy https_proxy no_proxy; do
	    upper=$$(echo "$$var" | tr a-z A-Z)
	    if ! grep -qE "^\s+-e \"$$var=[^\"]" "$$launcher"; then
	      echo "  FAIL: $$launcher sets $$upper but not a non-empty $$var."
	      echo "        curl reads http_proxy in lower case ONLY, so plaintext HTTP"
	      echo "        would bypass the governed proxy and be audited nowhere."
	      exit 1
	    fi
	    if ! grep -qE "^\s+-e \"$$upper=[^\"]" "$$launcher"; then
	      echo "  FAIL: $$launcher sets $$var but not a non-empty $$upper — both cases."
	      exit 1
	    fi
	  done
	  checked=$$((checked + 1))
	  echo "  ok $$launcher (both cases of http/https/no_proxy)"
	done
	# Fail closed on a vacuous pass, like the LAUNCHERS and SPDX globs above. At least
	# one tier has governed egress by definition, so "every launcher skipped" means the
	# detection above stopped matching — and this guard would report success having
	# checked nothing at all.
	if [ "$$checked" -eq 0 ]; then
	  echo "  FAIL: no launcher was found to set proxy env, so this guard checked"
	  echo "        nothing. Tier 1 has governed egress — did the -e lines change shape?"
	  exit 1
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
	  if grep -qE 'control-(ui-)?net|authorize-net' "$$launcher"; then
	    echo "  FAIL: $$launcher references a control-plane network — no sandbox tier"
	    echo "        may EVER attach to control-net, control-ui-net or authorize-net"
	    echo "        (the agent must have no route to the control plane, and"
	    echo "        authorize-net reaches it just as directly as the others)"
	    exit 1
	  fi
	done
	# The security-load-bearing nets MUST each be internal: sandbox-net (the
	# agent's only net) and the two control paths — control-net (management) and
	# authorize-net (the proxy's route to /authorize, which reaches the control
	# plane just as directly). Check each BY NAME — a bare count of 'internal:
	# true' can't tell that the RIGHT nets are the internal ones (a future edit
	# could flip sandbox-net to non-internal while some other net gained
	# 'internal: true', and a count would still pass). awk isolates each top-level
	# network block (2-space key .. next 2-space key) and asserts 'internal: true'
	# appears inside it. tests/test_topology.py asserts the same property from the
	# other side, along with who is attached to what; this stays because it is the
	# one that runs in `make consistency` alongside the launcher check above.
	for net in sandbox-net control-net authorize-net; do
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
	echo "  ok — no launcher attaches the sandbox to a control network; sandbox-net, control-net and authorize-net all internal"

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

# The catalogue servers are profile-gated AND live in a second compose file, so
# starting one by hand means both -f flags and the profile name — easy to get
# subtly wrong, and a bare `docker compose --profile mcp-github up -d` fails with
# "no such service" because it never reads mcp-servers.yml. These targets exist so
# the -f pair has exactly one definition (COMPOSE, above) rather than living in
# anyone's shell history.
mcp-up: ## Start one catalogue MCP server: make mcp-up SERVER=github
	@if [ -z "$(SERVER)" ]; then
	  echo "usage: make mcp-up SERVER=github   (profiles: $$(grep -oP '^\s+- \Kmcp-\S+' mcp-servers.yml | tr '\n' ' '))"
	  exit 2
	fi
	# No credential is passed in: measured, github-mcp-server ignores its env token in
	# http mode and takes a per-request bearer, so the container holds nothing and this
	# target has nothing secret to plumb. See mcp-servers.yml. A server that DOES take
	# an env credential reads it from $(MCP_SECRETS) — never from a repo `.env`.
	#
	# Checked rather than fixed: a mode this loose is a decision someone made, and
	# silently chmod-ing another person's files from a build target is worse than
	# saying so.
	@if [ -d "$(MCP_SECRETS)" ]; then
	  for f in "$(MCP_SECRETS)" "$(MCP_SECRETS)"/*.json; do
	    [ -e "$$f" ] || continue
	    if [ -n "$$(find "$$f" -maxdepth 0 -perm /077 2>/dev/null)" ]; then
	      echo "WARNING: $$f is group/world-readable — chmod 600 (700 for the dir)."
	    fi
	  done
	fi
	# --wait returns when the container is running and its dependencies are
	# healthy, so a failure here is real rather than a race. The server holds a
	# credential: if it exits immediately, read its log before re-running.
	$(COMPOSE) --profile mcp-$(SERVER) up -d --wait --wait-timeout 60 mcp-$(SERVER)

mcp-down: ## Stop one catalogue MCP server: make mcp-down SERVER=github
	@if [ -z "$(SERVER)" ]; then echo "usage: make mcp-down SERVER=github"; exit 2; fi
	# stop+rm rather than `down`, which would take the shared infra with it.
	$(COMPOSE) stop mcp-$(SERVER)
	$(COMPOSE) rm -f mcp-$(SERVER)

mcp-ps: ## Who is on mcp-net right now (should be the proxy plus enabled servers)
	docker network inspect mcp-net \
	  -f '{{range .Containers}}{{printf "%-16s %s\n" .Name .IPv4Address}}{{end}}'

destroy: ## Stop infra AND delete BOTH volumes: egress audit log + control-plane policy/audit store (destructive)
	$(COMPOSE) down -v

# Retention window for `audit-prune`, in days. A variable, not a literal in the
# script, so operators tune it without editing the recipe: `make audit-prune
# AUDIT_RETENTION_DAYS=90`. Kept out of prose elsewhere on purpose — this is the
# one place the number lives (numbers in prose rot; CLAUDE.md).
AUDIT_RETENTION_DAYS ?= 30

audit-prune: ## Trim audit rows older than AUDIT_RETENTION_DAYS (default 30) and reclaim disk; leaves policy + approvals intact (operator-run)
	# Retention for the audit table, and ONLY the audit table. This is NOT `make
	# destroy`: that deletes the whole volume — policy rules, approvals and the
	# ingest cursor with it — and is the fresh-start. This trims old DECISIONS to a
	# window and hands the freed disk back, leaving everything standing (rules,
	# pending/resolved approvals, the ingest offset) untouched. Deliberately manual:
	# the audit trail is what this design exists to keep trustworthy, so thinning it
	# is an operator's decision, never a timer's.
	#
	# Runs against the LIVE control plane over docker exec, using its own python3 +
	# stdlib sqlite3 — no tooling added to the choke-point-adjacent image, same
	# reasoning as split-check. VACUUM is why disk is actually returned: a bare
	# DELETE frees pages inside the file without shrinking it.
	docker exec -e AUDIT_RETENTION_DAYS=$(AUDIT_RETENTION_DAYS) control-plane \
	  python3 -c "$$AUDIT_PRUNE_PY"

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
	docker exec -it --user sandbox "$(SANDBOX)" /usr/local/bin/boundary-check.sh

check-boundary: ## Stand the infra up and assert containment from inside a throwaway tier-1 sandbox (what CI runs)
	# The repo's only AUTOMATED evidence that the containment boundary holds.
	# `make check` structurally cannot reach it: shellcheck reads init-firewall.sh
	# as text and tests/test_topology.py reads docker-compose.yml as YAML, so
	# between them they assert what the boundary is DECLARED to be. Nothing there
	# arms a firewall or tries to leave a container. This does, as the agent.
	#
	# Deliberately NOT part of `make check`, which is a static gate that must stay
	# fast and runnable anywhere: this one needs a live docker daemon, builds an
	# image if it is missing, and takes minutes.
	#
	# Deliberately NON-DESTRUCTIVE, because an operator may well run it against
	# live infrastructure: `up` keeps the volumes and leaves any running agent
	# session alone. It never calls `destroy` — the audit store is the crown
	# jewel, and no test target gets to delete it.
	#
	# `boundary` is the sibling for the case where you already HAVE a sandbox
	# running (and is the only way to check tier 2, whose own probes need the local
	# model server up). This one owns the whole lifecycle instead, which is what
	# makes it usable from a runner with no TTY and nothing running.
	#
	# Two lines because the launcher does the work: --boundary-check runs the check
	# as the container's command, so the container's exit status IS the verdict and
	# there is no container left to clean up, inspect or name-manage. See the
	# launch-mode comment in run-claude-sandbox.sh for why that beats exec'ing into
	# a long-running one.
	$(MAKE) --no-print-directory up
	SANDBOX_NAME="$(BOUNDARY_SANDBOX)" ./run-claude-sandbox.sh "$(WORKSPACE)" --boundary-check

split-check: ## Assert the running proxy reaches /authorize and NOT the management API
	# The API-surface split, checked where it actually applies. boundary-check.sh
	# cannot do this: it runs in the SANDBOX, which has no route to either listener
	# and gets a relay-guard 403 long before reachability is in question. The claim
	# here is about the PROXY's own routes, so it has to run in the proxy.
	#
	# Both directions, because either alone is satisfiable by a broken deployment:
	# a proxy that reaches nothing passes the negative check while governance is
	# down, and a proxy that reaches everything passes the positive one.
	#
	# python3 rather than curl — it is mitmproxy's own interpreter, so this adds no
	# tooling to the choke-point image (same reasoning as that container's
	# healthcheck in docker-compose.yml).
	docker exec egress-proxy python3 -c "$$SPLIT_CHECK_PY"

# Kept as a variable so the Python above stays readable and quoting stays sane.
define SPLIT_CHECK_PY
import socket, sys
ok = True

def connect(host, port):
    """Four outcomes, not two, because they prove different things.

    reached   — connected.
    refused   — the packet ARRIVED and something answered with an RST. The subnet
                is routable and the port is shut by luck, which is not a boundary.
    dropped   — no path (timeout / EHOSTUNREACH / ENETUNREACH).
    unresolved— the name did not resolve, so NOTHING WAS TESTED. Distinguished
                because a negative probe that never left the host would otherwise
                report PASS: with the control plane stopped, Docker's embedded DNS
                stops answering for it, and 'the management API is not served
                here' became true for the wrong reason.
    """
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port))
        return "reached", "connected"
    except socket.gaierror as e:
        return "unresolved", f"name does not resolve ({e})"
    except ConnectionRefusedError:
        return "refused", "REFUSED - host is reachable, nothing listening"
    except (socket.timeout, TimeoutError):
        return "dropped", "no answer (packets dropped)"
    except OSError as e:
        return "dropped", f"{type(e).__name__}: {e}"
    finally:
        s.close()

def check(host, port, expect, label):
    """One expected outcome per probe, named. This replaced a pair of booleans
    (`want` plus a `routable_is_failure` override) that between them encoded three
    real answers and one meaningless combination — accidental complexity from
    patching this twice. Naming the outcome is also STRICTER: the authorize-net
    management probe below asserts `refused` specifically, so it now proves the
    packet arrived and found no listener, where before it passed on any failure
    to connect at all."""
    global ok
    state, how = connect(host, port)
    if state == "unresolved":
        # Never the expected outcome, so always a failure. The wording splits
        # because the two cases need different words: a probe that should have
        # CONNECTED is a live outage, and that line must not be buried; one that
        # should have been refused or dropped is simply untested, and would have
        # been satisfied for the wrong reason.
        ok = False
        if expect == "reached":
            print(f"  FAIL {label}\n       {host}:{port} -> {how}"
                  f" - the proxy cannot ask, so egress is failing closed")
        else:
            print(f"  SKIP {label}\n       {host}:{port} -> {how}"
                  f" - nothing was tested, so this proves nothing")
        return
    good = state == expect
    ok = ok and good
    note = ""
    if not good and state == "refused":
        note = " - the packet ARRIVED; this subnet is routable and the port is " \
               "closed by luck, which is not a boundary"
    print(f"  {'PASS' if good else 'FAIL'} {label}\n"
          f"       {host}:{port} -> {how} (expected: {expect}){note}")

# Best-effort, and it must stay that way. This line once raised gaierror and took
# the whole check down with a traceback, at the exact moment the check had
# something useful to say: the control plane was stopped, so its name no longer
# resolved (Docker's embedded DNS answers for running containers only). A
# diagnostic that aborts the diagnosis is worse than no diagnostic.
try:
    addrs = sorted({a[4][0] for a in socket.getaddrinfo("control-plane", None)})
    print(f"  control-plane resolves to {', '.join(addrs)} from inside the proxy")
except OSError as e:
    print(f"  control-plane does NOT resolve from inside the proxy ({e}) - it is "
          f"probably not running; `docker compose ps -a`")

# By NAME: what the proxy actually talks to. Resolves to the authorize-net
# address, because that is the only network the two containers share. The second
# probe expects REFUSED rather than merely "not connected", and that is the whole
# evidence for the bind split: the packet reaches the control plane and finds no
# listener on that address, because management binds 172.31.0.2 alone.
check("control-plane", 8091, "reached",
      "authorize listener - the proxy must be able to ask")
check("control-plane", 8090, "refused",
      "management API is not served on the authorize-net address")

# By LITERAL control-net address: the other half of the argument, and the half
# that is Docker's behaviour rather than ours (inter-bridge forwarding dropped,
# both nets internal). Both expect DROPPED - no path at all, as against the
# refusal above, which is what distinguishes "unroutable subnet" from "reachable
# host, no listener". Port 8091 first, and it is the POSITIVE CONTROL: the
# authorize listener binds the wildcard, so it IS listening on 172.31.0.2:8091.
# If that is unreachable the subnet is genuinely closed, which is what makes the
# 8090 result below mean something beyond one shut port.
check("172.31.0.2", 8091, "dropped",
      "control-net subnet is unroutable (probing a port that IS listening)")
check("172.31.0.2", 8090, "dropped",
      "management API unreachable at its own address - no self-approval path")
sys.exit(0 if ok else 1)
endef
export SPLIT_CHECK_PY

# Body of `audit-prune` (see the target above). Runs inside the control-plane
# container so it shares the app's view of the store (CONTROL_DB, WAL mode).
define AUDIT_PRUNE_PY
import os, sqlite3, time

# Only the audit table is touched. Reads the same defaults app.py does, so an
# operator override of CONTROL_DB is honoured; AUDIT_RETENTION_DAYS is passed in
# by the Makefile (docker exec -e).
days = int(os.environ.get("AUDIT_RETENTION_DAYS", "30"))
db = os.environ.get("CONTROL_DB", "/var/lib/control-plane/control.db")
cutoff = time.time() - days * 86400

conn = sqlite3.connect(db, timeout=10.0)
# The app runs in WAL and the drain loop writes in short bursts; wait rather than
# fail on a momentary lock. VACUUM below needs the write lock to itself.
conn.execute("PRAGMA busy_timeout=10000")
deleted = conn.execute("DELETE FROM audit WHERE ts < ?", (cutoff,)).rowcount
conn.commit()
# DELETE frees pages inside the file but does not shrink it; VACUUM rebuilds the
# file compactly and returns the space to the OS. Must run outside a transaction,
# hence the commit above.
conn.execute("VACUUM")
conn.close()
print(f"audit-prune: deleted {deleted} audit row(s) older than {days}d, then VACUUM")
endef
export AUDIT_PRUNE_PY
