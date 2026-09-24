# SPDX-License-Identifier: Apache-2.0
# dockade — dev/ops entrypoints. Run `make` (or `make help`) to list targets.
#
# Two jobs:
#   - `make check`  static checks: linters (when installed) + repo consistency
#                   guards that always run + a build-verification that asserts
#                   every image still builds (skipped when docker is unavailable).
#                   CI-friendly: non-zero on any failure.
#   - compose/*     thin wrappers over the shared infrastructure (egress proxy +
#                   control plane + UI + tool gateway, plus the optional llm-*
#                   profiles) in
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
# derive-the-path-from-the-server-name rule). Bind-mounted READ-ONLY into the gateway,
# which is the only container given it: the servers hold no credential of their own in
# http mode (see mcp-servers.yml), so the gateway injects per request. Also read by
# hand when probing a server without one:
#   tok=$$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["token"])' \
#            $(MCP_SECRETS)/mcp-github.json)
MCP_SECRETS ?= $(DOCKADE_CONFIG_HOME)/secrets
# EXPORTED because docker-compose.yml interpolates it: the gateway bind-mounts this
# directory read-only. Compose reads the process environment, and a plain Make
# variable is not in it — without this the mount would silently fall back to the
# in-repo default and every token would read as missing.
export MCP_SECRETS
# The gateway bind-mounts that directory read-only, and the files in it are the host
# user's, mode 0600 — which `secrets-perm-check` below exists to keep that way. A
# container running as its image's own system uid simply cannot read them, so it runs
# as the INVOKING USER instead. Same move sandbox-lib.sh makes with
# `--build-arg USER_UID`, and for the same reason: a container that reads host-owned
# bind mounts has to share the owner.
DOCKADE_UID ?= $(shell id -u)
DOCKADE_GID ?= $(shell id -g)
export DOCKADE_UID
export DOCKADE_GID
# Durable per-machine config lives here, OUTSIDE the repo, for the reason above:
# this tree is bind-mounted read-write into a sandbox, so anything configured
# from inside it is agent-writable. Holds `secrets/` (MCP credentials),
# `marketplaces/` (plugin marketplace checkouts, mounted read-only) and `plugins`
# (which plugin@marketplace ids to enable).
#
# Spelled here AND in sandbox-lib.sh's sc_config_home, because make cannot source
# bash and the launchers cannot read a Makefile; `make consistency` asserts the
# two agree, which is the same two-spellings-plus-a-drift-guard shape as the
# firewall/policy allowlist check.
DOCKADE_CONFIG_HOME ?= $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)/dockade
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
           claude-sandbox/statusline.sh \
           claude-sandbox/claude-wrapper.sh
DOCKERFILES := claude-sandbox/Dockerfile opencode-sandbox/Dockerfile \
               proxies/egress/Dockerfile \
               control-plane/Dockerfile control-plane-ui/Dockerfile \
               tool-gateway/Dockerfile
# A GLOB for the workflows, not a list, so a second workflow is linted without
# anyone remembering to register it — same reasoning as LAUNCHERS above.
YAMLFILES := docker-compose.yml mcp-servers.yml .hadolint.yaml .yamllint \
             $(wildcard .github/workflows/*.yml)
JSONFILES := $(shell git ls-files '*.json' 2>/dev/null)
PYFILES := proxies/egress/addon.py control-plane-ui/app.py \
           control-plane/app.py control-plane/store.py control-plane/policy.py \
           control-plane/holds.py control-plane/ingest.py control-plane/audit.py \
           control-plane/inventory.py control-plane/provenance.py \
           control-plane/api_authorize.py control-plane/api_tool.py \
           control-plane/api_approvals.py control-plane/api_egress.py \
           control-plane/api_mcp.py control-plane/api_views.py \
           tool-gateway/app.py tool-gateway/outcomes.py tool-gateway/discovery.py \
           tool-gateway/protocol.py tool-gateway/surface.py tool-gateway/execute.py
# Dependency-free unit tests for the governance-critical decision logic. Kept
# separate from PYFILES so they can be linted with the app code but discovered
# and run on their own (python -m unittest, no pip installs — see tests/).
TESTFILES := $(shell git ls-files 'tests/*.py' 2>/dev/null)

# Files referenced by Dockerfile COPY / entrypoint — existence is asserted so a
# rename can't silently break the build.
REFFILES := $(SCRIPTS) \
            claude-sandbox/user-settings.json \
            claude-sandbox/user-CLAUDE.md \
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
            control-plane/inventory.py \
            control-plane/provenance.py \
            control-plane/api_authorize.py \
            control-plane/api_tool.py \
            control-plane/api_approvals.py \
            control-plane/api_egress.py \
            control-plane/api_mcp.py \
            control-plane/api_views.py \
            control-plane/requirements.txt \
            tool-gateway/app.py \
            tool-gateway/outcomes.py \
            tool-gateway/discovery.py \
            tool-gateway/execute.py \
            tool-gateway/protocol.py \
            tool-gateway/surface.py \
            tool-gateway/requirements.txt \
            control-plane-ui/app.py \
            control-plane-ui/requirements.txt \
            control-plane-ui/index.html \
            control-plane-ui/app.js \
            policies/egress-allowlist.txt

.PHONY: help check check-strict lint consistency test verify-build \
        up down destroy audit-prune control-tool-preflight backup restore \
        secrets-perm-check \
        rebuild logs-ep logs-cp logs-tg tool-outcomes print-config-home \
        mcp-up mcp-down mcp-ps mcp-tools gateway-tools \
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
	echo "== host config home: Makefile and sandbox-lib.sh agree =="
	# Two spellings of one path, in two languages that cannot read each other:
	# make has no way to source bash, and the launchers have no way to read a
	# Makefile. Drift here is silent and expensive — `make mcp-up` would warn about
	# permissions on a directory nobody uses while the launcher mounted
	# marketplaces from somewhere else. Invoke BOTH and compare, rather than
	# grepping for a literal, so the check tests the resolved value including the
	# XDG_CONFIG_HOME branch.
	lib=$$(bash -c '. ./sandbox-lib.sh && sc_config_home')
	mk="$(DOCKADE_CONFIG_HOME)"
	if [ "$$lib" != "$$mk" ]; then
	  echo "  FAIL: sandbox-lib.sh says '$$lib' but the Makefile says '$$mk'."
	  echo "        sc_config_home and DOCKADE_CONFIG_HOME must resolve identically."
	  exit 1
	fi
	# And again with XDG_CONFIG_HOME set, which is the branch a dev machine with a
	# default $$HOME never exercises — the two implementations could agree on the
	# fallback and disagree on the spec-mandated override.
	lib=$$(XDG_CONFIG_HOME=/tmp/xdg-probe bash -c '. ./sandbox-lib.sh && sc_config_home')
	mk=$$(XDG_CONFIG_HOME=/tmp/xdg-probe $(MAKE) --no-print-directory print-config-home)
	if [ "$$lib" != "$$mk" ]; then
	  echo "  FAIL: with XDG_CONFIG_HOME set, sandbox-lib.sh says '$$lib' but the"
	  echo "        Makefile says '$$mk'. One of them ignores XDG_CONFIG_HOME."
	  exit 1
	fi
	echo "  ok — both resolve to $(DOCKADE_CONFIG_HOME) (and honour XDG_CONFIG_HOME)"
	echo "== marketplace mounts are READ-ONLY =="
	# A writable plugin tree is a cross-session channel, not a convenience: the
	# agent edits a skill or a hook, and it lands in its own context — or executes —
	# on the next boot, outside the review that /workspace commits get. `:ro` is the
	# whole mitigation, and it is one character from being absent, so assert it
	# rather than trusting the comment next to it.
	# sandbox-lib.sh is in the file set, not just the launchers: tier 1 builds the
	# mount in sc_marketplaces, and a future tier might inline one instead. Both
	# places must obey the rule, so both are searched.
	mounted=0
	for f in $(LAUNCHERS) sandbox-lib.sh; do
	  tot=$$(grep -cE -- '-v "[^"]*":/marketplaces' "$$f" || true)
	  [ "$$tot" -gt 0 ] || continue
	  ro=$$(grep -cE -- '-v "[^"]*":/marketplaces:ro' "$$f" || true)
	  if [ "$$tot" != "$$ro" ]; then
	    echo "  FAIL: $$f constructs $$tot /marketplaces mount(s) but only $$ro carry :ro."
	    exit 1
	  fi
	  mounted=$$((mounted + tot))
	  echo "  ok $$f ($$tot mount(s), all :ro)"
	done
	if [ "$$mounted" -eq 0 ]; then
	  echo "  FAIL: nothing constructs a /marketplaces mount, so this guard checked"
	  echo "        nothing. Tier 1 does (sc_marketplaces) — did the mount change shape?"
	  echo "        If the feature was removed deliberately, remove this guard too."
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
	                             'Makefile' 'docker-compose.yml' 'mcp-servers.yml' \
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
	  if grep -qE 'control-(ui-)?net|(tool-)?authorize-net' "$$launcher"; then
	    echo "  FAIL: $$launcher references a control-plane network — no sandbox tier"
	    echo "        may EVER attach to control-net, control-ui-net, authorize-net"
	    echo "        or tool-authorize-net (the agent must have no route to the"
	    echo "        control plane, and both authorize nets reach it just as"
	    echo "        directly as the others)"
	    exit 1
	  fi
	done
	# The security-load-bearing nets MUST each be internal: sandbox-net (the
	# agent's only net), the control paths — control-net (management) and the two
	# enforcer bridges, authorize-net (the proxy's route to /authorize) and
	# tool-authorize-net (the MCP gateway's), each of which reaches the control
	# plane just as directly — and mcp-net, whose internal-ness is what leaves the
	# egress proxy as the only thing an MCP server container can reach, from a
	# container holding a write-capable credential. Check each BY NAME — a bare count of 'internal:
	# true' can't tell that the RIGHT nets are the internal ones (a future edit
	# could flip sandbox-net to non-internal while some other net gained
	# 'internal: true', and a count would still pass). awk isolates each top-level
	# network block (2-space key .. next 2-space key) and asserts 'internal: true'
	# appears inside it. tests/test_topology.py asserts the same property from the
	# other side, along with who is attached to what; this stays because it is the
	# one that runs in `make consistency` alongside the launcher check above.
	for net in sandbox-net control-net authorize-net tool-authorize-net mcp-net; do
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
	echo "  ok — no launcher attaches the sandbox to a control network; sandbox-net, control-net, authorize-net, tool-authorize-net and mcp-net all internal"

# No `##` description, so it stays out of `make help`: it exists for the config-home
# drift guard above, which needs make's own answer under a modified environment.
print-config-home:
	@printf '%s\n' "$(DOCKADE_CONFIG_HOME)"

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
	# nothing changed. Covers every Dockerfile — the compose services here, and
	# BOTH sandbox tiers (not compose services) via their launchers.
	echo "== docker compose build (the compose services) =="
	$(COMPOSE) build
	for launcher in $(LAUNCHERS); do
	  echo "== sandbox image build ($$launcher --build-only) =="
	  "./$$launcher" --build-only
	done

# ── shared infrastructure (docker-compose.yml) ──────────────────────────────

# Warn on a secrets directory anyone but its owner can read. Its own target because
# TWO paths now expose these files: `mcp-up` starts a server that may read one, and
# `up` starts the gateway, which bind-mounts the whole directory. A check that ran
# only on the first would go quiet exactly when the files became more exposed.
#
# Checked rather than fixed: a mode this loose is a decision someone made, and
# silently chmod-ing another person's files from a build target is worse than saying
# so. A warning, not a failure — the operator may have a reason, and a hard stop here
# would take down infra that has nothing to do with the credential.
secrets-perm-check:
	@if [ -d "$(MCP_SECRETS)" ]; then
	  for f in "$(MCP_SECRETS)" "$(MCP_SECRETS)"/*.json; do
	    [ -e "$$f" ] || continue
	    if [ -n "$$(find "$$f" -maxdepth 0 -perm /077 2>/dev/null)" ]; then
	      echo "WARNING: $$f is group/world-readable — chmod 600 (700 for the dir)."
	    fi
	  done
	fi

up: secrets-perm-check ## Bring up the shared infra (egress proxy + control plane + UI), building if needed
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
	  echo "usage: make mcp-up SERVER=github   (profiles: $$(grep -oE '^ +- mcp-[^ ]+' mcp-servers.yml | sed 's/.*- //' | tr '\n' ' '))"
	  exit 2
	fi
	# No credential is passed in: measured, github-mcp-server ignores its env token in
	# http mode and takes a per-request bearer, so the container holds nothing and this
	# target has nothing secret to plumb. See mcp-servers.yml. A server that DOES take
	# an env credential reads it from $(MCP_SECRETS) — never from a repo `.env`.
	#
	@$(MAKE) --no-print-directory secrets-perm-check
	# --wait returns when the container is running and its dependencies are
	# healthy, so a failure here is real rather than a race. If it exits
	# immediately, read its log before re-running.
	$(COMPOSE) --profile mcp-$(SERVER) up -d --wait --wait-timeout 60 mcp-$(SERVER)

mcp-down: ## Stop one catalogue MCP server: make mcp-down SERVER=github
	@if [ -z "$(SERVER)" ]; then echo "usage: make mcp-down SERVER=github"; exit 2; fi
	# stop+rm rather than `down`, which would take the shared infra with it.
	$(COMPOSE) stop mcp-$(SERVER)
	$(COMPOSE) rm -f mcp-$(SERVER)

mcp-ps: ## Who is on mcp-net right now (should be the proxy plus enabled servers)
	docker network inspect mcp-net \
	  -f '{{range .Containers}}{{printf "%-16s %s\n" .Name .IPv4Address}}{{end}}'

# A well-formed DUMMY, and both halves matter. Enumeration never calls GitHub, so no
# real credential is needed to read a server's tool surface — but the bearer is
# format-checked before that is discovered, and `Bearer not-a-real-token` is refused
# outright (NOTES.md, "Driving the server by hand"). Override to compare against what a
# real token returns: `make mcp-tools SERVER=github MCP_PROBE_TOKEN=$$tok`.
MCP_PROBE_TOKEN ?= ghp_000000000000000000000000000000000000
# The first catalogue server's transport, which is the image's own default rather than
# anything this repo chose. A second server that listens elsewhere makes these
# per-server; until one exists, two variables beat a lookup.
MCP_PORT ?= 8082
MCP_PATH ?= /mcp
# Unpinned on purpose, and the contrast with mcp-servers.yml is the reason: that file
# pins because the container holds a write-capable credential and `latest` would be an
# auto-updating supply chain into it. This one is thrown away after a single request
# and is handed a dummy token, so a floating tag costs nothing it could spend.
CURL_IMAGE ?= curlimages/curl:latest

# Reads the SSE reply on stdin. Exists mostly to make the failure modes legible: the
# server answers a bad bearer with a bare line of prose and no JSON at all, which
# `jq` reports as a parse error rather than as what happened.
define MCP_TOOLS_PY
import json, os, sys

raw = sys.stdin.read()
payloads = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
if not payloads:
    sys.exit(f"mcp-tools: not an MCP reply — the server said: {raw.strip()[:200]!r}")
if os.environ.get("MCP_RAW"):
    print(payloads[0])
    sys.exit(0)

message = json.loads(payloads[0])
if "error" in message:
    sys.exit(f"mcp-tools: the server returned an error — {message['error']}")
result = message["result"]
tools = sorted(result["tools"], key=lambda t: t["name"])

for tool in tools:
    notes = tool.get("annotations", {})
    kind = "RO" if notes.get("readOnlyHint") else "RW"
    print(f"{kind}  {tool['name']:<32} {notes.get('title', '')}")
    # A REQUIRED enum is a dispatcher: one tool name standing for several operations,
    # so a rule keyed on the name alone decides all of them together. Shown at the
    # point of listing because it is invisible in a bare list of names.
    schema = tool.get("inputSchema", {})
    properties = schema.get("properties", {})
    for field in schema.get("required", []):
        choices = properties.get(field, {}).get("enum")
        if choices:
            print(f"      {field}: {', '.join(choices)}")

writes = sum(1 for t in tools if not t.get("annotations", {}).get("readOnlyHint"))
total = len(json.dumps(result))
icons = sum(len(json.dumps(t.get("icons", []))) for t in tools)
print(f"\n{len(tools)} tools, {writes} not read-only; "
      f"{total} bytes of which {round(100 * icons / total)}% is icons")
endef
export MCP_TOOLS_PY

mcp-tools: ## List one server's tools and read-only hints: make mcp-tools SERVER=github [RAW=1]
	@if [ -z "$(SERVER)" ]; then echo "usage: make mcp-tools SERVER=github   [RAW=1 for the raw JSON]"; exit 2; fi
	@if ! docker ps --format '{{.Names}}' | grep -qx 'mcp-$(SERVER)'; then
	  echo "mcp-tools: mcp-$(SERVER) is not running — make mcp-up SERVER=$(SERVER)"
	  exit 2
	fi
	# A throwaway container ON mcp-net, because the network is internal with no
	# published ports: nothing on the host has a path in, which is the property
	# boundary-check.sh proves from the other side. It also takes the same route the
	# gateway will — dialling a sibling by name. The image pull happens before the
	# network is attached, so `internal:` does not block it, and the container takes a
	# dynamic address out of the subnet, which is why the real servers pin theirs.
	#
	# Three details are measured rather than guessed (NOTES.md): the Authorization
	# header is mandatory and format-checked, the server is stateless so `tools/list`
	# needs no `initialize` handshake and no session header, and the reply is SSE.
	docker run --rm --network mcp-net $(CURL_IMAGE) \
	  -sS -X POST 'http://mcp-$(SERVER):$(MCP_PORT)$(MCP_PATH)' \
	  -H 'Authorization: Bearer $(MCP_PROBE_TOKEN)' \
	  -H 'Content-Type: application/json' \
	  -H 'Accept: application/json, text/event-stream' \
	  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
	  | MCP_RAW='$(RAW)' python3 -c "$$MCP_TOOLS_PY"

# The gateway's agent-facing leg, BY ADDRESS. The gateway is triple-homed, so its name
# resolves to whichever leg Docker's DNS returns and two of the three would be the wrong
# one — the same reason the gateway itself dials the control plane by address. Held equal
# to compose by tests/test_topology.py, so a re-pin cannot leave this probing nothing.
GATEWAY_ADDR ?= 172.30.0.11
GATEWAY_PORT ?= 8100

# Reads the gateway's reply. Plain JSON rather than SSE — that is the gateway's own
# choice and not the servers' — but both shapes are accepted here for the reason
# discovery.py accepts both: the day one changes, a probe that silently printed nothing
# would be worse than one that kept working.
define GATEWAY_TOOLS_PY
import json, os, sys

raw = sys.stdin.read()
payloads = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
body = payloads[0] if payloads else raw.strip()
if not body:
    sys.exit("gateway-tools: empty reply — is tool-gateway serving?")
try:
    message = json.loads(body)
except ValueError:
    sys.exit(f"gateway-tools: not an MCP reply — the gateway said: {body[:200]!r}")
if os.environ.get("MCP_RAW"):
    print(json.dumps(message, indent=2))
    sys.exit(0)
if "error" in message:
    sys.exit(f"gateway-tools: the gateway returned an error — {message['error']}")

def count(n, noun):
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"

# Split on the SEPARATOR being there at all, which is how the gateway itself tells its
# own tools from proxied ones: every proxied name carries `<server>__`, and a native one
# cannot, so a missing separator is the whole discriminator (tool-gateway/surface.py).
servers, native = {}, []
for tool in message["result"]["tools"]:
    server, sep, name = tool["name"].partition("__")
    if sep:
        servers.setdefault(server, []).append(name)
    else:
        native.append(tool["name"])

for server in sorted(servers):
    print(server)
    for name in sorted(servers[server]):
        print(f"  {name}")
if native:
    print("the gateway's own")
    for name in sorted(native):
        print(f"  {name}")

proxied = sum(len(names) for names in servers.values())
if not proxied:
    # The empty surface is a real steady state — a fresh install, or every rule denied —
    # so it gets a sentence of its own rather than the summary below with zeroes in it.
    # The probe cannot tell the two causes apart from `tools/list`, and says so instead
    # of picking one. Printed and exited ZERO, unlike every other exit in here: those
    # are failures to get an answer, and this IS the answer.
    print("\nno tools from any server. Either none is enabled, or nothing on one is "
          "ruled allow or ask — both are the same empty surface from the agent's side.")
    sys.exit(0)

print(f"\n{count(proxied, 'tool')} from {count(len(servers), 'server')} — each is "
      f"ruled allow or ask. A tool with no rule, a denied one, and a tool no server "
      f"exposes are all absent, and absent means the same thing for all three.")
endef
export GATEWAY_TOOLS_PY

gateway-tools: ## List what the gateway serves the AGENT: make gateway-tools [RAW=1]
	@if ! docker ps --format '{{.Names}}' | grep -qx 'tool-gateway'; then
	  echo "gateway-tools: tool-gateway is not running — make up"
	  exit 2
	fi
	# A throwaway container on SANDBOX-NET, which is the agent's own position and the
	# only one this listener is served on. Probing from anywhere else would either fail
	# (the point of the single-address bind) or need a published port, which would put
	# the agent's tool surface on the host.
	#
	# No Authorization header, unlike mcp-tools: the gateway authenticates nobody. Its
	# leg is reachable by the sandbox alone, and a credential the agent had to hold
	# would be a credential the agent holds.
	docker run --rm --network sandbox-net $(CURL_IMAGE) \
	  -sS -X POST 'http://$(GATEWAY_ADDR):$(GATEWAY_PORT)/mcp' \
	  -H 'Content-Type: application/json' \
	  -H 'Accept: application/json, text/event-stream' \
	  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
	  | MCP_RAW='$(RAW)' python3 -c "$$GATEWAY_TOOLS_PY"

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

# Where `make backup` writes: OUTSIDE this repo, beside MCP_SECRETS and for the same
# reason. A backup is the crown-jewel state — every host the agent has ever asked
# for, and the operator's whole policy — and a sandbox launched with dockade as its
# workspace bind-mounts this tree read-write. A copy in the tree is one the agent
# can read whole and edit, and `restore` checks a file's shape, not its provenance,
# so an in-tree backup is a store the agent gets to write. Gitignoring `backups/`
# stays as the belt for anyone who points BACKUP_DIR back here by hand.
BACKUP_DIR ?= $(DOCKADE_CONFIG_HOME)/backups

# The image and volume `backup`/`restore` hand to `docker run`. Both are fixed in
# docker-compose.yml (the service's `image:` and the volume's `name:`) and restated
# here because `docker run` takes them as arguments rather than reading the file;
# tests/test_topology.py holds the two spellings together.
CONTROL_IMAGE := dockade-control-plane
CONTROL_VOLUME := dockade-control-state

# `docker run`, and neither of the two obvious alternatives:
#
#   - NOT `docker compose run`. It attaches the service's networks, and control-plane
#     PINS its address on both of them (see docker-compose.yml, and the reasoning in
#     "Pin every address on a network, or none"). A second container asking for an
#     address the running one holds fails at start with "Address already in use", and
#     `compose run` has no --network to override with. Measured, not predicted: this
#     is what the first cut of these targets did, and it failed the moment it met a
#     live stack.
#   - NOT `docker exec`. That needs a RUNNING container, and the moment you most want
#     a backup is the moment the stack is down (before a risky migration, after a bad
#     one).
#
# So the state is reached through the volume rather than through the service, and
# `--network none` follows honestly rather than as a workaround: this touches a file
# and needs a route nowhere — least of all the process holding the crown jewels open.
# `-i` is for `restore`, which feeds the backup on stdin; `backup` reads none.
CONTROL_TOOL = docker run --rm -i --network none \
  -v $(CONTROL_VOLUME):/var/lib/control-plane \
  --entrypoint python3 $(CONTROL_IMAGE)

# Prerequisite of both targets. Without it a missing image sends `docker run` to a
# registry for a name that was never pushed, and a missing volume is worse: docker
# CREATES an empty one, so `restore` would quietly populate a volume compose has
# never adopted and the store would look empty after a restore that reported success.
control-tool-preflight:
	@if ! docker image inspect $(CONTROL_IMAGE) >/dev/null 2>&1; then
	  echo "no $(CONTROL_IMAGE) image — build it first (make up, or make rebuild)"
	  exit 1
	fi
	@if ! docker volume inspect $(CONTROL_VOLUME) >/dev/null 2>&1; then
	  echo "no $(CONTROL_VOLUME) volume — there is no store yet (make up)"
	  exit 1
	fi

backup: control-tool-preflight ## Snapshot the control-plane store (policy + approvals + audit) into BACKUP_DIR (default ~/.config/dockade/backups)
	@# The crown-jewel state, which DESIGN.md says must be backed up independently of
	@# any container — this is that path. Non-destructive and safe to run against a
	@# LIVE stack: `VACUUM INTO` (in BACKUP_PY) takes a read lock and writes a
	@# consistent, compacted copy, so nothing has to be stopped and no decision is
	@# denied while it runs.
	@mkdir -p "$(BACKUP_DIR)"
	@out="$(BACKUP_DIR)/dockade-control-$$(date -u +%Y%m%dT%H%M%SZ).db"
	# Written to `.partial` and renamed only after the check below, so an interrupted
	# transfer never leaves something that looks like a usable backup.
	$(CONTROL_TOOL) -c "$$BACKUP_PY" > "$$out.partial"
	if [ "$$(head -c 15 "$$out.partial" 2>/dev/null)" != "SQLite format 3" ]; then
	  echo "backup: FAILED — the stream is not a SQLite database. Left at $$out.partial"
	  exit 1
	fi
	mv "$$out.partial" "$$out"
	echo "backup: wrote $$out ($$(du -h "$$out" | cut -f1))"

restore: control-tool-preflight ## Replace the control-plane store from a backup: make restore FILE=<BACKUP_DIR>/… (destructive)
	@# The other half of `backup`, and the destructive one: it discards the CURRENT
	@# rules, approvals and audit history. Deliberately manual, deliberately loud,
	@# and it validates the incoming file in two passes — a cheap one before the
	@# stack is touched at all, then the full one in RESTORE_PY before the replace,
	@# which is the last moment a half-checked file can still be refused.
	@if [ -z "$(FILE)" ]; then
	  echo "usage: make restore FILE=$(BACKUP_DIR)/dockade-control-<stamp>.db"
	  exit 2
	fi
	@if [ ! -f "$(FILE)" ]; then echo "restore: no such file: $(FILE)"; exit 2; fi
	@# The cheap half of the validation, hoisted to BEFORE the stack is touched. The
	# full check (integrity, the four tables, the schema version) needs the container
	# and therefore the maintenance window, but the mistake people actually make is
	# naming the wrong FILE — and that one is decidable from the first 15 bytes, at no
	# downtime and before the operator is even asked to confirm.
	@if [ "$$(head -c 15 "$(FILE)" 2>/dev/null)" != "SQLite format 3" ]; then
	  echo "restore: $(FILE) is not a SQLite database — nothing was touched"
	  exit 2
	fi
	@if [ -z "$(FORCE)" ]; then
	  printf 'Replace the control-plane store (policy rules, approvals, audit history) with %s? [y/N] ' "$(FILE)"
	  read -r reply
	  case "$$reply" in y|Y|yes) ;; *) echo "restore: aborted"; exit 1;; esac
	fi
	@# The proxy goes down FIRST and comes back last. It fails closed when the
	@# control plane is unreachable, so leaving it up would spend the restore
	@# window denying and auditing real agent requests — filling the very log
	@# being restored with records of the restore. A maintenance window where the
	@# sandbox sees a refused connection is the honest shape of this.
	$(COMPOSE) stop egress-proxy control-plane-ui control-plane
	@# The stack comes back WHATEVER happens next, which is why this is a trap and
	# not a line at the end. RESTORE_PY refuses on a file that fails validation —
	# that is it working — and the first cut let the refusal abort the recipe, so the
	# SAFE path was the one that left the governance plane and the proxy down until
	# someone noticed. A restore that declines to run must cost nothing but the
	# window. Observed, not theorised: `make restore FILE=README.md`.
	trap '$(COMPOSE) up -d --wait --wait-timeout 120' EXIT
	$(CONTROL_TOOL) -c "$$RESTORE_PY" < "$(FILE)"

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

logs-tg: ## Follow the tool-gateway log — decisions taken and how each call ENDED
	$(COMPOSE) logs -f tool-gateway

# How many outcome records `tool-outcomes` shows. A tail, not a window: this is the
# raw stream, and the queryable view of it is the control plane's audit (which is
# where it lands once ingested).
OUTCOMES ?= 20

tool-outcomes: ## Show the last $$OUTCOMES tool-call outcomes from the gateway's own stream
	# What the gateway recorded and the control plane drains — the record of how each
	# call ENDED, which the authority structurally cannot write: its claim row is
	# written before the call runs.
	#
	# Read from the GATEWAY's side of the volume, where it is writable and current.
	# The control plane's copy is read-only and lags by one drain interval, so a
	# disagreement between the two is a broken ingest rather than a missing record —
	# which is exactly the thing worth being able to tell apart.
	docker exec -e OUTCOMES=$(OUTCOMES) tool-gateway python3 -c "$$TOOL_OUTCOMES_PY"

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

split-check: ## Assert the running proxy reaches /authorize and NOT the management API or the gateway's bridge
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

# The MCP gateway's bridge, probed the same two ways and for a sharper reason: a
# bypassed relay guard must not reach the CLAIM endpoint, which is the one place
# the control plane releases an approved tool call. Two enforcers sharing a route
# is the lateral edge the second bridge exists to prevent, so this is where that
# claim is measured rather than asserted.
check("control-plane", 8092, "refused",
      "tool bridge is not served on the authorize-net address")
check("172.27.0.2", 8091, "dropped",
      "tool-authorize-net subnet is unroutable (probing a port that IS listening)")
check("172.27.0.2", 8092, "dropped",
      "tool bridge unreachable at its own address - the proxy cannot spend an "
      "approved ask")
sys.exit(0 if ok else 1)
endef
export SPLIT_CHECK_PY

# Body of `audit-prune` (see the target above). Runs inside the control-plane
# container so it shares the app's view of the store (CONTROL_DB, WAL mode).
# Body of `tool-outcomes` (see the target above). Reads the gateway's own JSONL
# stream and prints it one line per record, newest last.
#
# Deliberately incurious, like the control plane's ingest: a line that does not parse
# is COUNTED and skipped, never guessed at. A half-written tail is normal here — this
# reads a file another process is appending to — so silently dropping one is right,
# and saying how many were dropped is what keeps "unparseable" from looking like
# "nothing happened".
define TOOL_OUTCOMES_PY
import json, os, time

path = os.environ.get("GATEWAY_AUDIT_LOG", "/var/log/tool-gateway/audit.jsonl")
show = int(os.environ.get("OUTCOMES", "20"))
try:
    with open(path) as f:
        lines = f.read().splitlines()
except OSError as e:
    raise SystemExit(f"tool-outcomes: cannot read {path} ({e})")

rows, bad = [], 0
for line in lines[-show * 2:]:
    if not line.strip():
        continue
    try:
        rows.append(json.loads(line))
    except ValueError:
        bad += 1

if not rows:
    print(f"tool-outcomes: no outcomes recorded yet in {path}")
    raise SystemExit(0)

for r in rows[-show:]:
    when = time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0)))
    who = r.get("approval_id") or "-"
    line = (f"{when}  {r.get('status','?'):<16} "
            f"{r.get('server','?')}__{r.get('tool','?')}  approval={who}")
    if r.get("reason"):
        line += f"\n{' ' * 10}:: {r['reason']}"
    print(line)
print(f"-- {len(rows[-show:])} of {len(rows)} recent record(s)"
      + (f", {bad} unparseable line(s) skipped" if bad else ""))
endef
export TOOL_OUTCOMES_PY

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

# Body of `backup` (see the target above). Runs in a throwaway container built from
# the control-plane image, with the state volume mounted where the app expects it.
#
# STDOUT IS THE TRANSPORT — the snapshot's bytes and nothing else. Every human-
# readable line goes to stderr, or it would end up inside the .db file. That is why
# the alternative (write into the container, `docker cp` it out) was not taken: it
# needs a RUNNING container to copy from, and the moment you most want a backup is
# the moment the stack is down.
define BACKUP_PY
import os, sqlite3, shutil, sys

# The app's own module, imported rather than restated: DB_PATH is defined once and
# an operator's CONTROL_DB override is honoured for free. WORKDIR is its directory.
import store

src = store.DB_PATH
if not os.path.exists(src):
    sys.exit(f"backup: no store at {src} — nothing to back up (has it ever run?)")

# A sibling of the store, so it is on the same filesystem as the file being copied
# and inside the volume the container can write.
snap = src + ".backup-snapshot"
if os.path.exists(snap):
    os.unlink(snap)

conn = sqlite3.connect(src, timeout=10.0)
# The app is (or may be) live and writing in short bursts; wait rather than fail.
conn.execute("PRAGMA busy_timeout=10000")
# VACUUM INTO, not a file copy: it takes a read lock and writes a standalone,
# compacted database that already includes everything in the WAL. So it is
# consistent against a live store, and the result has NO -wal/-shm sidecar — which
# is what lets `restore` be a single file move rather than a three-file dance.
conn.execute("VACUUM INTO ?", (snap,))
conn.close()

snapconn = sqlite3.connect(snap)
counts = {t: snapconn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
          for t in ("rules", "approvals", "audit")}
version = snapconn.execute("PRAGMA user_version").fetchone()[0]
snapconn.close()

with open(snap, "rb") as f:
    shutil.copyfileobj(f, sys.stdout.buffer)
sys.stdout.buffer.flush()
os.unlink(snap)

print(f"backup: schema v{version}, " +
      ", ".join(f"{n} {t}" for t, n in counts.items()), file=sys.stderr)
endef
export BACKUP_PY

# Body of `restore` (see the target above). Reads the backup on STDIN — the mirror
# of BACKUP_PY writing it to stdout — so the file never has to be copied into a
# container that may not be running.
define RESTORE_PY
import os, sqlite3, sys

import store

dst = store.DB_PATH
tmp = dst + ".restore-incoming"

os.makedirs(os.path.dirname(dst), exist_ok=True)
with open(tmp, "wb") as f:
    f.write(sys.stdin.buffer.read())


def reject(why):
    """Refuse BEFORE the replace, and take the half-written file with us. Every
    check below is here because passing it is what makes the destructive step
    safe — after os.replace there is nothing left to compare against."""
    os.unlink(tmp)
    sys.exit(f"restore: REFUSED, store untouched — {why}")


try:
    conn = sqlite3.connect(tmp)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("rules", "approvals", "audit") if t in tables}
    conn.close()
except sqlite3.DatabaseError as e:
    reject(f"not a readable SQLite database ({e})")

if integrity != "ok":
    reject(f"integrity_check says {integrity!r}")
missing = {"rules", "audit", "approvals", "audit_cursor"} - tables
if missing:
    reject(f"not a control-plane store — missing table(s): {', '.join(sorted(missing))}")
# A backup from a NEWER build carries a schema this code has no steps for, and
# migration only runs forwards: restoring it would leave the app reading columns it
# does not understand, or writing rows the newer build would misread. The fix is to
# update the image, not to force it, so this refuses rather than warns.
if version > store.SCHEMA_VERSION:
    reject(f"backup is schema v{version}, this control plane understands "
           f"v{store.SCHEMA_VERSION} — update the image first")

os.replace(tmp, dst)
# Stale sidecars from the store just replaced. A -wal left behind belongs to a
# DIFFERENT database file and applying it over the restored one is how a good backup
# becomes a corrupt store, so failing to remove it is fatal, not a warning. Safe
# here because `restore` stops the stack first: nothing has the file open.
for side in ("-wal", "-shm"):
    try:
        os.unlink(dst + side)
    except FileNotFoundError:
        pass
    except OSError as e:
        sys.exit(f"restore: RESTORED but could not remove {dst + side} ({e}) — "
                 f"remove it before starting the control plane")

print(f"restore: store replaced from a schema-v{version} backup (" +
      ", ".join(f"{n} {t}" for t, n in counts.items()) + ")")
endef
export RESTORE_PY
