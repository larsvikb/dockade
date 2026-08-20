#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Shared launcher plumbing for every sandbox tier.
#
# Sourced by run-claude-sandbox.sh (tier 1, Claude + governed egress) and
# run-opencode-sandbox.sh (tier 2, local LLM, no egress). This file owns the
# mechanics that MUST NOT differ between tiers — above all the workspace safety
# guard, which is the single deliberate host coupling and the place where a
# copy-paste divergence would do the most damage.
#
# What deliberately does NOT live here: each tier's capability profile (which
# credentials, which egress, which sibling services it may reach). Those stay in
# the per-tier launcher, short and adjacent, so a reviewer can read the whole
# grant in one place. Shared plumbing, divergent capability — that split is the
# point.
#
# Functions set well-known globals rather than echoing, because several return
# bash ARRAYS (docker run flags) which cannot survive command substitution.
# Every such global is named in the function's comment.

# Repo root — the build context for sandbox images (see sc_build_image).
SANDBOX_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Image build
# ---------------------------------------------------------------------------
# sc_build_image <image_tag> <image_dir> [--no-cache]
#
# Context is the REPO ROOT, not <image_dir>, so the Dockerfile can COPY the
# shared sandbox-common/ scripts (a build context cannot reach outside itself).
# .dockerignore keeps that context small.
#
# The sandbox user is built to match the host uid/gid so bind-mounted workspaces
# are writable from both sides (avoids the drvfs/uid mismatch on WSL). Rebuild
# after switching hosts if the host uid differs.
sc_build_image() {
    local image_tag="$1" image_dir="$2" no_cache="${3:-}"
    local cache_args=()
    [[ "$no_cache" == "--no-cache" ]] && cache_args+=(--no-cache)
    echo "Building $image_tag (sandbox user uid/gid $(id -u)/$(id -g))..."
    docker build -t "$image_tag" \
        ${cache_args[@]+"${cache_args[@]}"} \
        --build-arg USER_UID="$(id -u)" \
        --build-arg USER_GID="$(id -g)" \
        -f "$SANDBOX_REPO_ROOT/$image_dir/Dockerfile" \
        "$SANDBOX_REPO_ROOT"
}

# ---------------------------------------------------------------------------
# Workspace safety guard  ->  sets SC_WORKSPACE
# ---------------------------------------------------------------------------
# The workspace is bind-mounted RW with the sandbox user matched to the host uid,
# so the agent gets full read/write to whatever this resolves to — and anything it
# writes there (git hooks, build scripts, .envrc, editor task files) later runs
# OUTSIDE the sandbox when the host next touches the repo. Mounting a home
# directory or the filesystem root would hand the agent host credentials
# (~/.ssh, ~/.aws, ...) plus a boundary-crossing foothold, defeating the point of
# the sandbox.
#
# So: hard-refuse the clearly-dangerous roots, and warn (non-fatal) when
# credential material lives inside the chosen workspace. Override the hard
# refusal with ALLOW_UNSAFE_WORKSPACE=1 for the rare deliberate case — a
# conscious, visible choice, in keeping with the "no silent unsafe defaults" ethos.
#
# This applies to EVERY tier. A tier-2 agent has no egress, but it still writes to
# the host filesystem, and host-side execution of what it writes is the risk here.
sc_guard_workspace() {
    local real_workspace real_home sensitive
    real_workspace="$(cd "$1" && pwd -P)"   # canonical, symlinks resolved
    real_home="$(cd "$HOME" 2>/dev/null && pwd -P || echo "$HOME")"

    _sc_deny_workspace() {
        echo "REFUSING to mount workspace: $real_workspace" >&2
        echo "  $1" >&2
        echo "  This would give the sandbox agent RW access to sensitive host files, and" >&2
        echo "  anything it writes there runs on the host later (git hooks, build scripts)." >&2
        echo "  Re-run from a dedicated project directory, or set ALLOW_UNSAFE_WORKSPACE=1" >&2
        echo "  to override deliberately." >&2
        exit 1
    }

    if [[ "${ALLOW_UNSAFE_WORKSPACE:-}" != "1" ]]; then
        if [[ "$real_workspace" == "/" ]]; then
            _sc_deny_workspace "that is the filesystem root."
        elif [[ "$real_workspace" == "$real_home" ]]; then
            _sc_deny_workspace "that is your home directory."
        elif [[ "$real_home" == "$real_workspace"/* ]]; then
            # Workspace is an ancestor of $HOME, so mounting it exposes the whole home.
            _sc_deny_workspace "your home directory ($real_home) is inside it."
        fi
    fi

    # Non-fatal: credential material sitting inside the chosen workspace. This is
    # legal (you may genuinely want to work there), but the agent will be able to
    # read and modify it, so make that visible rather than silent.
    for sensitive in .ssh .aws .gnupg .config/gcloud .kube .docker/config.json .netrc .git-credentials; do
        if [[ -e "$real_workspace/$sensitive" ]]; then
            echo "WARNING: workspace contains '$sensitive' — the sandbox agent will have RW access to it." >&2
        fi
    done

    # shellcheck disable=SC2034  # consumed by the sourcing launcher, not here
    SC_WORKSPACE="$real_workspace"
}

# ---------------------------------------------------------------------------
# Host config home  ->  echoes the dockade config directory
# ---------------------------------------------------------------------------
# Durable per-machine settings live OUTSIDE this repo. Not tidiness: a sandbox
# launched with dockade as its workspace bind-mounts this tree READ-WRITE, so
# anything configured from inside the repo is agent-writable — and a knob that
# decides which code loads into the agent must not be. That is the same reason
# MCP_SECRETS sits here (Makefile), which is why both derive from this one path;
# `make consistency` asserts the two spellings agree.
#
# Precedence: DOCKADE_CONFIG_HOME (explicit) > $XDG_CONFIG_HOME/dockade (the
# spec) > ~/.config/dockade (the fallback the spec itself prescribes).
#
# Echoes rather than setting a global: it is a plain string, needed in string
# contexts, and every caller wants it interpolated.
sc_config_home() {
    if [[ -n "${DOCKADE_CONFIG_HOME:-}" ]]; then
        printf '%s\n' "$DOCKADE_CONFIG_HOME"
    else
        printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/dockade"
    fi
}

# ---------------------------------------------------------------------------
# Plugin marketplaces  ->  sets SC_MARKETPLACE_ARGS (array), SC_MARKETPLACE_DESC
# ---------------------------------------------------------------------------
# Claude Code can take a marketplace from a local DIRECTORY, and a directory
# source is referenced in place — no clone, no copy, no egress, no credential
# (measured; see NOTES.md). That is what makes this the right shape for a
# container with no governed git path: the human clones marketplace repos on the
# host, and the sandbox reads them.
#
# Mounted READ-ONLY, and that is the load-bearing part. A writable plugin tree is
# a cross-session channel: the agent edits a skill or a hook now, and it lands in
# its own context — or executes — on the next boot, outside the diff review that
# /workspace commits get. Read-only costs nothing, because a directory-source
# marketplace is never written to in normal use (installing from a chmod-a-w tree
# works).
#
# Auto-mounted when the default path EXISTS, so the common case needs no
# configuration at all. SANDBOX_MARKETPLACES_DIR overrides the host path — and
# when it is set explicitly, a missing directory is FATAL rather than skipped:
# the operator named something specific, and silently launching without it is how
# you spend a session wondering where your plugins went.
#
# The container path is fixed at /marketplaces and is NOT the host path, because
# Claude Code records the path it was given verbatim as the marketplace's
# installLocation — it has to be the path as seen from inside.
sc_marketplaces() {
    SC_MARKETPLACE_ARGS=()
    SC_MARKETPLACE_DESC="none"

    local dir explicit=false
    if [[ -n "${SANDBOX_MARKETPLACES_DIR:-}" ]]; then
        dir="$SANDBOX_MARKETPLACES_DIR"
        explicit=true
    else
        dir="$(sc_config_home)/marketplaces"
    fi

    if [[ ! -d "$dir" ]]; then
        if [[ "$explicit" == "true" ]]; then
            echo "ERROR: SANDBOX_MARKETPLACES_DIR is set to '$dir', which is not a" >&2
            echo "       directory. Refusing to launch without it — unset the variable" >&2
            echo "       to run with no marketplaces." >&2
            exit 1
        fi
        SC_MARKETPLACE_DESC="none ($dir does not exist)"
        return 0
    fi

    local real real_home secrets
    real="$(cd "$dir" && pwd -P)"        # canonical, symlinks resolved
    real_home="$(cd "$HOME" 2>/dev/null && pwd -P || echo "$HOME")"
    secrets="$(sc_config_home)/secrets"
    [[ -d "$secrets" ]] && secrets="$(cd "$secrets" && pwd -P)"

    _sc_deny_marketplaces() {
        echo "REFUSING to mount marketplaces: $real" >&2
        echo "  $1" >&2
        echo "  Point SANDBOX_MARKETPLACES_DIR at a directory that holds ONLY" >&2
        echo "  marketplace checkouts (default: $(sc_config_home)/marketplaces)." >&2
        exit 1
    }

    # Read-only keeps the agent from WRITING what it reads; it does nothing about
    # what it can read. So the same over-broad paths sc_guard_workspace refuses
    # are refused here too, for the narrower reason that they would hand the agent
    # the contents of a home directory.
    if [[ "$real" == "/" ]]; then
        _sc_deny_marketplaces "that is the filesystem root."
    elif [[ "$real" == "$real_home" ]]; then
        _sc_deny_marketplaces "that is your home directory."
    elif [[ "$real_home" == "$real"/* ]]; then
        _sc_deny_marketplaces "your home directory ($real_home) is inside it."
    fi

    # A footgun this layout creates rather than one it inherits: the MCP client
    # credentials live next door, under the same config home, so the obvious
    # near-miss (SANDBOX_MARKETPLACES_DIR=~/.config/dockade) would mount the
    # secrets tree into the sandbox. Read-only, which is no comfort at all for a
    # credential — readable IS the compromise.
    if [[ "$secrets" == "$real" || "$secrets" == "$real"/* ]]; then
        _sc_deny_marketplaces "the MCP secrets directory ($secrets) is inside it."
    fi

    # Count what is actually there, so the launch line distinguishes "mounted, 3
    # marketplaces" from "mounted, and empty" — the second looks identical to a
    # working setup from the agent's side until a plugin is missing.
    #
    # Manifests PRESENT, not manifests valid: validating one means parsing JSON,
    # and this runs on the HOST, where jq is not a dependency this repo gets to
    # assume. The boot-side registration does parse them (the image has jq) and
    # warns per unusable file, so the exact figure surfaces there.
    local found=0 d
    if [[ -f "$real/.claude-plugin/marketplace.json" ]]; then
        found=1
    else
        for d in "$real"/*; do
            [[ -f "$d/.claude-plugin/marketplace.json" ]] && found=$((found + 1))
        done
    fi

    # shellcheck disable=SC2034  # both are consumed by the sourcing launcher, not here
    SC_MARKETPLACE_ARGS=(-v "$real":/marketplaces:ro)
    # shellcheck disable=SC2034
    SC_MARKETPLACE_DESC="$real -> /marketplaces:ro ($found marketplace(s))"
    if (( found == 0 )); then
        echo "NOTE: $real holds no .claude-plugin/marketplace.json — mounting it" >&2
        echo "      anyway, but no marketplace will be registered." >&2
    fi
}

# ---------------------------------------------------------------------------
# Plugin allowlist  ->  sets SC_PLUGIN_ARGS (array), SC_PLUGINS_DESC
# ---------------------------------------------------------------------------
# WHICH plugins are enabled is a separate decision from which marketplaces are
# available, and deliberately so: registering a marketplace only makes plugins
# installable, while a plugin loads only if it appears in `enabledPlugins`. It is
# also independent of the mount — an id may name a marketplace the config volume
# already knows, with nothing mounted at all.
#
# It has to be declared host-side because the sandbox's settings.json is
# rewritten from the image on every boot (see claude-sandbox/tier-setup.sh), so a
# plugin enabled in-session does not survive a restart. Without this file the
# feature would ship as "your marketplaces are mounted, now re-enable your
# plugins every session".
#
# Carried as an ENV VAR rather than a second mount, on the invariant that the
# sandbox gets no host path it does not need: the list is a handful of ids, and
# the file is read here, on the host. SANDBOX_PLUGINS overrides the file, which
# keeps the env > file > default precedence the launchers use everywhere else.
#
# Format: one `plugin@marketplace` per line, `#` comments and blank lines
# ignored. Commas are accepted too, because a one-line SANDBOX_PLUGINS is the
# natural way to write it on a command line.
sc_plugin_allowlist() {
    SC_PLUGIN_ARGS=()
    SC_PLUGINS_DESC="none"

    local raw="" file
    file="$(sc_config_home)/plugins"
    if [[ -n "${SANDBOX_PLUGINS:-}" ]]; then
        raw="$SANDBOX_PLUGINS"
    elif [[ -f "$file" ]]; then
        raw="$(sed 's/#.*//' "$file")"
    fi

    # Normalize to a single space-separated line: strip comments (above), split on
    # commas and newlines, collapse whitespace.
    local list
    list="$(printf '%s' "$raw" | tr ',\n\t' '   ' | tr -s ' ' | sed 's/^ *//; s/ *$//')"
    [[ -z "$list" ]] && return 0

    # shellcheck disable=SC2034  # both are consumed by the sourcing launcher, not here
    SC_PLUGIN_ARGS=(-e "SANDBOX_PLUGINS=$list")
    # shellcheck disable=SC2034
    SC_PLUGINS_DESC="$list"
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
# sc_ensure_network <net> <allow_fallback>
#
# docker-compose.yml owns sandbox-net (fixed subnet, internal: true, data-plane
# services on it). Attaching here buys Docker's embedded DNS (127.0.0.11, used
# only on user-defined networks — the firewall already expects it), name
# resolution for data-plane services, and isolation from default-bridge containers.
#
# allow_fallback=true (tier 1): if the network is absent, create a plain
# NON-internal bridge so the sandbox still runs standalone with direct egress via
# its own firewall allowlist. allow_fallback=false (tier 2): a tier with no egress
# has nothing to fall back TO, and silently creating a non-internal bridge would
# hand it the egress its design says it must not have — so refuse instead.
sc_ensure_network() {
    local net="$1" allow_fallback="$2"
    docker network inspect "$net" >/dev/null 2>&1 && return 0

    if [[ "$allow_fallback" != "true" ]]; then
        echo "ERROR: network '$net' not found." >&2
        echo "       Start the infrastructure first:  docker compose up -d" >&2
        echo "       (This tier will not fall back to a non-internal bridge — that" >&2
        echo "        would grant egress its design forbids.)" >&2
        exit 1
    fi
    echo "NOTE: network '$net' not found — creating a plain bridge (no egress proxy)." >&2
    echo "      For governed/audited egress, run 'docker compose up -d' first." >&2
    docker network create "$net" >/dev/null
}

# sc_service_ip <container> <net>  — echoes the IP or nothing.
# Discovering the address (rather than hardcoding it) keeps the launchers working
# even if the compose subnet changes.
sc_service_ip() {
    docker inspect -f \
        "{{with index .NetworkSettings.Networks \"$2\"}}{{.IPAddress}}{{end}}" \
        "$1" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Service readiness
# ---------------------------------------------------------------------------
# Discovering a service's IP proves a container EXISTS, not that it can serve.
# The gap is not cosmetic for either tier:
#   - tier 1: the egress proxy fails CLOSED while the control plane is not yet
#     answering /authorize, so a sandbox launched into the boot window gets real
#     denials written to the audit log — noise that reads exactly like policy.
#   - tier 2: llama-server binds its port immediately but returns 503 until the
#     GGUF is loaded and offloaded, which takes minutes; opencode's first turn
#     would simply fail.
# Both are the same bug — acting on "container up" when we mean "service ready" —
# so both tiers gate through the one helper below.

# sc_health_status <container> — echoes healthy|unhealthy|starting, or NOTHING
# when the container declares no healthcheck (or does not exist).
sc_health_status() {
    docker inspect -f \
        '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
        "$1" 2>/dev/null || true
}

# sc_wait_healthy <container> <why_it_matters> [timeout_seconds]
#
# Block until <container> reports healthy, then return. Fatal on timeout.
#
# An empty health status means the compose entry declares no healthcheck, or the
# service was started by hand — proceed rather than block, because the absence of
# a probe is not evidence of a problem (and gating on a probe that does not exist
# would break every hand-rolled setup).
#
# 'unhealthy' is treated as RETRYABLE, not terminal: Docker flips a container back
# to healthy the moment a probe succeeds, and a model whose load outruns its
# start_period passes through unhealthy on the way up. Only the timeout is fatal.
sc_wait_healthy() {
    local name="$1" why="$2" timeout="${3:-180}"
    local status waited=0 announced=false

    status="$(sc_health_status "$name")"
    if [[ -z "$status" ]]; then
        return 0
    fi

    while [[ "$status" != "healthy" ]]; do
        if (( waited >= timeout )); then
            echo "ERROR: '$name' is still '$status' after ${timeout}s — not launching." >&2
            echo "       $why" >&2
            echo "       Check it with:  docker logs $name" >&2
            exit 1
        fi
        if [[ "$announced" == "false" ]]; then
            echo "Waiting for '$name' to become healthy (currently '$status')..." >&2
            announced=true
        fi
        sleep 2
        waited=$((waited + 2))
        status="$(sc_health_status "$name")"
        # It can vanish mid-wait (crash, or a `compose down` in another terminal).
        if [[ -z "$status" ]]; then
            echo "ERROR: '$name' disappeared while waiting for it to become healthy." >&2
            exit 1
        fi
    done

    if [[ "$announced" == "true" ]]; then
        echo "  '$name' is healthy." >&2
    fi
}

# ---------------------------------------------------------------------------
# Container naming  ->  sets SC_CONTAINER_NAME
# ---------------------------------------------------------------------------
# One or many sandboxes share the infra, so names must not collide. Take the
# default; if taken, pick the next free suffix. (No `docker rm -f` of siblings —
# other sandboxes may be running.)
sc_alloc_container_name() {
    local base="$1" n=2
    if docker ps -a --format '{{.Names}}' | grep -qx "$base"; then
        while docker ps -a --format '{{.Names}}' | grep -qx "${base}-${n}"; do
            n=$((n+1))
        done
        base="${base}-${n}"
    fi
    # shellcheck disable=SC2034  # consumed by the sourcing launcher, not here
    SC_CONTAINER_NAME="$base"
}

# ---------------------------------------------------------------------------
# Git identity  ->  sets SC_GIT_ID_ARGS (array), SC_GIT_NAME, SC_GIT_EMAIL
# ---------------------------------------------------------------------------
# Per-person, so not baked into the image. Read from the host's git config at
# launch and forwarded; the entrypoint materializes it. Absent -> warn, don't
# fail: the container still starts, git only errors at commit time.
sc_git_identity() {
    # shellcheck disable=SC2034  # SC_GIT_ID_ARGS is consumed by the sourcing launcher
    SC_GIT_ID_ARGS=()
    SC_GIT_NAME="$(git config --get user.name || true)"
    SC_GIT_EMAIL="$(git config --get user.email || true)"
    if [[ -n "$SC_GIT_NAME" && -n "$SC_GIT_EMAIL" ]]; then
        # shellcheck disable=SC2034  # consumed by the sourcing launcher, not here
        SC_GIT_ID_ARGS=(-e "GIT_USER_NAME=$SC_GIT_NAME" -e "GIT_USER_EMAIL=$SC_GIT_EMAIL")
    else
        echo "WARNING: no host git identity (user.name/user.email) found;" >&2
        echo "         commits in the sandbox will fail until one is set." >&2
    fi
}

# ---------------------------------------------------------------------------
# DNS  ->  sets SC_UPSTREAM_DNS, SC_DNS_ARGS (array)
# ---------------------------------------------------------------------------
# resolv.conf points at Docker's embedded resolver (127.0.0.11), which resolves
# sibling-container names itself and FORWARDS everything else upstream. Two
# host-specific problems need the same set of resolver IPs, so compute it once:
#   - The upstream the embedded resolver auto-selects on a user-defined network is
#     broken on WSL2 / Docker Desktop (external lookups SERVFAIL even with no
#     firewall), so pin it explicitly via --dns. That keeps resolv.conf as
#     127.0.0.11 (sibling-service discovery preserved) while setting ExtServers.
#   - That forward egresses the in-container firewall's OUTPUT chain, so in
#     STANDALONE mode init-firewall.sh must whitelist the same IPs on port 53
#     (passed as UPSTREAM_DNS) or runtime DNS dies the moment default-deny arms.
#     (Governed mode blocks that forward on purpose — proxied tools hand the
#     hostname to the proxy — and allows only the embedded resolver, which still
#     answers sibling-container names like the proxy's.)
# One list for both means the resolver's upstream and the firewall's DNS allowlist
# can never drift apart. Selection order:
#   1. An explicit SANDBOX_DNS override, for locked-down / corporate / VPN networks
#      where resolvers must be internal ones (and the public fallback would be
#      blocked, or would leak internal hostnames).
#   2. The host's real uplink resolvers. systemd-resolved keeps these in
#      /run/systemd/resolve/resolv.conf; /etc/resolv.conf there is only the stub
#      127.0.0.53 (loopback, useless in the container), so prefer the uplink file
#      and filter loopback either way.
#   3. Docker's public fallback 8.8.8.8/8.8.4.4 — what Docker itself uses when the
#      host has no usable non-loopback resolver (e.g. WSL2 / Docker Desktop).
#
# Tiers with NO egress do not call this: they resolve only sibling names, which
# the embedded resolver answers locally with no upstream forward at all.
sc_upstream_dns() {
    local resolv ns
    if [[ -n "${SANDBOX_DNS:-}" ]]; then
        SC_UPSTREAM_DNS="$SANDBOX_DNS"
    else
        SC_UPSTREAM_DNS=""
        # `|| true` is load-bearing, not defensive habit. The launchers run under
        # `set -euo pipefail`, and awk exits 2 when a file does not exist — which
        # pipefail then propagates out of the pipeline, out of the command
        # substitution, and into the assignment, where set -e kills the launcher
        # outright. The very first candidate is absent on any host without
        # systemd-resolved, so `./run-claude-sandbox.sh` died there with a bare
        # rc=2 and no message, BEFORE the /etc/resolv.conf fallback it was about
        # to try — making both remaining fallbacks unreachable on exactly the
        # hosts they exist for. Silence is the reason this went unnoticed: on a
        # host that HAS the file, nothing is wrong.
        for resolv in /run/systemd/resolve/resolv.conf /etc/resolv.conf; do
            SC_UPSTREAM_DNS="$(awk '/^nameserver/ && $2 !~ /^127\./ {print $2}' "$resolv" 2>/dev/null | tr '\n' ' ' || true)"
            [[ -n "${SC_UPSTREAM_DNS// }" ]] && break
        done
        [[ -z "${SC_UPSTREAM_DNS// }" ]] && SC_UPSTREAM_DNS="8.8.8.8 8.8.4.4"
    fi
    SC_DNS_ARGS=()
    for ns in $SC_UPSTREAM_DNS; do SC_DNS_ARGS+=(--dns "$ns"); done
}

# ---------------------------------------------------------------------------
# Resource ceilings
# ---------------------------------------------------------------------------
# sc_clamp_cpus <requested>   -> sets SC_CPUS
#
# Docker REFUSES a --cpus larger than the host CPU count rather than saturating
# at it ("range of CPUs is from 0.01 to 2.00, as there are only 2 CPUs
# available", exit 125). So an over-large default is not a soft ceiling that
# quietly does nothing on a small host — it is a hard launch failure there, and
# only there. The default of 4 ran green on every dev machine and took CI down
# the first time it met a 2-core runner.
#
# Clamp rather than fail, because this ceiling is BLAST RADIUS, NOT A BOUNDARY
# (see DESIGN.md, "Resource limits"): nothing in the threat model rests on the
# number, so the useful behaviour on a small host is to run with what it has.
# The note goes to stderr because a ceiling silently different from the one you
# asked for is worth seeing once.
#
# Fractional values are legal (SANDBOX_CPUS=1.5), so the comparison runs through
# awk — bash arithmetic is integer-only and would read 1.5 as a syntax error.
# A host without nproc, or one that answers with something other than a number,
# leaves the request untouched and lets docker be the judge.
sc_clamp_cpus() {
    local requested="$1"
    SC_CPUS="$requested"

    local available
    available="$(nproc 2>/dev/null)" || return 0
    [[ "$available" =~ ^[0-9]+$ ]] || return 0

    if awk -v r="$requested" -v a="$available" 'BEGIN { exit !(r > a) }'; then
        echo "NOTE: SANDBOX_CPUS=$requested exceeds the $available CPU(s) on this host;" >&2
        echo "      using $available — docker rejects a --cpus above the host count." >&2
        # shellcheck disable=SC2034  # consumed by the sourcing launcher, not here
        SC_CPUS="$available"
    fi
}

# ---------------------------------------------------------------------------
# Host time zone
# ---------------------------------------------------------------------------
# sc_host_timezone   -> sets SC_TZ
#
# Both launchers pass -e TZ so a timestamp inside the container means what the
# human reading it expects. They used to pass `${TZ:-UTC}`, which is correct on a
# host that exports TZ and wrong on the one this repo is developed on: WSL2 never
# exports it. It carries the zone in /etc/localtime instead — a symlink into
# zoneinfo that WSL keeps in step with Windows — so the fallback fired on every
# launch and the container ran UTC while the host read CEST. Nothing broke; every
# container timestamp simply had to be offset by hand before it could be compared
# with anything from the host, which is a tax paid silently and forever.
#
# Detection order is most-explicit-first. An exported TZ still wins, so an
# operator keeps the override. Then /etc/timezone, which is a bare zone name on
# Debian and Ubuntu. Then the /etc/localtime symlink, the WSL case and the only
# one needing the zoneinfo prefix stripped. UTC last, the honest answer when the
# host says nothing.
#
# Deliberately NOT `timedatectl`: it is the tidy answer and it needs systemd,
# which this launcher has already been bitten by once on hosts without
# systemd-resolved. A file read and a readlink work everywhere.
#
# Only ever a zone NAME, passed as an env var — no host path is mounted to
# achieve it, so "never give the sandbox a direct path to anything" is untouched.
# And a name the image's tzdata does not carry degrades to UTC inside glibc, which
# is where this started, so a bad guess costs nothing.
# shellcheck disable=SC2034  # SC_TZ is consumed by the sourcing launcher, not here
sc_host_timezone() {
    SC_TZ="UTC"

    if [ -n "${TZ:-}" ]; then
        SC_TZ="$TZ"
        return 0
    fi

    local zone
    if [ -r /etc/timezone ]; then
        zone="$(tr -d '[:space:]' < /etc/timezone)"
        if [ -n "$zone" ]; then
            SC_TZ="$zone"
            return 0
        fi
    fi

    if [ -L /etc/localtime ]; then
        zone="$(readlink -f /etc/localtime 2>/dev/null)"
        case "$zone" in
            /usr/share/zoneinfo/?*) SC_TZ="${zone#/usr/share/zoneinfo/}" ;;
        esac
    fi
}
