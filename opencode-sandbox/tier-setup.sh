#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
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

# $CONFIG_DIR is a persistent volume the agent OWNS, so it can plant a symlink at
# this path between boots. `install -d` — run here as root, before the firewall and
# the privilege drop — FOLLOWS a symlink and chowns/chmods its target, turning an
# agent-controlled config path into a privileged write/chown outside the volume.
# Refuse a symlink (or any non-directory) first; a real directory from a previous
# boot is kept. (The file `install` below replaces a symlink destination rather than
# writing through it, so only the directory create needs this guard.)
if [ -L "$CONFIG_DIR/opencode" ] || \
   { [ -e "$CONFIG_DIR/opencode" ] && [ ! -d "$CONFIG_DIR/opencode" ]; }; then
    rm -f "$CONFIG_DIR/opencode"
fi
install -d -o "$USERNAME" -g "$USERNAME" -m 0755 "$CONFIG_DIR/opencode"
install -o "$USERNAME" -g "$USERNAME" -m 0644 \
    /etc/opencode/opencode.json \
    "$CONFIG_DIR/opencode/opencode.json"

# opencode reads its user-scope config from ~/.config/opencode (the XDG default;
# nothing here overrides it). Symlink that at the persistent volume so session
# state survives container churn, and so the config above is the one it loads.
install -d -o "$USERNAME" -g "$USERNAME" -m 0755 "/home/$USERNAME/.config"
ln -sfn "$CONFIG_DIR/opencode" "/home/$USERNAME/.config/opencode"
chown -h "$USERNAME:$USERNAME" "/home/$USERNAME/.config/opencode"
