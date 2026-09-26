#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Claude Code status line for the sandbox image.
#
# Wired via the baked user-settings.json template (user scope — a managed-scope
# command status line is gated behind an approval dialog the yolo flow suppresses).
# The script itself stays root-owned and baked here, so the non-root agent can
# toggle the pointer but not tamper with the code it runs (see DESIGN.md
# "the paved road"). This is a cosmetic default, not an enforced boundary.
#
# Claude Code invokes this with a JSON session blob on stdin. We surface:
#   - a sandbox indicator (this is a contained environment, always show it)
#   - the current directory (basename)
#   - the git branch, when inside a repo
#   - used context window (tokens + %)
#   - subscription rate-limit usage: the rolling 5-hour and 7-day windows,
#     each with a ⟳ countdown to when that window resets
#
# Context usage and the window's size arrive on stdin (.context_window.*), NOT
# from any API call. The sandbox has no direct egress by design (see DESIGN.md),
# so a network call here would be blocked and would need embedded credentials.
# The size is read rather than inferred from the model id, which does not carry
# it: claude-opus-5-5 has a 1M window and no [1m] suffix.
#
# The 5h/7d rate-limit percentages arrive on stdin too (.rate_limits.*), so they
# likewise need no network call: Claude Code fills them from its own inference
# API responses — the one endpoint the sandbox is already allowed to reach. They
# appear only for Pro/Max sessions and only after the first API response of the
# session, and either window can be absent, so we render each only when present.
set -euo pipefail

input="$(cat)"

field() { printf '%s' "$input" | jq -r "$1"; }

cwd="$(field '.workspace.current_dir // .cwd // ""')"

dir="workspace"
[ -n "$cwd" ] && dir="$(basename "$cwd")"

# Rate-limit windows: percentage of each rolling limit consumed (0-100, may be a
# float) plus the epoch-seconds instant the window resets. Either may be absent
# (see header) — // empty yields "" so the segment is simply skipped.
lim5h="$(field '.rate_limits.five_hour.used_percentage // empty')"
lim5h_reset="$(field '.rate_limits.five_hour.resets_at // empty')"
lim7d="$(field '.rate_limits.seven_day.used_percentage // empty')"
lim7d_reset="$(field '.rate_limits.seven_day.resets_at // empty')"

branch=""
if [ -n "$cwd" ] && git -C "$cwd" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    branch="$(git -C "$cwd" branch --show-current 2>/dev/null || true)"
fi

# Tokens in context (input + cache reads + cache writes) and the window's size.
# There is no current figure before the first API response (current_usage is
# null) or after /compact until the next one (Claude Code 2.1.283 sends a total
# of 0), so the segment is skipped in both. `numbers` keeps anything but a JSON
# number out of the arithmetic.
used="$(field '.context_window | select(.current_usage != null) | .total_input_tokens | numbers | select(. > 0)')"
ctx_limit="$(field '.context_window.context_window_size | numbers | select(. > 0)')"

# ANSI colors — status lines render with color.
dim=$'\033[2m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; cyan=$'\033[36m'; reset=$'\033[0m'

hum() { # 1000000 -> 1M, 142000 -> 142k, <1000 -> as-is
    if   [ "$1" -ge 1000000 ]; then printf '%dM' "$(( $1 / 1000000 ))"
    elif [ "$1" -ge 1000 ];    then printf '%dk' "$(( $1 / 1000 ))"
    else                            printf '%d'  "$1"; fi
}

pct_color() { # integer percent -> shared green/yellow/red threshold color
    if   [ "$1" -ge 80 ]; then printf '%s' "$red"
    elif [ "$1" -ge 50 ]; then printf '%s' "$yellow"
    else                       printf '%s' "$green"; fi
}

now="$(date +%s 2>/dev/null || echo 0)"

reset_fmt() { # epoch-seconds -> compact time-until ("2h7m"/"45m"); empty if past
    local at="${1//[^0-9]/}"
    [ -n "$at" ] && [ "$at" -gt "$now" ] || return 0
    local h=$(( (at - now) / 3600 )) m=$(( ((at - now) % 3600) / 60 ))
    if   [ "$h" -gt 0 ] && [ "$m" -gt 0 ]; then printf '%dh%dm' "$h" "$m"
    elif [ "$h" -gt 0 ];                   then printf '%dh' "$h"
    else                                        printf '%dm' "$m"; fi
}

# Render a "<label> <pct>% ⟳<reset>" segment for a rate-limit window, colored by
# usage. $1 = label, $2 = used_percentage (float/int/empty), $3 = resets_at epoch
# (optional). No-op when the percentage is empty; the ⟳ part is dropped if the
# reset instant is missing or already past.
limseg() {
    [ -n "$2" ] || return 0
    local p="${2%%.*}"           # drop any fractional part for integer compare
    p="${p//[^0-9]/}"; [ -n "$p" ] || p=0
    printf ' %s·%s %s%s %s%%%s' "$dim" "$reset" "$(pct_color "$p")" "$1" "$p" "$reset"
    local r; r="$(reset_fmt "${3:-}")"
    [ -n "$r" ] && printf ' %s⟳%s%s' "$dim" "$r" "$reset"
    return 0   # never let a false trailing test abort the caller under set -e
}

line="${yellow}🔒 sandbox${reset} ${dim}·${reset} ${green}${dir}${reset}"
[ -n "$branch" ] && line="${line} ${dim}·${reset} ${cyan}⎇ ${branch}${reset}"

if [ -n "$used" ] && [ -n "$ctx_limit" ]; then
    pct=$(( used * 100 / ctx_limit ))
    line="${line} ${dim}·${reset} $(pct_color "$pct")ctx $(hum "$used")/$(hum "$ctx_limit") ${pct}%${reset}"
fi

line="${line}$(limseg 5h "$lim5h" "$lim5h_reset")$(limseg 7d "$lim7d" "$lim7d_reset")"

printf '%s' "$line"
