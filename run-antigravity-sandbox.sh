#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Launch a TIER-1 sandbox for Google's Antigravity CLI, `agy`: governed egress via the
# proxy, MCP tools via the gateway.
# Usage: ./run-antigravity-sandbox.sh [workspace_path] [--rebuild] [--build-only]
#                                     [--no-cache] [--boundary-check]
#
# Mounts the given workspace (default: current dir) at /workspace/<folder-name>,
# and starts there (sandbox-lib.sh, sc_workspace_mount).
# agy's login, settings and conversations persist in the named volume
# `antigravity-sandbox-config`, which no other tier mounts. Log in once on first run.
#
# SANDBOX_REFS, a comma-separated list of host directories, mounts each one
# READ-ONLY at /refs/<name> as reference material (see sandbox-lib.sh, sc_refs).
#
# ~/.config/dockade/agy/AGENTS.md, when it exists, is mounted READ-ONLY at
# /operator/AGENTS.md and appended to the baked ~/.gemini/AGENTS.md at boot
# (sandbox-lib.sh, sc_operator_config; sandbox-common/operator-instructions.sh).
#
# The flags are run-claude-sandbox.sh's, and that file's comments say what each does
# and why --boundary-check runs the check as the container's command.
#
# Shared plumbing lives in sandbox-lib.sh and is identical for every tier, as is the
# wiring a governed tier reaches the data plane through (sc_governed_egress). THIS
# file owns only this tier's capability profile:
#
#   GRANT: a Google session login (in the config volume) and governed egress through
#   the egress proxy, with the tool gateway when one is up. NO standalone fallback:
#   with no proxy there is no route, so no upstream DNS either. No API key or
#   application-default credentials are passed. No route to the control plane —
#   sandbox-net only, asserted by `make consistency`.

set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=sandbox-lib.sh
source "$SCRIPT_DIR/sandbox-lib.sh"

IMAGE_NAME="${ANTIGRAVITY_SANDBOX_IMAGE:-antigravity-sandbox}"
IMAGE_DIR="antigravity-sandbox"
CONFIG_VOLUME="antigravity-sandbox-config"
SANDBOX_NET="sandbox-net"

# Resource ceilings, overridable per launch, sized as tier 1's are in
# run-claude-sandbox.sh: for the workload the agent runs, not for the agent.
SANDBOX_MEMORY="${SANDBOX_MEMORY:-4g}"
SANDBOX_CPUS="${SANDBOX_CPUS:-4}"
sc_clamp_cpus "$SANDBOX_CPUS"

REBUILD=false
BUILD_ONLY=false
NO_CACHE=false
BOUNDARY_CHECK=false
WORKSPACE="$PWD"

# No flag disables the firewall, as in every tier.
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

if [[ "$BUILD_ONLY" == "true" ]]; then
    sc_build_image "$IMAGE_NAME" "$IMAGE_DIR" "$CACHE_FLAG"
    echo "Built $IMAGE_NAME; not launching (--build-only)."
    exit 0
fi

sc_guard_workspace "$WORKSPACE"
WORKSPACE="$SC_WORKSPACE"
sc_workspace_mount "$WORKSPACE"
sc_refs
sc_operator_config agy "$WORKSPACE" AGENTS.md

if [[ "$REBUILD" == "true" ]] || ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    sc_build_image "$IMAGE_NAME" "$IMAGE_DIR" "$CACHE_FLAG"
fi

# No standalone fallback: that mode's firewall allowlist is Anthropic's, so this tier
# would have no model to reach, and creating a non-internal bridge would hand it
# egress no proxy audits. Refuse instead.
sc_ensure_network "$SANDBOX_NET" false

# Nothing but the egress proxy and the tool gateway may sit on both this network and
# mcp-net: see sandbox-lib.sh.
sc_refuse_dual_homed "$SANDBOX_NET"

# Governed egress and the tool gateway: see sandbox-lib.sh. The model host is where
# agy's inference goes (NOTES.md, "agy behind dockade's proxy, measured before its
# tier"); boundary-check.sh asserts it answers through the proxy.
sc_governed_egress "$SANDBOX_NET" daily-cloudcode-pa.googleapis.com

sc_alloc_container_name "${SANDBOX_NAME:-antigravity-sandbox}"
sc_git_identity
sc_host_timezone

echo "Sandbox:   $SC_CONTAINER_NAME (tier 1 — Antigravity)"
echo "Image:     $IMAGE_NAME"
echo "Workspace: $WORKSPACE -> $SC_WORKSPACE_MOUNT"
echo "Refs:      $SC_REFS_DESC"
[[ -z "$SC_CUSTOM_DESC" ]] || echo "Custom:    $SC_CUSTOM_DESC"
echo "Git ident: ${SC_GIT_NAME:-<none>} <${SC_GIT_EMAIL:-none}>"
echo "Egress:    $SC_EGRESS_DESC"
# Nothing added when there is no gateway: agy has no strict MCP mode, so a workspace
# .agents/mcp_config.json still loads whatever it names.
echo "Tools:     $SC_TOOLS_DESC"
echo ""

# Launch mode, as in run-claude-sandbox.sh: the two modes differ only in the TTY and
# in what runs after the entrypoint's privilege drop.
RUN_MODE_ARGS=(-it --rm)
RUN_CMD_ARGS=()
if [[ "$BOUNDARY_CHECK" == "true" ]]; then
    RUN_MODE_ARGS=(--rm)
    RUN_CMD_ARGS=(/usr/local/bin/boundary-check.sh)
fi

# Capabilities and resources exactly as run-claude-sandbox.sh grants them, whose
# comment says why each is there; tests/test_launcher_run_flags.py holds every
# launcher to them.
docker run "${RUN_MODE_ARGS[@]}" \
    --name "$SC_CONTAINER_NAME" \
    --hostname sandbox \
    --network "$SANDBOX_NET" \
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
    -v "$WORKSPACE":"$SC_WORKSPACE_MOUNT" \
    -w "$SC_WORKSPACE_MOUNT" \
    -v "$CONFIG_VOLUME":/config \
    ${SC_REFS_ARGS[@]+"${SC_REFS_ARGS[@]}"} \
    ${SC_OPERATOR_ARGS[@]+"${SC_OPERATOR_ARGS[@]}"} \
    \
    -e "TERM=${TERM:-xterm-256color}" \
    -e "TZ=$SC_TZ" \
    ${SC_PROXY_ENV_ARGS[@]+"${SC_PROXY_ENV_ARGS[@]}"} \
    ${SC_GIT_ID_ARGS[@]+"${SC_GIT_ID_ARGS[@]}"} \
    \
    "$IMAGE_NAME" ${RUN_CMD_ARGS[@]+"${RUN_CMD_ARGS[@]}"}
