#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Boundary smoke test, shared by every sandbox tier.
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
    llm_url="http://${LLM_IP:-llm}:${LLM_PORT:-8080}"
    if curl --connect-timeout 5 -s -o /dev/null "$llm_url/health" 2>/dev/null; then
        ok "inference service reachable (${LLM_IP:-llm}:${LLM_PORT:-8080}) — the one permitted destination"
        # Reachability is not usability. /health returns 200 once the GGUF is
        # resident, which a wedged or misconfigured server also does — so assert a
        # real COMPLETION round-trip. max_tokens 1 keeps it to a single decode
        # (~0.1 s) while still exercising the whole path the agent uses: chat
        # template, tool-call-capable --jinja parsing, sampler, detokenizer.
        #
        # Asserted on the SHAPE, never the content: the response must carry
        # `choices` and must not carry `error`. A local model's words are not a
        # test fixture — they change with the weights, the sampler and the quant,
        # and a check that asserts them is a check that gets disabled.
        #
        # 60 s because a cold prefill on an iGPU is slow and this check must not
        # fail for being impatient; it is diagnosing "does the model generate at
        # all", not latency.
        #
        # KNOWN COST, and it follows from `--parallel 1` in docker-compose.yml: the
        # prompt cache is per-slot and there is one slot, so this request EVICTS a
        # running agent's cached prefix. Running `make boundary` mid-session makes
        # the agent's next turn re-prefill its whole base prompt — tens of seconds
        # on this hardware, once. Acceptable for an on-demand diagnostic, and the
        # reason this is not on a timer.
        llm_reply=$(curl --connect-timeout 5 --max-time 60 -s \
                      -H 'Content-Type: application/json' \
                      -d '{"messages":[{"role":"user","content":"ping"}],"max_tokens":1}' \
                      "$llm_url/v1/chat/completions" 2>/dev/null)
        case "$llm_reply" in
            *'"choices"'*) ok "inference service completes a request (generation works, not just listening)" ;;
            *'"error"'*)   bad "inference service returned an error to a minimal completion: $(printf '%s' "$llm_reply" | tr -d '\n' | cut -c1-160)" ;;
            "")            bad "inference service accepted the connection but returned nothing to /v1/chat/completions (loading, wedged, or OOM?)" ;;
            *)             bad "inference service gave an unrecognised reply to /v1/chat/completions: $(printf '%s' "$llm_reply" | tr -d '\n' | cut -c1-160)" ;;
        esac
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
    # Enforced outcomes (all acceptable): the proxy declines to tunnel and MITM-
    # re-gates at the HTTP layer -> 403 "egress denied by policy"; OR the handshake
    # fails closed (no trusted CA) -> curl errors; OR the re-gate holds and we stop
    # waiting (see --max-time below) -> curl errors. Leak: a normal passthrough
    # response from the fronted host (curl succeeds, body is not the policy denial).
    #
    # --max-time for the same reason as the example.com probe above, and it is the
    # dominant cost of this whole script without it. After the SNI refusal the proxy
    # MITM-re-gates at the HTTP layer, where BOTH names are authorized (transport
    # host and Host header — see the request hook in addon.py). The Host header is
    # still the non-allowlisted example.com, so that gate HOLDS, and with no human
    # to answer it the probe sat for the full CONTROL_HOLD_TIMEOUT: a measured 120s
    # of a 5m04s run, in one probe.
    #
    # Bounding it cannot mask the failure this exists to catch, because A LEAK IS
    # FAST: a wrongly-tunnelled request returns the fronted host's own response
    # promptly (-k means no cert error to stall on). Only the hold path is slow, and
    # the hold path is a refusal. So a timeout here is one more way of being denied.
    #
    # Which is why the non-zero branch no longer NAMES a mechanism. It used to say
    # "handshake failed closed", true when the only way to get here was a cert error;
    # a timeout would now print it while something else entirely had happened. The
    # branch asserts what it can actually distinguish — no passthrough — and leaves
    # the how to the two reasons above it.
    front="$(curl -sS -k --connect-timeout 5 --max-time 8 \
        --connect-to example.com:443:api.anthropic.com:443 \
        https://example.com/ 2>&1)"
    frc=$?
    if [ "$frc" -ne 0 ]; then
        ok "SNI fronting blocked (no passthrough)"
    elif printf '%s' "$front" | grep -qi 'egress denied by policy'; then
        ok "SNI fronting refused by proxy (deny-sni)"
    else
        bad "SNI fronting NOT refused — got a passthrough response (domain-fronting leak)"
    fi

    # Control-plane relay governance: the egress proxy is the ONLY component on both
    # sandbox-net and a control network (authorize-net — see below), so it — not
    # segmentation — must refuse to relay the agent onto the control plane. Ask the
    # proxy (via HTTPS_PROXY, so
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
    # Both control subnets, and the SECOND one is the one that matters. The proxy
    # is attached to authorize-net (172.29.0.0/24) and NOT to control-net, so
    # 172.29.0.2 is the address a relayed connection could actually land on —
    # 172.31.0.2 is unroutable from the proxy and would fail even with the guard
    # off, which makes it the weaker of the two probes despite being the older one.
    # Keep both: the guard must not start depending on the topology for its effect.
    #
    # NEITHER probe can tell you WHICH list refused it. Both addresses also fall
    # inside PRIVATE_CIDRS (RFC1918 172.16.0.0/12), so a 403 here survives dropping
    # them from EGRESS_FORBIDDEN_CIDRS entirely — which is precisely the state the
    # startup assertion exists to prevent and cannot be observed from out here.
    # tests/test_topology.py asserts the CIDR list itself against the compose
    # subnets; this asserts the refusal a sandbox actually experiences.
    for cpip_addr in 172.31.0.2 172.29.0.2; do
        cpip="$(curl -sS -x "$HTTPS_PROXY" --connect-timeout 5 --max-time 8 \
            -o /dev/null "https://${cpip_addr}/" 2>&1 || true)"
        if printf '%s' "$cpip" | grep -q '403'; then
            ok "egress proxy refuses to relay to a control-net IP ($cpip_addr -> 403)"
        else
            bad "egress proxy did NOT 403 control IP $cpip_addr (got: ${cpip:-<none>}) — control-plane relay risk"
        fi
    done

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

    # Same two destinations, written as IPv4-MAPPED IPv6. This is the spelling that
    # used to defeat the relay guard entirely: address-family containment meant a
    # mapped address matched none of the v4 blocked ranges, and the resolve branch
    # re-tested the same unrecognized form, while connect() on a v4-mapped address
    # still reaches the v4 host. The dotted-quad probes above passed throughout, so
    # only an explicitly mapped probe can catch a regression here. Both must 403 for
    # the same reason as their dotted-quad twins — BEFORE any policy or port check.
    for mapped in "[::ffff:172.31.0.2]" "[::ffff:172.29.0.2]" "[::ffff:169.254.169.254]"; do
        resp="$(curl -sS -x "$HTTPS_PROXY" --connect-timeout 5 --max-time 8 \
            -o /dev/null "https://${mapped}/" 2>&1 || true)"
        if printf '%s' "$resp" | grep -q '403'; then
            ok "egress proxy refuses IPv4-mapped IPv6 destination ($mapped -> 403)"
        else
            bad "egress proxy did NOT 403 $mapped (got: ${resp:-<none>}) — mapped-IPv6 guard bypass"
        fi
    done

    # Plaintext HTTP must reach the proxy too, and nothing above can tell us that:
    # every governed probe here is https, and curl honours HTTPS_PROXY in EITHER
    # case, so all of them pass while `curl http://...` bypasses the proxy entirely
    # and dies at DNS. That was this sandbox's real state until the launcher began
    # setting the lowercase variable — curl reads `http_proxy` LOWER CASE ONLY, a
    # fact about curl recorded in NOTES.md. `make consistency` guards the pairing,
    # but it reads the launcher SOURCE; only a probe from inside sees the
    # environment a running container actually ended up with.
    #
    # control-plane over http:// is the discriminator. Unproxied, the name resolves
    # only on control-net, so curl fails DNS instantly with no timeout to sit
    # through. Proxied, the relay guard refuses it BEFORE any policy lookup — a
    # deterministic 403 that needs no seeded rule and raises no hold, so this check
    # does not depend on the state of the policy store. Distinguish all three
    # outcomes: "never reached the proxy" and "reached it and was not refused" are
    # opposite defects that would otherwise share one non-403 symptom.
    plain="$(curl -sS --connect-timeout 5 --max-time 8 \
        -o /dev/null -w '%{http_code}' http://control-plane/ 2>/dev/null || true)"
    case "$plain" in
        403)    ok "plaintext HTTP is proxied and governed (http://control-plane -> 403)" ;;
        000|"") bad "plaintext HTTP never reached the egress proxy — curl resolved the host itself (is LOWERCASE http_proxy set? see NOTES.md)" ;;
        *)      bad "plaintext HTTP reached the proxy but was NOT refused (got: $plain) — control-net relay risk on the http path" ;;
    esac
fi

printf '%s== control plane ==%s\n' "$bold" "$reset"
# THE step-2 invariant: the agent must have NO route to the control plane. It
# lives on control-net (internal), and the sandbox is deliberately not attached.
# Probe the control-plane's fixed control-net address DIRECTLY (172.31.0.2:8090,
# matching docker-compose.yml) — DNS-independent, like the raw-IP egress probe.
# --noproxy '*' so we test the sandbox's OWN routing, not the egress proxy (which
# legitimately can reach the control plane on control-net). Any reply is a leak.
# BOTH of its addresses, because it now answers a different surface on each and
# the agent must reach neither: 172.31.0.2:8090 is the management API (approvals,
# resolve, the audit store) and 172.29.0.2:8091 is /authorize on authorize-net.
# Probing only the first would leave the newer network unasserted precisely
# because it is newer.
cp_leak=0
for target in 172.31.0.2:8090 172.29.0.2:8091; do
    if curl --noproxy '*' --connect-timeout 5 -s -o /dev/null "http://${target}/healthz" 2>/dev/null; then
        bad "control plane reachable from sandbox ($target) — control-network leak"
        cp_leak=1
    fi
done
if [ "$cp_leak" -eq 0 ]; then
    ok "control plane unreachable from sandbox on both control networks"
fi

printf '%s== mcp ==%s\n' "$bold" "$reset"
# MCP servers hold the credentials the sandbox must not have (a GitHub PAT today),
# and nothing INSIDE those containers keeps the agent out — only the absence of a
# route does. They live on mcp-net, which the sandbox is not attached to.
#
# Probing a server directly would be a vacuous test: none runs unless its profile
# is enabled, so "connection failed" would pass for the wrong reason. Probe the
# EGRESS PROXY's mcp-net address instead — it is on that network permanently and
# genuinely listening there, so this measures routing and nothing else. The
# positive control is already in this run: the same service, same port, answered on
# 172.30.0.10 in the egress section above. Reaching it here and not there is the
# difference between "no listener" and "no route", which is the distinction that
# makes the result mean anything.
if curl --noproxy '*' --connect-timeout 5 -s -o /dev/null http://172.28.0.10:8080 2>/dev/null; then
    bad "mcp-net reachable from sandbox (172.28.0.10:8080) — the MCP servers' credentials are exposed"
else
    ok "mcp-net unreachable from sandbox (MCP server containers are out of reach)"
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
