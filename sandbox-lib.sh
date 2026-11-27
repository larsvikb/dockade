#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Shared launcher plumbing for every sandbox tier.
#
# Sourced by each run-*-sandbox.sh launcher (DESIGN.md's tier table, under
# "Tier 2, and what it shares with tier 1", has one row each). This file owns the
# mechanics that MUST NOT differ between tiers — above all the workspace safety
# guard, which is the single deliberate writable host coupling and the place where a
# copy-paste divergence would do the most damage.
#
# What deliberately does NOT live here: each tier's capability profile (which
# credentials, which egress, which sibling services it may reach). Those stay in
# the per-tier launcher, short and adjacent, so a reviewer can read the whole
# grant in one place. Shared plumbing, divergent capability — that split is the
# point. The wiring a governed tier uses to reach the data plane is plumbing and
# lives here (sc_governed_egress); the decision to use it stays in the launcher.
#
# Functions set well-known globals rather than echoing, because several return
# bash ARRAYS (docker run flags) which cannot survive command substitution.
# Every such global is named in the function's comment.

# Repo root — the build context for sandbox images (see sc_build_image).
SANDBOX_REPO_ROOT="$(CDPATH='' cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Image build
# ---------------------------------------------------------------------------
# sc_build_image <image_tag> <image_dir> [--no-cache] [build_arg...]
#
# Context is the REPO ROOT, not <image_dir>, so the Dockerfile can COPY the
# shared sandbox-common/ scripts (a build context cannot reach outside itself).
# .dockerignore keeps that context small.
#
# The sandbox user is built to match the host uid/gid so bind-mounted workspaces
# are writable from both sides (avoids the drvfs/uid mismatch on WSL). Rebuild
# after switching hosts if the host uid differs.
sc_build_image() {
    local image_tag="$1" image_dir="$2" no_cache="${3:-}"
    local cache_args=() extra_args=("${@:4}")
    [[ "$no_cache" == "--no-cache" ]] && cache_args+=(--no-cache)
    echo "Building $image_tag (sandbox user uid/gid $(id -u)/$(id -g))..."
    docker build -t "$image_tag" \
        ${cache_args[@]+"${cache_args[@]}"} \
        --build-arg USER_UID="$(id -u)" \
        --build-arg USER_GID="$(id -g)" \
        ${extra_args[@]+"${extra_args[@]}"} \
        -f "$SANDBOX_REPO_ROOT/$image_dir/Dockerfile" \
        "$SANDBOX_REPO_ROOT"
}

# ---------------------------------------------------------------------------
# JDK variant  ->  sets SC_IMAGE, SC_BUILD_ARGS (array)
# ---------------------------------------------------------------------------
# sc_jdk_variant <base_tag>
#
# SANDBOX_JDK=<major> asks for Claude's image's JDK variant: the Dockerfile's ARG
# JDK, built and run under <base_tag>-jdk<major>, so it and the default image never
# overwrite each other and a missing variant is built rather than the default
# reused. Unset or empty, the tag and the build are the default's. Anything but a
# major version is refused, since it becomes part of an apt package name.
# shellcheck disable=SC2034  # both globals are consumed by the sourcing launcher
sc_jdk_variant() {
    SC_IMAGE="$1"
    SC_BUILD_ARGS=()
    [[ -n "${SANDBOX_JDK:-}" ]] || return 0
    if [[ ! "$SANDBOX_JDK" =~ ^[1-9][0-9]*$ ]]; then
        echo "REFUSING SANDBOX_JDK='$SANDBOX_JDK': it takes a JDK major version, such as 21." >&2
        exit 1
    fi
    SC_IMAGE="$1-jdk$SANDBOX_JDK"
    SC_BUILD_ARGS=(--build-arg "JDK=$SANDBOX_JDK")
}

# ---------------------------------------------------------------------------
# Workspace safety guard  ->  sets SC_WORKSPACE
# ---------------------------------------------------------------------------
# The workspace is bind-mounted RW with the sandbox user matched to the host uid,
# so the agent gets full read/write to whatever this resolves to — and anything it
# writes there (git hooks, build scripts, .envrc, editor task files) later runs
# OUTSIDE the sandbox when the host next touches the repo. Mounting a home
# directory or the filesystem root would hand the agent host credentials
# (~/.ssh, ~/.aws, ...) plus a boundary-crossing foothold, defeating the point of
# the sandbox.
#
# So: hard-refuse the clearly-dangerous roots, and warn (non-fatal) when
# credential material lives inside the chosen workspace. Override the hard
# refusal with ALLOW_UNSAFE_WORKSPACE=1 for the rare deliberate case — a
# conscious, visible choice, in keeping with the "no silent unsafe defaults" ethos.
# The override passes this guard only: the folder must still be one
# sc_workspace_mount can name, which `/` never is.
#
# This applies to EVERY tier. A tier-2 agent has no egress, but it still writes to
# the host filesystem, and host-side execution of what it writes is the risk here.
# A Windows user profile is a home directory by another name: AppData holds browser
# profiles and credential stores, and the Startup folder runs whatever is written
# there at the next login. On WSL it is mounted at /mnt/c/Users/<name>, nowhere near
# $HOME, so the home-directory checks never see it. Recognised by what is IN it
# rather than where it is — every profile root holds NTUSER.DAT, the user's registry
# hive — which holds for any automount root, any drive letter, WSL 1 or 2, and a
# Windows disk mounted on plain Linux. Prints the reason to refuse, or nothing.
# Looks only at the directory and a fixed shape below it (a Users folder, a drive
# root, a mount root holding drives), so it never walks an ordinary project tree.
_sc_windows_profile_reason() {
    local dir="$1" pattern match
    if [[ -e "$dir/NTUSER.DAT" ]]; then
        echo "that is a Windows user profile."
        return 0
    fi
    for pattern in "$dir/*/NTUSER.DAT" "$dir/Users/*/NTUSER.DAT" \
                   "$dir/*/Users/*/NTUSER.DAT"; do
        match="$(compgen -G "$pattern" | head -n 1 || true)"
        if [[ -n "$match" ]]; then
            echo "a Windows user profile (${match%/NTUSER.DAT}) is inside it."
            return 0
        fi
    done
}

# A host unix socket in a mounted directory can be connected to from inside, read-only
# or not, and the sandbox user has the host's uid: a tmux server or an ssh-agent there
# is host command execution. Prints the reason to refuse, or nothing.
# /tmp also holds ordinary scratch directories, so only it and what holds it are
# refused; /run and $XDG_RUNTIME_DIR hold nothing anyone means to hand over.
_sc_runtime_dir_reason() {
    local real="$1" runtime
    for runtime in /tmp /run /var/run ${XDG_RUNTIME_DIR:+"$XDG_RUNTIME_DIR"}; do
        [[ -d "$runtime" ]] || continue
        runtime="$(CDPATH='' cd "$runtime" && pwd -P)"
        if [[ "$real" == "$runtime" || ( "$runtime" != /tmp && "$real" == "$runtime"/* ) ]]; then
            echo "that is $runtime, where host processes keep their sockets."
            return 0
        elif [[ "$runtime" == "$real"/* ]]; then
            echo "$runtime, where host processes keep their sockets, is inside it."
            return 0
        fi
    done
}

# _sc_walk_path <absolute_path>
# Resolves <path> physically, component by component, without needing it to exist.
# Prints a line for every location it reaches by name, each its canonical parent
# plus its own name: "dir <location>" for a directory it enters, "link <location>"
# for a symlink it follows, and "stop <location>" for the component it stops at,
# which is missing, is not a directory, cannot be entered, or is the link past the
# 40th (a loop); none of these is an error. Then "end <path>": the canonical prefix
# the walk reached, plus whatever was left when it stopped, normalised by name
# alone. A `..` after the stop does not resume it, so a link past one is not seen
# until the missing component exists. A `..` only returns to a location already
# printed, so the lines name every place on the route, up to the stop, that a
# later swap could redirect.
_sc_walk_path() {
    local cur="" next comp target hops=0
    local -a todo more
    IFS=/ read -ra todo <<< "$1"
    while ((${#todo[@]})); do
        comp="${todo[0]}"
        todo=(${todo[@]+"${todo[@]:1}"})
        case "$comp" in
            ""|.) continue ;;
            ..) cur="${cur%/*}"; continue ;;   # cur is canonical, so this is physical
        esac
        next="$cur/$comp"
        if [[ -L "$next" ]] && ((hops < 40)) && target="$(readlink "$next")"; then
            hops=$((hops + 1))
            printf 'link %s\n' "$next"
            [[ "$target" != /* ]] || cur=""
            IFS=/ read -ra more <<< "$target"
            todo=(${more[@]+"${more[@]}"} ${todo[@]+"${todo[@]}"})
        elif [[ -d "$next" && -x "$next" && ! -L "$next" ]]; then
            printf 'dir %s\n' "$next"
            cur="$next"
        else
            printf 'stop %s\n' "$next"
            cur="$next"
            for comp in ${todo[@]+"${todo[@]}"}; do
                case "$comp" in
                    ""|.) ;;
                    ..) cur="${cur%/*}" ;;
                    *) cur="$cur/$comp" ;;
                esac
            done
            break
        fi
    done
    printf 'end %s\n' "${cur:-/}"
}

# dockade's config home holds what every later launch reads from the host: the
# plugins allowlist and the marketplace checkouts, which load into the agent, each
# tier's operator files (sc_operator_config), which become its instructions, and the
# MCP secrets and backups. A workspace that can write any of them is a channel out
# of its own session — even when they do not exist yet, because a workspace of
# ~/.config can create dockade/plugins on a fresh host. So each of those paths is
# walked rather than resolved: the home, its four fixed entries, every tier's
# folder, a sibling tier's included, and every entry already in one. A tier file
# that does not exist yet needs a write to its folder, which the folder's walk
# covers. Every location on the way counts too: a workspace holding
# ~/.config/dockade -> ~/dotfiles/dockade can replace the link with a real
# directory, and one holding a directory or missing name that a `..` leaves again
# can replace it with a link that sends the `..` elsewhere. Prints the reason to
# refuse, or nothing.
# Only a location strictly inside the workspace counts: "dir <workspace>" and "dir
# <ancestor>" do occur, and pass, because the agent cannot swap its own mount point
# or what holds it. Only "end" is checked for "is" and "holds". A workspace of /
# never matches "holds"; the root refusal covers it.
_sc_config_home_reason() {
    local real="$1" home what entry line kind path tier found
    local -a entries=("" /plugins /marketplaces /secrets /backups)
    home="$(sc_config_home)"
    [[ "$home" == /* ]] || home="$PWD/$home"
    what="dockade's config home ($home), where later launches read plugins, marketplaces, operator files and secrets"
    for tier in "${_SC_TIERS[@]}"; do
        entries+=("/$tier")
        for found in "$home/$tier"/* "$home/$tier"/.[!.]* "$home/$tier"/..?*; do
            if [[ -e "$found" || -L "$found" ]]; then
                entries+=("/$tier/${found##*/}")
            fi
        done
    done
    for entry in "${entries[@]}"; do
        while IFS= read -r line; do
            kind="${line%% *}"
            path="${line#* }"
            if [[ "$path" == "$real"/* ]]; then
                echo "$path is inside it, and leads to $what."
                return 0
            elif [[ "$kind" == end && "$path" == "$real" ]]; then
                echo "that is $path, part of $what."
                return 0
            elif [[ "$kind" == end && "$real" == "$path"/* ]]; then
                echo "it lies inside $path, part of $what."
                return 0
            fi
        done < <(_sc_walk_path "$home$entry")
    done
}

# _sc_warn_credentials <dir> <what> <access>
# Non-fatal: credential material sitting inside a directory about to be mounted.
# Legal (you may genuinely want to work there), but the agent will be able to read
# it, so make that visible rather than silent.
_SC_CREDENTIAL_PATHS=(.ssh .aws .gnupg .config/gcloud .kube .docker/config.json .netrc .git-credentials secrets)
_sc_warn_credentials() {
    local sensitive
    for sensitive in "${_SC_CREDENTIAL_PATHS[@]}"; do
        if [[ -e "$1/$sensitive" ]]; then
            echo "WARNING: $2 contains '$sensitive' — the sandbox agent will have $3 access to it." >&2
        fi
    done
}

sc_guard_workspace() {
    local real_workspace real_home reason
    real_workspace="$(CDPATH='' cd "$1" && pwd -P)"   # canonical, symlinks resolved
    real_home="$(CDPATH='' cd "$HOME" 2>/dev/null && pwd -P || echo "$HOME")"

    _sc_deny_workspace() {
        echo "REFUSING to mount workspace: $real_workspace" >&2
        echo "  $1" >&2
        echo "  This would give the sandbox agent RW access to sensitive host files or live" >&2
        echo "  host sockets, and anything it writes there runs on the host later (git hooks," >&2
        echo "  build scripts)." >&2
        echo "  Re-run from a dedicated project directory, or set ALLOW_UNSAFE_WORKSPACE=1" >&2
        echo "  to override this check deliberately; the folder name must still be usable" >&2
        echo "  as /workspace/<folder-name>, which the override does not change." >&2
        exit 1
    }

    if [[ "${ALLOW_UNSAFE_WORKSPACE:-}" != "1" ]]; then
        if [[ "$real_workspace" == "/" ]]; then
            _sc_deny_workspace "that is the filesystem root."
        elif [[ "$real_workspace" == "$real_home" ]]; then
            _sc_deny_workspace "that is your home directory."
        elif [[ "$real_home" == "$real_workspace"/* ]]; then
            # Workspace is an ancestor of $HOME, so mounting it exposes the whole home.
            _sc_deny_workspace "your home directory ($real_home) is inside it."
        fi
        reason="$(_sc_windows_profile_reason "$real_workspace")"
        [[ -z "$reason" ]] || _sc_deny_workspace "$reason"
        reason="$(_sc_runtime_dir_reason "$real_workspace")"
        [[ -z "$reason" ]] || _sc_deny_workspace "$reason"
        reason="$(_sc_config_home_reason "$real_workspace")"
        [[ -z "$reason" ]] || _sc_deny_workspace "$reason"
    fi

    # Under the override the config home earns only the content warnings below, so a
    # workspace of ~/.config, which holds it a level down, passes this guard silently:
    # the override is the operator saying they know what the directory holds.
    # (sc_workspace_mount then refuses that one by its folder name, `.config`.) The
    # exceptions are the read-only mounts from it, which do not yield to it: when the
    # marketplaces directory or a tier's operator file exists, sc_marketplaces and
    # sc_operator_config still refuse a workspace that holds it or a link or directory
    # on the way to it (a dotfiles-managed ~/.config/dockade), through
    # _sc_ro_workspace_reason.
    _sc_warn_credentials "$real_workspace" "workspace" "RW"
    # dockade's own state, when dockade is the workspace. A control-plane backup is
    # the whole policy store in a file `make restore` will install after checking its
    # shape, not its provenance — so a copy the agent can edit is a store the agent
    # gets to write. A task registry backup is the same for `make restore-tasks`, and
    # its export beside it carries the notes the operator hid from agents. The
    # Makefile keeps them out of the tree by default; this catches the ones that got
    # here anyway.
    if compgen -G "$real_workspace/backups/dockade-control-*.db" >/dev/null \
        || compgen -G "$real_workspace/backups/dockade-tasks-*.db" >/dev/null \
        || compgen -G "$real_workspace/backups/dockade-tasks-*.md" >/dev/null; then
        echo "WARNING: workspace contains dockade backups (backups/dockade-control-*.db," >&2
        echo "         dockade-tasks-*.db or .md) — the sandbox agent can read and rewrite them," >&2
        echo "         and 'make restore' or 'make restore-tasks' would install one. Move them" >&2
        echo "         under \$(make print-config-home)/backups." >&2
    fi

    # shellcheck disable=SC2034  # consumed by the sourcing launcher, not here
    SC_WORKSPACE="$real_workspace"
}

# ---------------------------------------------------------------------------
# Workspace mount path  ->  sets SC_WORKSPACE_MOUNT
# ---------------------------------------------------------------------------
# sc_workspace_mount <canonical_workspace>
#
# The checkout mounts at /workspace/<folder-name>, and the launch starts there, so
# each project gets its own Claude Code state in the shared config volume (DESIGN.md,
# "The sandbox image"). The name is the canonical path's basename, so a symlinked
# checkout is named by its target, as a ref is in sc_refs.
#
# A name outside the rule is refused, never rewritten into one the operator did not
# choose, and ALLOW_UNSAFE_WORKSPACE does not reach it: that override is about what
# may be mounted, this is about where. The rule is sc_refs' less its leading '-',
# which no workspace folder may have: no leading dot (`.`, `..`, a hidden mount),
# no space, no ':' (docker -v splits on it), ASCII only. `/` has no name at all.
sc_workspace_mount() {
    # In another locale `=~` may let A-Z match an accented letter; bash restores it on
    # return.
    local LC_ALL=C
    local workspace="${1:?sc_workspace_mount needs the canonical workspace}" name
    name="${workspace##*/}"
    # sc_refs' name rule, less its leading '-'.
    if [[ ! "$name" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]]; then
        echo "REFUSING to mount workspace: $workspace" >&2
        echo "  Its folder name '$name' cannot be its path in the sandbox," >&2
        echo "  /workspace/<folder-name>: a usable name starts with an ASCII letter, a digit" >&2
        echo "  or '_', and holds only those, '.' and '-'. Rename the folder, or clone the" >&2
        echo "  repo into one whose name is usable; ALLOW_UNSAFE_WORKSPACE does not change this." >&2
        exit 1
    fi
    # shellcheck disable=SC2034  # consumed by the sourcing launcher, not here
    SC_WORKSPACE_MOUNT="/workspace/$name"
}

# ---------------------------------------------------------------------------
# Host config home  ->  echoes the dockade config directory
# ---------------------------------------------------------------------------
# Durable per-machine settings live OUTSIDE this repo. Not tidiness: a sandbox
# launched with dockade as its workspace bind-mounts this tree READ-WRITE, so
# anything configured from inside the repo is agent-writable — and a knob that
# decides which code loads into the agent must not be. That is the same reason
# MCP_SECRETS sits here (Makefile), which is why both derive from this one path;
# `make consistency` asserts the two spellings agree.
#
# Precedence: DOCKADE_CONFIG_HOME (explicit) > $XDG_CONFIG_HOME/dockade (the
# spec) > ~/.config/dockade (the fallback the spec itself prescribes).
#
# Echoes rather than setting a global: it is a plain string, needed in string
# contexts, and every caller wants it interpolated.
sc_config_home() {
    if [[ -n "${DOCKADE_CONFIG_HOME:-}" ]]; then
        printf '%s\n' "$DOCKADE_CONFIG_HOME"
    else
        printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/dockade"
    fi
}

# Every tier's name, which is its folder in the config home (sc_tier_config_dir).
# One list, because the workspace guard walks every tier's folder, not only the
# launching tier's: a workspace that can write a sibling tier's file injects
# instructions into that tier's next launch. tests/test_sandbox_wiring.py holds each
# launcher's tier to it.
_SC_TIERS=(claude opencode agy)

# sc_tier_config_dir <tier>  ->  echoes the tier's folder in the config home
#
# Follows sc_config_home, so DOCKADE_CONFIG_HOME and XDG_CONFIG_HOME move it too. A
# tier not in _SC_TIERS is refused: its folder would be one the guard never walks.
sc_tier_config_dir() {
    local tier="${1:?sc_tier_config_dir needs a tier}" known
    for known in "${_SC_TIERS[@]}"; do
        if [[ "$tier" == "$known" ]]; then
            printf '%s/%s\n' "$(sc_config_home)" "$tier"
            return 0
        fi
    done
    echo "ERROR: '$tier' is not a tier sandbox-lib.sh knows (_SC_TIERS: ${_SC_TIERS[*]})." >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Read-only mount guard  ->  echoes why <canonical_dir> must not be mounted
# ---------------------------------------------------------------------------
# Echoes nothing when the path may be mounted. Shared by every read-only host
# mount (sc_marketplaces, sc_refs, sc_operator_config), so they cannot drift apart
# on what they refuse.
#
# Read-only keeps the agent from WRITING what it reads; it does nothing about
# what it can read. So the same over-broad paths sc_guard_workspace refuses are
# refused here too, for the narrower reason that they would hand the agent the
# contents of a home directory — and, unlike the workspace, with no override.
#
# Read-only is also narrower than it sounds: it stops writes to files,
# directories and symlinks, but a unix socket under a :ro bind can still be
# connected to and a FIFO written — and the sandbox user has the host's uid, so
# a tmux server or an ssh-agent would take the agent's requests. Hence the
# runtime directories, where those live, and a shallow scan for any elsewhere.
_sc_ro_mount_reason() {
    local real="$1" real_home secrets reason hit
    real_home="$(CDPATH='' cd "$HOME" 2>/dev/null && pwd -P || echo "$HOME")"
    secrets="$(sc_config_home)/secrets"
    [[ -d "$secrets" ]] && secrets="$(CDPATH='' cd "$secrets" && pwd -P)"

    if [[ "$real" == "/" ]]; then
        echo "that is the filesystem root."
        return 0
    elif [[ "$real" == "$real_home" ]]; then
        echo "that is your home directory."
        return 0
    elif [[ "$real_home" == "$real"/* ]]; then
        echo "your home directory ($real_home) is inside it."
        return 0
    fi
    # A footgun the config-home layout creates rather than one it inherits: the
    # MCP client credentials live under the same config home as the marketplaces,
    # so the obvious near-miss (~/.config/dockade) would mount the secrets tree
    # into the sandbox. Read-only is no comfort for a credential — readable IS
    # the compromise. Inside it as much as holding it: an operator file linked into
    # the secrets directory under another name is a credential all the same.
    if [[ "$secrets" == "$real" || "$secrets" == "$real"/* ]]; then
        echo "the MCP secrets directory ($secrets) is inside it."
        return 0
    elif [[ "$real" == "$secrets"/* ]]; then
        echo "it lies inside the MCP secrets directory ($secrets)."
        return 0
    fi
    # The scan below covers the tmux-<uid> and ssh-* children of /tmp.
    reason="$(_sc_runtime_dir_reason "$real")"
    if [[ -n "$reason" ]]; then
        echo "$reason"
        return 0
    fi
    reason="$(_sc_windows_profile_reason "$real")"
    if [[ -n "$reason" ]]; then
        echo "$reason"
        return 0
    fi
    # Three levels reaches /tmp/tmux-<uid>/default and /tmp/ssh-*/agent.* from
    # anything holding them, without walking a whole checkout on every launch.
    hit="$(find "$real" -maxdepth 3 \( -type s -o -type p \) -print -quit 2>/dev/null || true)"
    if [[ -n "$hit" ]]; then
        echo "it holds a socket or FIFO ($hit), which read-only does not stop the agent using."
    fi
}

# _sc_ro_workspace_reason <canonical_path> <path_as_given> <canonical_workspace>
# Echoes why a read-only mount of <path> would still be writable through the
# workspace, which is mounted read-write, or nothing. Shared by the mounts that
# come from the config home (sc_marketplaces, sc_operator_config), which, unlike
# the workspace guard, do not yield to ALLOW_UNSAFE_WORKSPACE.
# <canonical_path> is, lies inside or holds the workspace; or a link on the way
# that the workspace holds can be replaced with a real directory or file, and a
# directory that a `..` leaves again with a link, either of which a later launch
# with the same path mounts. A "stop" inside the workspace before the end needs
# the layout to change after the caller resolved every component.
_sc_ro_workspace_reason() {
    local real="$1" path="$2" workspace="$3" line
    if [[ "$real" == "$workspace" ]]; then
        echo "that is the workspace, which is mounted read-write."
        return 0
    elif [[ "$workspace" == "/" || "$real" == "$workspace"/* ]]; then
        echo "it is inside the workspace ($workspace), which is mounted read-write."
        return 0
    elif [[ "$workspace" == "$real"/* ]]; then
        echo "the workspace ($workspace) is inside it, and is mounted read-write."
        return 0
    fi
    [[ "$path" == /* ]] || path="$PWD/$path"
    while IFS= read -r line; do
        case "$line" in
            "link $workspace"/*) printf "%s" "the link ${line#link }" ;;
            "dir $workspace"/*) printf "%s" "the directory ${line#dir }" ;;
            "stop $workspace"/*) printf "%s" "the name ${line#stop }" ;;
            *) continue ;;
        esac
        echo " on the way to it is inside the workspace ($workspace), which is mounted read-write."
        return 0
    done < <(_sc_walk_path "$path")
}

# ---------------------------------------------------------------------------
# Plugin marketplaces  ->  sets SC_MARKETPLACE_ARGS (array), SC_MARKETPLACE_DESC
# ---------------------------------------------------------------------------
# Claude Code can take a marketplace from a local DIRECTORY, and a directory
# source is referenced in place — no clone, no copy, no egress, no credential
# (measured; see NOTES.md). That is what makes this the right shape for a
# container with no governed git path: the human clones marketplace repos on the
# host, and the sandbox reads them.
#
# Mounted READ-ONLY, and that is the load-bearing part. A writable plugin tree is
# a cross-session channel: the agent edits a skill or a hook now, and it lands in
# its own context — or executes — on the next boot, outside the diff review that
# /workspace commits get. Read-only costs nothing, because a directory-source
# marketplace is never written to in normal use (installing from a chmod-a-w tree
# works).
#
# Auto-mounted when the default path EXISTS, so the common case needs no
# configuration at all. SANDBOX_MARKETPLACES_DIR overrides the host path — and
# when it is set explicitly, a missing directory is FATAL rather than skipped:
# the operator named something specific, and silently launching without it is how
# you spend a session wondering where your plugins went.
#
# The container path is fixed at /marketplaces and is NOT the host path, because
# Claude Code records the path it was given verbatim as the marketplace's
# installLocation — it has to be the path as seen from inside.
#
# sc_marketplaces <canonical_workspace>: a directory that is the workspace, lies
# inside it, holds it, or is reached through a link or directory the workspace
# holds is refused, because the workspace is mounted read-write and the same files
# would be writable through /workspace whatever the :ro says
# (_sc_ro_workspace_reason). Only this launch's workspace is known here. A
# sibling's cannot hold the default directory, since sc_guard_workspace refuses
# what holds the config home, but the cases that guard cannot see stay open
# (DESIGN.md, "Plugin marketplaces").
sc_marketplaces() {
    local workspace="${1:?sc_marketplaces needs the canonical workspace}"
    SC_MARKETPLACE_ARGS=()
    SC_MARKETPLACE_DESC="none"

    local dir explicit=false
    if [[ -n "${SANDBOX_MARKETPLACES_DIR:-}" ]]; then
        dir="$SANDBOX_MARKETPLACES_DIR"
        explicit=true
    else
        dir="$(sc_config_home)/marketplaces"
    fi

    if [[ ! -d "$dir" ]]; then
        if [[ "$explicit" == "true" ]]; then
            echo "ERROR: SANDBOX_MARKETPLACES_DIR is set to '$dir', which is not a" >&2
            echo "       directory. Refusing to launch without it — unset the variable" >&2
            echo "       to run with no marketplaces." >&2
            exit 1
        fi
        SC_MARKETPLACE_DESC="none ($dir does not exist)"
        return 0
    fi

    # Physical, like [[ -d ]] and the walk, so a `..` after a link means the same
    # directory to all three; CDPATH would send a relative name elsewhere.
    local real reason
    real="$(CDPATH='' cd -P "$dir" && pwd -P)"
    reason="$(_sc_ro_mount_reason "$real")"
    [[ -n "$reason" ]] || reason="$(_sc_ro_workspace_reason "$real" "$dir" "$workspace")"
    if [[ -n "$reason" ]]; then
        echo "REFUSING to mount marketplaces: $real" >&2
        echo "  $reason" >&2
        echo "  Point SANDBOX_MARKETPLACES_DIR at a directory that holds ONLY" >&2
        echo "  marketplace checkouts (default: $(sc_config_home)/marketplaces)." >&2
        exit 1
    fi

    # Count what is actually there, so the launch line distinguishes "mounted, 3
    # marketplaces" from "mounted, and empty" — the second looks identical to a
    # working setup from the agent's side until a plugin is missing.
    #
    # Manifests PRESENT, not manifests valid: validating one means parsing JSON,
    # and this runs on the HOST, where jq is not a dependency this repo gets to
    # assume. The boot-side registration does parse them (the image has jq) and
    # warns per unusable file, so the exact figure surfaces there.
    local found=0 d
    if [[ -f "$real/.claude-plugin/marketplace.json" ]]; then
        found=1
    else
        for d in "$real"/*; do
            [[ -f "$d/.claude-plugin/marketplace.json" ]] && found=$((found + 1))
        done
    fi

    # shellcheck disable=SC2034  # both are consumed by the sourcing launcher, not here
    SC_MARKETPLACE_ARGS=(-v "$real":/marketplaces:ro)
    # shellcheck disable=SC2034
    SC_MARKETPLACE_DESC="$real -> /marketplaces:ro ($found marketplace(s))"
    if (( found == 0 )); then
        echo "NOTE: $real holds no .claude-plugin/marketplace.json — mounting it" >&2
        echo "      anyway, but no marketplace will be registered." >&2
    fi
}

# ---------------------------------------------------------------------------
# Reference mounts  ->  sets SC_REFS_ARGS (array), SC_REFS_DESC
# ---------------------------------------------------------------------------
# SANDBOX_REFS is a comma-separated list of host directories to mount READ-ONLY
# at /refs/<name>, as material for the agent to read: another checkout, a
# library's source, a design archive. Every tier gets them; DESIGN.md says why.
#
#     SANDBOX_REFS=~/src/dockade,docs-b=/srv/b/docs
#
# <name> is the basename of the resolved path unless the entry says `name=path`.
# Two entries with one name are refused rather than suffixed: a /refs/docs-2
# would leave the agent guessing which is which. Every entry was named on
# purpose, so anything wrong with one is fatal, as with SANDBOX_MARKETPLACES_DIR.
#
# A symlink inside a ref resolves in the container's namespace, so one pointing
# at ~/.ssh dangles rather than leaks; what else inside a ref matters is
# _sc_ro_mount_reason's to find.
sc_refs() {
    SC_REFS_ARGS=()
    SC_REFS_DESC="none"
    [[ -n "${SANDBOX_REFS:-}" ]] || return 0

    local entries entry name path real reason sensitive seen=" "
    _sc_deny_ref() {
        echo "REFUSING to mount ref '$entry' (from SANDBOX_REFS)" >&2
        echo "  $1" >&2
        exit 1
    }

    # Newlines become commas: `read` stops at the first one, and a multi-line
    # export would otherwise lose everything after it without a word.
    IFS=',' read -ra entries <<< "${SANDBOX_REFS//$'\n'/,}"
    for entry in "${entries[@]}"; do
        entry="$(printf '%s' "$entry" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        [[ -n "$entry" ]] || continue

        name=""
        path="$entry"
        if [[ "$entry" =~ ^([A-Za-z0-9._-]+)=(.*)$ ]]; then
            name="${BASH_REMATCH[1]}"
            path="${BASH_REMATCH[2]}"
        fi
        # A quoted SANDBOX_REFS reaches us with its tildes unexpanded.
        # shellcheck disable=SC2088  # matching the literal tilde is the point
        [[ "$path" == "~" || "$path" == "~/"* ]] && path="$HOME${path:1}"

        [[ -d "$path" ]] || _sc_deny_ref "'$path' is not a directory."
        real="$(CDPATH='' cd "$path" && pwd -P)"
        reason="$(_sc_ro_mount_reason "$real")"
        [[ -z "$reason" ]] || _sc_deny_ref "$real: $reason"
        # docker -v splits on ':', so a path holding one would mount something else.
        [[ "$real" != *:* ]] || _sc_deny_ref "$real contains ':', which docker -v cannot mount."
        # The workspace only warns about these, because it may hold one by the
        # way; a ref that IS one was named on purpose, and has nothing else in it.
        for sensitive in "${_SC_CREDENTIAL_PATHS[@]}"; do
            if [[ "$real" == */"$sensitive" || "$real" == */"$sensitive"/* ]]; then
                _sc_deny_ref "$real is credential material ('$sensitive')."
            fi
        done

        [[ -n "$name" ]] || name="${real##*/}"
        # No leading dot: that would also allow `..`, and hide the ref from both
        # `ls /refs` and boundary-check.sh's /refs/*/ probe. It also keeps a ref
        # named `.claude` from becoming /refs/.claude. Claude's user settings
        # allow reads under /refs, and an extra directory given on the command
        # line does load skills from its root's .claude; the settings allow was
        # measured not to, so this rule is the margin should a release change
        # that (DESIGN.md, "Reference mounts"). sc_workspace_mount's rule is this
        # one less the leading '-', which a ref may have.
        if [[ ! "$name" =~ ^[A-Za-z0-9_-][A-Za-z0-9._-]*$ ]]; then
            _sc_deny_ref "'$name' is not a usable name under /refs; give one as name=path."
        fi
        [[ "$seen" != *" $name "* ]] || _sc_deny_ref "/refs/$name is already taken; give one as name=path."
        seen="$seen$name "

        _sc_warn_credentials "$real" "ref '$name'" "read"
        SC_REFS_ARGS+=(-v "$real":/refs/"$name":ro)
        if [[ "$SC_REFS_DESC" == "none" ]]; then
            SC_REFS_DESC="$real -> /refs/$name:ro"
        else
            SC_REFS_DESC="$SC_REFS_DESC"$'\n'"           $real -> /refs/$name:ro"
        fi
    done
}

# ---------------------------------------------------------------------------
# Operator customization  ->  sets SC_OPERATOR_DIR, SC_OPERATOR_ARGS (array),
#                             SC_CUSTOM_DESC
# ---------------------------------------------------------------------------
# sc_operator_config <tier> <canonical_workspace> <file>...
#
# The operator's own additions to a tier, per machine, from the tier's folder in the
# config home (sc_tier_config_dir): each <file> is a name in the format of the tier
# CLI's own file, such as CLAUDE.md, which the tier's boot appends to its baked
# instructions. DESIGN.md, "Operator customization", says what may be extended and
# what stays fixed.
#
# Each named file that is present is mounted ALONE, read-only, at /operator/<file>,
# not the folder: the sandbox gets no host path it does not need, as for the plugin
# allowlist. A link, a dotfiles one say, is followed physically and its target is
# what is mounted. Absent, nothing is mounted and nothing is said. Present but
# unusable is fatal, as an explicit SANDBOX_MARKETPLACES_DIR is: the operator put it
# there on purpose, and a launch without it would look like one with it. Refused:
# anything but a regular file, a dangling link included; what every read-only mount
# refuses (_sc_ro_mount_reason); a file the workspace can write, or reach on the
# way to (_sc_ro_workspace_reason), whatever ALLOW_UNSAFE_WORKSPACE says; a path
# holding ':', which docker -v splits on; and credential material, as for a ref.
#
# SC_OPERATOR_DIR is the folder, resolved physically, or empty when there is none:
# for a later reader that builds from it rather than mounting it. Never mounted.
#
# SC_CUSTOM_DESC is the launch banner's "Custom:" line, one entry per active
# customization, empty when there is none so the banner prints what it did before.
# Started here, so this runs before anything that adds to it with _sc_custom_add.
# shellcheck disable=SC2034  # the globals are consumed by the sourcing launcher
sc_operator_config() {
    local tier="${1:?sc_operator_config needs a tier}"
    local workspace="${2:?sc_operator_config needs the canonical workspace}"
    shift 2
    local dir name path real reason sensitive
    SC_OPERATOR_DIR=""
    SC_OPERATOR_ARGS=()
    SC_CUSTOM_DESC=""
    dir="$(sc_tier_config_dir "$tier")" || exit 1
    [[ "$dir" == /* ]] || dir="$PWD/$dir"
    if [[ -d "$dir" ]]; then
        SC_OPERATOR_DIR="$(CDPATH='' cd -P "$dir" && pwd -P)"
    fi

    _sc_deny_operator() {
        echo "REFUSING to mount operator file: $path" >&2
        echo "  $1" >&2
        echo "  Keep $name in $dir as a regular file of its own, outside every workspace:" >&2
        echo "  copy it there rather than linking it, or remove it to launch without it." >&2
        exit 1
    }

    for name in "$@"; do
        path="$dir/$name"
        [[ -e "$path" || -L "$path" ]] || continue
        [[ -e "$path" ]] || _sc_deny_operator "it is a link to nothing."
        [[ -f "$path" ]] || _sc_deny_operator "it is not a regular file."
        real="$(_sc_walk_path "$path" | sed -n 's/^end //p')"
        reason="$(_sc_ro_mount_reason "$real")"
        [[ -n "$reason" ]] || reason="$(_sc_ro_workspace_reason "$real" "$path" "$workspace")"
        [[ -z "$reason" ]] || _sc_deny_operator "$real: $reason"
        [[ "$real" != *:* ]] || _sc_deny_operator "$real contains ':', which docker -v cannot mount."
        for sensitive in "${_SC_CREDENTIAL_PATHS[@]}"; do
            if [[ "$real" == */"$sensitive" || "$real" == */"$sensitive"/* ]]; then
                _sc_deny_operator "$real is credential material ('$sensitive')."
            fi
        done
        SC_OPERATOR_ARGS+=(-v "$real":/operator/"$name":ro)
        _sc_custom_add "$real -> /operator/$name:ro"
    done
}

# _sc_custom_add <entry>: one more line for the banner's "Custom:" (sc_operator_config).
_sc_custom_add() {
    if [[ -z "${SC_CUSTOM_DESC:-}" ]]; then
        SC_CUSTOM_DESC="$1"
    else
        SC_CUSTOM_DESC="$SC_CUSTOM_DESC"$'\n'"           $1"
    fi
}

# ---------------------------------------------------------------------------
# Plugin allowlist  ->  sets SC_PLUGIN_ARGS (array), SC_PLUGINS_DESC
# ---------------------------------------------------------------------------
# WHICH plugins are enabled is a separate decision from which marketplaces are
# available, and deliberately so: registering a marketplace only makes plugins
# installable, while a plugin loads only if it appears in `enabledPlugins`. It is
# also independent of the mount — an id may name a marketplace the config volume
# already knows, with nothing mounted at all.
#
# It has to be declared host-side because the plugins a sandbox loads are this
# launch's, rendered at boot into its own /etc and handed to every `claude` call
# (see claude-sandbox/tier-setup.sh), while an in-session enable lands in the
# shared settings.json, which is rewritten from the image on every boot and is
# outranked by the launch's file anyway. Without this file the
# feature would ship as "your marketplaces are mounted, now re-enable your
# plugins every session".
#
# Carried as an ENV VAR rather than a second mount, on the invariant that the
# sandbox gets no host path it does not need: the list is a handful of ids, and
# the file is read here, on the host. SANDBOX_PLUGINS overrides the file, which
# keeps the env > file > default precedence the launchers use everywhere else.
#
# Format: one `plugin@marketplace` per line, `#` comments and blank lines
# ignored. Commas are accepted too, because a one-line SANDBOX_PLUGINS is the
# natural way to write it on a command line.
sc_plugin_allowlist() {
    SC_PLUGIN_ARGS=()
    SC_PLUGINS_DESC="none"

    local raw="" file
    file="$(sc_config_home)/plugins"
    if [[ -n "${SANDBOX_PLUGINS:-}" ]]; then
        raw="$SANDBOX_PLUGINS"
    elif [[ -f "$file" ]]; then
        raw="$(sed 's/#.*//' "$file")"
    fi

    # Normalize to a single space-separated line: strip comments (above), split on
    # commas and newlines, collapse whitespace.
    local list
    list="$(printf '%s' "$raw" | tr ',\n\t' '   ' | tr -s ' ' | sed 's/^ *//; s/ *$//')"
    [[ -z "$list" ]] && return 0

    # shellcheck disable=SC2034  # both are consumed by the sourcing launcher, not here
    SC_PLUGIN_ARGS=(-e "SANDBOX_PLUGINS=$list")
    # shellcheck disable=SC2034
    SC_PLUGINS_DESC="$list"
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
# sc_ensure_network <net> <allow_fallback>
#
# docker-compose.yml owns sandbox-net (fixed subnet, internal: true, data-plane
# services on it). Attaching here buys Docker's embedded DNS (127.0.0.11, used
# only on user-defined networks — the firewall already expects it), name
# resolution for data-plane services, and isolation from default-bridge containers.
#
# allow_fallback=true (Claude's tier): if the network is absent, create a plain
# NON-internal bridge so the sandbox still runs standalone with direct egress via
# its own firewall allowlist. That allowlist is Anthropic's, so the fallback is
# Claude's, not every governed tier's. allow_fallback=false (every other tier): a
# tier that reaches nothing outside except through the data plane has nothing to
# fall back TO, and silently creating a non-internal bridge would hand it the
# direct egress its design says it must not have — so refuse instead.
#
# Which is why, without the fallback, existence is not the question. A Claude
# standalone run creates sandbox-net as exactly that plain non-internal bridge, and
# the network outlives the container that prompted it. A later launch of any other
# tier asking only "does it exist?" would adopt it and get the egress the refusal
# above exists to prevent, with nothing to see: same name, same DNS, same subnet
# shape. So assert the property the tier depends on rather than the name it travels
# under.
sc_ensure_network() {
    local net="$1" allow_fallback="$2"

    if docker network inspect "$net" >/dev/null 2>&1; then
        if [[ "$allow_fallback" == "true" ]]; then
            return 0
        fi
        if [[ "$(docker network inspect "$net" -f '{{.Internal}}' 2>/dev/null)" == "true" ]]; then
            return 0
        fi
        echo "ERROR: network '$net' exists but is NOT internal." >&2
        echo "       This tier reaches nothing outside except through the data" >&2
        echo "       plane, which the network being internal enforces, so adopting" >&2
        echo "       this one would hand it direct egress. A bridge left behind by" >&2
        echo "       a Claude standalone run looks exactly like this." >&2
        echo "       Remove it and start the infrastructure:" >&2
        echo "         docker network rm $net && make -C \"$SANDBOX_REPO_ROOT\" up" >&2
        exit 1
    fi

    if [[ "$allow_fallback" != "true" ]]; then
        echo "ERROR: network '$net' not found." >&2
        echo "       Start the infrastructure first:  make -C \"$SANDBOX_REPO_ROOT\" up" >&2
        echo "       (This tier will not fall back to a non-internal bridge — that" >&2
        echo "        would grant egress its design forbids.)" >&2
        exit 1
    fi
    echo "NOTE: network '$net' not found — creating a plain bridge (no egress proxy)." >&2
    echo "      For governed/audited egress, run 'make -C \"$SANDBOX_REPO_ROOT\" up' first." >&2
    docker network create "$net" >/dev/null
}

# sc_service_ip <container> <net>  — echoes the IP or nothing.
# Discovering the address (rather than hardcoding it) keeps the launchers working
# even if the compose subnet changes.
sc_service_ip() {
    docker inspect -f \
        "{{with index .NetworkSettings.Networks \"$2\"}}{{.IPAddress}}{{end}}" \
        "$1" 2>/dev/null || true
}

# The two containers docker-compose.yml puts on both the sandbox network and mcp-net,
# by the container_name it gives them, and mcp-net by its compose name. One place,
# so the container sc_governed_egress grants the firewall /32 to and the one
# sc_refuse_dual_homed exempts cannot be two; EGRESS_PROXY_NAME and
# TOOL_GATEWAY_NAME override the first two for both functions.
SC_EGRESS_PROXY_DEFAULT=egress-proxy
SC_TOOL_GATEWAY_DEFAULT=tool-gateway
SC_MCP_NET=mcp-net

# sc_refuse_dual_homed <net>
#
# Refuse to launch while any container other than the egress proxy and the tool
# gateway is on both <net> and mcp-net. One there sits beside the credential-holding
# MCP servers yet is reachable from anything on the agent's network (a sibling
# sandbox, any other container there): a route onto mcp-net. The sandbox's own
# firewall grants only per-service /32s, so this is a second layer behind it, not
# the only one. tests/test_topology.py holds the compose files to that; this asks Docker,
# so it also sees another compose project's servers and a hand-run container.
#
# `-a`, so a stopped container that would rejoin both on its next start counts. The
# filter only narrows; the exact split of each network list decides. No mcp-net
# (standalone, or the stack never started) answers empty: nothing can bridge onto
# a network that does not exist. A query that fails refuses too, since by now the
# launcher has reached the daemon. Run it after sc_ensure_network, before any
# discovery, so a refusal comes before a health wait and before `docker run`.
sc_refuse_dual_homed() {
    local net="$1"
    local proxy_name="${EGRESS_PROXY_NAME:-$SC_EGRESS_PROXY_DEFAULT}"
    local gateway_name="${TOOL_GATEWAY_NAME:-$SC_TOOL_GATEWAY_DEFAULT}"
    local listing name networks entry on_net on_mcp offender
    local entries=() offenders=()

    if ! listing="$(docker ps -a --filter "network=$SC_MCP_NET" \
            --format '{{.Names}} {{.Networks}}')"; then
        echo "ERROR: could not list the containers on $SC_MCP_NET — not launching." >&2
        echo "       The launch checks that nothing but $proxy_name and $gateway_name" >&2
        echo "       sits on both $net and $SC_MCP_NET, and that check did not run." >&2
        exit 1
    fi

    while read -r name networks; do
        if [[ "$name" == "$proxy_name" || "$name" == "$gateway_name" ]]; then
            continue
        fi
        on_net=false
        on_mcp=false
        IFS=, read -ra entries <<< "$networks"
        for entry in ${entries[@]+"${entries[@]}"}; do
            if [[ "$entry" == "$net" ]]; then on_net=true; fi
            if [[ "$entry" == "$SC_MCP_NET" ]]; then on_mcp=true; fi
        done
        if [[ "$on_net" == "true" && "$on_mcp" == "true" ]]; then
            offenders+=("$name ($networks)")
        fi
    done <<< "$listing"

    if (( ${#offenders[@]} == 0 )); then
        return 0
    fi
    echo "ERROR: on both $net and $SC_MCP_NET, which only $proxy_name and $gateway_name may be:" >&2
    for offender in "${offenders[@]}"; do
        echo "         $offender" >&2
    done
    echo "       A container there sits beside the credential-holding MCP servers yet is" >&2
    echo "       reachable from anything on $net (a sibling sandbox, any other container" >&2
    echo "       there): it is a route onto $SC_MCP_NET. The sandbox firewall's per-service" >&2
    echo "       grants are the first layer against that; this check is the second." >&2
    echo "       Disconnect it from $net, or remove it:" >&2
    echo "         docker network disconnect $net <name>    docker rm -f <name>" >&2
    echo "       If it is the egress proxy or the tool gateway under another name, set" >&2
    echo "       EGRESS_PROXY_NAME or TOOL_GATEWAY_NAME to it." >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Service readiness
# ---------------------------------------------------------------------------
# Discovering a service's IP proves a container EXISTS, not that it can serve.
# The gap is not cosmetic for any tier; for example:
#   - tier 1: the egress proxy fails CLOSED while the control plane is not yet
#     answering /authorize, so a sandbox launched into the boot window gets real
#     denials written to the audit log — noise that reads exactly like policy.
#   - tier 2: llama-server binds its port immediately but returns 503 until the
#     GGUF is loaded and offloaded, which takes minutes; opencode's first turn
#     would simply fail.
# Both are the same bug — acting on "container up" when we mean "service ready" —
# so every tier gates through the one helper below.

# sc_health_status <container> — echoes healthy|unhealthy|starting, or NOTHING
# when the container declares no healthcheck (or does not exist).
sc_health_status() {
    docker inspect -f \
        '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
        "$1" 2>/dev/null || true
}

# sc_wait_healthy <container> <why_it_matters> [timeout_seconds]
#
# Block until <container> reports healthy, then return. Fatal on timeout.
#
# An empty health status means the compose entry declares no healthcheck, or the
# service was started by hand — proceed rather than block, because the absence of
# a probe is not evidence of a problem (and gating on a probe that does not exist
# would break every hand-rolled setup).
#
# 'unhealthy' is treated as RETRYABLE, not terminal: Docker flips a container back
# to healthy the moment a probe succeeds, and a model whose load outruns its
# start_period passes through unhealthy on the way up. Only the timeout is fatal.
sc_wait_healthy() {
    local name="$1" why="$2" timeout="${3:-180}"
    local status waited=0 announced=false

    status="$(sc_health_status "$name")"
    if [[ -z "$status" ]]; then
        return 0
    fi

    while [[ "$status" != "healthy" ]]; do
        if (( waited >= timeout )); then
            echo "ERROR: '$name' is still '$status' after ${timeout}s — not launching." >&2
            echo "       $why" >&2
            echo "       Check it with:  docker logs $name" >&2
            exit 1
        fi
        if [[ "$announced" == "false" ]]; then
            echo "Waiting for '$name' to become healthy (currently '$status')..." >&2
            announced=true
        fi
        sleep 2
        waited=$((waited + 2))
        status="$(sc_health_status "$name")"
        # It can vanish mid-wait (crash, or a `compose down` in another terminal).
        if [[ -z "$status" ]]; then
            echo "ERROR: '$name' disappeared while waiting for it to become healthy." >&2
            exit 1
        fi
    done

    if [[ "$announced" == "true" ]]; then
        echo "  '$name' is healthy." >&2
    fi
}

# ---------------------------------------------------------------------------
# Governed egress  ->  sets SC_PROXY_ENV_ARGS (array), SC_EGRESS_DESC, SC_TOOLS_DESC,
#                      SC_NO_GATEWAY
# ---------------------------------------------------------------------------
# sc_governed_egress <net> [model-host ...]
#
# How a governed tier reaches the data plane on <net>: the egress proxy, and in
# governed mode the tool gateway. Run it after sc_ensure_network, which decides
# whether <net> may be the standalone fallback; whether a tier calls this at all
# stays in its launcher.
#
# The model hosts are the tier's own inference endpoints, each a plain DNS name
# with at least one dot (no scheme, port, IP literal or wildcard: what the proxy is
# CONNECTed to on 443). A dotless name is refused too: localhost and the proxy's own
# name are in NO_PROXY, so a probe of one would go direct and never test the proxy.
# On the governed branch a non-empty list reaches the container as one
# SANDBOX_MODEL_HOSTS, space-separated, and boundary-check.sh asserts each answers
# through the proxy; standalone mode has no proxy to assert it through, so passes
# nothing. A tier names its hosts here, as a value, rather than forking the check.
# Claude's launcher names none: api.anthropic.com is the lifeline boundary-check.sh
# already probes for every cloud tier. Any other shape is refused before docker is
# asked anything, in either mode.
#
# Discover the egress proxy on <net> (started by compose). Present -> governed
# egress: route the sandbox's HTTP(S) through it (HTTPS_PROXY) and allowlist it in
# the firewall (EGRESS_PROXY_IP). Absent -> direct egress via the firewall only;
# SC_EGRESS_DESC says so.
#
# The gateway is discovered only on the governed branch, so no caller can ask for
# it without the proxy: that branch is what mode-gates the grant (DESIGN.md, "MCP
# gateway": firewall grants are mode-gated).
#
# SC_NO_GATEWAY is "true" only when the proxy is present and the gateway is not,
# and empty otherwise. SC_TOOLS_DESC states only that fact; what it means for the
# tier's tool surface is the launcher's to say, since it depends on how its CLI
# loads MCP servers.
#
# EGRESS_PROXY_NAME/PORT and TOOL_GATEWAY_NAME/PORT are environment overrides.
# shellcheck disable=SC2034  # the descriptions are consumed by the sourcing launcher
sc_governed_egress() {
    local net="$1"
    shift
    local proxy_name="${EGRESS_PROXY_NAME:-$SC_EGRESS_PROXY_DEFAULT}"
    local proxy_port="${EGRESS_PROXY_PORT:-8080}"
    local proxy_ip gateway_name gateway_port gateway_ip proxy_url no_proxy_list
    local label='[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?' model_host model_hosts=""
    for model_host in "$@"; do
        # The second test refuses a dotted quad, which the first reads as labels:
        # no top-level domain is all digits.
        if ! [[ "$model_host" =~ ^($label\.)+$label$ ]] || [[ "$model_host" =~ (^|\.)[0-9]+$ ]]; then
            echo "ERROR: model host '$model_host' is not a plain DNS name." >&2
            echo "       Name the host alone, with its domain: no scheme, port, IP literal or wildcard." >&2
            exit 1
        fi
        model_hosts="${model_hosts:+$model_hosts }$model_host"
    done
    proxy_ip="$(sc_service_ip "$proxy_name" "$net")"

    SC_PROXY_ENV_ARGS=()
    SC_NO_GATEWAY=""
    if [[ "$proxy_ip" =~ ^[0-9.]+$ ]]; then
        # The proxy exists, but until the control plane answers /authorize the addon
        # fails CLOSED — so launching now would not stall the agent's first requests,
        # it would DENY them and write those denials to the audit log as if they were
        # policy. Wait for the proxy's healthcheck (which compose in turn gates on the
        # control plane being healthy) so the log stays a record of decisions.
        sc_wait_healthy "$proxy_name" \
            "Governed egress is unavailable, and this tier's traffic all flows through it."

        # BY ADDRESS, not by name — and not because the name would resolve wrongly.
        # Docker's embedded DNS answers a querier only with the legs it SHARES with the
        # target, and this sandbox shares exactly one with the triple-homed gateway, so
        # the name would give the same answer. The address is used because it is needed
        # here regardless: the in-container firewall grants a /32, which means the leg
        # has to be resolved before the container starts. Handing that one resolution to
        # both the grant and the client config is what keeps them naming one address
        # rather than two that agree by luck.
        gateway_name="${TOOL_GATEWAY_NAME:-$SC_TOOL_GATEWAY_DEFAULT}"
        gateway_port="${TOOL_GATEWAY_PORT:-8100}"
        gateway_ip="$(sc_service_ip "$gateway_name" "$net")"

        proxy_url="http://${proxy_name}:${proxy_port}"
        no_proxy_list="localhost,127.0.0.1,::1,${proxy_name}"
        if [[ "$gateway_ip" =~ ^[0-9.]+$ ]]; then
            # THE GATEWAY MUST NOT BE PROXIED, and leaving it out of this list is not a
            # missing optimisation — it is a broken tool surface with a misleading error.
            # A client that honours the proxy environment (Claude Code's MCP transport
            # does) would send every MCP request to the egress proxy, which hard-blocks
            # private ranges by design; the agent would get "egress denied by policy",
            # naming the wrong component entirely and describing a refusal that governance
            # never made. Measured from inside a sandbox before this line existed.
            #
            # Both spellings, because the URL is built from the address and a client that
            # resolved the name would otherwise miss the exemption.
            no_proxy_list="${no_proxy_list},${gateway_name},${gateway_ip}"
        fi
        # BOTH CASES of each, and the lowercase ones are not redundant styling.
        #
        # curl reads `http_proxy` in LOWER CASE ONLY, while honouring HTTPS_PROXY and
        # NO_PROXY in either case. That is deliberate, not an oversight: under CGI a
        # client-supplied `Proxy:` request header arrives in the environment as
        # HTTP_PROXY, so honouring it would let a remote caller redirect a server's
        # outbound HTTP through a proxy of their choosing (httpoxy, CVE-2016-5385).
        #
        # With only the uppercase set, `curl http://...` in the sandbox did not reach the
        # governed proxy at all: it resolved the target itself and died at DNS in about a
        # millisecond. Containment held — sandbox-net is internal, so there was nowhere
        # to go — but the agent got "Could not resolve host" instead of a hold it could
        # have had approved, and the attempt reached no audit log, because it never
        # reached the control plane. HTTPS was unaffected, which is why it went unnoticed
        # until a decisions-table row showed a plaintext request that had never arrived.
        #
        # Setting both cases is the conventional pairing and covers other tools with the
        # same rule. `make consistency` asserts the pairing so it cannot drift back.
        #
        # NODE_USE_ENV_PROXY=1 because Node's built-in fetch and http/https agents read
        # none of these unless it is set: without it a Node fetch resolves the host
        # itself and dies at DNS, like curl above. It is set here rather than in the
        # image because it means something only beside the proxy variables.
        SC_PROXY_ENV_ARGS=(
            -e "HTTPS_PROXY=$proxy_url"
            -e "HTTP_PROXY=$proxy_url"
            -e "NO_PROXY=$no_proxy_list"
            -e "https_proxy=$proxy_url"
            -e "http_proxy=$proxy_url"
            -e "no_proxy=$no_proxy_list"
            -e "NODE_USE_ENV_PROXY=1"
            -e "EGRESS_PROXY_IP=$proxy_ip"
            -e "EGRESS_PROXY_PORT=$proxy_port"
        )
        if [ -n "$model_hosts" ]; then
            SC_PROXY_ENV_ARGS+=(-e "SANDBOX_MODEL_HOSTS=$model_hosts")
        fi
        SC_EGRESS_DESC="governed via $proxy_name ($proxy_ip:$proxy_port)"

        if [[ "$gateway_ip" =~ ^[0-9.]+$ ]]; then
            # Waited on for the reason the proxy is: a container that exists is not one
            # that serves, and an MCP server that fails to connect at session start is a
            # startup error the agent then reasons around. Its healthcheck is liveness on
            # the agent leg, so this is short — and unlike the proxy, the gateway has no
            # `depends_on` to satisfy first.
            sc_wait_healthy "$gateway_name" \
                "The governed tool surface would be absent from this session."
            SC_PROXY_ENV_ARGS+=(
                -e "TOOL_GATEWAY_IP=$gateway_ip"
                -e "TOOL_GATEWAY_PORT=$gateway_port"
            )
            SC_TOOLS_DESC="governed via $gateway_name ($gateway_ip:$gateway_port)"
        else
            # NOT a failure, and the sandbox still launches.
            SC_TOOLS_DESC="NONE (no $gateway_name on $net)"
            SC_NO_GATEWAY="true"
        fi
    else
        # No proxy found. If <net> is internal (the compose infra defines sandbox-net
        # that way), there is NO egress route at all, so standalone/direct mode can't
        # work — fail with guidance rather than launch a sandbox that reaches nothing.
        if [ "$(docker network inspect "$net" -f '{{.Internal}}' 2>/dev/null)" = "true" ]; then
            echo "ERROR: '$net' is internal but no egress proxy is running." >&2
            echo "       Start the infrastructure first:  make -C \"$SANDBOX_REPO_ROOT\" up" >&2
            exit 1
        fi
        SC_EGRESS_DESC="DIRECT (no egress proxy on $net; firewall only, no audit)"
        # Standalone: no governed tool path either, and deliberately not a fallback. The
        # gateway brokers credentials, so a mode with no audited egress is the last place
        # to hand one out.
        SC_TOOLS_DESC="NONE (standalone mode has no governed tool path)"
    fi
}

# ---------------------------------------------------------------------------
# Container naming  ->  sets SC_CONTAINER_NAME
# ---------------------------------------------------------------------------
# One or many sandboxes share the infra, so names must not collide. Take the
# default; if taken, pick the next free suffix. (No `docker rm -f` of siblings —
# other sandboxes may be running.)
sc_alloc_container_name() {
    local base="$1" n=2
    if docker ps -a --format '{{.Names}}' | grep -qx "$base"; then
        while docker ps -a --format '{{.Names}}' | grep -qx "${base}-${n}"; do
            n=$((n+1))
        done
        base="${base}-${n}"
    fi
    # shellcheck disable=SC2034  # consumed by the sourcing launcher, not here
    SC_CONTAINER_NAME="$base"
}

# ---------------------------------------------------------------------------
# Git identity  ->  sets SC_GIT_ID_ARGS (array), SC_GIT_NAME, SC_GIT_EMAIL
# ---------------------------------------------------------------------------
# Per-person, so not baked into the image. Read from the host's git config at
# launch and forwarded; the entrypoint materializes it. Absent -> warn, don't
# fail: the container still starts, git only errors at commit time.
sc_git_identity() {
    # shellcheck disable=SC2034  # SC_GIT_ID_ARGS is consumed by the sourcing launcher
    SC_GIT_ID_ARGS=()
    SC_GIT_NAME="$(git config --get user.name || true)"
    SC_GIT_EMAIL="$(git config --get user.email || true)"
    if [[ -n "$SC_GIT_NAME" && -n "$SC_GIT_EMAIL" ]]; then
        # shellcheck disable=SC2034  # consumed by the sourcing launcher, not here
        SC_GIT_ID_ARGS=(-e "GIT_USER_NAME=$SC_GIT_NAME" -e "GIT_USER_EMAIL=$SC_GIT_EMAIL")
    else
        echo "WARNING: no host git identity (user.name/user.email) found;" >&2
        echo "         commits in the sandbox will fail until one is set." >&2
    fi
}

# ---------------------------------------------------------------------------
# DNS  ->  sets SC_UPSTREAM_DNS, SC_DNS_ARGS (array)
# ---------------------------------------------------------------------------
# resolv.conf points at Docker's embedded resolver (127.0.0.11), which resolves
# sibling-container names itself and FORWARDS everything else upstream. Two
# host-specific problems need the same set of resolver IPs, so compute it once:
#   - The upstream the embedded resolver auto-selects on a user-defined network is
#     broken on WSL2 / Docker Desktop (external lookups SERVFAIL even with no
#     firewall), so pin it explicitly via --dns. That keeps resolv.conf as
#     127.0.0.11 (sibling-service discovery preserved) while setting ExtServers.
#   - That forward egresses the in-container firewall's OUTPUT chain, so in
#     STANDALONE mode init-firewall.sh must whitelist the same IPs on port 53
#     (passed as UPSTREAM_DNS) or runtime DNS dies the moment default-deny arms.
#     (Governed mode blocks that forward on purpose — proxied tools hand the
#     hostname to the proxy — and allows only the embedded resolver, which still
#     answers sibling-container names like the proxy's.)
# One list for both means the resolver's upstream and the firewall's DNS allowlist
# can never drift apart. Selection order:
#   1. An explicit SANDBOX_DNS override, for locked-down / corporate / VPN networks
#      where resolvers must be internal ones (and the public fallback would be
#      blocked, or would leak internal hostnames).
#   2. The host's real uplink resolvers. systemd-resolved keeps these in
#      /run/systemd/resolve/resolv.conf; /etc/resolv.conf there is only the stub
#      127.0.0.53 (loopback, useless in the container), so prefer the uplink file
#      and filter loopback either way.
#   3. Docker's public fallback 8.8.8.8/8.8.4.4 — what Docker itself uses when the
#      host has no usable non-loopback resolver (e.g. WSL2 / Docker Desktop).
#
# The upstreams matter only where the firewall lets that forward out, which is
# standalone mode: elsewhere a tier resolves only sibling names, which the embedded
# resolver answers locally. So a tier with no standalone mode need not call this;
# Claude's launcher calls it in every mode.
sc_upstream_dns() {
    local resolv ns
    if [[ -n "${SANDBOX_DNS:-}" ]]; then
        SC_UPSTREAM_DNS="$SANDBOX_DNS"
    else
        SC_UPSTREAM_DNS=""
        # `|| true` is load-bearing, not defensive habit. The launchers run under
        # `set -euo pipefail`, and awk exits 2 when a file does not exist — which
        # pipefail then propagates out of the pipeline, out of the command
        # substitution, and into the assignment, where set -e kills the launcher
        # outright. The very first candidate is absent on any host without
        # systemd-resolved, so `./run-claude-sandbox.sh` died there with a bare
        # rc=2 and no message, BEFORE the /etc/resolv.conf fallback it was about
        # to try — making both remaining fallbacks unreachable on exactly the
        # hosts they exist for. Silence is the reason this went unnoticed: on a
        # host that HAS the file, nothing is wrong.
        for resolv in /run/systemd/resolve/resolv.conf /etc/resolv.conf; do
            SC_UPSTREAM_DNS="$(awk '/^nameserver/ && $2 !~ /^127\./ {print $2}' "$resolv" 2>/dev/null | tr '\n' ' ' || true)"
            [[ -n "${SC_UPSTREAM_DNS// }" ]] && break
        done
        [[ -z "${SC_UPSTREAM_DNS// }" ]] && SC_UPSTREAM_DNS="8.8.8.8 8.8.4.4"
    fi
    SC_DNS_ARGS=()
    for ns in $SC_UPSTREAM_DNS; do SC_DNS_ARGS+=(--dns "$ns"); done
}

# ---------------------------------------------------------------------------
# Resource ceilings
# ---------------------------------------------------------------------------
# sc_clamp_cpus <requested>   -> sets SC_CPUS
#
# Docker REFUSES a --cpus larger than the host CPU count rather than saturating
# at it ("range of CPUs is from 0.01 to 2.00, as there are only 2 CPUs
# available", exit 125). So an over-large default is not a soft ceiling that
# quietly does nothing on a small host — it is a hard launch failure there, and
# only there. The default of 4 ran green on every dev machine and took CI down
# the first time it met a 2-core runner.
#
# Clamp rather than fail, because this ceiling is BLAST RADIUS, NOT A BOUNDARY
# (see DESIGN.md, "Resource limits"): nothing in the threat model rests on the
# number, so the useful behaviour on a small host is to run with what it has.
# The note goes to stderr because a ceiling silently different from the one you
# asked for is worth seeing once.
#
# Fractional values are legal (SANDBOX_CPUS=1.5), so the comparison runs through
# awk — bash arithmetic is integer-only and would read 1.5 as a syntax error.
# A host without nproc, or one that answers with something other than a number,
# leaves the request untouched and lets docker be the judge.
sc_clamp_cpus() {
    local requested="$1"
    SC_CPUS="$requested"

    local available
    available="$(nproc 2>/dev/null)" || return 0
    [[ "$available" =~ ^[0-9]+$ ]] || return 0

    if awk -v r="$requested" -v a="$available" 'BEGIN { exit !(r > a) }'; then
        echo "NOTE: SANDBOX_CPUS=$requested exceeds the $available CPU(s) on this host;" >&2
        echo "      using $available — docker rejects a --cpus above the host count." >&2
        # shellcheck disable=SC2034  # consumed by the sourcing launcher, not here
        SC_CPUS="$available"
    fi
}

# ---------------------------------------------------------------------------
# Host time zone
# ---------------------------------------------------------------------------
# sc_host_timezone   -> sets SC_TZ
#
# Each launcher passes -e TZ so a timestamp inside the container means what the
# human reading it expects. They used to pass `${TZ:-UTC}`, which is correct on a
# host that exports TZ and wrong on the one this repo is developed on: WSL2 never
# exports it. It carries the zone in /etc/localtime instead — a symlink into
# zoneinfo that WSL keeps in step with Windows — so the fallback fired on every
# launch and the container ran UTC while the host read CEST. Nothing broke; every
# container timestamp simply had to be offset by hand before it could be compared
# with anything from the host, which is a tax paid silently and forever.
#
# Detection order is most-explicit-first. An exported TZ still wins, so an
# operator keeps the override. Then /etc/timezone, which is a bare zone name on
# Debian and Ubuntu. Then the /etc/localtime symlink, the WSL case and the only
# one needing the zoneinfo prefix stripped. UTC last, the honest answer when the
# host says nothing.
#
# Deliberately NOT `timedatectl`: it is the tidy answer and it needs systemd,
# which this launcher has already been bitten by once on hosts without
# systemd-resolved. A file read and a readlink work everywhere.
#
# Only ever a zone NAME, passed as an env var — no host path is mounted to
# achieve it, so "never give the sandbox a direct path to anything" is untouched.
# And a name the image's tzdata does not carry degrades to UTC inside glibc, which
# is where this started, so a bad guess costs nothing.
# shellcheck disable=SC2034  # SC_TZ is consumed by the sourcing launcher, not here
sc_host_timezone() {
    SC_TZ="UTC"

    if [ -n "${TZ:-}" ]; then
        SC_TZ="$TZ"
        return 0
    fi

    local zone
    if [ -r /etc/timezone ]; then
        zone="$(tr -d '[:space:]' < /etc/timezone)"
        if [ -n "$zone" ]; then
            SC_TZ="$zone"
            return 0
        fi
    fi

    if [ -L /etc/localtime ]; then
        zone="$(readlink -f /etc/localtime 2>/dev/null)"
        case "$zone" in
            /usr/share/zoneinfo/?*) SC_TZ="${zone#/usr/share/zoneinfo/}" ;;
        esac
    fi
}
