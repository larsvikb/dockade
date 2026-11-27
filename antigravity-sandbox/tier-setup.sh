#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# Tier-1 (Antigravity, agy) setup hook. Invoked as root by the shared entrypoint,
# before the firewall is armed and before the drop to the non-root sandbox user — and
# it hands itself to that user at once, because nothing here needs root: every write
# lands in the config volume or the agent's home (see the entrypoint's contract).
#
# Three things, each rewritten on every boot from what the image bakes in
# /etc/antigravity, so the effective config always matches the repo and an in-session
# edit lasts until the next boot that writes it: this container's for the two per
# container, any sibling's for the settings. None is a containment control: the agent
# owns all of them, and what makes that harmless is the firewall and the proxy, not
# these files.
#
#   - agy's user settings, ~/.gemini/antigravity-cli/settings.json, which lies in
#     the config volume. They are the image's and nothing else:
#       enableTerminalSandbox false — agy's own terminal sandbox cannot start under
#         this container's dropped capabilities and no-new-privileges, and where it
#         is on, every command fails (NOTES.md). The container is the boundary.
#       agentMode "" — the mode agy's docs call default, which reviews a command or
#         an edit before it runs; agy-yolo is the opt-in that does not. agy makes
#         Auto the default where an account offers it, and Auto approves commands
#         because agy counts its own sandbox as the boundary, which here is off.
#       a read_file allow on /refs — the reference mounts read without an ask, as
#         in every tier (DESIGN.md, "Reference mounts"); the mount's `:ro` is what
#         keeps them read-only.
#       statusLine — the status line Claude's tier shows, from the root-owned
#         /etc/antigravity/statusline.sh (sandbox-common/statusline.sh), which
#         makes no network call. Cosmetic, as in claude-sandbox.
#     Every sibling shares the volume, so whichever booted last wrote the file; they
#     all write the same one, and each writes it beside the old one and renames it
#     over, so a sibling's agy starting at that moment reads a whole file, old or new.
#   - the gateway entry, ~/.gemini/config/mcp_config.json, PER CONTAINER: it carries
#     this boot's sandbox id, which siblings must never share. User-owned, unlike
#     Claude's root-owned pointer, because agy takes no flag naming an MCP config
#     file; it reads this path, and a workspace's .agents/mcp_config.json beside it.
#   - the instructions, ~/.gemini/AGENTS.md, one of agy's user-level rule files,
#     with the operator's own appended when the launcher mounted any. Per
#     container, since they hold nothing a sibling differs in.

USERNAME=sandbox
CONFIG_DIR="${SANDBOX_CONFIG_DIR:-/config}"
# Where the image bakes the templates; tests/test_antigravity_setup.py points it at a
# temp dir.
IMAGE_DIR=/etc/antigravity

# Root has nothing to do here. Re-exec as the sandbox user before the first write, so
# a symlink an agent planted in the shared volume can lead only to places the agent
# could already write.
if [ "$(id -u)" -eq 0 ]; then
    exec gosu "$USERNAME" "$0" "$@"
fi

# The volume halves of agy's state: ~/.gemini/antigravity-cli (the login token,
# settings, logs, conversations) and ~/.config/antigravity (a credential scope
# file), each a link the image bakes into the volume. A link or a stray file where a
# target belongs is not the directory agy expects, so clear it and make a real one; a
# real directory from an earlier boot is kept, with the login in it.
for dir in antigravity-cli antigravity; do
    if [ -L "$CONFIG_DIR/$dir" ] || \
       { [ -e "$CONFIG_DIR/$dir" ] && [ ! -d "$CONFIG_DIR/$dir" ]; }; then
        rm -f "$CONFIG_DIR/$dir"
    fi
    install -d -m 0700 "$CONFIG_DIR/$dir"
done

settings="$CONFIG_DIR/antigravity-cli/settings.json"
install -m 0600 "$IMAGE_DIR/settings.json" "$settings.tmp.$$"
mv -f "$settings.tmp.$$" "$settings"

install -d -m 0700 "$HOME/.gemini" "$HOME/.gemini/config"
install -m 0644 "$IMAGE_DIR/AGENTS.md.template" "$HOME/.gemini/AGENTS.md"
# Then the operator's own, from ~/.config/dockade/agy/AGENTS.md on the host, when the
# launcher mounted one, under a heading after the baked text. Per container, like the
# file it is appended to. /operator is nowhere agy looks for rules on its own, so it
# loads once.
# shellcheck source=sandbox-common/operator-instructions.sh
source /usr/local/lib/dockade/operator-instructions.sh
append_operator_instructions "/operator/AGENTS.md" "$HOME/.gemini/AGENTS.md"

# ---------------------------------------------------------------------------
# The MCP gateway  ->  ~/.gemini/config/mcp_config.json
# ---------------------------------------------------------------------------
# Removed first, unconditionally: written only when the launcher found a gateway,
# so a file left by an earlier boot of this container would point the session at an
# address that may no longer answer.
#
# BY ADDRESS, as Claude's entry is: the launcher resolved it for the firewall's /32
# grant, and reusing that value keeps the config and the grant on one address.
#
# THE SANDBOX ID: minted on every boot, by the pipeline claude-sandbox/tier-setup.sh
# uses, and sent as `X-Dockade-Sandbox` in the entry's `headers`, which agy sends on
# every request to the gateway (NOTES.md). A card shows its first 8, printed below;
# what it is and is not: DESIGN.md, "A card names its sandbox by an id the sandbox
# mints". The key is `gateway`, the gateway's own name, as in Claude's entry.
MCP_CONFIG="$HOME/.gemini/config/mcp_config.json"
rm -f "$MCP_CONFIG"
if [[ "${TOOL_GATEWAY_IP:-}" =~ ^[0-9.]+$ ]]; then
    url="http://${TOOL_GATEWAY_IP}:${TOOL_GATEWAY_PORT:-8100}/mcp"
    SANDBOX_ID="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    jq -n --arg url "$url" --arg id "$SANDBOX_ID" \
        '{mcpServers: {gateway: {url: $url, headers: {"X-Dockade-Sandbox": $id}}}}' \
        | install -m 0600 /dev/stdin "$MCP_CONFIG"
    echo "  MCP gateway -> $url"
    echo "  Sandbox id  -> ${SANDBOX_ID:0:8} (the prefix this sandbox's approval cards show)"
fi
