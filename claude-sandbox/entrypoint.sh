#!/bin/bash
set -euo pipefail

# Runs as root. Sets up the network boundary, then drops to the non-root
# sandbox user for everything else.

USERNAME=sandbox
export HOME=/home/$USERNAME
export USER=$USERNAME

# The config dir is a mounted volume; ensure the sandbox user owns it.
chown -R "$USERNAME:$USERNAME" "${CLAUDE_CONFIG_DIR:-/config}" 2>/dev/null || true

# User settings are declarative config owned by the image, not mutable state in
# the volume. Overwrite them authoritatively on every boot from the baked
# template, so: config always matches the repo, wiping the config volume for a
# clean slate never loses it (only credentials/runtime state live in the volume),
# and any in-session agent edits are transient by design. These are steering /
# mistake-prevention defaults, NOT an enforcement layer — no client settings file
# is one here (under org auth the org's remote managed source shadows any local
# managed file; enforcement is the firewall + capability containment). statusLine
# lives here (user scope) rather than managed settings because a
# managed-scope command status line is gated behind an interactive approval
# dialog the yolo launch flow suppresses. The status-line script itself stays
# root-owned and baked at /etc/claude-code/statusline.sh — only the pointer is
# user-editable. No chmod-to-read-only: it would be theater (owner can re-chmod,
# and /config is agent-writable so the file can be replaced). See DESIGN.md.
install -o "$USERNAME" -g "$USERNAME" -m 0644 \
    /etc/claude-code/user-settings.json \
    "${CLAUDE_CONFIG_DIR:-/config}/settings.json"

# Git identity comes from the host at launch (forwarded as env by
# run-claude-sandbox.sh), not baked into the image — keeping the tree host-agnostic.
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

# Drop privileges and run the requested command as the sandbox user.
exec gosu "$USERNAME" "$@"
