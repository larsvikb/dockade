#!/bin/bash
# Launch the TIER-1 sandbox: Claude Code, governed egress via the proxy.
# Usage: ./run-claude-sandbox.sh [workspace_path] [--rebuild] [--build-only] [--no-cache]
#
# Mounts the given workspace (default: current dir) at /workspace.
# Claude config/auth persists in the named volume `claude-sandbox-config`,
# isolated from the host's ~/.claude. Authenticate once on first run.
#
# --build-only builds the image and exits WITHOUT launching (no workspace mount,
# no network) — a headless way to assert the image still builds (used by
# `make check`); implies a rebuild.
# --no-cache forces a from-scratch build (bypasses the layer cache); implies a
# rebuild. Combine with --build-only for a clean headless rebuild (`make rebuild`).
#
# Shared plumbing (workspace guard, image build, naming, git identity, DNS) lives
# in sandbox-lib.sh and is identical for every tier. THIS file owns only tier 1's
# capability profile — which is deliberately the whole point of the split:
#
#   TIER 1 GRANT: Anthropic session credentials (in the config volume) and
#   governed egress through the egress proxy (standalone fallback: direct
#   firewall allowlist + upstream DNS). No route to the control plane —
#   sandbox-net only, asserted by `make consistency`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=sandbox-lib.sh
source "$SCRIPT_DIR/sandbox-lib.sh"

# Image tag to build/run. Override with SANDBOX_IMAGE to A/B-test a variant
# (e.g. an alternate base image) without clobbering the default `claude-sandbox`.
IMAGE_NAME="${SANDBOX_IMAGE:-claude-sandbox}"
IMAGE_DIR="claude-sandbox"
CONFIG_VOLUME="claude-sandbox-config"
SANDBOX_NET="sandbox-net"

REBUILD=false
BUILD_ONLY=false
NO_CACHE=false
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
        -*)            echo "Unknown flag: $arg" >&2; exit 1 ;;
        *)             WORKSPACE="$(cd "$arg" && pwd)" ;;
    esac
done

CACHE_FLAG=""
[[ "$NO_CACHE" == "true" ]] && CACHE_FLAG="--no-cache"

# --build-only: assert the image builds, then stop before any launch machinery.
# Deliberately ahead of the workspace guard below — a pure build neither mounts a
# workspace nor needs one, so validating it (and possibly refusing) would be noise.
if [[ "$BUILD_ONLY" == "true" ]]; then
    sc_build_image "$IMAGE_NAME" "$IMAGE_DIR" "$CACHE_FLAG"
    echo "Built $IMAGE_NAME; not launching (--build-only)."
    exit 0
fi

sc_guard_workspace "$WORKSPACE"
WORKSPACE="$SC_WORKSPACE"

if [[ "$REBUILD" == "true" ]] || ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    sc_build_image "$IMAGE_NAME" "$IMAGE_DIR" "$CACHE_FLAG"
fi

# Tier 1 permits the standalone fallback: with no compose infra, a plain
# non-internal bridge still gives direct (firewall-allowlisted, unaudited) egress,
# which is a degraded but useful mode for this tier.
sc_ensure_network "$SANDBOX_NET" true

# Discover the egress proxy on sandbox-net (started by compose). Present ->
# governed egress: route the sandbox's HTTP(S) through it (HTTPS_PROXY) and
# allowlist it in the firewall (EGRESS_PROXY_IP). Absent -> direct egress via the
# firewall only, with a warning.
EGRESS_PROXY_NAME="${EGRESS_PROXY_NAME:-egress-proxy}"
EGRESS_PROXY_PORT="${EGRESS_PROXY_PORT:-8080}"
EGRESS_PROXY_IP="$(sc_service_ip "$EGRESS_PROXY_NAME" "$SANDBOX_NET")"

PROXY_ENV_ARGS=()
if [[ "$EGRESS_PROXY_IP" =~ ^[0-9.]+$ ]]; then
    # The proxy exists, but until the control plane answers /authorize the addon
    # fails CLOSED — so launching now would not stall the agent's first requests,
    # it would DENY them and write those denials to the audit log as if they were
    # policy. Wait for the proxy's healthcheck (which compose in turn gates on the
    # control plane being healthy) so the log stays a record of decisions.
    sc_wait_healthy "$EGRESS_PROXY_NAME" \
        "Governed egress is unavailable, and this tier's traffic all flows through it."

    PROXY_URL="http://${EGRESS_PROXY_NAME}:${EGRESS_PROXY_PORT}"
    PROXY_ENV_ARGS=(
        -e "HTTPS_PROXY=$PROXY_URL"
        -e "HTTP_PROXY=$PROXY_URL"
        -e "NO_PROXY=localhost,127.0.0.1,::1,${EGRESS_PROXY_NAME}"
        -e "EGRESS_PROXY_IP=$EGRESS_PROXY_IP"
        -e "EGRESS_PROXY_PORT=$EGRESS_PROXY_PORT"
    )
    EGRESS_DESC="governed via $EGRESS_PROXY_NAME ($EGRESS_PROXY_IP:$EGRESS_PROXY_PORT)"
else
    # No proxy found. If sandbox-net is internal (the compose infra defines it
    # that way), there is NO egress route at all, so standalone/direct mode can't
    # work — fail with guidance rather than launch a sandbox that reaches nothing.
    if [ "$(docker network inspect "$SANDBOX_NET" -f '{{.Internal}}' 2>/dev/null)" = "true" ]; then
        echo "ERROR: '$SANDBOX_NET' is internal but no egress proxy is running." >&2
        echo "       Start the infrastructure first:  docker compose up -d" >&2
        exit 1
    fi
    EGRESS_DESC="DIRECT (no egress proxy on $SANDBOX_NET; firewall only, no audit)"
fi

sc_alloc_container_name "${SANDBOX_NAME:-claude-sandbox}"
sc_git_identity
sc_upstream_dns

echo "Sandbox:   $SC_CONTAINER_NAME (tier 1 — Claude)"
echo "Workspace: $WORKSPACE -> /workspace"
echo "Git ident: ${SC_GIT_NAME:-<none>} <${SC_GIT_EMAIL:-none}>"
echo "DNS upstreams: $SC_UPSTREAM_DNS (pinned via --dns; whitelisted on :53 in standalone mode only)"
echo "Egress:    $EGRESS_DESC"
echo ""

# Capabilities: cap-drop=ALL, then add back ONLY what the root entrypoint needs
# during setup — NET_ADMIN for the iptables/ipset firewall; CHOWN/DAC_OVERRIDE/
# FOWNER to own /config and materialize config as the sandbox user; SETGID/SETUID
# for the gosu privilege drop. All of these are cleared when gosu drops to the
# non-root agent (the entrypoint asserts the agent ends up with none). NET_RAW is
# deliberately NOT granted: the firewall needs NET_ADMIN, not raw sockets, so
# raw-socket / packet-spoofing capability never exists in the container at all.
docker run -it --rm \
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
    --memory=8g \
    --cpus=4 \
    --pids-limit=512 \
    \
    -v "$WORKSPACE":/workspace \
    -v "$CONFIG_VOLUME":/config \
    \
    -e "TERM=${TERM:-xterm-256color}" \
    -e "TZ=${TZ:-UTC}" \
    -e "UPSTREAM_DNS=$SC_UPSTREAM_DNS" \
    ${PROXY_ENV_ARGS[@]+"${PROXY_ENV_ARGS[@]}"} \
    ${SC_GIT_ID_ARGS[@]+"${SC_GIT_ID_ARGS[@]}"} \
    \
    "$IMAGE_NAME"
