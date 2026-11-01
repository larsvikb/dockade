#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# Tier-1 (Claude) setup hook. Invoked as root by the shared entrypoint, before the
# firewall is armed and before the drop to the non-root sandbox user — and it hands
# itself to that user as soon as root has done the things only root can.
#
# Only Claude-specific materialization belongs here. Everything tier-agnostic —
# config ownership, git identity, the firewall, the capability assertion, the
# gosu drop — lives in sandbox-common/entrypoint.sh and is shared with tier 2.
#
# TWO PHASES, split by privilege rather than by topic (the entrypoint states the
# contract). Root writes the gateway pointer, the launch settings and the managed
# note into /etc, image layer, and nothing else. Then `exec gosu` re-runs this
# script as the sandbox user for everything that lands in $CONFIG_DIR — a volume
# the agent owns and every concurrent tier-1 sandbox shares, so a sibling's agent
# can be rearranging it while this boot runs.
# Written as root, a settings.json that had become a symlink was a root write (and
# a root READ, through jq) wherever the link pointed; written as the user, a
# symlink leads only to places the agent could already write.
#
# A THIRD ENTRY, `--render-launch-settings`, is the root phase asking the sandbox
# user to read the mounted marketplaces for it (see the launch settings below).

USERNAME=sandbox
CONFIG_DIR="${SANDBOX_CONFIG_DIR:-${CLAUDE_CONFIG_DIR:-/config}}"

# The refs the launcher mounted under <dir> (sc_refs in sandbox-lib.sh), as one
# instruction bullet naming them; nothing when none are.
# shellcheck disable=SC2016  # the backticks are markdown, not command substitution
refs_note() {
    local names=() d list
    for d in "$1"/*/; do
        [[ -d "$d" ]] && names+=("$(basename "$d")")
    done
    (( ${#names[@]} > 0 )) || return 0
    list="$(printf '`%s`, ' "${names[@]/#/$1/}")"
    printf -- '- **Reference material is mounted read-only**: %s.\n' "${list%, }"
    cat <<'REFS_NOTE'
  Host directories the human mounted for you to read — another checkout, docs, a
  library's source. Look there when the task points outside `/workspace`;
  changes belong in `/workspace`, never there.
REFS_NOTE
}

# The JDK variant's toolchain, as one instruction bullet naming the versions its
# build recorded in <version_file> (claude-sandbox/Dockerfile); nothing in an image
# built without it. Read from the file, so no JVM starts at boot.
# shellcheck disable=SC2016  # the backticks are markdown, not command substitution
jdk_note() {
    local versions=() list
    [[ -f "$1" ]] || return 0
    mapfile -t versions < "$1"
    list="$(printf '`%s`, ' "${versions[@]}")"
    printf -- '- **A JDK and the JUnit 5 console launcher are installed**: %s.\n' "${list%, }"
    cat <<'JDK_NOTE'
  Compile with `javac`, then run the tests with
  `java -jar /opt/junit/junit-platform-console-standalone.jar execute --class-path <classes>:<deps> --scan-class-path`.
  There is no Maven or Gradle: dependencies come from the workspace, where the
  host-side build copied them. A test JVM that runs out of heap takes `-Xmx`
  before `-jar`; its default is a quarter of the container's memory. Java ignores
  `HTTPS_PROXY`, so a test that opens a network connection fails.
JDK_NOTE
}

# What launch settings rendered by the sandbox user may hold before root installs
# them: the two keys and nothing else, each a non-empty object, every plugin set
# to true, and marketplace names in the alphabet the render allows, since they
# become managed-note text below. \A and \z, because jq's $ also matches before a
# trailing newline.
LAUNCH_SHAPE='type == "object"
    and length > 0
    and (keys - ["extraKnownMarketplaces", "enabledPlugins"] | length == 0)
    and all(.[]; type == "object" and length > 0)
    and ((.extraKnownMarketplaces // {}) | keys | all(test("\\A[A-Za-z0-9._-]+\\z")))
    and ((.enabledPlugins // {}) | all(.[]; . == true))'

# The note for launch settings <json> that passed LAUNCH_SHAPE, as one instruction
# bullet; nothing when they are empty. Read from the settings themselves, so it
# cannot describe a marketplace they do not register.
# shellcheck disable=SC2016  # the backticks are markdown, not command substitution
launch_note() {
    local names=() plugins list
    mapfile -t names < <(jq -r '.extraKnownMarketplaces // {} | keys[]' <<<"$1")
    plugins="$(jq '.enabledPlugins // {} | length' <<<"$1")"
    if (( ${#names[@]} > 0 )); then
        list="$(printf '`%s`, ' "${names[@]}")"
        printf -- '- **Plugin marketplaces are mounted read-only at `/marketplaces`**: %s.\n' "${list%, }"
        cat <<'MARKETPLACE_NOTE'
  They are the human's host-side checkouts, so you cannot edit them, and
  updating them is their step rather than yours.
MARKETPLACE_NOTE
    elif (( plugins > 0 )); then
        printf -- '- **Plugins are enabled host-side for this sandbox.**\n'
    else
        return 0
    fi
    cat <<'PLUGINS_NOTE'
  Which plugins are ENABLED is set host-side, on every boot. A `/plugin
  install` here or in a sibling sandbox lasts only until a restart (it lands in
  the settings every sibling shares, beside these), and a `/plugin disable`
  of a host-enabled plugin, or `enabledPlugins: false` in a project's or your
  own settings, has no effect at all: ask for the allowlist to change instead.
  This holds for `claude` run without a `--settings` of its own; one given
  replaces the host's, and none of these plugins load in that session.
PLUGINS_NOTE
}

# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 — root. The image layer only.
# ═══════════════════════════════════════════════════════════════════════════
if [ "$(id -u)" -eq 0 ]; then
    # ---------------------------------------------------------------------------
    # The MCP gateway  ->  the file /usr/local/bin/claude points --mcp-config at
    # ---------------------------------------------------------------------------
    # Written HERE rather than baked into the image because the address is discovered
    # at launch, and written to /etc rather than into $CONFIG_DIR because the config
    # volume persists: a gateway entry left there would outlive the launch that found
    # it, and would then name an address nothing serves. /etc is image layer, so this
    # file exists for exactly the life of the container that wrote it.
    #
    # ROOT-OWNED AND 0644. The agent can read it and cannot edit it, which is the same
    # footing as the status-line script — and, like that one, this is mistake-prevention
    # rather than containment (see claude-wrapper.sh on why that is enough here).
    #
    # `mcpServers` is Claude Code's own declarative form, and this is the only channel
    # that works for it: the same block in a settings file is silently ignored, with no
    # warning and no error (measured — NOTES.md).
    #
    # The key is `gateway`, so the agent sees `mcp__gateway__<server>__<tool>` and the
    # client renders a call as "Calling gateway". It names the SURFACE, not the system:
    # `dockade` there read as the whole system being invoked, when what is being called is
    # one governed door into it. The word is also the one the rest of the repo already
    # uses for this component (`tool-gateway`, `make logs-tg`), so it adds no vocabulary.
    # Held equal to the gateway's own `protocol.SERVER_NAME` by
    # tests/test_sandbox_wiring.py — a mismatch would be invisible, since both sides
    # would work and only the name in a transcript would be wrong.
    MCP_GATEWAY_CONFIG=/etc/claude-code/mcp-gateway.json
    # Removed first, unconditionally. The absence of this file is what tells the wrapper
    # there is no gateway, so a stale one from an earlier run of the same container is a
    # session pointed at an address that may no longer answer.
    rm -f "$MCP_GATEWAY_CONFIG"
    if [[ "${TOOL_GATEWAY_IP:-}" =~ ^[0-9.]+$ ]]; then
        # BY ADDRESS, and not because a name would resolve wrongly: the launcher had
        # to resolve this leg anyway to arm the firewall's /32 grant, and reusing that
        # value is what keeps the config and the grant on one address (the launcher
        # says why).
        install -o root -g root -m 0644 /dev/stdin "$MCP_GATEWAY_CONFIG" <<EOF
{
  "mcpServers": {
    "gateway": {
      "type": "http",
      "url": "http://${TOOL_GATEWAY_IP}:${TOOL_GATEWAY_PORT:-8100}/mcp"
    }
  }
}
EOF
        echo "  MCP gateway -> http://${TOOL_GATEWAY_IP}:${TOOL_GATEWAY_PORT:-8100}/mcp"
    fi

    # ---------------------------------------------------------------------------
    # Marketplaces and plugins  ->  the file /usr/local/bin/claude points --settings at
    # ---------------------------------------------------------------------------
    # In /etc for the managed note's reason below: SANDBOX_MARKETPLACES_DIR and
    # SANDBOX_PLUGINS are per launch, so in the shared volume's settings.json they
    # would be whichever sibling booted last. `--settings` layers the file over every
    # other scope for each `claude` call and is never written back (measured —
    # NOTES.md). Never named managed-settings.json, which org auth ignores.
    #
    # RENDERED BY THE USER, INSTALLED BY ROOT. Root opens no marketplace file: a
    # sibling whose workspace holds this launch's checkout can write it, `:ro` here
    # or not. The sandbox user reads the manifests and SANDBOX_PLUGINS, which gosu
    # carries, and prints the JSON; root checks it is one document of that shape
    # (`jq -e` alone judges only the last of several) and installs it. A render
    # that fails, or prints something else, costs this launch its plugins and never
    # its boot.
    LAUNCH_SETTINGS=/etc/claude-code/launch-settings.json
    rm -f "$LAUNCH_SETTINGS"
    if ! launch="$(gosu "$USERNAME" "$0" --render-launch-settings)"; then
        echo "WARNING: rendering the plugin settings failed — launching without plugins." >&2
        launch='{}'
    elif [[ "$launch" != "{}" ]] \
            && ! jq -se "length == 1 and (.[0] | $LAUNCH_SHAPE)" >/dev/null 2>&1 <<<"$launch"; then
        echo "WARNING: the rendered plugin settings are malformed — launching without plugins." >&2
        launch='{}'
    fi
    if [[ "$launch" != "{}" ]]; then
        install -o root -g root -m 0644 /dev/stdin "$LAUNCH_SETTINGS" <<<"$launch"
    fi

    # The managed note, for the refs, the plugins and the JDK. In /etc for the gateway
    # pointer's reason and a sharper one: every concurrent tier-1 sandbox shares the
    # config volume, and each launch has its own SANDBOX_REFS and plugins, so a note
    # there would describe whichever sibling booted last. A CLAUDE.md in
    # /etc/claude-code loads as managed memory beside the user-scope one (measured —
    # NOTES.md), so this needs no pointer.
    MANAGED_NOTE=/etc/claude-code/CLAUDE.md
    rm -f "$MANAGED_NOTE"
    note="$(refs_note /refs)"
    plugin_note="$(launch_note "$launch")"
    if [[ -n "$plugin_note" ]]; then
        note="${note:+$note$'\n'}$plugin_note"
    fi
    jdk="$(jdk_note /etc/claude-code/jdk-version)"
    if [[ -n "$jdk" ]]; then
        note="${note:+$note$'\n'}$jdk"
    fi
    if [[ -n "$note" ]]; then
        install -o root -g root -m 0644 /dev/stdin "$MANAGED_NOTE" <<<"$note"
    fi

    # Root is done. Everything below lands in the agent-owned volume, so it runs as
    # the agent. `exec` so there is one process and one exit status, and the env
    # (the config dir) travels through gosu unchanged.
    exec gosu "$USERNAME" "$0" "$@"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 — the sandbox user. The config volume.
# ═══════════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Plugin marketplaces mounted at <dir>, and SANDBOX_PLUGINS  ->  stdout
# ---------------------------------------------------------------------------
# The launch settings root installs, as one line of JSON: `{}` when there is
# nothing to register. The launcher mounts the human's marketplace checkouts
# read-only (see sc_marketplaces in sandbox-lib.sh), and registering them takes
# two keys: `extraKnownMarketplaces` (what exists) and `enabledPlugins` (what
# loads). Both are Claude Code's own declarative form — measured, not assumed:
# `claude plugin marketplace add <dir>` does nothing else, and settings alone are
# enough for a session to resolve a plugin with no install step ever run. See
# NOTES.md.
#
# Generated here rather than by `claude plugin marketplace add`, because that
# writes a settings scope, and none of the scopes the CLI writes is this
# container's own: user scope is the shared volume, and project scope is the
# workspace. Deriving the whole set from what is mounted, on every boot, also
# means removing a checkout on the host makes it disappear here — no stale entry
# to clean up, and no state that outlives its source.
#
# Deliberately NOT a general host-supplied settings overlay, tempting though that
# is (one channel, a schema we do not own, room for every future user-scope
# knob). "What settings is this sandbox running?" is a two-file question now, and
# a host file could quietly undo a paved-road default. So the second file is
# root-installed, holds these two keys and nothing else (LAUNCH_SHAPE), and comes
# only from what is mounted and allowlisted. See DESIGN.md.
render_launch_settings() {
    local mk_json='{}' en_json='{}' mk_dirs=() d name id plugin market manifest
    if [[ -d "$1" ]]; then
        # Two layouts, because both are things a human plausibly does: the mounted
        # directory IS one marketplace (its own checkout), or it CONTAINS several.
        if [[ -f "$1/.claude-plugin/marketplace.json" ]]; then
            mk_dirs=("$1")
        else
            for d in "$1"/*; do
                [[ -f "$d/.claude-plugin/marketplace.json" ]] && mk_dirs+=("$d")
            done
        fi
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
        # The name becomes a settings key and managed-note text, and a sibling
        # whose workspace holds the checkout can write it.
        if [[ ! "$name" =~ ^[A-Za-z0-9._-]+$ ]]; then
            echo "WARNING: $d/.claude-plugin/marketplace.json names itself outside [A-Za-z0-9._-] — skipping." >&2
            continue
        fi
        if jq -e --arg n "$name" 'has($n)' <<<"$mk_json" >/dev/null; then
            echo "WARNING: two mounted marketplaces both call themselves '$name'; using $d." >&2
        fi
        mk_json="$(jq --arg n "$name" --arg p "$d" \
            '.[$n] = {source: {source: "directory", path: $p}}' <<<"$mk_json")"
    done

    # The enabled set comes from the host allowlist (SANDBOX_PLUGINS, assembled by
    # sc_plugin_allowlist). Ids naming a MOUNTED marketplace are checked against its
    # manifest, because a typo there is otherwise perfectly silent — the plugin
    # simply never loads. Ids naming any other marketplace (e.g. one the config
    # volume already knows) pass through unchecked: there is nothing here to check
    # them against, and refusing them would break the case of enabling a plugin from
    # the official marketplace with nothing mounted at all. plugin_advisory below
    # says which of those nothing knows.
    #
    # Commas accepted as well as whitespace. sc_plugin_allowlist already normalizes
    # both, so in the launched path this is redundant — deliberately: SANDBOX_PLUGINS
    # is an ordinary env var, and a comma-separated one set by hand (or by a future
    # caller that skips the launcher) would otherwise produce one absurd id, be
    # written to the settings, and load nothing. Correct on its own input beats
    # correct on its caller's.
    local en_ids="${SANDBOX_PLUGINS:-}"
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
        fi
        en_json="$(jq --arg i "$id" '.[$i] = true' <<<"$en_json")"
    done

    jq -cn --argjson mk "$mk_json" --argjson en "$en_json" '
        (if ($mk | length) > 0 then {extraKnownMarketplaces: $mk} else {} end)
        + (if ($en | length) > 0 then {enabledPlugins: $en} else {} end)'
}

# Enabled ids in launch settings <file> whose marketplace neither they nor the
# config volume's cache <known_marketplaces.json> knows, said once per boot.
# Written anyway — the entry is inert, not harmful (measured: an unresolvable id
# costs a session nothing and triggers no fetch), and the marketplace may
# legitimately be added later in the session. But a mistyped marketplace name is
# otherwise completely silent, which is the one failure mode of this feature a
# human cannot see from the outside. The cache is every sibling's (NOTES.md), so
# whether this speaks can depend on which of them ran first.
plugin_advisory() {
    local id market
    [[ -f "$1" ]] || return 0
    while IFS= read -r id; do
        market="${id##*@}"
        jq -e --arg m "$market" '.extraKnownMarketplaces // {} | has($m)' "$1" >/dev/null \
            && continue
        jq -e --arg m "$market" 'has($m)' "$2" >/dev/null 2>&1 && continue
        echo "NOTE: '$id' names marketplace '$market', which is neither mounted nor" >&2
        echo "      known to this config volume — it will not load until it is." >&2
    done < <(jq -r '.enabledPlugins // {} | keys[]' "$1")
}

if [[ "${1:-}" == "--render-launch-settings" ]]; then
    render_launch_settings /marketplaces
    exit 0
fi

# No `-o`/`-g` on the installs below: the files are owned by whoever writes them,
# and that is now the user they are for.

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
install -m 0644 /etc/claude-code/user-settings.json "$CONFIG_DIR/settings.json"

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
install -m 0644 /etc/claude-code/CLAUDE.md.template "$CONFIG_DIR/CLAUDE.md"


# Said here rather than by the render, which root runs once and whose warnings
# are its own; this needs the volume's cache, and so the user.
plugin_advisory /etc/claude-code/launch-settings.json \
    "$CONFIG_DIR/plugins/known_marketplaces.json" || true
