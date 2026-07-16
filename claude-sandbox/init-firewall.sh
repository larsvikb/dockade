#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

# ============================================================
# Default-deny egress firewall for the Claude sandbox.
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
# governed entirely by the filter-table default-deny + allowed-domains ipset
# below; nat only rewrites addresses (embedded-DNS DNAT, Docker's own
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
# (resolv.conf hides them behind 127.0.0.11), so run-claude-sandbox.sh computes
# them on the host and forwards them as UPSTREAM_DNS. Whitelisting those
# specific IPs keeps the anti-exfiltration narrowing intact (named resolvers,
# not "any nameserver").
DNS_SERVERS=$(awk '/^nameserver/ {print $2}' /etc/resolv.conf 2>/dev/null)
# UPSTREAM_DNS arrives space-separated from the host launcher; the script-wide
# IFS ($'\n\t') would treat it as a single token, so split it on spaces here.
IFS=' ' read -r -a UPSTREAM_ARR <<< "${UPSTREAM_DNS:-}"
for ns in $DNS_SERVERS "${UPSTREAM_ARR[@]}" 127.0.0.11; do
    [[ "$ns" =~ ^[0-9.]+$ ]] || continue   # IPv4 only; IPv6 DNS is blocked by ip6tables below
    iptables -A OUTPUT -p udp --dport 53 -d "$ns" -j ACCEPT
    iptables -A OUTPUT -p tcp --dport 53 -d "$ns" -j ACCEPT
done

ipset create allowed-domains hash:net

# GitHub IP ranges (dynamic) — git clone/pull/push, gh, API.
# TRANSITIONAL: belongs behind the governed git path in the target design;
# whitelisted here only until that data plane exists.
echo "Fetching GitHub IP ranges..."
gh_ranges=$(curl -s https://api.github.com/meta || true)
if [ -n "$gh_ranges" ] && echo "$gh_ranges" | jq -e '.web and .api and .git' >/dev/null 2>&1; then
    while read -r cidr; do
        [[ "$cidr" =~ ^[0-9.]+/[0-9]{1,2}$ ]] && ipset add allowed-domains "$cidr" 2>/dev/null || true
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
        [[ "$ip" =~ ^[0-9.]+$ ]] && ipset add allowed-domains "$ip" 2>/dev/null || true
    done <<< "$ips"
done

# Allow only the default gateway (the Docker host) as a /32 — NOT the whole
# subnet. Egress to permitted externals already matches on destination IP via
# the allowed-domains ipset (the gateway is just the L2 next hop, not the
# packet's dst), return traffic matches the ESTABLISHED,RELATED rule, and DNS
# matches the dedicated port-53 rules above — so a broad /24 buys nothing for
# normal operation. What a /24 *does* add is reach to the host's own listening
# ports and to every sibling container on the bridge: a lateral/pivot surface
# the sandbox is explicitly not supposed to have ("no direct path to anything",
# "the agent must never reach the control plane"). Pin to the gateway /32.
HOST_IP=$(ip route | awk '/default/ {print $3; exit}')
if [[ "$HOST_IP" =~ ^[0-9.]+$ ]]; then
    iptables -A INPUT  -s "$HOST_IP" -j ACCEPT
    iptables -A OUTPUT -d "$HOST_IP" -j ACCEPT
fi

# NOTE (multi-container phase): with the gateway pinned to /32 above, sibling
# containers are NOT reachable by default — they hit the REJECT below. That is
# intended. When the data plane lands, the sandbox is meant to reach its
# sanctioned services (egress proxy, package cache, test runners) on
# sandbox-net, so add an EXPLICIT allow for that subnet (per-service /32s are
# tighter), e.g.:
#     iptables -A OUTPUT -d "<sandbox-net-subnet>" -j ACCEPT
# Scope it to sandbox-net ONLY — never control-net, or the agent regains a path
# to the control plane. DNS for sibling names still resolves via Docker's
# embedded 127.0.0.11 (NAT rules preserved at the top of this script); this
# allow gates the connection, not the lookup.

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
iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

echo "=== Firewall configured (default-deny, whitelist only) ==="

# Sanity checks
if curl --connect-timeout 5 -s https://example.com >/dev/null 2>&1; then
    echo "WARNING: firewall leak — reached example.com"
else
    echo "  OK — example.com blocked"
fi
curl --connect-timeout 5 -s https://api.anthropic.com >/dev/null 2>&1 \
    && echo "  OK — api.anthropic.com reachable" \
    || echo "  NOTE — api.anthropic.com not reachable yet"
