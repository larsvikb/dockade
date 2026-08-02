#!/bin/bash
# Boundary smoke test for the Claude sandbox.
#
# Run this INSIDE the container, as the sandbox agent, to assert the v1
# containment boundary still holds:
#     boundary-check.sh
#
# It complements the boot-time checks in init-firewall.sh / entrypoint.sh, which
# (a) run as root BEFORE the privilege drop, (b) mostly only warn, and (c) scroll
# past at startup with no aggregate result. This runs on demand, from the agent's
# own (capability-less) security context, and exits non-zero if any hard invariant
# is violated — a regression baseline to run before and after the proxy work.
#
# Baked root-owned at /usr/local/bin so the non-root agent can run but not tamper
# with it (same rationale as the firewall/status-line scripts; see DESIGN.md).
#
# NOT `set -e`: we want every check to run and the results aggregated, not an
# abort on the first failure.
set -uo pipefail

pass=0; fail=0
green=$'\033[32m'; red=$'\033[31m'; dim=$'\033[2m'; bold=$'\033[1m'; reset=$'\033[0m'
ok()   { printf '  %sPASS%s %s\n' "$green" "$reset" "$1"; pass=$((pass+1)); }
bad()  { printf '  %sFAIL%s %s\n' "$red"   "$reset" "$1"; fail=$((fail+1)); }
info() { printf '  %s·%s    %s\n' "$dim"   "$reset" "$1"; }

printf '%s== egress ==%s\n' "$bold" "$reset"
# The core default-deny property is the DIRECT firewall boundary, so probe it with
# --noproxy '*' — otherwise, when HTTPS_PROXY is set, curl would route through the
# proxy and we'd be testing the proxy's policy instead of the firewall. An
# arbitrary external site must be unreachable directly; example.com presents a
# valid cert, so a successful direct fetch is a genuine leak.
if curl --noproxy '*' --connect-timeout 5 -s -o /dev/null https://example.com 2>/dev/null; then
    bad "direct egress leak — reached example.com (bypassing any proxy)"
else
    ok "direct egress to example.com blocked (firewall)"
fi
# Raw-IP direct egress (no DNS needed) — isolates the egress block from a mere
# DNS failure. With sandbox-net internal there is no route off-box at all, so even
# a literal IP must be unreachable directly (not just unresolvable names).
if curl --noproxy '*' --connect-timeout 5 -s -o /dev/null https://1.1.1.1 2>/dev/null; then
    bad "direct egress leak — reached 1.1.1.1 by IP (bypassing any proxy)"
else
    ok "direct egress to raw IP (1.1.1.1) blocked"
fi
# The Anthropic lifeline. Its expected state is INVERTED between tiers, so branch
# on the mode rather than asserting one of them universally:
#   tier 1 — reachable via whatever path the agent uses (proxy if governed, direct
#            otherwise), so DON'T set --noproxy here: use the ambient env. Any HTTP
#            response (even 4xx) means the connection succeeded, which is all we assert.
#   tier 2 — LOCAL mode holds no Anthropic credentials and has no egress, so
#            reachability would be a boundary FAILURE, not a healthy lifeline.
if [ "${SANDBOX_MODE:-}" = "local" ]; then
    if curl --connect-timeout 5 -s -o /dev/null https://api.anthropic.com 2>/dev/null; then
        bad "api.anthropic.com reachable — LOCAL mode must have NO egress at all"
    else
        ok "api.anthropic.com unreachable (correct: LOCAL mode has no egress)"
    fi
    # The single sanctioned destination for this tier must actually work, or the
    # sandbox is inert. Checked by IP because that is what the firewall permits.
    if curl --connect-timeout 5 -s -o /dev/null \
         "http://${LLM_IP:-llm}:${LLM_PORT:-8080}/health" 2>/dev/null; then
        ok "inference service reachable (${LLM_IP:-llm}:${LLM_PORT:-8080}) — the one permitted destination"
    else
        bad "inference service NOT reachable (${LLM_IP:-llm}:${LLM_PORT:-8080}) — tier 2 has no other capability"
    fi
else
    if [ -n "${HTTPS_PROXY:-}" ]; then lifeline_path="via proxy $HTTPS_PROXY"; else lifeline_path="direct"; fi
    if curl --connect-timeout 5 -s -o /dev/null https://api.anthropic.com 2>/dev/null; then
        ok "api.anthropic.com reachable ($lifeline_path)"
    else
        bad "api.anthropic.com NOT reachable ($lifeline_path) — lifeline down"
    fi
fi
# Governed mode: when routed through the egress proxy, a non-allowlisted domain
# must be refused BY THE PROXY (a 403 on the CONNECT), not merely by the firewall.
# This asserts the proxy's default-deny policy is actually enforcing.
if [ -n "${HTTPS_PROXY:-}" ]; then
    # Since 2b an unknown host is HELD for approval — it blocks pending a human,
    # then default-denies on timeout — rather than being refused outright. So it
    # must NOT succeed from here. Bound the wait (--max-time) so we don't sit on
    # the full hold window; treat held/denied/timeout as pass, and only a real
    # success (the proxy allowing a non-allowlisted host with no rule and no
    # approval) as a policy-enforcement leak.
    if curl --connect-timeout 5 --max-time 8 -s -o /dev/null https://example.com 2>/dev/null; then
        bad "egress proxy allowed non-allowlisted example.com — policy not enforcing"
    else
        ok "egress proxy does not allow non-allowlisted example.com (held or denied)"
    fi

    # Port governance: the proxy is an HTTP(S) proxy, so a CONNECT to an
    # allowlisted host on a NON-443 port must be refused with a 403 — otherwise an
    # allowlisted host would carry arbitrary TCP (SSH, other TLS services) over a
    # raw tunnel. We require the 403 specifically (not just any failure): if the
    # port gate regressed, the proxy would tunnel to :8443 and curl would fail on
    # the unreachable upstream instead — no 403 — which this correctly flags.
    portresp="$(curl -sS --connect-timeout 5 -o /dev/null https://api.anthropic.com:8443/ 2>&1 || true)"
    if printf '%s' "$portresp" | grep -q '403'; then
        ok "egress proxy refuses non-443 CONNECT (api.anthropic.com:8443 -> 403)"
    else
        bad "egress proxy did NOT 403 a non-443 CONNECT to api.anthropic.com:8443 (got: ${portresp:-<none>})"
    fi

    # SNI / domain-fronting governance: CONNECT an allowlisted host but present a
    # NON-allowlisted TLS SNI. --connect-to makes curl send CONNECT
    # api.anthropic.com:443 (so http_connect passes on the authority) while the
    # TLS SNI and Host header stay example.com (not allowlisted), which is exactly
    # domain-fronting. The proxy must refuse the passthrough on the SNI.
    #   -k: if the proxy wrongly tunnelled it, we'd get the real fronted response
    #       rather than a curl cert error masking the leak.
    # Enforced outcomes (both acceptable): the proxy declines to tunnel and MITM-
    # re-gates at the HTTP layer -> 403 "egress denied by policy"; OR the handshake
    # just fails closed (no trusted CA) -> curl errors. Leak: a normal passthrough
    # response from the fronted host (curl succeeds, body is not the policy denial).
    front="$(curl -sS -k --connect-timeout 5 \
        --connect-to example.com:443:api.anthropic.com:443 \
        https://example.com/ 2>&1)"
    frc=$?
    if [ "$frc" -ne 0 ]; then
        ok "SNI fronting blocked (handshake failed closed; no passthrough)"
    elif printf '%s' "$front" | grep -qi 'egress denied by policy'; then
        ok "SNI fronting refused by proxy (deny-sni)"
    else
        bad "SNI fronting NOT refused — got a passthrough response (domain-fronting leak)"
    fi

    # Control-plane relay governance: the egress proxy is the ONLY component
    # on both sandbox-net and control-net, so it — not segmentation — must refuse
    # to relay the agent onto the control plane. Ask the proxy (via HTTPS_PROXY, so
    # IT resolves the name/connects) to reach the control-plane host and a literal
    # control-net IP; both must be refused with a 403 BEFORE any policy/port check.
    # (grep the combined output for 403, like the port/SNI checks above: a refused
    # CONNECT makes curl exit non-zero while still reporting "response 403".)
    cphost="$(curl -sS -x "$HTTPS_PROXY" --connect-timeout 5 --max-time 8 \
        -o /dev/null https://control-plane/ 2>&1 || true)"
    if printf '%s' "$cphost" | grep -q '403'; then
        ok "egress proxy refuses to relay to the control-plane host (403)"
    else
        bad "egress proxy did NOT 403 the control-plane host (got: ${cphost:-<none>}) — control-net relay risk"
    fi
    cpip="$(curl -sS -x "$HTTPS_PROXY" --connect-timeout 5 --max-time 8 \
        -o /dev/null https://172.31.0.2/ 2>&1 || true)"
    if printf '%s' "$cpip" | grep -q '403'; then
        ok "egress proxy refuses to relay to a control-net IP (172.31.0.2 -> 403)"
    else
        bad "egress proxy did NOT 403 control-net IP 172.31.0.2 (got: ${cpip:-<none>}) — control-net relay risk"
    fi

    # SSRF hardening: the proxy's default route is egress-net, which can reach the
    # cloud instance-metadata service (169.254.169.254 -> cloud credentials) and
    # the host's private network. Those are hard-blocked by the relay guard's
    # private/special-use CIDRs (PRIVATE_CIDRS), BEFORE policy — so no allow rule
    # or human approval can open them. Probe over https (a CONNECT) like the
    # control-net IP check above, so a refused tunnel surfaces "response 403" in
    # curl's output; the proxy 403s before dialing, so the (HTTP-only) metadata
    # host never actually needs to answer.
    imds="$(curl -sS -x "$HTTPS_PROXY" --connect-timeout 5 --max-time 8 \
        -o /dev/null https://169.254.169.254/ 2>&1 || true)"
    if printf '%s' "$imds" | grep -q '403'; then
        ok "egress proxy refuses to relay to the metadata IP (169.254.169.254 -> 403)"
    else
        bad "egress proxy did NOT 403 metadata IP 169.254.169.254 (got: ${imds:-<none>}) — SSRF/metadata risk"
    fi
fi

printf '%s== control plane ==%s\n' "$bold" "$reset"
# THE step-2 invariant: the agent must have NO route to the control plane. It
# lives on control-net (internal), and the sandbox is deliberately not attached.
# Probe the control-plane's fixed control-net address DIRECTLY (172.31.0.2:8090,
# matching docker-compose.yml) — DNS-independent, like the raw-IP egress probe.
# --noproxy '*' so we test the sandbox's OWN routing, not the egress proxy (which
# legitimately can reach the control plane on control-net). Any reply is a leak.
if curl --noproxy '*' --connect-timeout 5 -s -o /dev/null http://172.31.0.2:8090/healthz 2>/dev/null; then
    bad "control plane reachable from sandbox (172.31.0.2:8090) — control-net leak"
else
    ok "control plane unreachable from sandbox (control-net isolated)"
fi

printf '%s== ipv6 ==%s\n' "$bold" "$reset"
if [ -e /proc/net/if_inet6 ]; then
    # Connect to a literal v6 address (no AAAA lookup needed). We assert on the
    # curl exit code, not overall success: 7 (connect failed) and 28 (timeout)
    # mean the packet was blocked; a TLS-layer error (35/51/60) would mean the TCP
    # connection actually OPENED — i.e. an ungoverned v6 path — so treat anything
    # that isn't a clean connect-failure as reachable.
    curl --noproxy '*' -6 --connect-timeout 5 -s -o /dev/null "https://[2606:4700:4700::1111]" 2>/dev/null
    rc=$?
    case "$rc" in
        7|28) ok "IPv6 egress blocked (curl rc=$rc)" ;;
        *)    bad "IPv6 egress reachable (curl rc=$rc)" ;;
    esac
else
    ok "no IPv6 stack present (/proc/net/if_inet6 absent)"
fi

printf '%s== privilege ==%s\n' "$bold" "$reset"
# THE load-bearing property: the agent holds no Linux capabilities, so it cannot
# run iptables (NET_ADMIN) to tear down the firewall or otherwise re-privilege.
# Read from this process's own status — it runs as the agent, so this is the
# agent's real cap set (Eff/Prm/Amb are the sets that grant or preserve caps).
caps="$(grep -E '^Cap(Eff|Prm|Amb):' /proc/self/status | awk '{print $2}')"
caps_held=0
for c in $caps; do [ "$c" = "0000000000000000" ] || caps_held=1; done
if [ "$caps_held" -eq 1 ]; then
    # shellcheck disable=SC2086  # intentional word-split: flatten multiline $caps to one line
    bad "agent holds capabilities (Eff/Prm/Amb: $(printf '%s ' $caps))"
else
    ok "agent holds no capabilities (Eff/Prm/Amb all zero)"
fi
# no-new-privileges must be in effect, so any setuid-root binary can't
# re-elevate around the missing caps. (The image also ships no sudo — see the
# Dockerfile — so there is nothing whose sole purpose is escalation.)
nnp="$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)"
if [ "$nnp" = "1" ]; then ok "no_new_privs set"; else bad "no_new_privs NOT set (got '${nnp:-}')"; fi
# The Docker socket must never be present — it would be a direct path to the host
# control plane, the one thing the sandbox must never reach.
if [ -S /var/run/docker.sock ] || [ -S /run/docker.sock ]; then
    bad "docker socket present — direct host control-plane path"
else
    ok "no docker socket"
fi

printf '%s== dns ==%s\n' "$bold" "$reset"
# In GOVERNED mode the firewall allows DNS only to the embedded resolver
# (127.0.0.11); the upstream forward is dropped, so a crafted name can't be
# smuggled to a recursive resolver — reaching an external resolver directly would
# be a leak, so ASSERT it's blocked. In STANDALONE mode the pinned upstreams are
# permitted (host-dependent), so report rather than assert.
if command -v dig >/dev/null 2>&1; then
    if dig +time=3 +tries=1 @1.1.1.1 example.com >/dev/null 2>&1; then
        if [ -n "${HTTPS_PROXY:-}" ]; then
            bad "external resolver 1.1.1.1:53 reachable — governed mode should block direct DNS"
        else
            info "reached 1.1.1.1:53 — it is the pinned upstream, or port-53 is broad (check)"
        fi
    else
        if [ -n "${HTTPS_PROXY:-}" ]; then
            ok "direct DNS to external resolvers blocked (only embedded 127.0.0.11 allowed)"
        else
            info "1.1.1.1:53 unreachable (consistent with resolver pinning)"
        fi
    fi
else
    info "dig not available; skipping DNS probe"
fi

echo
if [ "$fail" -gt 0 ]; then
    printf '%s%d passed, %d FAILED%s\n' "$red" "$pass" "$fail" "$reset"
    exit 1
fi
printf '%sall %d boundary checks passed%s\n' "$green" "$pass" "$reset"
