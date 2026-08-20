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

# ---------------------------------------------------------------------------
# Plugin marketplaces mounted at /marketplaces  ->  settings.json
# ---------------------------------------------------------------------------
# The launcher mounts the human's marketplace checkouts read-only (see
# sc_marketplaces in sandbox-lib.sh). Registering them means writing two keys
# into the user settings materialized above: `extraKnownMarketplaces` (what
# exists) and `enabledPlugins` (what loads). Both are Claude Code's own
# declarative form — measured, not assumed: `claude plugin marketplace add
# <dir>` does nothing else, and settings alone are enough for a session to
# resolve a plugin with no install step ever run. See NOTES.md.
#
# Generated here rather than by shelling out to `claude plugin marketplace add`,
# for three reasons. The CLI would be a SECOND writer to the file this script
# owns authoritatively, which is how "config always matches the repo" stops being
# true. It would have to run as the sandbox user (this runs as root, before the
# gosu drop), so it needs a gosu hop to avoid leaving root-owned files in
# /config. And deriving the whole set from what is mounted, on every boot, means
# removing a checkout on the host makes it disappear here — no stale entry to
# clean up, and no state that outlives its source.
#
# Deliberately NOT a general host-supplied settings overlay, tempting though that
# is (one channel, a schema we do not own, room for every future user-scope
# knob). It would reopen exactly what the block above closes: "what settings is
# this sandbox running?" would become a two-file question, and a host file could
# quietly undo a paved-road default. Narrow keys, single purpose. See DESIGN.md.
MARKETPLACES_DIR=/marketplaces
SETTINGS="$CONFIG_DIR/settings.json"
mk_json='{}'
mk_names=()

if [[ -d "$MARKETPLACES_DIR" ]]; then
    # Two layouts, because both are things a human plausibly does: the mounted
    # directory IS one marketplace (its own checkout), or it CONTAINS several.
    mk_dirs=()
    if [[ -f "$MARKETPLACES_DIR/.claude-plugin/marketplace.json" ]]; then
        mk_dirs=("$MARKETPLACES_DIR")
    else
        for d in "$MARKETPLACES_DIR"/*; do
            [[ -f "$d/.claude-plugin/marketplace.json" ]] && mk_dirs+=("$d")
        done
    fi

    for d in ${mk_dirs[@]+"${mk_dirs[@]}"}; do
        # Keyed by the manifest's `name`, NOT the directory name — verified: plugin
        # ids are `plugin@<manifest name>`, so a directory-derived key would produce
        # an allowlist that silently matches nothing.
        name="$(jq -r '.name // empty' "$d/.claude-plugin/marketplace.json" 2>/dev/null || true)"
        if [[ -z "$name" ]]; then
            # Warn and skip rather than abort: one malformed checkout on the host
            # must not cost the operator their whole session.
            echo "WARNING: $d/.claude-plugin/marketplace.json has no usable 'name' — skipping." >&2
            continue
        fi
        if jq -e --arg n "$name" 'has($n)' <<<"$mk_json" >/dev/null; then
            echo "WARNING: two mounted marketplaces both call themselves '$name'; using $d." >&2
        fi
        mk_json="$(jq --arg n "$name" --arg p "$d" \
            '.[$n] = {source: {source: "directory", path: $p}}' <<<"$mk_json")"
        mk_names+=("$name")
    done
fi

# The enabled set comes from the host allowlist (SANDBOX_PLUGINS, assembled by
# sc_plugin_allowlist). Ids naming a MOUNTED marketplace are checked against its
# manifest, because a typo there is otherwise perfectly silent — the plugin
# simply never loads. Ids naming any other marketplace (e.g. one the config
# volume already knows) pass through unchecked: there is nothing here to check
# them against, and refusing them would break the case of enabling a plugin from
# the official marketplace with nothing mounted at all.
en_json='{}'
# Commas accepted as well as whitespace. sc_plugin_allowlist already normalizes
# both, so in the launched path this is redundant — deliberately: SANDBOX_PLUGINS
# is an ordinary env var, and a comma-separated one set by hand (or by a future
# caller that skips the launcher) would otherwise produce one absurd id, be
# written to settings.json, and load nothing. Correct on its own input beats
# correct on its caller's.
en_ids="${SANDBOX_PLUGINS:-}"
for id in ${en_ids//,/ }; do
    if [[ "$id" != *"@"* ]]; then
        echo "WARNING: plugin id '$id' is not in plugin@marketplace form — skipping." >&2
        continue
    fi
    plugin="${id%@*}"
    market="${id##*@}"
    if jq -e --arg m "$market" 'has($m)' <<<"$mk_json" >/dev/null; then
        manifest="$(jq -r --arg m "$market" '.[$m].source.path' <<<"$mk_json")/.claude-plugin/marketplace.json"
        if ! jq -e --arg p "$plugin" \
                'any(.plugins[]?; (.name // "") == $p)' "$manifest" >/dev/null 2>&1; then
            echo "WARNING: marketplace '$market' declares no plugin '$plugin' — skipping '$id'." >&2
            continue
        fi
    elif ! jq -e --arg m "$market" 'has($m)' \
            "$CONFIG_DIR/plugins/known_marketplaces.json" >/dev/null 2>&1; then
        # Written anyway — the entry is inert, not harmful (measured: an
        # unresolvable id costs a session nothing and triggers no fetch), and the
        # marketplace may legitimately be added later in the session. But a
        # mistyped marketplace name is otherwise completely silent, which is the
        # one failure mode of this feature a human cannot see from the outside.
        echo "NOTE: '$id' names marketplace '$market', which is neither mounted nor" >&2
        echo "      known to this config volume — it will not load until it is." >&2
    fi
    en_json="$(jq --arg i "$id" '.[$i] = true' <<<"$en_json")"
done

if [[ "$mk_json" != '{}' || "$en_json" != '{}' ]]; then
    # Merge into the settings installed above rather than rewriting them, and via
    # a temp file so a jq failure cannot leave a truncated settings.json behind.
    tmp="$(mktemp)"
    jq --argjson mk "$mk_json" --argjson en "$en_json" '
        .
        + (if ($mk | length) > 0 then {extraKnownMarketplaces: $mk} else {} end)
        + (if ($en | length) > 0 then {enabledPlugins: $en} else {} end)
    ' "$SETTINGS" > "$tmp"
    install -o "$USERNAME" -g "$USERNAME" -m 0644 "$tmp" "$SETTINGS"
    rm -f "$tmp"
fi

# Tell the agent what it has, in the file it already reads. Appended only when
# something was registered, so the statement is never a claim about a mount that
# is not there — the rest of that file is capability facts, and this is one.
if (( ${#mk_names[@]} > 0 )); then
    mk_list="$(printf '%s, ' "${mk_names[@]}")"
    {
        # shellcheck disable=SC2016  # the backticks are markdown, not command substitution
        printf '\n- **Plugin marketplaces are mounted read-only at `/marketplaces`**: %s.\n' \
            "${mk_list%, }"
        cat <<'MARKETPLACE_NOTE'
  They are the human's host-side checkouts, so you cannot edit them, and
  updating them is their step rather than yours. Which plugins are ENABLED is set
  host-side too and re-derived on every boot, so a `/plugin install` in this
  session does not survive a restart — ask for the allowlist to change instead.
MARKETPLACE_NOTE
    } >> "$CONFIG_DIR/CLAUDE.md"
fi
