#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
IFS=$'\n\t'

# This script's OWN setup traffic (GitHub-range fetch, DNS digs, sanity curls)
# must go DIRECT, never through the agent's egress proxy: it bootstraps the
# boundary and runs before the proxy is guaranteed reachable. Unset any proxy
# vars for THIS process only — the agent still inherits them from the container
# env and routes its runtime traffic through the proxy.
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy

# Three modes, selected by what the launcher passes. Shared by every sandbox tier.
#
#   GOVERNED   — tier 1 with the compose infra (EGRESS_PROXY_IP set). The proxy is
#                the sandbox's egress path for ALL external domains, so the
#                firewall drops to the bare minimum: DNS to the embedded resolver
#                only, the proxy /32, loopback, established. No direct IP
#                allowlist (hence no ipset), no upstream DNS forward, no gateway.
#   LOCAL      — tier 2, the local-LLM agent (SANDBOX_MODE=local). NO egress
#                whatsoever and no proxy: the ONLY permitted destination is the
#                local inference service (LLM_IP), plus loopback / embedded DNS /
#                established. Deliberately NOT the egress proxy — a tier-2 agent
#                must not reach it even when it is running on the same network.
#   STANDALONE — tier 1 with no infra. Keeps the fuller direct-egress ipset
#                allowlist; the only mode with any direct outbound allowance.
#
# STANDALONE is the sole mode with a direct IP allowlist, so several branches
# below key off exactly that.
MODE=standalone
if [[ "${SANDBOX_MODE:-}" == "local" ]]; then
    MODE=local
elif [[ "${EGRESS_PROXY_IP:-}" =~ ^[0-9.]+$ ]]; then
    MODE=governed
fi
if [[ "$MODE" == "standalone" ]]; then DIRECT_EGRESS=1; else DIRECT_EGRESS=0; fi

# Fail closed: LOCAL mode exists to reach exactly one service. Without its address
# the agent would boot fully mute, which is a silent misconfiguration rather than
# a boundary — refuse instead, matching this script's other fail-closed branches.
if [[ "$MODE" == "local" && ! "${LLM_IP:-}" =~ ^[0-9.]+$ ]]; then
    echo "FATAL: SANDBOX_MODE=local but LLM_IP is unset or not an IPv4 address." >&2
    echo "       The tier-2 sandbox has no egress and no proxy; the inference" >&2
    echo "       service is its only permitted destination. Refusing to start." >&2
    exit 1
fi

# ============================================================
# Default-deny egress firewall, shared by every sandbox tier.
# Whitelist only. This is the real network boundary; client-side
# settings (permissions.deny etc.) are steering, not containment.
# Adapted from Anthropic's Claude Code devcontainer firewall.
# ============================================================

# Flush the filter and mangle tables — but DELIBERATELY NOT the nat table.
#
# Docker's embedded DNS resolver (127.0.0.11, used on user-defined networks like
# sandbox-net) works via DNAT rules Docker installs in the nat table. Flushing
# nat destroys them, which kills ALL name resolution inside the container — even
# before default-deny is armed. The previous "save the 127.0.0.11 rules, flush,
# restore them" dance tried to undo that self-inflicted damage, but under the
# nf_tables iptables backend (this image is iptables-nft) the save/grep/restore
# is unreliable and was silently leaving the resolver broken.
#
# So don't touch nat at all: it is NOT where containment lives. Egress is
# governed entirely in the filter table (default-deny + the egress-proxy allow in
# governed mode, or the allowed-domains ipset in standalone mode); nat only
# rewrites addresses (embedded-DNS DNAT, Docker's own
# masquerade) and holds no egress-permitting rule that could bypass the filter
# policy. Leaving Docker's nat rules intact keeps the embedded resolver working
# — for external names now, and for data-plane service discovery on sandbox-net
# later — at zero cost to the boundary.
iptables -F; iptables -X
iptables -t mangle -F; iptables -t mangle -X
ipset destroy allowed-domains 2>/dev/null || true

# Loopback first (also carries Docker's embedded DNS at 127.0.0.11)
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# DNS only to the resolver(s) this container actually uses — NOT to arbitrary
# external resolvers. This NARROWS but does NOT eliminate DNS exfiltration:
# pinning to the configured resolver stops the agent reaching arbitrary
# nameservers directly, but if that resolver is recursive (the usual case) an
# attacker-controlled name (`<data>.exfil.example.com`) is still forwarded on to
# its authoritative NS — so it is a reduction, not a block. Fully closing this
# channel means removing direct DNS entirely (the target design routes lookups
# through the egress proxy). The original broad `--dport 53 ACCEPT` was worse
# still: it let data be smuggled to *any* nameserver.
# On Docker's *default* bridge the container inherits the
# host's /etc/resolv.conf (the embedded 127.0.0.11 resolver is only used on
# user-defined networks), so hardcoding 127.0.0.11 drops all lookups once the
# default-deny policy goes live. 127.0.0.11 is kept as a fallback.
#
# On a *user-defined* network (sandbox-net) the only nameserver in resolv.conf
# is 127.0.0.11, and that embedded resolver does NOT answer external names
# itself — it FORWARDS them to the host's real upstream resolvers, and that
# forward egresses this OUTPUT chain. So allowing 127.0.0.11 alone lets the
# container reach the embedded resolver but still DROPs its upstream forward the
# moment default-deny arms — every runtime lookup dies. (Init-time resolution in
# the domain loop below still works: the policy is not ACCEPT->DROP until the
# end of this script.) The container can't discover those upstreams
# (resolv.conf hides them behind 127.0.0.11), so the launcher (sandbox-lib.sh)
# computes them on the host and forwards them as UPSTREAM_DNS. Whitelisting those
# specific IPs keeps the anti-exfiltration narrowing intact (named resolvers,
# not "any nameserver").
mapfile -t DNS_SERVERS < <(awk '/^nameserver/ {print $2}' /etc/resolv.conf 2>/dev/null)
# UPSTREAM_DNS arrives space-separated from the host launcher; the script-wide
# IFS ($'\n\t') would treat it as a single token, so split it on spaces here.
IFS=' ' read -r -a UPSTREAM_ARR <<< "${UPSTREAM_DNS:-}"
if [ "$DIRECT_EGRESS" -eq 0 ]; then
    # Governed and local both: the only name the sandbox must resolve is sibling
    # services (egress-proxy in governed mode, the inference service in local
    # mode), which the embedded resolver at 127.0.0.11 answers LOCALLY,
    # with no upstream forward. External names are resolved by the PROXY, not
    # here. So allow DNS ONLY to 127.0.0.11 and let the embedded resolver's
    # upstream forward hit default-deny — which CLOSES the residual DNS-exfil
    # channel (a crafted `<data>.exfil.example.com` can no longer be smuggled to
    # a recursive upstream). Sibling lookups still work; external direct lookups
    # fail, as intended. (127.0.0.11 is loopback, so `-o lo` already permits it;
    # this rule is kept explicit for clarity.)
    DNS_ALLOW=(127.0.0.11)
else
    # Standalone: no proxy, so the embedded resolver MUST forward external lookups
    # upstream (for the direct allowlist to resolve, and for runtime DNS). Allow
    # the pinned upstreams too — narrowed to named resolvers, not "any nameserver".
    DNS_ALLOW=("${DNS_SERVERS[@]}" "${UPSTREAM_ARR[@]}" 127.0.0.11)
fi
for ns in "${DNS_ALLOW[@]}"; do
    [[ "$ns" =~ ^[0-9.]+$ ]] || continue   # IPv4 only; IPv6 DNS is blocked by ip6tables below
    iptables -A OUTPUT -p udp --dport 53 -d "$ns" -j ACCEPT
    iptables -A OUTPUT -p tcp --dport 53 -d "$ns" -j ACCEPT
done

# Direct per-domain IP allowlist (ipset). This is a STANDALONE-mode fallback:
# when an egress proxy is present, every external domain is reached THROUGH it,
# so the sandbox needs no direct allowlist — and we skip ipset entirely, which
# also sidesteps kernels that lack the ip_set/xt_set modules (e.g. stock WSL2,
# where `ipset create` may work but iptables `-m set --match-set` cannot).
USE_IPSET=0
if [ "$MODE" = "governed" ]; then
    echo "Governed egress: proxy at $EGRESS_PROXY_IP present — external domains route"
    echo "  through it; skipping the direct IP allowlist (no ipset required)."
elif [ "$MODE" = "local" ]; then
    echo "LOCAL mode: no egress and no proxy — the only permitted destination is"
    echo "  the inference service at $LLM_IP:${LLM_PORT:-8080}. No direct allowlist."
else
    # No proxy: open direct egress to a resolved IP allowlist via ipset. Requires
    # the kernel ip_set/xt_set modules — present on most hosts, NOT on stock WSL2
    # kernels. Fail closed with guidance rather than a raw error if unavailable.
    if ! ipset create allowed-domains hash:net 2>/tmp/ipset.err; then
        echo "FATAL: cannot create ipset ($(tr -d '\n' </tmp/ipset.err))." >&2
        echo "       The kernel lacks the ip_set module (common on stock WSL2)." >&2
        echo "       Start the egress proxy and run in governed mode instead" >&2
        echo "       ('docker compose up -d' first) — it needs no ipset. See DESIGN.md." >&2
        exit 1
    fi
    USE_IPSET=1

    # Reject a CIDR that is absurdly broad (prefix < /8) or private/loopback/
    # link-local — a hostile or MITM'd api.github.com/meta response could send
    # 0.0.0.0/0, which a shape-only check would add, turning "whitelist only" into
    # allow-all while still printing the reassuring banner below. GitHub never
    # publishes such ranges. Fail-safe: over-rejecting only drops direct git egress
    # (recoverable), it can never open a hole.
    _sane_cidr() {
        local c="$1" prefix="${1##*/}"
        [[ "$c" =~ ^[0-9.]+/[0-9]{1,2}$ ]] || return 1
        [[ "$prefix" =~ ^[0-9]+$ && "$prefix" -ge 8 ]] || return 1
        [[ "$c" =~ ^(0|10|127|169\.254|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\. ]] && return 1
        return 0
    }

    # GitHub IP ranges (dynamic) — git clone/pull/push, gh, API.
    # TRANSITIONAL: belongs behind the governed git path in the target design;
    # whitelisted here only until that data plane exists.
    echo "Fetching GitHub IP ranges..."
    gh_ranges=$(curl -s https://api.github.com/meta || true)
    if [ -n "$gh_ranges" ] && echo "$gh_ranges" | jq -e '.web and .api and .git' >/dev/null 2>&1; then
        while read -r cidr; do
            if _sane_cidr "$cidr"; then
                ipset add allowed-domains "$cidr" 2>/dev/null || true
            else
                echo "  skipping suspicious GitHub CIDR: $cidr"
            fi
        done < <(echo "$gh_ranges" | jq -r '(.web + .api + .git)[]' | aggregate -q 2>/dev/null \
                 || echo "$gh_ranges" | jq -r '(.web + .api + .git)[]')
    else
        echo "WARNING: could not fetch GitHub IP ranges"
    fi

    # Whitelisted domains, grouped by target-state disposition.
    ALLOWED_DOMAINS=(
        # PERMANENT — the agent's own API/auth lifeline. In the target design this
        # is the only egress the sandbox keeps (routed through the egress proxy as
        # an always-allow so it is audited; direct for now).
        "api.anthropic.com"
        "claude.ai"
        "platform.claude.com"

        # TRANSITIONAL — package registries. These belong behind the pull-through
        # cache (whose upstream goes through the governed egress proxy). Whitelisted
        # here only because that data plane does not exist yet; remove when it lands.
        "registry.npmjs.org"
        "pypi.org"
        "files.pythonhosted.org"
    )

    for domain in "${ALLOWED_DOMAINS[@]}"; do
        ips=$(dig +noall +answer A "$domain" 2>/dev/null | awk '$4 == "A" {print $5}')
        [ -z "$ips" ] && { echo "WARNING: failed to resolve $domain"; continue; }
        while read -r ip; do
            if [[ "$ip" =~ ^[0-9.]+$ ]]; then
                ipset add allowed-domains "$ip" 2>/dev/null || true
            fi
        done <<< "$ips"
    done
fi

# Default gateway (the Docker host) as a /32 — STANDALONE mode only. In
# standalone mode egress to permitted externals matches on destination IP via the
# allowed-domains ipset (the gateway is just the L2 next hop, not the packet's
# dst), return traffic matches ESTABLISHED,RELATED, and DNS matches the port-53
# rules above — so a broad /24 buys nothing and only adds reach to the host's own
# ports and every sibling container on the bridge (a lateral/pivot surface the
# sandbox must not have). Pin to the gateway /32.
#
# In GOVERNED and LOCAL modes drop it entirely: the sandbox reaches its one
# sanctioned service by that service's own /32 (below) and needs nothing on the
# host gateway, so this host-local surface is removed rather than pinned.
if [ "$DIRECT_EGRESS" -eq 1 ]; then
    HOST_IP=$(ip route | awk '/default/ {print $3; exit}')
    if [[ "$HOST_IP" =~ ^[0-9.]+$ ]]; then
        iptables -A INPUT  -s "$HOST_IP" -j ACCEPT
        iptables -A OUTPUT -d "$HOST_IP" -j ACCEPT
    fi
fi

# Sanctioned data-plane service: the egress proxy on sandbox-net. When the
# launcher finds the proxy (compose infra up), it passes its IP/port here and
# points the agent's HTTP(S)_PROXY at it — so this is the explicit per-service
# allow the NOTE below anticipates. In step 0 it runs ALONGSIDE the direct
# allowed-domains egress (a fallback); at the proxy-only phase that direct egress
# is removed and this becomes the sandbox's sole path off-box.
#
# The `$MODE` != local test is belt-and-braces: a tier-2 launcher never passes
# EGRESS_PROXY_IP, but a tier-2 agent reaching the egress proxy would convert a
# no-egress tier into a governed-egress one silently. Assert it here rather than
# rely on the launcher never regressing.
if [[ "$MODE" != "local" && "${EGRESS_PROXY_IP:-}" =~ ^[0-9.]+$ ]]; then
    iptables -A OUTPUT -p tcp -d "$EGRESS_PROXY_IP" \
        --dport "${EGRESS_PROXY_PORT:-8080}" -j ACCEPT
    echo "  egress proxy allowed -> $EGRESS_PROXY_IP:${EGRESS_PROXY_PORT:-8080}"
fi

# Sanctioned data-plane service: local LLM inference, for LOCAL mode only. This is
# the per-service /32 the multi-container NOTE below anticipates — tighter than a
# subnet allow, so the tier-2 agent reaches the inference service and NOTHING else
# on sandbox-net (not the egress proxy, not a sibling sandbox, not the package
# cache). Tier 1 deliberately does not get this: a capability is granted when it
# has a consumer, not when it becomes possible (see DESIGN.md "Local inference").
if [ "$MODE" = "local" ]; then
    iptables -A OUTPUT -p tcp -d "$LLM_IP" \
        --dport "${LLM_PORT:-8080}" -j ACCEPT
    echo "  inference service allowed -> $LLM_IP:${LLM_PORT:-8080}"
fi

# NOTE (multi-container phase): sibling containers are NOT reachable by default —
# they hit the REJECT below. That is intended, and it is the default that every
# new data-plane service starts from: reachability is granted per service, as an
# explicit /32 above, never as a blanket subnet allow. The two grants that exist
# today are the egress proxy (governed mode) and the inference service (local
# mode); a package cache or test runner would each get their own.
#
# Scope every such allow to sandbox-net ONLY — never control-net, or the agent
# regains a path to the control plane. DNS for sibling names still resolves via
# Docker's embedded 127.0.0.11 (NAT rules preserved at the top of this script);
# these allows gate the connection, not the lookup, which is why a blocked
# sibling shows up as a fast connection refusal rather than a name failure.

# IPv6: default-deny everything. We only govern/allow IPv4 above, so an open
# ip6tables (default ACCEPT) would be ungoverned egress — a containment leak —
# and hosts with AAAA records (e.g. api.anthropic.com) make curl try IPv6 first,
# which then hangs instead of falling back to the whitelisted IPv4 path.
#
# Fail CLOSED, not open. The three DROP policies ARE the v6 boundary, so they must
# not be allowed to fail silently (the old `|| true` on them could leave v6 wide
# open). But distinguish a live IPv6 stack from one that is simply absent:
#   - No /proc/net/if_inet6  -> the kernel has no IPv6 stack at all, so there is
#     no v6 egress to leak and ip6tables would legitimately error. Skip safely.
#   - if_inet6 present but ip6tables missing -> we have a v6 stack we cannot
#     filter. That is exactly the leak we refuse to accept: abort (set -e).
#   - if_inet6 present and ip6tables present -> install the DROP policies WITHOUT
#     `|| true`, so any failure trips set -e and the container fails closed before
#     the agent ever runs. The loopback ACCEPTs are functionality-only (their
#     failure would only over-restrict), so they keep the tolerant `|| true`.
if [ -e /proc/net/if_inet6 ]; then
    if ! command -v ip6tables >/dev/null 2>&1; then
        echo "FATAL: IPv6 stack present but ip6tables missing — cannot close v6 egress." >&2
        exit 1
    fi
    ip6tables -F || true
    ip6tables -X || true
    ip6tables -P INPUT DROP
    ip6tables -P FORWARD DROP
    ip6tables -P OUTPUT DROP
    ip6tables -A INPUT  -i lo -j ACCEPT || true
    ip6tables -A OUTPUT -o lo -j ACCEPT || true
else
    echo "  OK — no IPv6 stack (/proc/net/if_inet6 absent); nothing to lock down"
fi

# Default-deny
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

iptables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
if [ "$USE_IPSET" -eq 1 ]; then
    # Direct IP allowlist (standalone mode only). This match needs the kernel
    # xt_set module; on kernels without it (stock WSL2) `ipset create` above can
    # still succeed while THIS fails, so fail closed with guidance.
    # Restrict the direct allowlist to HTTP(S) ports. Matching only the destination IP
    # let an allowlisted host be reached on ANY port (SSH, arbitrary TLS) over raw TCP,
    # widening the channel well past what "allow <host>" grants; DNS is already handled
    # by the port-53 rules above. The first rule doubles as the xt_set capability probe
    # (ipset create can succeed while the match module is absent — stock WSL2).
    if ! iptables -A OUTPUT -p tcp --dport 443 -m set --match-set allowed-domains dst -j ACCEPT 2>/tmp/xtset.err; then
        echo "FATAL: iptables cannot use ipset matches ($(tr -d '\n' </tmp/xtset.err))." >&2
        echo "       The kernel lacks the xt_set module (common on stock WSL2)." >&2
        echo "       Use the egress proxy (governed mode) instead. See DESIGN.md." >&2
        exit 1
    fi
    iptables -A OUTPUT -p tcp --dport 80 -m set --match-set allowed-domains dst -j ACCEPT
fi
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

echo "=== Firewall configured (default-deny, whitelist only) ==="

# Sanity checks. NB: proxy vars are unset in this script, so these curls test
# DIRECT egress. example.com must always be blocked directly.
if curl --connect-timeout 5 -s https://example.com >/dev/null 2>&1; then
    # A reachable arbitrary site is a real containment failure, not a warning:
    # example.com is never allowlisted in any mode, so a direct hit means egress
    # is open. Fail CLOSED — abort before the gosu drop rather than launch a yolo
    # agent with an open boundary, matching every other fatal check in this script.
    echo "FATAL: firewall leak — reached example.com directly. Egress is open." >&2
    exit 1
else
    echo "  OK — example.com blocked (direct)"
fi
if [ "$MODE" = "local" ]; then
    # Tier 2 has no Anthropic lifeline at all — it must NOT reach api.anthropic.com
    # by any path, and the inference service must be reachable. Both are asserted
    # here so a misconfigured no-egress tier fails loudly at boot rather than
    # looking healthy until the agent tries to work.
    if curl --connect-timeout 5 -s https://api.anthropic.com >/dev/null 2>&1; then
        # Tier 2's entire premise is zero egress; reaching Anthropic means the
        # boundary failed. Fail CLOSED rather than warn — a no-egress tier that can
        # egress is broken, not degraded.
        echo "FATAL: firewall leak — api.anthropic.com reachable in LOCAL mode." >&2
        exit 1
    else
        echo "  OK — api.anthropic.com unreachable (no egress in LOCAL mode)"
    fi
    if curl --connect-timeout 5 -s -o /dev/null "http://$LLM_IP:${LLM_PORT:-8080}/health" 2>/dev/null; then
        echo "  OK — inference service reachable at $LLM_IP:${LLM_PORT:-8080}"
    else
        echo "  NOTE — inference service not answering at $LLM_IP:${LLM_PORT:-8080} yet"
        echo "         (model may still be loading; the firewall rule is installed)"
    fi
elif [ "$USE_IPSET" -eq 1 ]; then
    # Standalone: api.anthropic.com is in the direct allowlist, so it should work.
    if curl --connect-timeout 5 -s https://api.anthropic.com >/dev/null 2>&1; then
        echo "  OK — api.anthropic.com reachable (direct)"
    else
        echo "  NOTE — api.anthropic.com not reachable yet (direct)"
    fi
else
    # Governed: no direct allowlist — the agent reaches api.anthropic.com through
    # the proxy (it keeps HTTPS_PROXY set), so a DIRECT probe here is EXPECTED to
    # fail. Confirm that; the real lifeline check is boundary-check.sh via the proxy.
    if curl --connect-timeout 5 -s https://api.anthropic.com >/dev/null 2>&1; then
        echo "  WARNING: api.anthropic.com reachable DIRECTLY (expected proxy-only)"
    else
        echo "  OK — no direct api.anthropic.com (agent reaches it via the proxy)"
    fi
fi
