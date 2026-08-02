#!/bin/bash
set -euo pipefail

# Tier-2 (opencode) setup hook. Run as root by the shared entrypoint, before the
# firewall is armed and before the drop to the non-root sandbox user.
#
# Materializes the opencode provider config pointing at the local inference
# service. Same rationale as tier 1's hook: this is DECLARATIVE CONFIG OWNED BY
# THE IMAGE, not mutable state, so it is rewritten authoritatively on every boot
# from the baked template. Wiping the config volume never loses it, and any
# in-session agent edits are transient by design.
#
# It is emphatically NOT a containment control — the agent can rewrite it, and a
# rewritten baseURL buys nothing: the firewall permits exactly one destination,
# so pointing opencode elsewhere yields a rejected connection, not egress. That
# is the "capability, not configuration" split in miniature.

USERNAME=sandbox
CONFIG_DIR="${SANDBOX_CONFIG_DIR:-/config}"

install -d -o "$USERNAME" -g "$USERNAME" -m 0755 "$CONFIG_DIR/opencode"
install -o "$USERNAME" -g "$USERNAME" -m 0644 \
    /etc/opencode/opencode.json \
    "$CONFIG_DIR/opencode/opencode.json"

# opencode reads its user-scope config from XDG_CONFIG_HOME. Point that at the
# persistent volume so session state survives container churn, and so the config
# above is the one it actually loads.
install -d -o "$USERNAME" -g "$USERNAME" -m 0755 "/home/$USERNAME/.config"
ln -sfn "$CONFIG_DIR/opencode" "/home/$USERNAME/.config/opencode"
chown -h "$USERNAME:$USERNAME" "/home/$USERNAME/.config/opencode"
