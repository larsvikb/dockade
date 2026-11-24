#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# `agy`, with this launch's registry id in its environment. Installed root-owned at
# /usr/local/bin/agy; the real binary is off PATH, so every `agy` resolves here, the
# agy-yolo alias's included.
#
# THE ID: a fresh UUID as DOCKADE_SESSION_ID each time agy starts, which every command
# agy runs inherits. agy's own ANTIGRAVITY_CONVERSATION_ID is not shared with its
# subagents (NOTES.md, "agy behind dockade's proxy, measured before its tier"), so by
# the rule in task-registry/DESIGN.md, "Who an agent is", the tier takes this
# fallback. Per launch: a new chat started in place keeps it, a new process does not.
#
# It passes every argument through unchanged and refuses none: agy's server-side
# entry points are accepted for this tier (DESIGN.md, "Antigravity
# (`run-antigravity-sandbox.sh`)").
#
# WHAT THIS IS NOT: a containment boundary. The agent can run the real binary by its
# path, with any environment it likes; the id is the agent's own word to the registry,
# which authenticates nothing.
set -eu

# The real binary, by absolute path. Held equal to where the Dockerfile installs it by
# tests/test_sandbox_wiring.py.
REAL="/opt/antigravity-cli/agy"

if [ ! -x "$REAL" ]; then
    echo "agy: the Antigravity CLI binary is missing from $REAL" >&2
    exit 127
fi

DOCKADE_SESSION_ID="$(cat /proc/sys/kernel/random/uuid)"
export DOCKADE_SESSION_ID
exec "$REAL" "$@"
