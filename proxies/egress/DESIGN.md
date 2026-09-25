# Egress proxy — the relay guard, and the proxy's side of `/authorize`

The two things the proxy decides for itself: which destinations it refuses before any
policy is asked, and how it asks the control plane about everything else. Both are
`addon.py`, in this directory, and its tests, `tests/test_egress_addon.py` and
`tests/test_egress_hooks.py`. What the proxy is to the rest of the system stays in the
root, and this file does not repeat it:

- how it is wired in — the compose split, the firewall's governed and standalone
  modes, and why it is the sole egress at the network layer, `DESIGN.md` → "Egress
  proxy — the sole path off-box";
- the fold of every name to its A-label, whose other consumers are the approval card and
  the rules persisted from it, `DESIGN.md` → "One destination, one spelling";
- why the one control network it can reach carries only `/authorize`, `DESIGN.md` →
  "The third net, `authorize-net`";
- what the control plane does with the decisions this proxy takes alone, `DESIGN.md` →
  "Ingesting the decisions the proxy makes alone".

## The relay guard — refused before policy is asked

**Control-plane relay guard (the proxy is the bridge, so the proxy must refuse
it).** The egress proxy is the *one* component attached to both `sandbox-net` and
a control network (`authorize-net` — see "The third net" in `DESIGN.md`; it is
deliberately **not** on `control-net`), so segmentation alone does **not** isolate
the agent from the control plane — the proxy could in principle relay a connection
onto that network. It therefore hard-refuses, **before** any policy /
permanent-lifeline / port check, any destination that names a control-plane host (`control-plane`,
`control-plane-ui`) or resolves into **any** control subnet — every network the
control plane is homed on, not only the one this proxy can route to, because
unroutability is a property of the compose file that the addon cannot verify. The
list is `FORBIDDEN_CIDRS_DEFAULT` in `proxies/egress/addon.py` and is deliberately
not restated here; `tests/test_topology.py` holds it equal to the real subnets, and
`_forbidden` is the check (`EGRESS_FORBIDDEN_HOSTS` / `EGRESS_FORBIDDEN_CIDRS`).
Because the guard is checked first and never weighed against policy, no rule, human
approval, or change to the port allowlist can widen it, and a public name whose DNS
is pointed at a control subnet is caught by the resolve step. This makes the
CLAUDE.md invariant ("the agent must never reach the control plane") independently
enforced at the one place segmentation cannot cover; `boundary-check.sh` asserts the
proxy 403s a control-plane host and a literal IP in every control subnet, each in
its dotted-quad and its IPv4-mapped-IPv6 spelling — the second list has to gain
whatever the first gains, since only the mapped probe can catch a regression of the
address-family bypass. And the
damage a bypass could do is bounded by the API-surface split (in `DESIGN.md`): the
network the proxy can reach carries only `/authorize`, so even a total bypass reaches
a listener that cannot approve a held request.

The same guard also hard-blocks the **private / special-use ranges** —
cloud-metadata / link-local (`169.254.0.0/16`), loopback (`127.0.0.0/8`), RFC1918
(`10/8`, `172.16/12`, `192.168/16`), `0.0.0.0/8` (`connect(0.0.0.0)` reaches
localhost on Linux — loopback under another spelling), CGNAT/overlay
(`100.64.0.0/10`), protocol-assignment and benchmarking (`192.0.0.0/24`,
`198.18.0.0/15`), and multicast/reserved/broadcast (`224.0.0.0/4`, `240.0.0.0/4`),
plus their IPv6 equivalents — via a
separate `EGRESS_PRIVATE_CIDRS` set (`_forbidden`, checked before policy). The
proxy's default route is egress-net, a masquerading bridge with a path to the
cloud instance-metadata service (a credential-theft target), the Docker host, and
the host's internal network; without a hard block, reaching those would rest
solely on the default-deny allowlist, so a single mistaken rule or human approval
could turn the proxy into an SSRF pivot. The proxy's only legitimate upstreams are
public hosts (sibling data-plane services are reached by the agent *directly* on
sandbox-net, never through the proxy), so blocking every private range costs
nothing; RFC1918 `172.16/12` also transitively covers control-net and sandbox-net.
Kept a *separate* env from `EGRESS_FORBIDDEN_CIDRS` so the control-net guard's
fail-closed startup assertion stays specifically about control-net. Override
`EGRESS_PRIVATE_CIDRS` (narrow, don't empty) only for a deployment that
legitimately proxies to a private target such as an internal package mirror.
`boundary-check.sh` asserts the proxy 403s `169.254.169.254`.

*A destination, not a string — spelling normalization (fixed bug, keep the
regression tests).* Both hard-blocked sets are lists of **IP ranges**, but what
arrives is a **hostname field**, and the two are only equivalent after
normalization. The guard therefore lowercases, strips a trailing FQDN dot, strips
the brackets an IPv6 literal wears in an authority (`[::1]`), and — the part that
was missing — folds the IPv6 forms that carry an **embedded IPv4 address** down to
the v4 address they actually dial: v4-mapped (`::ffff:a.b.c.d`), the deprecated
v4-compatible (`::a.b.c.d`), NAT64 (`64:ff9b::/96`) and 6to4 (`2002::/16`); see
`_embedded_ipv4` / `_blocked_cidr`. Without that fold, `::ffff:169.254.169.254` was
**the metadata service under a spelling neither deterministic branch recognized**
— `ipaddress` containment never crosses address families, so it matched none of
the v4 ranges, and the resolve branch was no help because `getaddrinfo` returns the
same mapped form straight back, while `connect()` on a v4-mapped address delivers
to the v4 host. It was reachable in practice because metadata serves on `:80`,
already a permitted HTTP port; only the host policy's default-deny stood behind
it, which is exactly the "one mistaken rule or approval" case this guard exists to
remove. Teredo (`2001::/32`) is deliberately *not* folded — its embedded v4 is the
client's own NAT, not a destination anything delivers to. The fold is precise
rather than a blanket v6 ban: `::ffff:8.8.8.8` still goes to policy like any other
host. `boundary-check.sh` now probes the **mapped** spelling of both control-net
and metadata alongside the dotted-quad ones — the dotted-quad probes passed
throughout this bug, so they cannot catch its return.

*Defense-in-depth, not the sole control — and honest about the resolve branch.*
The hostname and literal-IP checks are **deterministic** (decided from the request
alone); the resolve branch is **best-effort** — it depends on a DNS lookup, so it
carries a TOCTOU/rebind gap (mitmproxy re-resolves when it dials) and can be
skipped on resolution failure (logged, returns "not forbidden" — safe, because an
unresolvable name is also undialable, and reaching the control plane is prevented
first by topology and by the port gate: the control plane listens on `:8090` and
`:8091` while CONNECT/HTTP are gated to `:443`/`:80`, so a rebound name is dialed
on a port nothing serves). **That last bound covers the control plane and nothing
else** — the other destinations this guard exists to refuse (the metadata IP, the
Docker host, the LAN) answer on the very `:80`/`:443` the port gate permits, so for
those the rebind gap is bounded by none of the above. It is carried as a known open
finding in `SECURITY.md` rather than claimed as closed here.
To keep the guard from being *silently* disabled by
misconfiguration, `load()` calls `_assert_guard_configured()`, which **fails
closed at startup** (refuses to run) if `EGRESS_FORBIDDEN_CIDRS` is empty — an
empty CIDR set would drop both the literal-IP and resolve branches, leaving only
exact-hostname matching. The durable fix for the residual rebind gap was never to
keep hardening the DNS check but to **cap the blast radius of any bypass**, and
that is now built: the API surface is split, so a bypass reaches a listener that
can only *query* `/authorize` and can never self-approve through the management
API — see "the third net, `authorize-net`" in `DESIGN.md`.

## A control-plane client

The egress proxy is now a **control-plane client** rather than a static-allowlist
enforcer: on every connection it calls `POST /authorize {host, ...}`, which
returns the decision **and** records the audit row in one call — so policy and
audit share the round-trip, and there is no client-side cache (an operator rule
edit applies to the very next connection). The call runs in a worker thread
so it never blocks mitmproxy's event loop, and the egress image gains no new
dependency. The canonical allow policy now lives at `policies/egress-allowlist.txt`
(the control plane seeds SQLite from it on first boot, idempotently); the proxy
no longer bakes or reads an allowlist file. Two deliberate properties: (1) the
**permanent lifeline** (Anthropic API/auth) is allowed by a *local* check in the
proxy *before* the control plane is consulted, so a control-plane outage never
bricks the agent's own API; (2) everything else **fails closed** — if the control
plane is unreachable or times out, the request is denied and audited locally. That
second property has to include the proxy's own errors: mitmproxy answers an
exception in an addon hook by logging it and letting the flow proceed, so an
unhandled error anywhere in a hook is a request dialled ungoverned and unaudited.
Every hook is therefore wrapped to refuse the flow and write a local deny row on
any exception (`_fail_closed` in `proxies/egress/addon.py`).

**The lifeline is scoped by client, and it is the only decision here that is.**
Everywhere else this proxy is deliberately client-agnostic: it asks the policy
authority and the authority decides on the host. The lifeline is the one allow that
skips that call, so who is asking has to be part of it — its justification names its
own scope, *the agent's* own API, and only sandbox-net runs the agent. Since
`mcp-net`, this proxy also serves MCP server containers, which hold credentials and
never talk to Anthropic; a host-only check would hand them the sole egress path
nothing holds and the control plane never records. Enforced by `_is_lifeline` in
`proxies/egress/addon.py` against `EGRESS_LIFELINE_CIDRS`, whose default
`tests/test_topology.py` holds equal to sandbox-net's subnet. Emptying it fails the
safe way — no client qualifies and every host becomes the control plane's call —
which is why, unlike `EGRESS_FORBIDDEN_CIDRS`, it carries no startup assertion.
