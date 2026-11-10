#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Launch the TIER-1 sandbox: Claude Code, governed egress via the proxy.
# Usage: ./run-claude-sandbox.sh [workspace_path] [--rebuild] [--build-only]
#                                [--no-cache] [--boundary-check]
#
# Mounts the given workspace (default: current dir) at /workspace.
# Claude config/auth persists in the named volume `claude-sandbox-config`,
# isolated from the host's ~/.claude. Authenticate once on first run.
#
# SANDBOX_REFS, a comma-separated list of host directories, mounts each one
# READ-ONLY at /refs/<name> as reference material (see sandbox-lib.sh, sc_refs).
#
# ~/.config/dockade/marketplaces, when it exists, is mounted READ-ONLY at
# /marketplaces and every marketplace in it is registered at boot;
# ~/.config/dockade/plugins lists the `plugin@marketplace` ids to enable. Both
# are optional and neither needs network. See sandbox-lib.sh (sc_marketplaces,
# sc_plugin_allowlist) and claude-sandbox/tier-setup.sh.
#
# --build-only builds the image and exits WITHOUT launching (no workspace mount,
# no network) — a headless way to assert the image still builds (used by
# `make check`); implies a rebuild.
# --no-cache forces a from-scratch build (bypasses the layer cache); implies a
# rebuild. Combine with --build-only for a clean headless rebuild (`make rebuild`).
# --boundary-check launches the container exactly as normal — same network, caps,
# mounts, firewall, privilege drop — but runs boundary-check.sh instead of a shell
# and exits with its verdict. Needs no TTY, so it is how `make check-boundary` (and
# therefore CI) asserts containment; see .github/workflows/check.yml.
#
# Shared plumbing (workspace guard, image build, naming, git identity, DNS) lives
# in sandbox-lib.sh and is identical for every tier, as does the wiring a governed
# tier reaches the data plane through (sc_governed_egress). THIS file owns only
# tier 1's capability profile — which is deliberately the whole point of the split:
#
#   TIER 1 GRANT: Anthropic session credentials (in the config volume) and
#   governed egress through the egress proxy (standalone fallback: direct
#   firewall allowlist + upstream DNS). No route to the control plane —
#   sandbox-net only, asserted by `make consistency`.

set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=sandbox-lib.sh
source "$SCRIPT_DIR/sandbox-lib.sh"

# Image tag to build/run. Override with SANDBOX_IMAGE to A/B-test a variant
# (e.g. an alternate base image) without clobbering the default `claude-sandbox`.
# SANDBOX_JDK=21 adds a JDK and suffixes the tag (`claude-sandbox-jdk21`); see
# sc_jdk_variant and README.md, "Java in the sandbox".
sc_jdk_variant "${SANDBOX_IMAGE:-claude-sandbox}"
IMAGE_NAME="$SC_IMAGE"
IMAGE_DIR="claude-sandbox"
CONFIG_VOLUME="claude-sandbox-config"
SANDBOX_NET="sandbox-net"

# Resource ceilings, overridable per launch:
#     SANDBOX_MEMORY=16g ./run-claude-sandbox.sh
#
# 4g is sized for the WORKLOAD, not the agent — a measured session (linters, a
# 68-test suite, ~25 compiles) peaked at 353 MB, so the agent process itself is
# not what this bounds. What can need the headroom is what the agent RUNS: tsc,
# jest, cargo, a language server on a large tree. Raise it per-workspace when a
# build needs it rather than carrying the worst case as the default; the failure
# mode when it is too low is an OOM-kill of the container, which takes the whole
# interactive session and its context with it.
SANDBOX_MEMORY="${SANDBOX_MEMORY:-4g}"
#
# 4 is a REQUEST, not the final figure: sc_clamp_cpus lowers it to the host's
# CPU count, because docker rejects a --cpus above that rather than saturating.
SANDBOX_CPUS="${SANDBOX_CPUS:-4}"
sc_clamp_cpus "$SANDBOX_CPUS"

REBUILD=false
BUILD_ONLY=false
NO_CACHE=false
BOUNDARY_CHECK=false
WORKSPACE="$PWD"

# The firewall is the real containment boundary, so there is deliberately no
# flag to disable it: a yolo agent with open egress and no boundary is exactly
# the state this sandbox exists to prevent. Debugging without it means editing
# the image, which is a conscious, reviewable act.
for arg in "$@"; do
    case "$arg" in
        --rebuild)     REBUILD=true ;;
        --build-only)  BUILD_ONLY=true; REBUILD=true ;;
        --no-cache)    NO_CACHE=true; REBUILD=true ;;
        --boundary-check) BOUNDARY_CHECK=true ;;
        -*)            echo "Unknown flag: $arg" >&2; exit 1 ;;
        *)             WORKSPACE="$(CDPATH='' cd "$arg" && pwd)" ;;
    esac
done

CACHE_FLAG=""
[[ "$NO_CACHE" == "true" ]] && CACHE_FLAG="--no-cache"

# --build-only: assert the image builds, then stop before any launch machinery.
# Deliberately ahead of the workspace guard below — a pure build neither mounts a
# workspace nor needs one, so validating it (and possibly refusing) would be noise.
if [[ "$BUILD_ONLY" == "true" ]]; then
    sc_build_image "$IMAGE_NAME" "$IMAGE_DIR" "$CACHE_FLAG" ${SC_BUILD_ARGS[@]+"${SC_BUILD_ARGS[@]}"}
    echo "Built $IMAGE_NAME; not launching (--build-only)."
    exit 0
fi

sc_guard_workspace "$WORKSPACE"
WORKSPACE="$SC_WORKSPACE"
sc_refs

if [[ "$REBUILD" == "true" ]] || ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    sc_build_image "$IMAGE_NAME" "$IMAGE_DIR" "$CACHE_FLAG" ${SC_BUILD_ARGS[@]+"${SC_BUILD_ARGS[@]}"}
fi

# Tier 1 permits the standalone fallback: with no compose infra, a plain
# non-internal bridge still gives direct (firewall-allowlisted, unaudited) egress,
# which is a degraded but useful mode for this tier.
sc_ensure_network "$SANDBOX_NET" true

# Governed egress and, in governed mode, the tool gateway: see sandbox-lib.sh.
sc_governed_egress "$SANDBOX_NET"

sc_alloc_container_name "${SANDBOX_NAME:-claude-sandbox}"
sc_git_identity
sc_upstream_dns
sc_host_timezone

# Plugin marketplaces and the enabled-plugin allowlist: tier 1 only, because
# Claude Code plugins are Claude Code's mechanism — tier 2 runs opencode and has
# nothing to do with them. Both are host-side reads with no egress involved; see
# sandbox-lib.sh for why the mount is read-only and why the allowlist travels as
# an env var rather than a second mount.
sc_marketplaces "$WORKSPACE"
sc_plugin_allowlist

echo "Sandbox:   $SC_CONTAINER_NAME (tier 1 — Claude)"
echo "Image:     $IMAGE_NAME"
echo "Workspace: $WORKSPACE -> /workspace"
echo "Refs:      $SC_REFS_DESC"
echo "Markets:   $SC_MARKETPLACE_DESC"
echo "Plugins:   $SC_PLUGINS_DESC"
echo "Git ident: ${SC_GIT_NAME:-<none>} <${SC_GIT_EMAIL:-none}>"
echo "DNS upstreams: $SC_UPSTREAM_DNS (pinned via --dns; whitelisted on :53 in standalone mode only)"
echo "Egress:    $SC_EGRESS_DESC"
echo "Tools:     $SC_TOOLS_DESC"
echo ""

# Launch mode. Everything below this — network, capabilities, mounts, firewall,
# privilege drop — is IDENTICAL in both modes on purpose: a boundary check is
# worth something only if it runs against the real boundary, so the modes may
# differ ONLY in whether a TTY is attached and in what runs after the
# entrypoint's gosu drop.
#
#   default          -it, image CMD (`bash`) -> the interactive shell the
#                    operator starts the agent from.
#   --boundary-check no TTY, command overridden to boundary-check.sh. One-shot:
#                    the container exits with the check's verdict, and `docker
#                    run` hands that back as this script's exit status.
#
# ONE-SHOT, rather than "start an idle container and `docker exec` the check into
# it" — which is the obvious shape and the worse one, for two reasons.
#
# The overridden command replaces only CMD (the Dockerfile sets ENTRYPOINT
# separately), so the entrypoint still arms the firewall, asserts the agent holds
# no capabilities, and drops privileges. boundary-check.sh therefore runs as the
# gosu-dropped CHILD of that entrypoint: the same process lineage, and the same
# environment, an agent gets. A `docker exec` is instead a sibling Docker builds
# from the container's stored config — it inherits the `-e` variables but NOT
# anything the entrypoint exported at runtime (SANDBOX_CONFIG_DIR, HOME, USER),
# so a probe that branched on one of those would quietly take the wrong branch
# and still report PASS. Nothing in boundary-check.sh reads them today; the point
# is that this mode cannot grow that bug, and the exec shape can.
#
# Second, the entrypoint's output and the probe results arrive in ONE ordered
# stream on success and failure alike. That matters most in the case that matters
# most: the entrypoint is designed to abort when the firewall cannot be armed, and
# here that abort IS the output, rather than something left behind in `docker logs`
# for someone to know to go and find. It also makes --rm safe to keep.
RUN_MODE_ARGS=(-it --rm)
RUN_CMD_ARGS=()
if [[ "$BOUNDARY_CHECK" == "true" ]]; then
    RUN_MODE_ARGS=(--rm)
    RUN_CMD_ARGS=(/usr/local/bin/boundary-check.sh)
fi

# Capabilities: cap-drop=ALL, then add back ONLY what the root entrypoint needs
# during setup — NET_ADMIN for the iptables/ipset firewall; CHOWN/DAC_OVERRIDE/
# FOWNER to own /config and materialize config as the sandbox user; SETGID/SETUID
# for the gosu privilege drop. All of these are cleared when gosu drops to the
# non-root agent (the entrypoint asserts the agent ends up with none). NET_RAW is
# deliberately NOT granted: the firewall needs NET_ADMIN, not raw sockets, so
# raw-socket / packet-spoofing capability never exists in the container at all.
#
# Resources: --memory-swap is set EQUAL to --memory, which disables swap and makes
# the cap hard. Docker otherwise defaults swap to 2x memory, so a bare --memory=4g
# would really mean 4g RAM + 4g swap — on a 15 GiB host that is a ceiling above
# what exists, which is no ceiling at all. Both flags read the same variable, so
# they cannot drift apart. (Note --cpus is a CFS quota, not a cpuset: `nproc` still
# reports every host CPU, so build tools that size their worker pool from it will
# oversubscribe and get throttled.)
docker run "${RUN_MODE_ARGS[@]}" \
    --name "$SC_CONTAINER_NAME" \
    --hostname sandbox \
    --network "$SANDBOX_NET" \
    ${SC_DNS_ARGS[@]+"${SC_DNS_ARGS[@]}"} \
    --user root \
    \
    --cap-drop=ALL \
    --cap-add=NET_ADMIN \
    --cap-add=CHOWN \
    --cap-add=DAC_OVERRIDE \
    --cap-add=FOWNER \
    --cap-add=SETGID \
    --cap-add=SETUID \
    --security-opt no-new-privileges \
    \
    --memory="$SANDBOX_MEMORY" \
    --memory-swap="$SANDBOX_MEMORY" \
    --cpus="$SC_CPUS" \
    --pids-limit=512 \
    \
    -v "$WORKSPACE":/workspace \
    -v "$CONFIG_VOLUME":/config \
    ${SC_MARKETPLACE_ARGS[@]+"${SC_MARKETPLACE_ARGS[@]}"} \
    ${SC_REFS_ARGS[@]+"${SC_REFS_ARGS[@]}"} \
    \
    -e "TERM=${TERM:-xterm-256color}" \
    -e "TZ=$SC_TZ" \
    -e "UPSTREAM_DNS=$SC_UPSTREAM_DNS" \
    ${SC_PLUGIN_ARGS[@]+"${SC_PLUGIN_ARGS[@]}"} \
    ${SC_PROXY_ENV_ARGS[@]+"${SC_PROXY_ENV_ARGS[@]}"} \
    ${SC_GIT_ID_ARGS[@]+"${SC_GIT_ID_ARGS[@]}"} \
    \
    "$IMAGE_NAME" ${RUN_CMD_ARGS[@]+"${RUN_CMD_ARGS[@]}"}
