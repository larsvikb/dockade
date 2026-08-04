#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# Runs as root. Sets up the network boundary, then drops to the non-root
# sandbox user for everything else.
#
# SHARED BY EVERY SANDBOX TIER (claude-sandbox, opencode-sandbox). Everything here
# is tier-agnostic: config ownership, git identity, the firewall, the capability
# assertion, and the privilege drop. Anything specific to one agent belongs in
# that image's /usr/local/bin/tier-setup.sh hook (invoked below), NOT here.

USERNAME=sandbox
export HOME=/home/$USERNAME
export USER=$USERNAME

# The config dir is a mounted volume; ensure the sandbox user owns it. Tier 1
# names it via CLAUDE_CONFIG_DIR; SANDBOX_CONFIG_DIR is the tier-agnostic name.
SANDBOX_CONFIG_DIR="${SANDBOX_CONFIG_DIR:-${CLAUDE_CONFIG_DIR:-/config}}"
export SANDBOX_CONFIG_DIR
chown -R "$USERNAME:$USERNAME" "$SANDBOX_CONFIG_DIR" 2>/dev/null || true

# Per-tier setup hook: baked root-owned by each image, run as root BEFORE the
# firewall and the privilege drop. This is where agent-specific declarative config
# is materialized (tier 1: Claude user settings; tier 2: the opencode provider
# config). Optional — a tier that needs nothing simply ships no hook.
if [ -x /usr/local/bin/tier-setup.sh ]; then
    /usr/local/bin/tier-setup.sh
fi

# Git identity comes from the host at launch (forwarded as env by the launcher),
# not baked into the image — keeping the tree host-agnostic.
# Materialize it into the sandbox user's global git config each boot, alongside the
# shared, non-personal defaults baked in ~/.gitconfig (init.defaultBranch, pull.rebase).
# Absent -> skip; git errors only at commit time. See DESIGN.md.
if [ -n "${GIT_USER_NAME:-}" ] && [ -n "${GIT_USER_EMAIL:-}" ]; then
    gosu "$USERNAME" git config --global user.name "$GIT_USER_NAME"
    gosu "$USERNAME" git config --global user.email "$GIT_USER_EMAIL"
fi

# Network boundary: default-deny firewall with a whitelist. This is the real
# egress boundary — not client-side settings — so it always runs; there is no
# opt-out. `set -e` makes a failure here abort before the agent starts (this
# runs before the privilege drop below), i.e. the container fails closed rather
# than launching a yolo agent with no boundary.
/usr/local/bin/init-firewall.sh

# Load-bearing invariant: the non-root agent must hold NO Linux capabilities. The
# whole containment model assumes it cannot run iptables (NET_ADMIN) to tear down
# the firewall, or otherwise re-privilege. By design this holds — the gosu
# setuid->non-root transition clears the permitted/effective/ambient cap sets,
# Docker grants no ambient caps, and no-new-privileges blocks re-elevation — but
# this is THE property everything else rests on, so assert it here rather than
# trust it silently. We probe a process spawned exactly the way the agent is
# (`gosu "$USERNAME" ...`), so its /proc/self/status reflects the agent's caps.
# CapEff/CapPrm/CapAmb are the sets that actually grant or preserve capability;
# CapBnd (bounding) may legitimately still list the added caps and is not by
# itself exploitable under no-new-privileges. Fail closed on any non-zero set.
agent_caps="$(gosu "$USERNAME" grep -E '^Cap(Eff|Prm|Amb):' /proc/self/status | awk '{print $2}')"
for cap in $agent_caps; do
    if [ "$cap" != "0000000000000000" ]; then
        echo "FATAL: sandbox user would start with Linux capabilities (Eff/Prm/Amb: $agent_caps)." >&2
        echo "       The containment model requires the agent to hold none — refusing to start." >&2
        echo "       See DESIGN.md; check --cap-add / ambient caps / no-new-privileges." >&2
        exit 1
    fi
done

# Drop privileges and run the requested command as the sandbox user.
exec gosu "$USERNAME" "$@"
