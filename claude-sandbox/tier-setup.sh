#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# Tier-1 (Claude) setup hook. Run as root by the shared entrypoint, before the
# firewall is armed and before the drop to the non-root sandbox user.
#
# Only Claude-specific materialization belongs here. Everything tier-agnostic —
# config ownership, git identity, the firewall, the capability assertion, the
# gosu drop — lives in sandbox-common/entrypoint.sh and is shared with tier 2.

USERNAME=sandbox
CONFIG_DIR="${SANDBOX_CONFIG_DIR:-${CLAUDE_CONFIG_DIR:-/config}}"

# User settings are declarative config owned by the image, not mutable state in
# the volume. Overwrite them authoritatively on every boot from the baked
# template, so: config always matches the repo, wiping the config volume for a
# clean slate never loses it (only credentials/runtime state live in the volume),
# and any in-session agent edits are transient by design. These are steering /
# mistake-prevention defaults, NOT an enforcement layer — no client settings file
# is one here (under org auth the org's remote managed source shadows any local
# managed file; enforcement is the firewall + capability containment). statusLine
# lives here (user scope) rather than managed settings because a managed-scope
# command status line is gated behind an interactive approval dialog the yolo
# launch flow suppresses. The status-line script itself stays root-owned and baked
# at /etc/claude-code/statusline.sh — only the pointer is user-editable. No
# chmod-to-read-only: it would be theater (owner can re-chmod, and /config is
# agent-writable so the file can be replaced). See DESIGN.md.
install -o "$USERNAME" -g "$USERNAME" -m 0644 \
    /etc/claude-code/user-settings.json \
    "$CONFIG_DIR/settings.json"

# User-scope CLAUDE.md, on the same terms and for the same reasons as the
# settings above: baked in the image, overwritten every boot, transient if the
# agent edits it. Its content is the container's CAPABILITY facts — no egress but
# the proxy, no push, no docker, /workspace shared with the host — which the agent
# otherwise rediscovers one failed command at a time. Enablement, not enforcement:
# an agent that ignores the file is stopped by the firewall exactly as before.
# Deliberately says nothing about how to work in the repo; that is the repo's own
# CLAUDE.md, which travels with the checkout and is loaded alongside this.
#
# The source is `.template` on purpose — /etc/claude-code is a managed source and
# a CLAUDE.md sitting there is loaded as managed memory in its own right, which
# put the same text in context twice. The extension is what keeps the baked copy
# inert. See the Dockerfile comment and NOTES.md.
install -o "$USERNAME" -g "$USERNAME" -m 0644 \
    /etc/claude-code/CLAUDE.md.template \
    "$CONFIG_DIR/CLAUDE.md"
