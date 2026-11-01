#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# `claude`, with this sandbox's MCP surface and plugins attached. Installed root-owned at
# /usr/local/bin/claude, which is EARLIER on PATH than the agent-writable
# ~/.local/bin the real binary lives in, so every invocation resolves here first.
#
# A WRAPPER RATHER THAN THE SHELL ALIAS, and the difference is not style. An alias is
# expanded only by an interactive shell, so `claude -p` from a script, a hook or a
# Makefile would silently run without these flags — and `-p` is precisely where a
# workspace `.mcp.json` was measured starting a server with no approval gate at all
# (NOTES.md). The wrapper also keeps `claude-yolo` correct for free, since that alias
# resolves through PATH and therefore through this file.
#
# WHAT THIS IS NOT: a containment boundary. The agent can call the real binary by its
# absolute path, or edit it — it owns that directory. What makes that harmless is not
# this file but the capability argument: an MCP server the agent adds runs inside the
# sandbox, inheriting no egress and no credentials, so it can do nothing `Bash` could
# not already do. The value here is that the sandbox's tool surface is REVIEWABLE IN
# THE REPO instead of accumulating in a config volume. The same goes for the plugins:
# they are this container's, not whatever a sibling last wrote into the shared volume.
set -eu

# The real binary, by absolute path. Not a PATH search: this file IS the first
# `claude` on PATH, so searching would find it again. Held equal to where the
# Dockerfile installs it by tests/test_sandbox_wiring.py.
REAL="/home/sandbox/.local/bin/claude"

# The gateway entry, written at boot by tier-setup.sh and only when a gateway was
# discovered. Its ABSENCE is meaningful and is the no-gateway case below.
MCP_CONFIG="/etc/claude-code/mcp-gateway.json"

# This launch's marketplaces and enabled plugins, written at boot by tier-setup.sh and
# only when there is one of either. Held equal to tier-setup.sh's path by
# tests/test_launch_settings.py.
LAUNCH_SETTINGS="/etc/claude-code/launch-settings.json"

if [ ! -x "$REAL" ]; then
    echo "claude: the Claude Code binary is missing from $REAL" >&2
    exit 127
fi

# THE TWO MCP FLAGS ARE SEPARABLE, and that is what makes the gateway-absent case
# clean. `--strict-mcp-config` goes on unconditionally: with nothing supplied it yields
# zero MCP servers (measured), so "no gateway" means a provably empty tool surface
# rather than whatever a `.mcp.json` in the workspace or the config volume happens to
# hold. `--mcp-config` is added only when there is something to point at, because a
# dead entry costs a startup error and misleads the agent about its own capability.
# `--settings` likewise, only when this launch has plugins.
#
# PREPENDED, because appending fails: after a subcommand (`claude plugin list`) the
# main command's options are "unknown option". ORDER IS LOAD-BEARING, for two rules:
#   - a variadic flag is followed by an option. `--mcp-config` is greedy — `claude
#     --mcp-config X mcp list` consumes `mcp` and `list` as further config paths
#     (NOTES.md) — and `--strict-mcp-config` right after it stops the greed before
#     the caller's arguments can be swallowed;
#   - a caller's `--settings` wins. Claude Code takes the last one given and drops the
#     rest (measured), so the launch file goes first and a caller's replaces it whole,
#     which is said on stderr rather than left silent.
if [ -f "$LAUNCH_SETTINGS" ]; then
    for arg in "$@"; do
        case "$arg" in
            --settings|--settings=*)
                echo "claude: your --settings replaces this sandbox's launch settings, so the" \
                     "host's plugins and marketplaces are not loaded in this session." >&2
                break ;;
        esac
    done
fi
set -- --strict-mcp-config "$@"
if [ -f "$MCP_CONFIG" ]; then
    set -- --mcp-config "$MCP_CONFIG" "$@"
fi
if [ -f "$LAUNCH_SETTINGS" ]; then
    set -- --settings "$LAUNCH_SETTINGS" "$@"
fi
exec "$REAL" "$@"
