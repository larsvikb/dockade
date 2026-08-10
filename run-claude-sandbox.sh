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
    NO_PROXY_LIST="localhost,127.0.0.1,::1,${EGRESS_PROXY_NAME}"
    # BOTH CASES of each, and the lowercase ones are not redundant styling.
    #
    # curl reads `http_proxy` in LOWER CASE ONLY, while honouring HTTPS_PROXY and
    # NO_PROXY in either case. That is deliberate, not an oversight: under CGI a
    # client-supplied `Proxy:` request header arrives in the environment as
    # HTTP_PROXY, so honouring it would let a remote caller redirect a server's
    # outbound HTTP through a proxy of their choosing (httpoxy, CVE-2016-5385).
    #
    # With only the uppercase set, `curl http://...` in the sandbox did not reach the
    # governed proxy at all: it resolved the target itself and died at DNS in about a
    # millisecond. Containment held — sandbox-net is internal, so there was nowhere
    # to go — but the agent got "Could not resolve host" instead of a hold it could
    # have had approved, and the attempt reached no audit log, because it never
    # reached the control plane. HTTPS was unaffected, which is why it went unnoticed
    # until a decisions-table row showed a plaintext request that had never arrived.
    #
    # Setting both cases is the conventional pairing and covers other tools with the
    # same rule. `make consistency` asserts the pairing so it cannot drift back.
    PROXY_ENV_ARGS=(
        -e "HTTPS_PROXY=$PROXY_URL"
        -e "HTTP_PROXY=$PROXY_URL"
        -e "NO_PROXY=$NO_PROXY_LIST"
        -e "https_proxy=$PROXY_URL"
        -e "http_proxy=$PROXY_URL"
        -e "no_proxy=$NO_PROXY_LIST"
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
sc_host_timezone

echo "Sandbox:   $SC_CONTAINER_NAME (tier 1 — Claude)"
echo "Workspace: $WORKSPACE -> /workspace"
echo "Git ident: ${SC_GIT_NAME:-<none>} <${SC_GIT_EMAIL:-none}>"
echo "DNS upstreams: $SC_UPSTREAM_DNS (pinned via --dns; whitelisted on :53 in standalone mode only)"
echo "Egress:    $EGRESS_DESC"
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
    \
    -e "TERM=${TERM:-xterm-256color}" \
    -e "TZ=$SC_TZ" \
    -e "UPSTREAM_DNS=$SC_UPSTREAM_DNS" \
    ${PROXY_ENV_ARGS[@]+"${PROXY_ENV_ARGS[@]}"} \
    ${SC_GIT_ID_ARGS[@]+"${SC_GIT_ID_ARGS[@]}"} \
    \
    "$IMAGE_NAME" ${RUN_CMD_ARGS[@]+"${RUN_CMD_ARGS[@]}"}
