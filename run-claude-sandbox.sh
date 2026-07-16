#!/bin/bash
# Launch the Claude sandbox container.
# Usage: ./run-claude-sandbox.sh [workspace_path] [--rebuild]
#
# Mounts the given workspace (default: current dir) at /workspace.
# Claude config/auth persists in the named volume `claude-sandbox-config`,
# isolated from the host's ~/.claude. Authenticate once on first run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="claude-sandbox"
CONFIG_VOLUME="claude-sandbox-config"
SANDBOX_NET="sandbox-net"
# CONTAINER_NAME is chosen later (after proxy discovery) so multiple sandboxes
# can share the compose infra without name collisions.

REBUILD=false
WORKSPACE="$PWD"

# The firewall is the real containment boundary, so there is deliberately no
# flag to disable it: a yolo agent with open egress and no boundary is exactly
# the state this sandbox exists to prevent. Debugging without it means editing
# the image, which is a conscious, reviewable act.
for arg in "$@"; do
    case "$arg" in
        --rebuild)     REBUILD=true ;;
        -*)            echo "Unknown flag: $arg" >&2; exit 1 ;;
        *)             WORKSPACE="$(cd "$arg" && pwd)" ;;
    esac
done

# Workspace safety guard. The workspace is bind-mounted RW with the sandbox user
# matched to the host uid, so the yolo agent gets full read/write to whatever this
# resolves to — and anything it writes there (git hooks, build scripts, .envrc,
# editor task files) later runs OUTSIDE the sandbox when the host next touches the
# repo. Mounting a home directory or the filesystem root would hand the agent host
# credentials (~/.ssh, ~/.aws, ...) plus a boundary-crossing foothold, defeating
# the point of the sandbox. So: hard-refuse the clearly-dangerous roots, and warn
# (non-fatal) when credential material lives inside the chosen workspace. Override
# the hard refusal with ALLOW_UNSAFE_WORKSPACE=1 for the rare deliberate case — a
# conscious, visible choice, in keeping with the "no silent unsafe defaults" ethos.
REAL_WORKSPACE="$(cd "$WORKSPACE" && pwd -P)"   # canonical, symlinks resolved
REAL_HOME="$(cd "$HOME" 2>/dev/null && pwd -P || echo "$HOME")"

deny_workspace() {
    echo "REFUSING to mount workspace: $REAL_WORKSPACE" >&2
    echo "  $1" >&2
    echo "  This would give the yolo agent RW access to sensitive host files, and" >&2
    echo "  anything it writes there runs on the host later (git hooks, build scripts)." >&2
    echo "  Re-run from a dedicated project directory, or set ALLOW_UNSAFE_WORKSPACE=1" >&2
    echo "  to override deliberately." >&2
    exit 1
}

if [[ "${ALLOW_UNSAFE_WORKSPACE:-}" != "1" ]]; then
    if [[ "$REAL_WORKSPACE" == "/" ]]; then
        deny_workspace "that is the filesystem root."
    elif [[ "$REAL_WORKSPACE" == "$REAL_HOME" ]]; then
        deny_workspace "that is your home directory."
    elif [[ "$REAL_HOME" == "$REAL_WORKSPACE"/* ]]; then
        # Workspace is an ancestor of $HOME, so mounting it exposes the whole home.
        deny_workspace "your home directory ($REAL_HOME) is inside it."
    fi
fi

# Non-fatal: credential material sitting inside the chosen workspace. This is
# legal (you may genuinely want to work there), but the agent will be able to
# read and modify it, so make that visible rather than silent.
for sensitive in .ssh .aws .gnupg .config/gcloud .kube .docker/config.json .netrc .git-credentials; do
    if [[ -e "$REAL_WORKSPACE/$sensitive" ]]; then
        echo "WARNING: workspace contains '$sensitive' — the sandbox agent will have RW access to it." >&2
    fi
done
WORKSPACE="$REAL_WORKSPACE"

if [[ "$REBUILD" == "true" ]] || ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "Building $IMAGE_NAME (sandbox user uid/gid $(id -u)/$(id -g))..."
    # Match the sandbox user to the host uid/gid so bind-mounted workspaces are
    # writable from both sides (avoids the drvfs/uid mismatch on WSL). Rebuild
    # (--rebuild) after switching hosts if the host uid differs.
    docker build -t "$IMAGE_NAME" \
        --build-arg USER_UID="$(id -u)" \
        --build-arg USER_GID="$(id -g)" \
        "$SCRIPT_DIR/claude-sandbox"
fi

# Infrastructure network. docker-compose.yml owns sandbox-net (with a fixed
# subnet and the egress proxy on it) — bring the infra up first:
#     docker compose up -d
# The sandbox attaches to that shared network here. Even single-container this
# buys: Docker's embedded DNS (127.0.0.11, used only on user-defined networks —
# the firewall already expects it), name resolution for the data-plane services,
# and isolation from other default-bridge containers.
#
# If the network is absent, fall back to a plain bridge so the sandbox still runs
# STANDALONE (direct egress via its firewall, no proxy audit) — this launcher is
# useful with or without the compose infra. NOT `internal: true` yet: the end
# state is an internal sandbox-net with no direct egress, but that needs the
# proxy proven as the sole path first (see DESIGN.md); flip at step 3.
if ! docker network inspect "$SANDBOX_NET" >/dev/null 2>&1; then
    echo "NOTE: network '$SANDBOX_NET' not found — creating a plain bridge (no egress proxy)." >&2
    echo "      For governed/audited egress, run 'docker compose up -d' first." >&2
    docker network create "$SANDBOX_NET" >/dev/null
fi

# Discover the egress proxy on sandbox-net (started by compose). Present ->
# governed egress: route the sandbox's HTTP(S) through it (HTTPS_PROXY) and
# allowlist it in the firewall (EGRESS_PROXY_IP). Absent -> direct egress via the
# firewall only, with a warning. Discovering the IP (rather than hardcoding it)
# keeps this working even if the compose subnet/address changes.
EGRESS_PROXY_NAME="${EGRESS_PROXY_NAME:-egress-proxy}"
EGRESS_PROXY_PORT="${EGRESS_PROXY_PORT:-8080}"
EGRESS_PROXY_IP="$(docker inspect -f \
    "{{with index .NetworkSettings.Networks \"$SANDBOX_NET\"}}{{.IPAddress}}{{end}}" \
    "$EGRESS_PROXY_NAME" 2>/dev/null || true)"

PROXY_ENV_ARGS=()
if [[ "$EGRESS_PROXY_IP" =~ ^[0-9.]+$ ]]; then
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
    EGRESS_DESC="DIRECT (no egress proxy on $SANDBOX_NET; firewall only, no audit)"
fi

# One or many sandboxes share the infra, so container names must not collide.
# Default to claude-sandbox; if taken, pick the next free suffix. Override with
# SANDBOX_NAME. (No `docker rm -f` of siblings — other sandboxes may be running.)
CONTAINER_NAME="${SANDBOX_NAME:-claude-sandbox}"
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    n=2
    while docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}-${n}"; do
        n=$((n+1))
    done
    CONTAINER_NAME="${CONTAINER_NAME}-${n}"
fi

# Git identity is per-person, so it is not baked into the image. Read it from the
# host's git config at launch and forward it; the entrypoint materializes it into
# the sandbox user's git config. Absent on the host -> warn, don't fail: the
# container still starts; git only errors at commit time ("Author identity unknown").
GIT_ID_ARGS=()
HOST_GIT_NAME="$(git config --get user.name || true)"
HOST_GIT_EMAIL="$(git config --get user.email || true)"
if [[ -n "$HOST_GIT_NAME" && -n "$HOST_GIT_EMAIL" ]]; then
    GIT_ID_ARGS=(-e "GIT_USER_NAME=$HOST_GIT_NAME" -e "GIT_USER_EMAIL=$HOST_GIT_EMAIL")
else
    echo "WARNING: no host git identity (user.name/user.email) found;" >&2
    echo "         commits in the sandbox will fail until one is set." >&2
fi

# DNS on sandbox-net. resolv.conf points at Docker's embedded resolver
# (127.0.0.11), which resolves sibling-container names itself and FORWARDS
# everything else upstream. Two host-specific problems have to be solved with the
# same set of resolver IPs, so compute it once here:
#   - The upstream the embedded resolver auto-selects on a user-defined network
#     is broken on WSL2 / Docker Desktop (external lookups SERVFAIL even with no
#     firewall), so we must pin it explicitly via --dns below. That keeps
#     resolv.conf as 127.0.0.11 (sibling-service discovery preserved) while
#     setting the forward target (ExtServers).
#   - That forward egresses the in-container firewall's OUTPUT chain, so
#     init-firewall.sh must whitelist the same IPs on port 53 (passed in as
#     UPSTREAM_DNS) or runtime DNS dies the moment default-deny arms.
# Using one list for both means the resolver's upstream and the firewall's DNS
# allow-list can never drift apart. Selection order:
#   1. An explicit SANDBOX_DNS override, for locked-down / corporate / VPN
#      networks where the resolvers must be internal ones (and where the public
#      fallback below would be blocked, or would leak internal hostnames).
#   2. The host's real uplink resolvers. systemd-resolved keeps these in
#      /run/systemd/resolve/resolv.conf; /etc/resolv.conf there is only the stub
#      127.0.0.53 (loopback, useless inside the container), so prefer the uplink
#      file and filter loopback either way.
#   3. Docker's public fallback 8.8.8.8/8.8.4.4 — what Docker itself uses when
#      the host has no usable non-loopback resolver (e.g. WSL2 / Docker Desktop).
if [[ -n "${SANDBOX_DNS:-}" ]]; then
    UPSTREAM_DNS="$SANDBOX_DNS"
else
    UPSTREAM_DNS=""
    for resolv in /run/systemd/resolve/resolv.conf /etc/resolv.conf; do
        UPSTREAM_DNS="$(awk '/^nameserver/ && $2 !~ /^127\./ {print $2}' "$resolv" 2>/dev/null | tr '\n' ' ')"
        [[ -n "${UPSTREAM_DNS// }" ]] && break
    done
    [[ -z "${UPSTREAM_DNS// }" ]] && UPSTREAM_DNS="8.8.8.8 8.8.4.4"
fi
DNS_ARGS=()
for ns in $UPSTREAM_DNS; do DNS_ARGS+=(--dns "$ns"); done

echo "Sandbox:   $CONTAINER_NAME"
echo "Workspace: $WORKSPACE -> /workspace"
echo "Git ident: ${HOST_GIT_NAME:-<none>} <${HOST_GIT_EMAIL:-none}>"
echo "DNS upstreams: $UPSTREAM_DNS (pinned via --dns; firewall-whitelisted on :53)"
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
    --name "$CONTAINER_NAME" \
    --hostname sandbox \
    --network "$SANDBOX_NET" \
    ${DNS_ARGS[@]+"${DNS_ARGS[@]}"} \
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
    -e "UPSTREAM_DNS=$UPSTREAM_DNS" \
    ${PROXY_ENV_ARGS[@]+"${PROXY_ENV_ARGS[@]}"} \
    ${GIT_ID_ARGS[@]+"${GIT_ID_ARGS[@]}"} \
    \
    "$IMAGE_NAME"
