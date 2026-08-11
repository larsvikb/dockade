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
#
# The same file's `tools` map is there for CONTEXT, not for governance. A `false`
# entry becomes a deny rule that `Permission.visibleTools` applies before the
# request is built, so the tool's schema never reaches the model — the saving is
# prompt tokens, not privilege. That matters on a 24k window whose base prompt
# already takes a quarter of it, and where tool descriptions are the largest
# single part of it. The five disabled tools are the ones with no capability
# behind them: webfetch and websearch need egress this tier does not have, lsp
# needs a language server the image does not install and cannot fetch, and task
# spawns a subagent that re-prefills a whole base prompt on hardware where that
# costs ~36 s. todowrite is the one exception — it works; it is dropped because
# a 9B model on a short local session gets less from it than its description
# costs. None of this stops the agent re-enabling them, and none of it needs to.
#
# Two traps if this list is ever edited. `write` and `patch` normalize to the
# `edit` permission, which `edit`, `write` and `apply_patch` all share, so no one
# of those three can be disabled alone. And opencode itself parses this file as
# JSONC, but `make consistency` reads it with Python's `json.load` — so it stays
# comment-free JSON, which is why this rationale lives here.

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

# AGENTS.md beside it, on the same terms. This is opencode's instruction file:
# the session path (opencode/src/session/instruction.ts) reads
# `join(global.config, "AGENTS.md")` FIRST and stops at the first global match —
# `~/.claude/CLAUDE.md` is the next slot, not an addition — then walks the project
# tree (AGENTS.md, else CLAUDE.md, else CONTEXT.md). So the global config dir,
# symlinked below, is where a container-wide one belongs, and a workspace file
# stacks on top of it rather than replacing it.
#
# There IS a top-level `instructions` array that could name a path instead
# (absolute, `~/`-relative, or a URL, fetched with a 5s timeout — useless here).
# Not used, because discovery needs no config at all: the agent owns this volume
# and can rewrite opencode.json, and an instruction file that only loads while a
# config key survives is the weaker of the two. Neither is a control either way.
#
# Content is this tier's CAPABILITY facts, and deliberately NOT a copy of tier 1's
# CLAUDE.md: that file's longest bullet describes the governed proxy, and this tier
# has neither a proxy nor egress. Shorter, too, and for a reason beyond taste — the
# window here is 32k with a base prompt already taking a quarter of it, so
# instructions cost roughly thirty times what they cost in tier 1.
install -o "$USERNAME" -g "$USERNAME" -m 0644 \
    /etc/opencode/AGENTS.md.template \
    "$CONFIG_DIR/opencode/AGENTS.md"

# opencode reads its user-scope config from ~/.config/opencode (the XDG default;
# nothing here overrides it). Symlink that at the persistent volume so session
# state survives container churn, and so the config above is the one it loads.
install -d -o "$USERNAME" -g "$USERNAME" -m 0755 "/home/$USERNAME/.config"
ln -sfn "$CONFIG_DIR/opencode" "/home/$USERNAME/.config/opencode"
chown -h "$USERNAME:$USERNAME" "/home/$USERNAME/.config/opencode"
