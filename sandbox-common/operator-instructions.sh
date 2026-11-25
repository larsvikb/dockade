#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Sourced by each tier's setup hook, in its sandbox-user phase, right after it
# installs the tier's baked user-scope instructions. Shared, so every tier marks
# the operator's text the same way.
#
# The launcher mounts the operator's file for this tier, when there is one,
# read-only at /operator/<name> (sandbox-lib.sh, sc_operator_config). It is
# APPENDED, never substituted: the baked text above it is the container's
# capability facts, and an operator's text cannot make them untrue, only make the
# agent wrong about them (DESIGN.md, "Operator customization").
#
# Run as the sandbox user, so root never opens a file from the host: the reason
# claude-sandbox/tier-setup.sh renders the launch settings as the user.

# The line that starts the operator's text: a heading rather than an HTML comment,
# so it reaches the agent as well as a reader of the file, and no host path.
OPERATOR_INSTRUCTIONS_HEADING="# The operator's own instructions"

# append_operator_instructions <operator_file> <dest>
# Absent, empty or only whitespace: <dest> is left byte for byte as baked.
# Otherwise appends a blank line, the heading and a line saying whose text is
# whose, the file's content, and a final newline.
# Unusable (not a regular file, unreadable) or unwritable: a warning, <dest> as
# baked, and status 0, so under a hook's set -euo pipefail it costs the append and
# never the boot.
append_operator_instructions() {
    local src="$1" dest="$2" content
    [[ -e "$src" || -L "$src" ]] || return 0
    if [[ ! -f "$src" ]]; then
        echo "WARNING: $src is not a regular file — the operator's instructions were not added." >&2
        return 0
    fi
    if ! content="$(cat -- "$src" 2>/dev/null)"; then
        echo "WARNING: $src could not be read — the operator's instructions were not added." >&2
        return 0
    fi
    [[ -n "${content//[[:space:]]/}" ]] || return 0
    if ! printf '\n%s\n\n%s\n\n%s\n' "$OPERATOR_INSTRUCTIONS_HEADING" \
            "Everything above this heading is dockade's, the same in every sandbox of this tier. What follows is the operator's, added on this machine." \
            "$content" >> "$dest" 2>/dev/null; then
        echo "WARNING: $dest could not be written — the operator's instructions were not added." >&2
        return 0
    fi
}
