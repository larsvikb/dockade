#!/bin/bash
# Launch the TIER-2 sandbox: opencode driven by the local LLM. No egress.
# Usage: ./run-opencode-sandbox.sh [workspace_path] [--rebuild] [--build-only] [--no-cache]
#
# Shared plumbing (workspace guard, image build, naming, git identity) lives in
# sandbox-lib.sh and is identical for every tier. THIS file owns only tier 2's
# capability profile, which is defined by subtraction:
#
#   TIER 2 GRANT: the local inference service on sandbox-net, and nothing else.
#   NO credentials of any kind (no Anthropic session, no tokens). NO egress,
#   governed or direct — so no proxy env, no upstream DNS. No route to the
#   control plane. The workspace bind mount is the only host coupling.
#
# That makes this the strongest available test of the containment boundary: run
# boundary-check.sh inside it and everything should fail except the inference
# service. See DESIGN.md "Local inference".
#
# --build-only builds the image and exits WITHOUT launching; implies a rebuild.
# --no-cache forces a from-scratch build; implies a rebuild.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=sandbox-lib.sh
source "$SCRIPT_DIR/sandbox-lib.sh"

IMAGE_NAME="${OPENCODE_SANDBOX_IMAGE:-opencode-sandbox}"
IMAGE_DIR="opencode-sandbox"
CONFIG_VOLUME="opencode-sandbox-config"
SANDBOX_NET="sandbox-net"

# Resource ceilings, overridable per launch:
#     SANDBOX_MEMORY=8g ./run-opencode-sandbox.sh
#
# Deliberately HALF of tier 1's default, for two tier-specific reasons rather than
# thrift. opencode is a thin client here — inference runs in the `llm` service, so
# the model's memory is that container's problem, not this one's. And this tier has
# no egress, so `npm install` / `pip install` cannot fetch: its workload physically
# cannot grow the way tier 1's can, because it can only ever run what is already in
# the image or the workspace.
SANDBOX_MEMORY="${SANDBOX_MEMORY:-2g}"
SANDBOX_CPUS="${SANDBOX_CPUS:-4}"

REBUILD=false
BUILD_ONLY=false
NO_CACHE=false
WORKSPACE="$PWD"

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

# No standalone fallback for this tier. A non-internal bridge would hand a
# no-egress agent the egress its whole design forbids, so refuse instead.
sc_ensure_network "$SANDBOX_NET" false

# The inference service is this tier's ONLY permitted destination, so its absence
# is fatal rather than degraded: an opencode sandbox with no model is not a
# reduced-capability sandbox, it is a broken one. Discover the address (rather
# than hardcoding it) so a compose subnet change cannot silently break the
# firewall's /32.
LLM_NAME="${LLM_NAME:-llm}"
LLM_PORT="${LLM_PORT:-8080}"
LLM_IP="$(sc_service_ip "$LLM_NAME" "$SANDBOX_NET")"
if [[ ! "$LLM_IP" =~ ^[0-9.]+$ ]]; then
    # The alias `llm` is shared by every accelerator profile, so resolve the
    # concrete container behind it for the address lookup.
    for candidate in llm-intel llm-nvidia llm-vulkan; do
        LLM_IP="$(sc_service_ip "$candidate" "$SANDBOX_NET")"
        [[ "$LLM_IP" =~ ^[0-9.]+$ ]] && { LLM_NAME="$candidate"; break; }
    done
fi
if [[ ! "$LLM_IP" =~ ^[0-9.]+$ ]]; then
    echo "ERROR: no local inference service found on $SANDBOX_NET." >&2
    echo "       Tier 2 has no egress and no other capability — it cannot run" >&2
    echo "       without a model. Start one first:" >&2
    echo "         docker compose --profile llm-intel  up -d llm-intel   # Intel/WSL" >&2
    echo "         docker compose --profile llm-nvidia up -d llm-nvidia  # NVIDIA" >&2
    echo "         docker compose --profile llm-vulkan up -d llm-vulkan  # AMD/Intel, native Linux" >&2
    exit 1
fi

# Found it — but llama-server binds its port long before it can serve, returning
# 503 while the GGUF loads and offloads to the GPU (minutes, not seconds). Since
# inference is this tier's ONLY capability, starting opencode against a
# still-loading server means its very first turn fails. Wait it out instead. The
# timeout is generous for the same reason the healthcheck's start_period is.
sc_wait_healthy "$LLM_NAME" \
    "Inference is this tier's only capability — opencode cannot do anything without it." \
    600

sc_alloc_container_name "${SANDBOX_NAME:-opencode-sandbox}"
sc_git_identity

echo "Sandbox:   $SC_CONTAINER_NAME (tier 2 — opencode, local model)"
echo "Workspace: $WORKSPACE -> /workspace"
echo "Git ident: ${SC_GIT_NAME:-<none>} <${SC_GIT_EMAIL:-none}>"
echo "Inference: $LLM_NAME ($LLM_IP:$LLM_PORT) — the ONLY permitted destination"
echo "Egress:    NONE (no proxy, no direct; no credentials in this tier)"
echo ""

# Capabilities: identical to tier 1 — cap-drop=ALL plus only what the root
# entrypoint needs before the gosu drop. NET_ADMIN for the firewall;
# CHOWN/DAC_OVERRIDE/FOWNER to own /config and materialize config; SETGID/SETUID
# for the privilege drop. All cleared when gosu drops to the non-root agent (the
# shared entrypoint asserts the agent ends up holding none).
#
# Deliberately absent versus tier 1: HTTP(S)_PROXY, EGRESS_PROXY_IP, UPSTREAM_DNS
# and any --dns flags. This tier resolves only sibling names, which Docker's
# embedded resolver answers locally with no upstream forward — so there is no
# upstream to pin and no DNS-exfiltration channel to narrow.
#
# Resources: --memory-swap equals --memory so the cap is hard rather than silently
# doubled into swap (same reasoning as tier 1).
docker run -it --rm \
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
    --cpus="$SANDBOX_CPUS" \
    --pids-limit=512 \
    \
    -v "$WORKSPACE":/workspace \
    -v "$CONFIG_VOLUME":/config \
    \
    -e "TERM=${TERM:-xterm-256color}" \
    -e "TZ=${TZ:-UTC}" \
    -e "SANDBOX_MODE=local" \
    -e "LLM_IP=$LLM_IP" \
    -e "LLM_PORT=$LLM_PORT" \
    ${SC_GIT_ID_ARGS[@]+"${SC_GIT_ID_ARGS[@]}"} \
    \
    "$IMAGE_NAME"
