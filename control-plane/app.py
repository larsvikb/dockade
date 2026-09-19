# SPDX-License-Identifier: Apache-2.0
"""
Control plane — governance authority for the governed data-plane proxies.

Step 2b: policy + audit + **hold-for-approval**. The management app the agent can
never reach — it lives on internal networks the sandbox is not attached to.
Governed proxies call it on the control path to authorize connections; unknown
requests are held for a human, who approves/rejects them in a live UI.

This file is the HTTP surface — the three apps, the request models, the routes and
the process entry point. The machinery each route drives lives beside it, one
module per concern, and every call below is written qualified (``policy._decide``,
``holds._reserve_hold``) so a reader can see which one is being asked:

  - ``store``   — SQLite: the schema, the audit write, the seed. The crown jewel.
  - ``audit``   — the READ side of that store: the two decision views, their shared
    filters and the record view's paging.
  - ``policy``  — what a rule pattern matches, and what a host decides to.
  - ``holds``   — the in-process registry a blocked request waits on, its caps
    and its duplicate grouping.
  - ``ingest``  — draining the egress proxy's own audit file into the store.

They import in that order and never back: nothing under this file imports this
file.

The authorize flow (one call from the proxy, `POST /authorize`). Every rule is scoped
to a CLIENT CLASS — the ingress network the caller reached the proxy on, named by
``policy._client_class`` — so "matches" below means matches for the class asking, and
a rule written for one client population decides nothing for another:
  - host matches a BLOCK rule            -> deny   (audited)
  - host matches an ALLOW rule           -> allow  (audited)
  - no matching rule                     -> HOLD: record a pending approval and
    BLOCK the request until a human resolves it or CONTROL_HOLD_TIMEOUT elapses
    (-> default-deny). The proxy only ever sees allow/deny; the hold is internal.
    A request identical to one already held JOINS it rather than raising a second
    approval, so a retrying agent produces one card and one decision — which is
    also why one click can release several blocked requests (see holds._group_key).

A human resolves holds over the approvals API (the SSE stream at /approvals/stream
and POST /approvals/{id}/resolve), surfaced by the separate control-plane-ui
frontend; the backend serves no HTML itself. GET /approvals is the non-streaming
form of the same list and is still served here, but the UI does not use it and the
frontend no longer relays it — see _RELAY_ROUTES in control-plane-ui/app.py. Five
read-only views back the rest of that UI: GET /api/audit (recent decisions, folded),
GET /api/audit/events (the same record unfolded, filtered and paged — the two are one
per ``audit.py``'s glance/record split), GET /api/egress/rules (the standing policy — see
``api_rules`` for why that one has to be visible), GET /api/egress/leases (the timed
grants deciding right now) and GET /api/config (the hold window, so a card can show
its countdown, and the lease duration, so the button that grants one can label
itself). The resolve vocabulary is a DURATION LADDER (``EGRESS_ACTIONS``):
  - allow-once / deny-once     — decide just this request
  - allow-lease                — also allow that exact host, for that client class,
    until ``policy.LEASE_SECONDS`` elapses. The rung between one request and standing
    policy, for a host an agent is about to hit repeatedly; it takes no pattern choice
    because it answers the breadth question by expiring.
  - allow-persist / deny-persist — also write a rule so future connections skip
    the hold (progressive trust; DESIGN.md "auto-approve progressively more"). WHICH
    rule is the operator's choice from a bounded set derived from the requested host
    (``policy._persist_candidates``), not a string the agent's request can supply.

Standing policy is also editable directly, which is the half that does not begin with
a request: POST /api/egress/rules writes a rule (``create_rule``) and POST
/api/egress/rules/{id}/revoke takes one back (``revoke_rule``). The pattern there IS
caller-supplied — there is no held host to derive candidates from — so that path
validates it (``policy._rule_error``) where the persist path constrains it. A lease has
only the taking-back half, POST /api/egress/leases/{id}/revoke (``revoke_lease``):
nothing creates one without a card to grant it from, because a timed grant with no
request behind it is just a standing rule somebody will forget they wrote.

Granting egress is the privileged act here, and there are two ways to perform it:
resolving a hold, and writing a standing rule outright (``create_rule``, the config-
first half of policy — the other three rule paths are all downstream of a request the
agent already made). Both record the PROVENANCE of whoever did it (``_actor``) — on
the durable approvals row (``resolved_by``) and in the audit reason for a resolution,
in the audit reason for a rule written or revoked. That is detection, not prevention:
the self-reported fields are forgeable by a host-local caller. It exists so a forged approval is at least visible in the
record afterwards, which it previously was not — an operator's click and a
scripted POST were indistinguishable once written.

THREE LISTENERS, on three networks, because the dangerous surface is the management
API and not the questions the enforcers ask it. Both ways of granting egress live on
the management one — `resolve` and `create_rule` — so anything that reaches it can
self-approve, while an enforcer's bridge can only ever answer a policy question.
They are therefore served separately (``main``):

  - the AUTHORIZE listener (CONTROL_AUTHORIZE_PORT, on authorize-net) serves
    exactly POST /authorize and GET /healthz — ``authorize_app`` below. It is the
    only surface the egress proxy has a route to.
  - the TOOL listener (CONTROL_TOOL_PORT, on tool-authorize-net) serves the MCP
    gateway's three questions and nothing else — ``tool_app`` below.
  - the MANAGEMENT listener (CONTROL_MANAGE_PORT, bound to the control-net address
    ALONE — a wildcard bind is refused at startup) serves everything else, and is
    reachable only from control-plane-ui.

The two enforcer bridges are separate networks and separate sockets rather than one
shared "ask policy" surface, because a lateral edge between two enforcers is what
splitting them bought its way out of: the proxy's relay guard is best-effort by
construction, and a bypassed proxy must not gain a route to the gateway's claim
endpoint, which is the one place a side effect gets released. That is also why the
TOOL listener binds ONE address where the authorize listener binds the wildcard —
the asymmetry is not an oversight, it is the whole point (see
``_assert_listeners_separated``).

This is blast-radius containment, not the primary control: the agent is kept off
this service by network topology, by the proxy's relay guard and by the proxy's
port gate. What the split adds is that all three failing at once yields a policy
QUERY rather than a self-approval. See DESIGN.md.

Concurrency model: run under a SINGLE uvicorn worker. `/authorize` and the
resolve endpoint are sync (FastAPI runs them in a threadpool); a held request
blocks its worker on a threading.Event that the resolve endpoint sets. Blocked
workers are bounded (CONTROL_MAX_WAITERS / CONTROL_MAX_WAITERS_PER_CLIENT) so a
sandbox cannot pin every worker and stall governance for all sandboxes; CARDS are
bounded separately (CONTROL_MAX_PENDING / CONTROL_MAX_PENDING_PER_CLIENT), which
protects the operator's attention rather than the pool — see holds.py for why the
two nouns need four caps. Over any of them /authorize fails closed. The SQLite store is the source of truth for the UI
(the SSE stream polls it). Do NOT run multiple workers — the pending-event
registry is in-process (holds.py). That constraint is also why the listeners
above are separate sockets in ONE process rather than separate services: a held
/authorize and the `resolve` that releases it must share memory, precisely because
they must not share a socket.

No egress: this service sits on control-net, authorize-net and tool-authorize-net,
all internal. It must never be given an internet route — it is pure management
state (the crown jewel).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import threading
import time
import uuid
from typing import Any

import audit
import holds
import ingest
import inventory
import policy
import store
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

# How many recent audit rows /api/audit folds into its grouped view (see api_audit).
# Bounds the aggregation regardless of table size; the slice itself rides audit_ts.
AUDIT_GROUP_SCAN = int(os.environ.get("CONTROL_AUDIT_GROUP_SCAN", "5000"))

# The reason the egress proxy writes when it denies because it could not reach THIS
# service (proxies/egress/addon.py, ``_authorize``). Those denials are correct — that
# is fail-closed working — but they are not policy, and until they were told apart an
# operator could not distinguish "your rules refused this" from "governance is down
# and everything is being refused". Both render as a red `deny` row against a host.
#
# Matched as a PREFIX because the proxy appends the underlying exception. Matched at
# all, rather than shared as a constant, because these are separate services in
# separate images with no common module — so a test asserts the two strings still
# agree (tests/test_control_plane_api.py). If they ever drift, the classification
# silently returns to what it was before this existed: an ordinary deny. That is the
# safe direction, and it is why matching is acceptable here at all.
FAIL_CLOSED_REASON = "control-plane unreachable"

# ── the three listeners (see the module docstring) ──────────────────────────
# The proxy-facing surface binds a WILDCARD on purpose: it is the safe one, it
# only answers policy questions, and the container's healthcheck reaches it over
# loopback. The management surface binds ONE address, and the default is loopback
# so that a deployment which forgets to set it fails VISIBLY (the UI cannot reach
# the backend) instead of silently re-exposing `resolve` to the proxy's network.
AUTHORIZE_BIND = os.environ.get("CONTROL_AUTHORIZE_BIND", "0.0.0.0")  # noqa: S104
AUTHORIZE_PORT = int(os.environ.get("CONTROL_AUTHORIZE_PORT", "8091"))
MANAGE_BIND = os.environ.get("CONTROL_MANAGE_BIND", "127.0.0.1")
MANAGE_PORT = int(os.environ.get("CONTROL_MANAGE_PORT", "8090"))
# The MCP gateway's bridge. Binds ONE address like the management surface, not the
# wildcard the authorize surface uses, and the default is loopback for the same
# reason: a deployment that forgets to set it fails VISIBLY (the gateway cannot
# reach it) rather than silently serving the claim endpoint on authorize-net, where
# the egress proxy could burn an approved ask the agent is coming back for.
#
# The consequence of the authorize listener's wildcard, stated rather than left to be
# discovered: the gateway CAN reach /authorize, since that socket answers on every
# interface. Accepted, because that surface only ever answers a policy question — the
# same property that makes it safe for the proxy. The reverse direction is the one
# that had to be closed, and this bind is what closes it.
TOOL_BIND = os.environ.get("CONTROL_TOOL_BIND", "127.0.0.1")
TOOL_PORT = int(os.environ.get("CONTROL_TOOL_PORT", "8092"))
#: Every spelling of "listen on every interface", including the empty string, which
#: uvicorn treats as one. Listed rather than substring-matched: a substring test
#: would also reject a legitimate address that happens to contain one of these.
_WILDCARDS = ("", "0.0.0.0", "::", "*")  # noqa: S104
#: Where the tool bridge must NOT be served, as CIDRs rather than as a wildcard test.
#: A wildcard is one way to put the claim endpoint on authorize-net; naming that
#: network's address outright is the other, and it passes a wildcard check while
#: producing exactly the outcome the check exists to refuse — the claim endpoint
#: within the egress proxy's reach, with every healthcheck green. This is the whole
#: reason the assertion is in the app and not only in tests/test_topology.py: it is
#: here so as not to trust the compose file, so it cannot be satisfied by a test that
#: reads the compose file.
#:
#: Defaults mirror docker-compose.yml, the same arrangement ``FORBIDDEN_CIDRS`` in
#: proxies/egress/addon.py uses, and tests/test_topology.py holds them equal to the
#: real subnets. control-net is listed alongside authorize-net because the rule is one
#: bridge per enforcer: sharing the management network would not reach the proxy, but
#: it would put the gateway on the operator's path, and neither enforcer belongs on
#: the other's leg.
_TOOL_BIND_FORBIDDEN = os.environ.get(
    "CONTROL_TOOL_BIND_FORBIDDEN", "172.29.0.0/24,172.31.0.0/24")
#: The same refusal for the management surface, which is the more dangerous one to
#: get wrong: `resolve` lives here. A wildcard check alone let
#: ``CONTROL_MANAGE_BIND=172.29.0.2`` — this container's own authorize-net address —
#: start cleanly and serve `resolve` and `create_rule` to the egress proxy, with
#: every healthcheck green. The two enforcers' networks are listed; control-net is
#: not, because that is the address this listener is required to bind.
_MANAGE_BIND_FORBIDDEN = os.environ.get(
    "CONTROL_MANAGE_BIND_FORBIDDEN", "172.29.0.0/24,172.27.0.0/24")

#: The most either enforcer-facing listener accepts in one request body, in bytes.
#: Both peers — the proxy on `/authorize`, the gateway on the tool bridge — relay what
#: the SANDBOX sent, and FastAPI materializes the whole body through the request
#: model before any handler can measure it (``ToolCallRequest.args`` is `Any`, and
#: ``holds.TOOL_ARGS_MAX`` applies only after the parse, on the `ask` path). Uncapped,
#: a few hundred megabytes of `args` was an OOM kill of this process under its 512m
#: limit — governance down on the sandbox's say-so. Not a bypass, since egress fails
#: closed while the container restarts, but the sandbox's one lever against
#: availability, and SECURITY.md puts "pressure becoming permission" in scope.
#:
#: Sized to the peer. An egress question is a host, a port and a URL; the tool bridge
#: carries a tool call's complete arguments, which the gateway has already bounded
#: at its own intake (``GATEWAY_BODY_MAX``), so this is the backstop behind that cap
#: rather than the cap itself. Fail-closed bounds, so env vars (DESIGN.md, "Hold
#: bounds are fail-closed, so their values stay env vars").
AUTHORIZE_BODY_MAX = int(os.environ.get("CONTROL_AUTHORIZE_BODY_MAX", str(64 * 1024)))
TOOL_BODY_MAX = int(os.environ.get("CONTROL_TOOL_BODY_MAX", str(2 * 1024 * 1024)))

# Everything except /authorize: the approvals API, the read-only views, /status.
app = FastAPI(title="dockade control plane", version="2b")
# POST /authorize and GET /healthz, and nothing else, ever. Adding a route here
# hands it to the egress proxy — the one component whose compromise this split
# exists to survive — so the question to ask of any new endpoint is not "is it
# read-only" but "would I let a bypassed relay guard call it".
authorize_app = FastAPI(title="dockade control plane (authorize)", version="2b")
# The MCP gateway's three questions, and nothing else, ever: what may this call do,
# which servers and tools are configured, and may I now run the ask a human approved.
# The question to ask of any new route here is the one above with a different
# enforcer: "would I let a compromised MCP gateway call it". Nothing on this app may
# GRANT — no rule is written here and no approval is decided here (`resolve` stays on
# the management app, asserted by tests/test_control_plane_api.py).
tool_app = FastAPI(title="dockade control plane (tool)", version="2b")


def _body_cap(cap: int):
    """Middleware refusing a request body over ``cap`` BEFORE FastAPI reads it.

    Decided from ``Content-Length`` alone, and that is enough here where it would not
    be on the gateway's agent-facing listener: both peers of these two apps are the
    stdlib `urllib` in the proxy and the gateway, which always sends the header and
    never chunks. A POST without one is therefore not a peer this listener knows, and
    is refused as such (411) rather than read to find out how big it is. A GET carries
    no body and passes untouched — the roster is fetched that way."""
    async def middleware(request: Request, call_next):
        if request.method.upper() in ("POST", "PUT", "PATCH"):
            declared = request.headers.get("content-length")
            if declared is None or not declared.isdigit():
                return JSONResponse(
                    {"detail": "Content-Length required on this listener"},
                    status_code=411)
            if int(declared) > cap:
                return JSONResponse(
                    {"detail": f"request body of {declared} bytes exceeds the "
                               f"{cap}-byte cap for this listener"},
                    status_code=413)
        return await call_next(request)
    return middleware


# Registered on the two enforcer-facing apps and NOT on the management app: the
# management surface is the operator's, reached through the UI relay, and is not
# what the sandbox can lean on.
authorize_app.middleware("http")(_body_cap(AUTHORIZE_BODY_MAX))
tool_app.middleware("http")(_body_cap(TOOL_BODY_MAX))


# ── provenance ──────────────────────────────────────────────────────────────

# Header the control-plane-ui relay uses to assert the BROWSER's address. The relay
# strips any client-supplied copy before setting it (see control-plane-ui/app.py),
# so a caller cannot self-report this value — but it is only as trustworthy as the
# relay, which is why _actor labels it as asserted rather than observed.
ACTOR_HEADER = "x-dockade-actor"
# Bound each recorded self-reported header (User-Agent, Origin) so a hostile one
# cannot bloat the store.
_ACTOR_UA_MAX = 120


def _actor(request) -> str:
    """Compact provenance for whoever resolved an approval, for the durable record.

    The trust level differs per field, so the labels distinguish them:
      - ``peer``   — the socket address this process itself observed. Unforgeable by
        the caller, but for anything arriving through the UI it is the
        control-plane-ui container, so it identifies the RELAY, not the human.
      - ``via-ui`` — the browser address the relay asserts (``ACTOR_HEADER``).
      - ``origin`` / ``ua`` — self-reported by the client, therefore forgeable.
        Recorded anyway because they are usually what betrays a non-browser caller.

    DETECTION, not prevention. A process running on the host can forge every
    self-reported field, and origin/Host checks in the relay are browser-enforced so
    they do not constrain it either. Preventing host-local forgery needs a human
    presence gesture the host cannot replay (WebAuthn user-presence or an
    out-of-band confirm) — see DESIGN.md. Until then the goal is that a forged
    approval leaves a trace that reads differently from an operator's click."""
    if request is None:                      # hand-invoked / non-HTTP caller
        return "unrecorded (no request context)"
    client = getattr(request, "client", None)
    parts = [f"peer={getattr(client, 'host', None) or '?'}"]
    headers = getattr(request, "headers", None) or {}
    asserted = headers.get(ACTOR_HEADER)
    if asserted:
        parts.append(f"via-ui={asserted}")
    origin = headers.get("origin")
    if origin:
        parts.append(f"origin={origin[:_ACTOR_UA_MAX]}")
    ua = headers.get("user-agent")
    if ua:
        parts.append(f'ua="{ua[:_ACTOR_UA_MAX]}"')
    return " ".join(parts)


# ── API models ──────────────────────────────────────────────────────────────

class AuthorizeRequest(BaseModel):
    host: str
    port: int | None = None
    proto: str | None = None
    client: str | None = None
    method: str | None = None
    url: str | None = None
    stage: str | None = None


class AuthorizeResponse(BaseModel):
    decision: str                  # allow | deny  (hold is resolved internally)
    reason: str


class ResolveRequest(BaseModel):
    action: str                    # allow_once | allow_persist | deny_once | deny_persist
    # Which pattern a `*_persist` action writes. Must be one of the approval's
    # ``policy._persist_candidates``; omitted means the narrowest of them (the exact
    # host). Ignored by the two `*_once` actions, which write no rule at all.
    pattern: str | None = None


class RuleCreateRequest(BaseModel):
    # Validated and normalized server-side (policy._normalize_pattern / _rule_error).
    # Unlike a `*_persist` pattern, this one is not chosen from a derived candidate set
    # — there is no held request to derive one from — so this model is the widest input
    # in the service, and ``create_rule`` is where that is answered for.
    pattern: str
    action: str                    # allow | block
    # Required, with no default. A default would be a class the caller did not name,
    # and every wrong guess is a rule that decides for a population the operator did
    # not mean — silently, since a mis-scoped rule looks correct in the rules view.
    client_class: str
    # NOTE the field that is absent: ``source``. It is server-set to 'operator' and
    # must never be caller-supplied, because 'seed' is the value ``revoke_rule``
    # refuses to delete — a caller that could set it could write an UNREVOCABLE rule.


class RuleEditRequest(BaseModel):
    # The TARGET state, not a delta: both fields are required even when one of them is
    # unchanged. An absent field would have to mean "leave this alone", which is
    # indistinguishable from a caller that meant to send it and did not — and ``action``
    # is the field where that ambiguity decides egress. Stating both is also what lets
    # the audit row report a before and an after that the caller actually asked for.
    pattern: str
    action: str                    # allow | block
    # NOTE the two fields that are absent. ``source``, for the reason
    # ``RuleCreateRequest`` gives. And ``client_class``, because a rule's class is not
    # editable: moving one between classes takes policy away from one population and
    # gives it to another, which is two changes wearing one audit row. See ``edit_rule``.


class ServerCreateRequest(BaseModel):
    # Registering a server, not starting one: compose declares it and a human brings
    # the container up. This names it so policy can be written about it.
    server: str
    # The auth descriptor, defaulted to the preferred case — a server holding its own
    # credential, with the gateway injecting nothing. `header` is for a server that
    # refuses that, and then both fields below are required (policy._auth_descriptor_error).
    auth_type: str = "none"
    auth_header: str | None = None
    auth_template: str | None = None
    # NOTE the field that is absent: ``enabled``. A registration cannot arrive
    # pre-enabled, because then one call both introduces a server and opens it — and
    # the audit row for "registered" would be the same row as "turned on".


class ServerEditRequest(BaseModel):
    # The TARGET state, for the reason ``RuleEditRequest`` gives: an absent field
    # meaning "leave this alone" is indistinguishable from one a caller meant to send.
    enabled: bool
    auth_type: str = "none"
    auth_header: str | None = None
    auth_template: str | None = None


class InventoryRequest(BaseModel):
    # What the gateway OBSERVED, which is a different kind of thing from every other
    # model in this file: those carry an operator's decision and must be right, this
    # carries a third party's claim and need only be bounded. `inventory.record` is
    # where that bounding happens — not here, because a shape check is not a cap.
    servers: dict | None = None


class ToolRuleCreateRequest(BaseModel):
    # Which server's tool. Checked for EXISTENCE against ``mcp_servers`` rather than
    # re-validated as a name — a rule naming a server nobody registered is inert, and
    # an inert rule reads as policy in force while deciding nothing.
    server: str
    tool: str
    action: str                    # allow | deny | ask


class ToolRuleEditRequest(BaseModel):
    # ACTION only, and the two absent fields are the point. A tool rule's identity IS
    # (server, tool): changing either does not adjust a rule, it retires one and
    # writes another, which is two changes wearing one audit row. Promoting a tool
    # between deny, ask and allow is the operator's actual workflow, and that is what
    # this is.
    action: str


class ToolCallRequest(BaseModel):
    """A tool call the gateway is about to make, as a question rather than a report:
    nothing has run when this arrives, which is what makes a `deny` worth anything."""
    server: str
    tool: str
    # The COMPLETE arguments, as the gateway received them. Not a summary and not a
    # subset: on the `ask` path this becomes what a human reads and what the grant is
    # bound to (``holds._canonical_args`` re-serializes it into the one canonical
    # form), so a payload trimmed here would bind an approval to something other than
    # the call. ``Any`` because it is a tool's own JSON — whatever shape that server's
    # schema allows, and this is not the place that judges it. Oversize is refused,
    # not truncated (``holds.TOOL_ARGS_MAX``).
    args: Any = None
    # The SANDBOX's address, relayed by the gateway — the same arrangement as
    # ``AuthorizeRequest.client``, where the proxy reports the peer it observed. It is
    # what the per-client ask cap counts and part of what an identical retry joins on,
    # so the gateway reporting its OWN address instead would make one shared bucket of
    # every sandbox and let one agent's asks answer another's.
    client: str | None = None


class ToolResumeRequest(BaseModel):
    """Resumption: the agent has come back for an ask a human approved.

    ``client`` is checked against the ask's own, so an id that leaked or was guessed
    from another sandbox is answered as unknown rather than claimed — both tiers share
    `sandbox-net`, and the gateway is the only thing that knows which of them is
    calling."""
    client: str | None = None


class AckRequest(BaseModel):
    # How many over-cap rejections the operator has read. A count rather than a
    # "dismiss" flag, so a rejection arriving between the render and the click is
    # still unread afterwards. Clamped server-side — see ``api_saturation_ack``.
    count: int


# ── lifecycle ───────────────────────────────────────────────────────────────

def _bootstrap() -> None:
    """Prepare the store. Called by ``main`` BEFORE either listener binds, so no
    request — from the proxy or the UI — can observe an unseeded database. This
    used to be a Starlette startup handler, which worked only because there was a
    single app: with two, each has its own lifespan and the authorize listener
    would race the management one's seed."""
    store._init_db()
    # A held request cannot survive a restart (its blocked connection is gone),
    # so any 'pending' rows from a previous process are stale — expire them.
    #
    # ``tool_approvals`` is deliberately NOT swept here, and the difference is the
    # whole point of that table: nothing is blocked on a tool ask, so a pending one
    # is not stale after a restart — it is a question still waiting for a human, with
    # an agent that can still come back for the answer. Its window is enforced by its
    # own ``deadline`` column instead (``holds._expire_tool_asks``). Sweeping it here
    # would throw away exactly the state that answering immediately was chosen to make
    # durable.
    with store._connect() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired', resolved_at=? "
            "WHERE status='pending'", (time.time(),))
        conn.commit()
    seeded = store._seed_if_empty()
    if seeded:
        print(f"control-plane: seeded {seeded} allow rules from {store.SEED_PATH}",
              flush=True)
    # The hold caps, with their UNITS, once per boot. Not decoration: `CONTROL_MAX_
    # PENDING` used to count blocked requests and now counts cards, so an operator who
    # set it under the old meaning has a different limit than they think. A line that
    # says which noun each number counts is what makes that discoverable without
    # reading holds.py, and it costs one line in a log an operator already tails for
    # the live decision feed.
    print(f"control-plane: hold caps — cards {holds.MAX_PENDING} global / "
          f"{holds.MAX_PENDING_PER_CLIENT} per client, blocked requests "
          f"{holds.MAX_WAITERS} global / {holds.MAX_WAITERS_PER_CLIENT} per client "
          f"(0 = refuse all on a global cap, disabled on a per-client one)", flush=True)
    # The tool surface's bounds, on their own line because they count a different
    # thing: cards only, since nothing blocks on an ask, and a window measured against
    # a human's attention rather than against a proxy's patience.
    print(f"control-plane: tool asks — cards {holds.MAX_TOOL_PENDING} global / "
          f"{holds.MAX_TOOL_PENDING_PER_CLIENT} per client, ask window "
          f"{holds.TOOL_HOLD_TIMEOUT:g}s, grant window "
          f"{holds.TOOL_GRANT_TIMEOUT:g}s, payload ceiling {holds.TOOL_ARGS_MAX}B",
          flush=True)
    _warn_on_dead_caps()


def _warn_on_dead_caps() -> None:
    """Name a cap that cannot fire, at the boot that configured it.

    A card holds at least one blocked request, so cards are always <= waiters and a card
    cap set at or above its waiter cap can never be the one to refuse — the waiter cap
    gets there first, and the card number is a limit the operator believes in and does
    not have. ``test_a_card_cap_at_or_above_its_waiter_cap_is_dead`` pins the shipped
    defaults against that, which covers every case except the one the caps exist for:
    being set by hand.

    A WARNING rather than the ``SystemExit`` that ``_assert_listeners_separated`` uses,
    and the asymmetry is the point. A wildcard management bind restores a self-approval
    path, so refusing to start is strictly safer than starting. A dead cap is not a
    containment failure at all — waiters still bound cards, so the hold queue stays
    bounded and only the operator's model of WHICH limit binds is wrong. Refusing to boot
    would answer that by taking the governance authority down, which denies every
    sandbox's egress: a worse outcome than the misconfiguration, and caused by us.

    Zero is exempt at both scopes because zero is meaningful at both — a global zero
    refuses everything, a per-client zero disables that cap (see ``holds.py``). Warning
    on either would be warning that a documented setting works.
    """
    for cards, waiters, scope in (
            (holds.MAX_PENDING, holds.MAX_WAITERS, "global"),
            (holds.MAX_PENDING_PER_CLIENT, holds.MAX_WAITERS_PER_CLIENT, "per-client")):
        if cards > 0 and waiters > 0 and cards >= waiters:
            print(f"control-plane: WARNING — the {scope} card cap ({cards}) is at or "
                  f"above the {scope} blocked-request cap ({waiters}), so it can never "
                  f"refuse: every card holds at least one blocked request, so the "
                  f"request cap always fires first. Lower the card cap to make it bind.",
                  flush=True)


def _forbidden_nets(var: str, value: str,
                    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """The networks a listener must not be served on, parsed from one of the two
    ``*_BIND_FORBIDDEN`` lists (``var`` names it for the error).

    An unparseable entry is FATAL here, unlike the addon's tolerant CIDR parsing: that
    one drops a bad entry because its list is long and mostly redundant, while these
    have two members each and dropping either silently removes the guard. A typo in a
    hand-set override must not read as "nothing is forbidden"."""
    nets = []
    for raw in (part.strip() for part in value.split(",")):
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:
            raise SystemExit(
                f"control-plane: {var} entry {raw!r} is not a CIDR ({exc}), so the "
                f"bind guard cannot be evaluated. Refusing to start (fail closed).") from exc
    return tuple(nets)


def _forbidden_tool_nets() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _forbidden_nets("CONTROL_TOOL_BIND_FORBIDDEN", _TOOL_BIND_FORBIDDEN)


def _forbidden_manage_nets() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _forbidden_nets("CONTROL_MANAGE_BIND_FORBIDDEN", _MANAGE_BIND_FORBIDDEN)


def _bind_within(bind: str, net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    """Whether ``bind`` names an address inside ``net``.

    A bind that is not an address at all — a hostname, or a wildcard the caller has
    already rejected — is not "inside" anything and answers False. Resolving a name
    here would make the guard depend on DNS, which is the class of check the relay
    guard exists because it cannot trust."""
    try:
        return ipaddress.ip_address(bind) in net
    except ValueError:
        return False


def _assert_listeners_separated() -> None:
    """Fail closed on a configuration that undoes the split.

    The management API is only out of the proxy's reach because it binds ONE
    address, on a network the proxy is not attached to. A wildcard bind serves it
    on every interface — including authorize-net — which silently restores exactly
    the self-approval path the split removes, while every healthcheck and every
    page in the UI keeps working. Nothing downstream can detect that, so it is
    refused here (the same shape as the proxy's ``_assert_guard_configured``).

    Three refusals, because a bind can undo the split three ways: a wildcard, a
    concrete address on another enforcer's network, and a shared port. The first two
    are the same mistake spelled differently and one check does not imply the
    other — and both binds get both, the management one included, because it is
    the listener that grants."""
    if MANAGE_BIND in _WILDCARDS:
        raise SystemExit(
            f"control-plane: CONTROL_MANAGE_BIND={MANAGE_BIND!r} is a wildcard, "
            f"which would serve the management API (including "
            f"/approvals/{{id}}/resolve) on authorize-net, where the egress proxy "
            f"can reach it. Bind the control-net address instead. Refusing to "
            f"start (fail closed).")
    if TOOL_BIND in _WILDCARDS:
        raise SystemExit(
            f"control-plane: CONTROL_TOOL_BIND={TOOL_BIND!r} is a wildcard, which "
            f"would serve the gateway's bridge (including the claim endpoint that "
            f"releases an approved call) on authorize-net, where the egress proxy "
            f"can reach it — a lateral edge between two enforcers. Bind the "
            f"tool-authorize-net address instead. Refusing to start (fail closed).")
    # The other spelling of the same mistake, and the one a wildcard test misses: an
    # address on another enforcer's network. Refused for the reason the wildcard is,
    # because the outcome is the same one.
    for net in _forbidden_manage_nets():
        if _bind_within(MANAGE_BIND, net):
            raise SystemExit(
                f"control-plane: CONTROL_MANAGE_BIND={MANAGE_BIND!r} is inside {net}, "
                f"which is an enforcer's network — serving the management API "
                f"(including /approvals/{{id}}/resolve) there puts self-approval "
                f"within that enforcer's reach, and nothing downstream can detect "
                f"it. Bind the control-net address instead. Refusing to start "
                f"(fail closed).")
    for net in _forbidden_tool_nets():
        if _bind_within(TOOL_BIND, net):
            raise SystemExit(
                f"control-plane: CONTROL_TOOL_BIND={TOOL_BIND!r} is inside {net}, "
                f"which is another enforcer's network — serving the claim endpoint "
                f"there is the lateral edge the separate bridge exists to remove, and "
                f"nothing downstream can detect it. Bind the tool-authorize-net "
                f"address instead. Refusing to start (fail closed).")
    # Every listener gets its OWN PORT, checked across all three pairs and without
    # regard to the addresses. Same address and same port is one socket serving two
    # apps' worth of surface, which is the obvious case; same port on different
    # addresses is the subtle one and is refused too, because with a wildcard in the
    # mix — and the authorize listener is one — which app answers depends on which
    # bind is more specific for the address dialled. That is not a property an
    # operator reading a healthcheck or a firewall rule can see, and nothing here
    # needs it.
    for (a_name, a_bind, a_port), (b_name, b_bind, b_port) in (
            (("management", MANAGE_BIND, MANAGE_PORT),
             ("authorize", AUTHORIZE_BIND, AUTHORIZE_PORT)),
            (("management", MANAGE_BIND, MANAGE_PORT),
             ("tool", TOOL_BIND, TOOL_PORT)),
            (("authorize", AUTHORIZE_BIND, AUTHORIZE_PORT),
             ("tool", TOOL_BIND, TOOL_PORT))):
        if a_port == b_port:
            raise SystemExit(
                f"control-plane: the {a_name} listener ({a_bind}:{a_port}) and the "
                f"{b_name} listener ({b_bind}:{b_port}) share a port, so which "
                f"surface answers depends on which bind is more specific. Give each "
                f"listener its own port. Refusing to start.")


@app.get("/healthz")
@authorize_app.get("/healthz")
@tool_app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# ── authorize (proxy-facing) ────────────────────────────────────────────────

def _decision_scope(status_row) -> str:
    """How far a human's decision reached, for the audit reason a released waiter
    writes. Read from the durable ``mode`` column, so it reports what was RECORDED
    rather than what was asked for.

    A function rather than the inline conditional it replaced, because there are three
    modes now and the middle one is the reason: a lease is neither "this request only"
    nor standing policy, and a log that collapsed it into either would misreport the one
    thing an operator comes to this line to find out. `None` — no resolver row at all —
    is the expiry path, which reached nothing.

    Naming the lease's own deadline here would mean a second read of the ``leases``
    table per released request; the configured duration is the same for every lease and
    the row itself carries the exact instant (``/api/egress/leases``), so this states
    the duration and lets that be the record."""
    mode = status_row["mode"] if status_row else None
    if mode == "persist":
        # Names the PATTERN, because "allow api.example.co.uk" and "allow .co.uk"
        # are the same click and very different policy. Read from the durable
        # column; a row that predates it says only that a rule was written.
        pattern = status_row["pattern"]
        return f"standing rule written: {pattern}" if pattern else "standing rule written"
    if mode == "lease":
        return f"lease written, {policy._short_duration(policy.LEASE_SECONDS)}"
    return "this request only"


@authorize_app.post("/authorize", response_model=AuthorizeResponse)
def authorize(req: AuthorizeRequest) -> AuthorizeResponse:
    # Derived HERE, once, and carried through every write this request makes — the
    # audit rows, the approvals row, and via that row the rule a persist writes. One
    # derivation rather than several means the value that DECIDED the request is the
    # same one that gets recorded, by construction instead of by two lookups agreeing.
    client_class = policy._client_class(req.client)
    decision, reason = policy._decide(req.host, client_class)

    if decision in ("allow", "deny"):
        # Every decision is audited — no governed path bypasses the log (CLAUDE.md).
        store._audit(decision, stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, client_class=client_class,
                     method=req.method, url=req.url, reason=reason)
        return AuthorizeResponse(decision=decision, reason=reason)

    # HOLD (bounded): reserve a hold slot atomically with the cap check, so
    # concurrent holds can't race past the cap. Over the global or per-client cap,
    # fail CLOSED immediately rather than registering another worker-blocking hold.
    # A request identical to one already held JOINS it instead of raising a second
    # card (holds._group_key) — a retrying agent used to fill its whole card budget
    # with copies of one question.
    slot = holds._reserve_hold(uuid.uuid4().hex, threading.Event(), req.client,
                               req.host, req.port, req.proto)
    if slot.refused is not None:
        store._audit("deny", stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, client_class=client_class,
                     method=req.method, url=req.url, reason=slot.refused)
        return AuthorizeResponse(decision="deny", reason=slot.refused)
    approval_id, event = slot.approval_id, slot.event

    # From here the slot is RESERVED, so every exit has to give it back — which is what
    # the `finally` is for, and it is not defensive habit. A reservation that leaks is
    # not merely a lost slot: `_GROUPS` still names this approval id, so every later
    # request with the same (client, host, port, proto) JOINS a card that has no
    # approvals row and no waiter coming for it. Those requests block out the original
    # window, default-deny with a reason that reads as operator inaction, and never
    # raise a card anyone can approve — so one failed write makes that destination
    # permanently un-decidable, and enough of them exhaust MAX_WAITERS and fail every
    # sandbox's holds closed until a restart. The store write below is the reachable
    # trigger (a full disk, a lock held past the busy timeout).
    #
    # Nothing is audited on that path and nothing needs to be: the exception becomes a
    # 500, the proxy's `_authorize` fails closed on it, and the proxy writes the denial
    # to its own stream, which the ingest picks up. The decision is recorded by the
    # component that made it.
    try:
        if not slot.joined:
            now = time.time()
            with store._connect() as conn:
                conn.execute(
                    "INSERT INTO approvals(id, ts, host, port, proto, client, "
                    "client_class, method, url, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?, 'pending')",
                    (approval_id, now, req.host, req.port, req.proto, req.client,
                     client_class, req.method, req.url))
                conn.commit()
        # Audited PER REQUEST either way, with this request's own method and url,
        # because grouping is a concept of the screen and the worker pool — never of
        # the record. The joiner's reason names the card it attached to, so the log
        # explains on its own terms why four requests produced one approval and one
        # decision.
        store._audit("hold", stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, client_class=client_class,
                     method=req.method, url=req.url,
                     reason=(f"joined hold {approval_id} — duplicate of a request "
                             "already awaiting approval"
                             if slot.joined else "held for approval"))

        # Block until a human resolves this hold or the window elapses. The wakeup is
        # advisory: the DURABLE approvals row is the single source of truth for the
        # outcome. Exactly one of this timeout path and resolve() flips the row out of
        # 'pending' — each via an atomic conditional UPDATE (…WHERE status='pending')
        # that SQLite serializes — so a resolve landing just as the hold times out can
        # no longer leave the row 'allowed' (and persist a rule) while the agent is
        # told 'deny'. Whoever's UPDATE wins decides; the loser reads the winner's row.
        # The card's remaining window, not a fresh one — see holds._PENDING_DEADLINE.
        # Every waiter on a card therefore wakes at the same instant, which is what
        # lets them race harmlessly for the expiry UPDATE below.
        event.wait(max(0.0, slot.deadline - time.time()))
        with store._connect() as conn:
            expired = conn.execute(
                "UPDATE approvals SET status='expired', resolved_at=? "
                "WHERE id=? AND status='pending'", (time.time(), approval_id)).rowcount
            status_row = None if expired else conn.execute(
                "SELECT status, mode, resolved_by, pattern FROM approvals WHERE id=?",
                (approval_id,)).fetchone()
            conn.commit()
        if expired:
            # Only the waiter that WON the expiry closes the group, and it does so
            # before releasing its slot: the card is now decided, so nothing may still
            # join it.
            holds._close_group(approval_id)
    finally:
        holds._release_hold(approval_id)

    # Carry the resolver's provenance (recorded by resolve()) into the audit reason,
    # so the log answers "who granted this egress" and not merely "a human did".
    actor = (status_row["resolved_by"] if status_row else None) or "actor unrecorded"
    # How far the decision REACHED belongs in the audit line: three modes that are the
    # same allow for this request and very different afterwards, and the log said
    # nothing about which had happened. See ``_decision_scope``.
    scope = _decision_scope(status_row)
    # Read the STATUS rather than "did I win the expiry UPDATE": with duplicates
    # grouped, several waiters wake together and only one of them wins it. The losers
    # read status='expired' and must report the timeout too — testing `expired` alone
    # would have told every one of them a human had rejected their request.
    status = "expired" if expired or status_row is None else status_row["status"]
    if status == "expired":
        final, why = "deny", "no decision within hold timeout — default-deny"
    elif status == "allowed":
        final, why = "allow", f"human approval ({scope}) [{actor}]"
    else:  # 'denied' (or any non-allowed terminal state) — default-deny
        final, why = "deny", f"human rejection ({scope}) [{actor}]"
    store._audit(final, stage=req.stage, host=req.host, port=req.port,
                 proto=req.proto, client=req.client, client_class=client_class,
                 method=req.method, url=req.url, reason=why)
    return AuthorizeResponse(decision=final, reason=why)


# ── the tool bridge (gateway-facing) ────────────────────────────────────────
#
# The MCP gateway's entire conversation with the control plane, and it is three
# questions rather than one: what may this call do, what is configured, and may I now
# run the ask a human approved. All three are served on ``tool_app`` — its own socket
# on its own network — and none of them grants anything (see the app's comment).
#
# It is the tool surface's counterpart to ``/authorize`` and deliberately not that
# endpoint. The shapes diverge where the surfaces do: `/authorize` answers allow or
# deny and resolves a hold INTERNALLY by blocking a worker, while this one answers
# allow, deny or `ask` and hands the id back at once, because nothing waits (DESIGN.md,
# "An ``ask`` answers immediately"). Sharing the endpoint would mean one handler whose
# every branch forked on which surface asked.
#
# What the gateway is TRUSTED with here, stated because it is the trust model rather
# than an oversight: it reports the sandbox address it observed, exactly as the egress
# proxy does on `/authorize`. A gateway that lied would misattribute an ask's client
# and spend another sandbox's per-client budget. It cannot forge a DECISION, which is
# the part that matters — policy is read here, and the human's answer lands on a row
# the gateway never writes.

@tool_app.post("/tool/authorize")
def tool_authorize(req: ToolCallRequest) -> dict:
    """Decide one tool call: allow, deny, or ask (registered, with an id to return
    with).

    Always a 200 and always an answer, like ``authorize``: a policy question has a
    policy answer, and a gateway should never have to read an HTTP status to learn
    what governance said. That includes the refusals — a malformed name, a server
    nobody registered, a disabled one, an oversized payload and a saturated queue all
    come back as `deny` with a reason, because every one of them is a decision the
    gateway has to act on identically.

    NOTHING IS CACHEABLE IN THIS ANSWER, and the gateway must not try: an operator's
    `deny` that waits for a TTL is not a deny (DESIGN.md, "The gateway pulls"). The
    roster below is the half that may be polled; this is the half that may not.

    What is NOT recorded here is the payload of an allowed call. The audit row carries
    the server, the tool and the decision; the arguments reach the store only when a
    human has to read them (an ``ask``, where they are the thing being approved). The
    other half of that record — what a call sent and what came back, at minimum size
    and hash, because the response is the channel that steers an agent — belongs to
    the gateway's own audit stream, ingested the way the proxy's is. Naming the gap is
    the point: it is not covered yet, and this endpoint is not where it lands."""
    # Derived here and carried into the audit row, the same discipline ``authorize``
    # follows: the class that named this caller is the one the record shows. It scopes
    # nothing — a tool rule is keyed on (server, tool), never on a client class, which
    # is exactly the conflation "Per-server identity has two different answers" keeps
    # apart. It is here because the record has to say WHICH population called, and a
    # tool ask's own row cannot: `tool_approvals` has no class column, so `resolve`
    # would have to re-derive it from an address recorded earlier — the reverse of the
    # rule that a decision is recorded under the class it was decided with.
    client_class = policy._client_class(req.client)
    server = (getattr(req, "server", "") or "").strip().lower()
    tool = (getattr(req, "tool", "") or "").strip()
    decision, reason = policy._decide_tool(server, tool)

    if decision in ("allow", "deny"):
        store._audit(decision, stage="tool-call", client=req.client,
                     client_class=client_class, server=server or None, tool=tool or None,
                     reason=f"{tool or '(no tool)'} on {server or '(no server)'}: "
                            f"{reason}")
        return {"decision": decision, "reason": reason}

    # ASK. Registered, not held: the row is the ask, the gateway takes the id back
    # immediately and the agent gets a pending result it can come back with. An
    # identical ask from the same client JOINS the pending one instead of raising a
    # second card, which is what keeps a retrying agent from filling the operator's
    # queue with copies of one question.
    ask = holds._register_tool_ask(server, tool, req.args, req.client)
    if ask.refused is not None:
        # Over a cap, or a payload too large to show a human in full. A deny, and
        # audited as one — the refusal is real and the gateway acts on it exactly as
        # it would on a policy deny. `holds._refuse_tool` has already recorded the
        # cap case in the saturation account, which is what makes a refusal that
        # raises no card visible to an operator at all.
        store._audit("deny", stage="tool-call", client=req.client,
                     client_class=client_class, server=server or None, tool=tool or None,
                     reason=f"{tool} on {server}: {ask.refused}")
        return {"decision": "deny", "reason": ask.refused}

    # 'hold' rather than a new word, and the vocabulary is the reason: `decision` is
    # shared with the audit views, the filter facet and the page's <option> list
    # (audit.KINDS), which have no compiler between them, and "deferred to a
    # human" is what `hold` already means. The reason line carries the difference that
    # matters — nothing is blocked, and the id is how the answer gets collected.
    why = (f"{tool} on {server}: registered as tool ask {ask.approval_id}"
           + (" (joined an identical ask already pending)" if ask.joined else "")
           + " — nothing is blocked; the agent resumes with the id")
    store._audit("hold", stage="tool-call", client=req.client,
                 client_class=client_class, server=server or None, tool=tool or None,
                 approval_id=ask.approval_id, reason=why)
    return {"decision": "ask", "reason": reason, "approval_id": ask.approval_id,
            # ABSOLUTE, like every other time in a payload here: a remaining-seconds
            # field would tick, which turns the SSE change-detector into a 1 Hz
            # emitter and gives the gateway a number that is stale on arrival.
            "deadline": ask.deadline, "joined": ask.joined}


@tool_app.get("/tool/roster")
def tool_roster() -> list[dict]:
    """The enabled servers, their auth descriptors, and the standing policy for each
    one's tools — everything the gateway needs to know what to dial and what to
    present.

    POLLABLE, unlike the decision above, and that split is deliberate: this answers
    "what is configured", which the gateway needs at session start and on change, while
    execution policy is per-call and must never be cached. The gateway re-reads it
    on an interval rather than being pushed to (protocol.py says why `list_changed`
    is deliberately not advertised).

    A DISABLED server is simply absent, which is the whole meaning of the switch — the
    gateway dials what the roster names. It is not reported as disabled, because the
    gateway has nothing different to do with that fact; the operator's view of it is
    ``/api/mcp/servers``, on the management listener where configuration belongs.

    Every rule ships, `deny` rows included. Presentation is the gateway's filter
    (withholding a schema keeps an agent from planning around a capability it cannot
    have), but the two axes are separate: a `deny` is enforced at EXECUTION by the
    decision endpoint above regardless of whether the tool was ever presented, because
    a tool name can arrive from anywhere — a transcript, a `CLAUDE.md`, text injected
    by an earlier tool result.

    The auth descriptor is here and the secret is not. The descriptor is enough to
    build a request and useless to steal; the material lives in a file the gateway
    reads at a path DERIVED from the server name, so nothing in this response — or in
    the store behind it — can point one server at another's credential."""
    with store._connect() as conn:
        servers = conn.execute(
            "SELECT server, enabled, auth_type, auth_header, auth_template, "
            "created_at FROM mcp_servers WHERE enabled=1 ORDER BY server").fetchall()
        rules: dict[str, list[dict]] = {}
        for row in conn.execute(
                "SELECT server, tool, action FROM tool_rules ORDER BY server, tool"):
            rules.setdefault(row["server"], []).append(
                {"tool": row["tool"], "action": row["action"]})
    # ``_server_view`` minus ``enabled``: every server here is enabled by definition,
    # and a constant field invites a reader to believe it varies.
    return [{"server": r["server"],
             "auth": {"type": r["auth_type"], "header": r["auth_header"],
                      "template": r["auth_template"]},
             "tools": rules.get(r["server"], [])}
            for r in servers]


@tool_app.post("/tool/inventory")
def tool_inventory(req: InventoryRequest, request: Request) -> JSONResponse:
    """What the gateway found when it dialled each enabled server.

    THE ONLY WRITE ON THIS BRIDGE, and it stays inside the criterion that keeps the
    bridge's width honest: none of these endpoints grants. Nothing here writes a rule,
    registers a server or decides an approval — it records a claim, in memory, that
    ``policy._decide_tool`` never reads. A tool listed here is denied exactly as it was
    before the push, until a human writes a rule naming it.

    PUSHED rather than pulled, and that direction is forced. This process has no leg on
    the gateway's networks and must not be given one: dialling the agent-facing service
    from the crown jewel is the lateral edge the gateway's bind guard exists to
    prevent. So the component that can reach both ends does the reaching.

    Audits CHANGES only. A push arrives whenever the roster moves, which during an
    operator's session is often; a row per push would bury the rows worth keeping. What
    IS worth keeping is a server's surface moving — an image bump that starts exposing
    a destructive tool, with no human in the loop, is a supply-chain event and reads as
    one in the log."""
    try:
        _, moved = inventory.record({"servers": req.servers or {}})
    except inventory.InventoryError as exc:
        # A refusal the gateway can print. Not a 500: this is a well-formed request
        # carrying something out of bounds, and the sender needs to say so in its log
        # rather than retry it every poll.
        #
        # The TYPE is the point, not the 400. `InventoryError` carries a contract that
        # its message holds only the caller's own payload shape and this module's
        # constants, so serving it verbatim discloses nothing; anything else raised in
        # there is unexpected and becomes a 500 with no body, rather than having its
        # text relayed. Same arrangement as `audit.FilterError` and `_bad_filter`.
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    for server, line in moved:
        store._audit("observe", stage="mcp-tools", client=_actor(request),
                     server=server, reason=line)
    return JSONResponse({"ok": True, "changed": len(moved)})


@tool_app.post("/tool/asks/{approval_id}/claim")
def tool_claim(approval_id: str, req: ToolResumeRequest) -> JSONResponse:
    """Take single-use ownership of an approved ask, and hand back what to run.

    This is the resumption path: the agent returns with the id from its pending
    result, and the gateway executes only after this call succeeds. Executing HERE
    rather than at the human's click is what makes the stranded-caller property
    structural — an approved call nobody comes back for simply never runs, so a side
    effect cannot happen with nobody left to receive it.

    ONE ID, and no listing counterpart. A roster of pending asks would leak approvals
    the caller never raised — the gateway's agent-facing listener is on a network both
    sandbox tiers share — and past the leak it hands the agent a read on the
    operator's queue (DESIGN.md, "One id at a time").

    The response carries the ARGUMENTS, in the canonical form the human read and the
    digest covers. That is the point of returning them rather than having the gateway
    replay its own copy: the arguments that execute are necessarily the approved ones,
    with no second chance for a reformulated payload to ride an old grant.

    An id belonging to a DIFFERENT client is answered as unknown, not as forbidden.
    Both tiers share `sandbox-net`, so an id that leaked between them must not confirm
    it exists — and a 404 makes a guessed id and a real one indistinguishable.

    POLICY IS RE-READ HERE, and the approval alone is not enough to release the call.
    An ask is decided at one moment and redeemed at another, and everything the
    decision rested on can change in between: the server can be disabled — the
    one-click way to stop it without touching its rules (``revoke_mcp_server``) — or
    the rule can be revoked or flipped to `deny`. None of those touch `tool_approvals`,
    so without this check the switch an operator reaches for does not reach the one
    surface that releases a side effect. Same rule as ``_decide_tool``'s own: a gateway
    that asks anyway must get a refusal from the authority rather than a grant it is
    trusted not to act on."""
    ask = holds._get_tool_ask(approval_id)
    if ask is None or (ask["client"] or None) != (req.client or None):
        # ``terminal`` is set here too, so the gateway branches on one field for
        # every refusal below: an id that is unknown to this caller does not become
        # known by asking again, whatever the reason it is unknown.
        return JSONResponse(
            {"ok": False, "detail": "unknown approval", "status": None,
             "spent": False, "terminal": True},
            status_code=404)
    # Derived once and used by every row below, for the reason ``tool_authorize``
    # states: the record has to say which population called. ``ask["client"]`` rather
    # than ``req.client`` only to read as what it is — the check above holds them
    # equal, and the stored one is the value the card was raised under.
    client_class = policy._client_class(ask["client"])

    # Policy BEFORE the claim, so a refusal does not consume the grant: the server may
    # be switched back on, and the approval is then still there to be redeemed. The
    # narrow window this leaves — a disable landing between this read and the UPDATE
    # below — is the same in-flight race a disable always has against a call already
    # on the wire, and closing it here would only move it.
    decision, why = policy._decide_tool(ask["server"], ask["tool"])
    if decision == "deny":
        store._audit("deny", stage="tool-resume", client=ask["client"],
                     client_class=client_class, server=ask["server"], tool=ask["tool"],
                     approval_id=approval_id,
                     reason=f"approved tool ask {approval_id} not released: {why} — "
                            f"the approval stands, the server's state does not")
        # TERMINAL, though the underlying state is reversible. An agent should not sit
        # in a retry loop on a policy refusal, and if the operator does switch the
        # server back on the right move is a fresh ``/tool/authorize`` — a new decision
        # made in the new circumstances — rather than a grant resurrected under them.
        return JSONResponse(
            {"ok": False, "detail": f"not releasable ({why})",
             "status": ask["status"], "spent": False, "terminal": True},
            status_code=409)

    # The claim itself is the atomic conditional UPDATE (`status='allowed' AND
    # claimed_at IS NULL`), so exactly one resumption of one approval ever performs
    # the side effect. Attempted BEFORE reporting a status, for the reason
    # ``_resolve_tool_ask`` orders itself the same way: reading first and then writing
    # would let two concurrent resumptions both read 'allowed' and both proceed.
    claimed = holds._claim_tool_ask(approval_id)
    if claimed is None:
        current = holds._get_tool_ask(approval_id) or ask
        # Four different refusals, and the gateway needs them apart: `pending` means
        # come back later, `denied` and `expired` are terminal and must be
        # unmistakably so — an agent that cannot tell a refusal from a delay retries
        # one forever — and an already-claimed approval is spent rather than refused,
        # which is the answer to a duplicate resumption of a call that already ran.
        spent = current["status"] == "allowed" and current["claimed_at"] is not None
        return JSONResponse(
            {"ok": False,
             "detail": ("this approval has already been claimed and its call has run"
                        if spent else
                        f"not claimable ({current['status']})"),
             "status": current["status"], "spent": spent,
             "terminal": spent or current["status"] in ("denied", "expired")},
            status_code=409)

    # Audited as an ALLOW at the moment capability is actually released, which is here
    # and not at the click: the resolve row records what a human decided, this one
    # records that it is being acted on. Two rows for one grant, deliberately — the
    # gap between them is exactly the window in which an approved call was never run,
    # and a record with only the first could not show it.
    store._audit("allow", stage="tool-resume", client=claimed["client"],
                 client_class=client_class, server=claimed["server"],
                 tool=claimed["tool"], approval_id=approval_id,
                 reason=f"approved tool ask {approval_id} claimed for execution; "
                        f"{claimed['tool']} on {claimed['server']} — single-use, this "
                        f"claim is the only one that can run it")
    return JSONResponse({"ok": True, "status": claimed["status"],
                         "server": claimed["server"], "tool": claimed["tool"],
                         # The canonical string, not re-parsed JSON: it is what the
                         # digest covers and what was shown, so anything that
                         # re-serializes it on the way out could hand the gateway a
                         # payload that differs from the approved one.
                         "args_json": claimed["args_json"],
                         "claimed_at": claimed["claimed_at"]})


# ── approvals (human-facing) ────────────────────────────────────────────────

@app.get("/approvals")
def approvals() -> dict:
    return holds._pending_payload()


#: What each surface's cards may be resolved WITH. Per-surface rather than a union,
#: because the two vocabularies mean different things and an action from the wrong one
#: is a caller that has misread which card it is looking at. The tool set has no
#: `*_persist` member at all: persisting an egress decision writes a host pattern from
#: a bounded candidate set, and the argument-shaped analogue for a payload — this call,
#: then this tool with these arguments, then this tool always — is not built. Offering
#: `allow_persist` here would have to mean "allow this tool forever", which is the one
#: rung of that ladder nobody should reach by clicking the same button twice.
#:
#: The egress set is a DURATION LADDER — this request, this host for a while, this
#: pattern forever — and two absences on it are decisions:
#:
#:   - There is no `deny_lease`. An unmatched host is HELD, not denied, so a timed deny
#:     would mean "suppress the card for a while", which is a different feature
#:     (silencing a looping agent) wearing this one's name.
#:   - There is no breadth choice on `allow_lease`. The `_persist_candidates` ladder
#:     exists because a permanent rule needs an operator decision about how wide it is;
#:     a lease answers that by expiring instead, and buying breadth would double the
#:     card's decision surface for it. So a lease is always the exact host.
EGRESS_ACTIONS = ("allow_once", "allow_lease", "allow_persist",
                  "deny_once", "deny_persist")
TOOL_ACTIONS = ("allow", "deny")


@app.post("/approvals/{approval_id}/resolve")
def resolve(approval_id: str, req: ResolveRequest, request: Request) -> JSONResponse:
    """One endpoint, two surfaces, dispatched on which table holds the id.

    The queue is deliberately merged (``holds._pending_payload``), so the operator
    clicks cards of both kinds from one list and the id is all the client sends back.
    That is enough: an approval id belongs to exactly one table, so the card's kind is
    already determined by the time this is called and nothing has to be trusted from
    the request body to find it.

    The egress path below is untouched by the split. Everything tool-shaped lives in
    ``_resolve_tool_ask_request`` rather than as branches threaded through it, because
    this is the endpoint that turns a held request into allowed egress — the one whose
    reasoning is worth being able to read straight through."""
    if holds._get_tool_ask(approval_id) is not None:
        return _resolve_tool_ask_request(approval_id, req, request)
    # Normalised the way the tool path normalises its action, so a client that sends
    # `Allow_Once ` is refused for being unknown rather than for its spelling.
    action = (req.action or "").strip().lower()
    if action not in EGRESS_ACTIONS:
        return JSONResponse({"ok": False, "detail": "bad action"}, status_code=400)
    outcome = "allow" if action.startswith("allow") else "deny"
    persist = action.endswith("persist")
    lease = action.endswith("lease")
    # Captured BEFORE the update so the same value lands on the durable row and, via
    # that row, in the audit reason the blocked authorize() waiter writes.
    actor = _actor(request)

    with holds._LOCK:
        event = holds._PENDING_EVENTS.get(approval_id)
    if event is None:
        # Already resolved, expired, or unknown — nothing to wake.
        return JSONResponse(
            {"ok": False, "detail": "not pending (expired or already resolved)"},
            status_code=409)

    with store._connect() as conn:
        row = conn.execute(
            "SELECT host, client_class FROM approvals WHERE id=?",
            (approval_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown"}, status_code=404)
        # The class the request was DECIDED under, read from the durable row — the
        # same discipline ``host`` follows just below, and for the same reason. The
        # rule this writes must be scoped to the population the card was raised for,
        # and the durable row is the only thing that knows that; re-deriving it from
        # the address here would be a second implementation of the classification for
        # the one caller whose answer becomes standing policy.
        client_class = row["client_class"]
        # Settle WHAT a persist writes before anything is written, and settle it from
        # the host on the durable row rather than from the request body — the caller
        # chooses among candidates, it does not supply them (see
        # policy._persist_candidates).
        pattern = None
        # Any grant that OUTLIVES this request — a standing rule or a timed lease —
        # has to be scoped to a client class, and "whoever we could not identify" is
        # not one: it would grant to every future unidentified client, which is
        # precisely the union-of-needs erosion the class dimension exists to stop.
        #
        # The lease is refused for that reason too, even though it expires. Expiry
        # bounds HOW LONG a grant lasts; it does nothing about WHO it covers, and a
        # lease with no class to scope to covers a population rather than a client.
        #
        # Refused before the UPDATE like the two branches below, so the approval stays
        # pending — the operator can still decide this request with `allow_once`, and
        # is never stuck.
        if (persist or lease) and (
                not client_class or client_class == policy.UNCLASSIFIED):
            return JSONResponse(
                {"ok": False,
                 "detail": f"this request came from an unclassified client "
                           f"({row['client_class'] or 'none recorded'}), so no "
                           f"{'standing rule' if persist else 'lease'} can be scoped "
                           f"to it. Decide it with a *_once action, or map its network "
                           f"in CONTROL_CLIENT_CLASSES."}, status_code=400)
        if persist:
            allowed = policy._persist_candidates(row["host"])
            if not allowed:
                return JSONResponse(
                    {"ok": False,
                     "detail": f"no rule pattern can be derived from host "
                               f"{row['host']!r}"}, status_code=400)
            pattern = (req.pattern or "").strip().lower() or allowed[0]
            if pattern not in allowed:
                # Refused BEFORE the UPDATE, so a rejected pattern neither resolves the
                # hold nor consumes it: the approval stays pending and the operator can
                # choose again. (A `*_persist` that half-applied — decision recorded,
                # rule not — would be the worst of both.)
                return JSONResponse(
                    {"ok": False,
                     "detail": f"pattern {pattern!r} is not one this approval may "
                               f"persist (allowed: {', '.join(allowed)})"},
                    status_code=400)
            # A rule for this pattern may ALREADY EXIST with the opposite action, and
            # the insert below is INSERT OR IGNORE against UNIQUE(pattern, client_class) — so it
            # would silently write nothing while this endpoint reported persisted:true
            # and the card confirmed a standing rule. Deny-over-allow is the dangerous
            # direction: the operator believes they have permanently blocked a subtree,
            # and every later request to it is allowed without even raising a hold.
            #
            # Refused BEFORE the UPDATE for the same reason as the branch above — the
            # approval stays pending and decidable, rather than half-applying with the
            # decision recorded and the rule not.
            #
            # Reachable only through a rule created WHILE this hold was pending: every
            # candidate is derived from the held host and matches it, so a pre-existing
            # rule would have decided the request instead of holding it. Two concurrent
            # holds for sibling hosts, resolved with the same broadened pattern in
            # opposite directions, is the shape — which is what a burst of holds across
            # one domain looks like.
            #
            # Scoped to THIS client class, matching the UNIQUE(pattern, client_class)
            # the insert below collides on. A rule for the same pattern in another
            # class is not a conflict — it is a different rule that decides for a
            # different client population, and refusing on it would make one class's
            # policy unwritable because another's already covered the host.
            existing = conn.execute(
                "SELECT action FROM rules WHERE pattern=? AND client_class=?",
                (pattern, client_class)).fetchone()
            wanted = "allow" if outcome == "allow" else "block"
            if existing is not None and existing["action"] != wanted:
                return JSONResponse(
                    {"ok": False,
                     "detail": f"a standing rule for {pattern!r} already exists for "
                               f"client class {client_class!r} and "
                               f"{existing['action']}s it; this would write "
                               f"{wanted!r} and cannot, because nothing here replaces "
                               f"a rule. Decide this request with a *_once action, or "
                               f"persist a different pattern.",
                     "conflict": {"pattern": pattern, "action": existing["action"],
                                  "client_class": client_class}},
                    status_code=409)
            # Same action already present is NOT a conflict — the policy the operator
            # is asking for is already in force. Proceed, and report below that this
            # call wrote nothing, so the card stops claiming a write it did not make.
        wrote_rule = False
        lease_expires_at = None
        now = time.time()
        updated = conn.execute(
            "UPDATE approvals SET status=?, mode=?, resolved_at=?, resolved_by=?, "
            "pattern=? WHERE id=? AND status='pending'",
            ("allowed" if outcome == "allow" else "denied",
             "persist" if persist else "lease" if lease else "once", now, actor,
             pattern if persist else None, approval_id)).rowcount
        if updated and lease:
            # INSIDE the `updated` guard, which is the whole of what keeps a lease
            # honest. The conditional UPDATE above is what makes exactly one of this
            # call and the waiter's timeout the decider; writing the lease before it —
            # where the persist path does its VALIDATION — would leave a live grant
            # behind a card that expired and default-denied the request that raised it.
            #
            # Expired rows are swept here rather than by a background timer: this is
            # the only place the table grows, so sweeping on it bounds the size without
            # a second mechanism to reason about. It is not what ends a lease —
            # ``policy._live_lease`` filters on the deadline, so a row that survives
            # the sweep still cannot grant.
            conn.execute("DELETE FROM leases WHERE expires_at <= ?", (now,))
            lease_expires_at = now + policy.LEASE_SECONDS
            # The host from the DURABLE row, normalized the one way `_decide` compares
            # hosts — the same discipline the pattern follows, and here it is
            # load-bearing rather than tidy: a lease is matched by equality, so a host
            # stored in any other shape is a grant no request can ever equal.
            conn.execute(
                "INSERT INTO leases(host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES (?,?,?,?,?,?)",
                (policy._normalize_host(row["host"]), client_class, approval_id,
                 now, lease_expires_at, actor))
        if updated and persist:
            # OR IGNORE stays, even though the conflicting case is now refused above:
            # the check and this insert are not one atomic statement, so a rule could
            # still appear between them. What changes is that the outcome is READ from
            # rowcount instead of assumed — the response reports whether a row was
            # actually written, not whether one was asked for.
            wrote_rule = conn.execute(
                "INSERT OR IGNORE INTO rules(pattern, action, source, created_at, "
                "client_class) VALUES (?,?, 'operator', ?, ?)",
                (pattern, "allow" if outcome == "allow" else "block",
                 time.time(), client_class)).rowcount > 0
        conn.commit()

    if not updated:
        return JSONResponse(
            {"ok": False, "detail": "raced — no longer pending"}, status_code=409)

    # We won the conditional UPDATE above, so the durable row already carries the
    # decision the waiters will read. Wake them — but only while the slot is still
    # registered: if the hold window elapsed and it released between our UPDATE and
    # here, skip, so we don't set a dead event. A missed wake is harmless (the
    # waiter already read, or will read, the decision from the durable row).
    #
    # ``event.set()`` releases EVERY waiter on this card, which is the whole of what
    # grouping does to this endpoint: one click, one durable row, one audit line per
    # released request. Closing the group here stops further joins. The decision
    # committed just above (outside _LOCK), so a duplicate can still slip into the
    # narrow gap before this line and inherit this outcome — but it is identical by
    # group key (client/host/port/proto), so it rides the same grant just made, and a
    # deny is fail-safe. (Fully closing the gap would mean holding _LOCK across the DB
    # commit above.)
    with holds._LOCK:
        holds._close_group_locked(approval_id)
        if approval_id in holds._PENDING_EVENTS:
            event.set()
    # ``pattern`` is echoed so the UI reports what was actually STORED rather than what
    # was clicked — the two differ when the request omitted a pattern (defaulting to the
    # exact host) and, more usefully, it is the string an operator would have to go and
    # delete by hand.
    # ``persisted`` is whether THIS call wrote a rule, read from the insert's rowcount
    # rather than from what was asked for. The two differ when the same rule was
    # already in place, and that difference is precisely what used to be reported as a
    # successful write. ``already_present`` carries the other half, so the UI can say
    # "already in place" instead of either claiming a write or going silent about
    # policy the operator just asked for.
    # ``client_class`` is echoed beside ``pattern`` because the two together are the
    # rule: the same pattern persisted from two cards is two different rules, and a
    # confirmation naming only the pattern would read identically for both.
    # ``leased`` and ``lease_expires_at`` are the lease's half of the same honesty:
    # the card reports the DEADLINE it was given rather than adding a configured
    # duration to its own clock, so a page whose `/api/config` is stale — or whose
    # machine's clock is off — cannot show a grant ending at a time it does not.
    return JSONResponse({"ok": True, "outcome": outcome,
                         "persisted": wrote_rule,
                         "already_present": persist and not wrote_rule,
                         "pattern": pattern,
                         "leased": lease_expires_at is not None,
                         "lease_expires_at": lease_expires_at,
                         "client_class": client_class if persist or lease else None})


def _resolve_tool_ask_request(approval_id: str, req: ResolveRequest,
                              request: Request) -> JSONResponse:
    """Answer a tool ask. The tool-shaped half of ``resolve``, and shorter than the
    egress half by everything that exists to release a blocked worker.

    There is no event to set, no group to close and no waiter to wake, because nothing
    is blocked: the agent already has a pending result and an id to come back with, so
    the decision simply lands on the row and waits to be collected. What this does NOT
    do is execute anything — the gateway runs the call when the agent resumes and
    claims the approval, which is what keeps an approved side effect from happening
    with nobody left to receive it.

    It DOES write its own audit row, and that is the asymmetry worth naming: on the
    egress path the released waiter writes the audit line as it returns, so ``resolve``
    itself records nothing. Here there is no waiter, so a decision that wrote no audit
    row would be a human granting capability with nothing in the trail — the one thing
    no governed path may do."""
    action = (getattr(req, "action", "") or "").strip().lower()
    if action not in TOOL_ACTIONS:
        return JSONResponse(
            {"ok": False,
             "detail": f"action must be one of {', '.join(TOOL_ACTIONS)} for a tool "
                       f"ask, not {action!r}",
             "actions": list(TOOL_ACTIONS)}, status_code=400)
    if (getattr(req, "pattern", "") or "").strip():
        # Refused rather than ignored, unlike a `*_once` egress action which shares a
        # vocabulary with the persisting ones. Nothing on this surface persists at all,
        # so a pattern here is a caller that thinks it is writing standing policy —
        # better told than quietly humoured.
        return JSONResponse(
            {"ok": False,
             "detail": "a tool ask persists nothing, so it takes no pattern"},
            status_code=400)

    actor = _actor(request)
    ask = holds._get_tool_ask(approval_id)
    status = holds._resolve_tool_ask(
        approval_id, "allowed" if action == "allow" else "denied", actor)
    if status is None:
        # Lost a race, or the window elapsed between the render and the click. The
        # ask's own status says which, and saying so beats a bare conflict: expired
        # and already-decided call for different things from the operator.
        current = holds._get_tool_ask(approval_id)
        return JSONResponse(
            {"ok": False,
             "detail": f"not pending ({current['status'] if current else 'unknown'})",
             "status": current["status"] if current else None}, status_code=409)

    store._audit("allow" if action == "allow" else "deny", stage="tool-ask",
                 client=ask["client"], server=ask["server"], tool=ask["tool"],
                 approval_id=approval_id,
                 reason=f"tool ask {action}ed by {actor}; {ask['tool']} on "
                        f"{ask['server']} — the call runs only if the agent returns "
                        f"for it" if action == "allow" else
                        f"tool ask denied by {actor}; {ask['tool']} on "
                        f"{ask['server']} will not run")
    return JSONResponse({"ok": True, "kind": "tool", "outcome": action,
                         "status": status, "server": ask["server"],
                         "tool": ask["tool"]})


@app.get("/approvals/stream")
async def approvals_stream(request: Request) -> StreamingResponse:
    """Server-sent events: push the pending-approval payload whenever it changes.
    Polls SQLite once a second (a fast indexed query; the brief sync read is
    negligible on the event loop) and emits on change, plus a periodic heartbeat
    so proxies/clients can detect a dead stream.

    Change-detection is on the SERIALIZED payload, which is why every field in it
    must be stable while nothing happens — see the note in ``holds._saturation``
    about absolute timestamps. A field that ticks turns this into a 1 Hz emitter."""
    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            payload = json.dumps(holds._pending_payload())
            if payload != last:
                last = payload
                yield f"event: pending\ndata: {payload}\n\n"
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ── UI + status ─────────────────────────────────────────────────────────────

def _bad_filter(exc: audit.FilterError) -> JSONResponse:
    """A refused filter, in the shape every other refusal here takes.

    400 and a sentence, rather than ignoring the parameter and serving a list. Both
    directions of a silently-dropped filter mislead: a widened one reports decisions
    the reader excluded, a narrowed one reports an empty record as a quiet system.
    Neither is visible on screen, which is why the answer is an error and not a
    best-effort list."""
    return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)


@app.get("/api/audit")
def api_audit(limit: int = 50, q: str | None = None, kind: str | None = None,
              since: float | None = None, until: float | None = None) -> dict:
    """Recent decisions, newest first, for the UI's decisions table.

    ``client`` is here because this control plane is SHARED ACROSS SANDBOXES. Without
    it a row records that egress to a host was allowed but not whose request it was,
    which is the question an audit trail exists to answer the moment more than one
    agent is running. It is the peer address the proxy observed — there is no sandbox
    name to map it to, and the raw address is what the saturation banner's detail line
    reports too.

    ``client_class`` is the address made meaningful: it is what the decision was
    actually taken against (``policy._decide``), so without it a reader can see that a
    host was allowed for `172.28.0.3` but not that this was the MCP population rather
    than the agent — the difference the rule was written to make. Served from the
    stored column rather than re-derived, so a row keeps the class it was decided
    under even after the CIDR map changes; NULL on rows that predate the column.

    The column list is deliberately narrower than the table. ``port``/``proto``/
    ``method``/``url`` are recorded and queryable but not served here: the URL in
    particular is agent-controlled and unbounded, and this is a glanceable list of
    forty rows rather than the forensic interface. ``make logs-cp`` and the store
    itself remain the complete record.

    **Rows are grouped, and the group key is exactly what the UI renders.** A client
    that retries a permanently-refused host on a timer — a background exporter or
    updater refused by a standing rule, once a minute, forever — otherwise fills this
    list with 1440 identical rows a day and pushes everything else off the bottom
    within the hour. Two properties follow from keying on the DISPLAYED fields:

      - No two rows here can look identical, because rows that would look the same
        ARE the same group. That is the property that makes the list scannable,
        stated directly rather than approximated.
      - ``client`` is in the key. One host refused for two sandboxes is two facts,
        and attribution is the entire reason that column exists.

    Read a grouped ``client`` as an ADDRESS, not as a sandbox. Docker hands ``.2`` to
    whichever container starts first, so a group spanning days covers however many
    sandboxes held that address over the span — a live sighting of ``.2`` and ``.3``
    really is two concurrent sandboxes, but "400x from 172.30.0.2 since last week" is
    an address's history, not an agent's. Grouping is what introduced this: an
    ungrouped row was one instant, where the peer address is unambiguous. ``first_ts``
    is the visible cue that a long span is in play. Fixing it properly needs a stable
    per-sandbox identity, and the only ways to get one are the Docker socket (which
    this proxy must never hold) or a launcher-to-control-plane path that does not
    exist — neither is worth inventing for a label.

    ``port``/``proto`` are deliberately NOT in the key: they are not displayed, so
    keying on them would split one group into rows a reader cannot tell apart.

    Grouping is a property of this VIEW and never of the record — the ``audit`` table
    keeps every row, and ``n``/``first_ts`` are how the view stays honest about what
    it folded. Note the inner slice: it bounds the work by EVENT COUNT rather than by
    time, so the cost is fixed as the table grows (it rides ``audit_ts``), and the
    span covered adapts on its own — about a day when something is retrying every
    minute, months when nothing is. A time bound would go empty on a quiet system,
    which is the one thing a decisions list must not do.

    **Filters narrow the raw rows, before the fold** (``audit.grouped``), so ``scan``
    bounds the matching events read rather than the events read — a search for a quiet
    host therefore reaches back past however many thousand rows a chatty one just
    wrote. ``q`` matches the DISPLAYED columns only, which is the same principle the
    group key follows; the record view searches its own wider set. ``total`` follows
    the filter for the reason the field exists at all: compared against the whole
    table, a complete filtered view would report itself as truncated.

    ``filtered`` is served so the frontend's coverage line can say "matching" rather
    than implying the store itself is that size. It is the one thing the browser cannot
    work out from the response — it has the parameters it sent, but not whether this
    backend understood them as a filter."""
    try:
        filt = audit.parse(q=q, kind=kind, since=since, until=until,
                           search=audit.GROUPED_SEARCH)
    except audit.FilterError as exc:
        return _bad_filter(exc)
    limit = audit.clamp(limit, 50, 500)
    with store._connect() as conn:
        rows = audit.grouped(conn, limit, filt, AUDIT_GROUP_SCAN)
        # What the list is a WINDOW ONTO. Without it the view silently truncates:
        # forty rows look like the whole record, and grouping made that worse rather
        # than better, because the counts on each row appear to explain the volume
        # away. The cost is a COUNT(*) per poll, which is why the frontend stops
        # polling in a hidden tab — that gating is what makes this affordable instead
        # of a scan every four seconds for as long as the page is open.
        total = audit.total(conn, filt)
    return {"rows": [_audit_view(r) for r in rows], "total": total,
            "filtered": filt.active}


@app.get("/api/audit/events")
def api_audit_events(limit: int = audit.EVENTS_LIMIT_DEFAULT, q: str | None = None,
                     kind: str | None = None, since: float | None = None,
                     until: float | None = None, before: str | None = None) -> dict:
    """The record itself: one row per decision, newest first, paged backwards without
    bound. The forensic interface ``api_audit``'s docstring keeps deferring to.

    It exists because "browse the audit log" was, until now, `docker compose exec` and
    SQL against the crown-jewel volume. The glance above answers *what is happening*;
    this answers *what happened* — which request, from which client, with which URL,
    and everything before it. An audit trail nobody can page through is a file, not a
    trail.

    Serves the columns the glance drops (``port``/``proto``/``method``/``url``), and
    that is a deliberate reversal rather than an oversight there or here. The glance
    omits them because a forty-row list scanned at a glance must stay legible and
    ``url`` is agent-controlled and unbounded; this view is read deliberately, one
    request at a time, and without those fields it cannot answer the question it is
    for. The unboundedness is handled where it belongs — capped on WRITE
    (``store.DRAIN_MAX_FIELD``), page-bounded here (``audit.EVENTS_LIMIT_MAX``), and
    escaped in the page under a CSP that gives an injected string nowhere to go.

    Paged by CURSOR, not by offset, and the cursor is ``(ts, id)`` — see
    ``audit.encode_cursor`` for why both halves are needed and why an offset would
    drop rows between pages exactly while something interesting was happening.
    ``next`` is null at the end of the record, which is how the pager knows to stop.

    ``total`` is the size of the MATCHING set and does not move as pages advance: the
    cursor narrows the query but never the total, or paging back through history would
    look like the record shrinking."""
    try:
        filt = audit.parse(q=q, kind=kind, since=since, until=until,
                           search=audit.EVENT_SEARCH)
        limit = audit.clamp(limit, audit.EVENTS_LIMIT_DEFAULT, audit.EVENTS_LIMIT_MAX)
        with store._connect() as conn:
            rows, nxt = audit.events(conn, limit, filt, before)
            total = audit.total(conn, filt)
    except audit.FilterError as exc:
        # Covers the cursor as well as the filters — a malformed `before` is refused
        # rather than treated as "start from the beginning", which would silently
        # serve page 1 while the operator believed they were reading page 12.
        return _bad_filter(exc)
    return {"rows": [_audit_view(r) for r in rows], "total": total,
            "filtered": filt.active, "next": nxt}


def _audit_view(row) -> dict:
    """One audit row as the UI receives it — grouped or raw, this shaping is the same
    for both — plus the one thing it cannot work out for itself: whether this denial
    was policy or an outage.

    Classified HERE rather than in the frontend so the marker string lives next to
    the guard test that pins it, and so the browser is not matching on prose. The
    field is additive — a client that ignores it renders exactly what it rendered
    before."""
    out = dict(row)
    reason = out.get("reason") or ""
    out["fail_closed"] = reason.startswith(FAIL_CLOSED_REASON)
    return out


@app.get("/api/egress/rules")
def api_rules() -> list[dict]:
    """Read-only view of the policy store — the rules that decide every request.

    Exists because standing policy was INVISIBLE from the interface built to govern
    it: the UI showed pending approvals and recent decisions, but never the rules, so
    answering "what have I permanently allowed?" meant `docker compose exec` and SQL
    against the volume. Policy that accumulates unseen drifts, and a `*_persist`
    approval writes to it with no way to review the result.

    Deliberately UNPAGINATED: this is the complete policy, and a silently truncated
    view of it would be worse than none — the whole point is that nothing standing is
    hidden. Rules are operator/seed-created and bounded in practice, unlike the audit
    table (which is capped for exactly the opposite reason: it grows without bound and
    nobody needs all of it at once).

    Grouped by CLIENT CLASS first, then blocks before allows, because that is the
    order ``policy._decide`` applies: it filters to the asking client's class and only
    then lets a block win over an allow. A flat alphabetical listing would put two
    rules for the same pattern in different classes side by side and imply they
    interact, which is the one thing they do not do.

    Read-only itself: the writes on this path are ``create_rule`` (POST here) and
    ``revoke_rule``, and what neither of them offers is an atomic EDIT — see the
    rule-mutation item in DESIGN.md."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, pattern, action, source, created_at, client_class FROM rules "
            "ORDER BY client_class, action DESC, pattern").fetchall()
    # `id` is served so revocation can key on it. Not cosmetic: patterns are the one
    # field a revoke could plausibly key on instead, and they carry a live
    # normalization gap (``policy._match`` lowercases but does not strip a trailing
    # FQDN dot — see DESIGN.md), so a pattern-keyed delete inherits every such mismatch
    # and can miss the row the operator is looking at. An id cannot.
    return [dict(r, scope=policy._pattern_scope(r["pattern"])) for r in rows]


@app.get("/api/config")
def api_config() -> dict:
    """The settings the UI cannot behave correctly without knowing. Read-only, and
    non-secret by construction — nothing here decides anything.

    The hold window is load-bearing: a held request BLOCKS the agent and default-denies
    after ``holds.HOLD_TIMEOUT``, so a card that cannot say how long is left cannot
    distinguish hold-for-approval from a slow deny. Sent rather than hardcoded in the
    page, so the number the operator sets is the number they see.

    The client classes are here for the same reason and a sharper one: ``create_rule``
    refuses a class it does not know, so a page that guesses the list offers rules that
    cannot be written. Deriving it from the RULES instead would be worse than guessing —
    a class with no rules yet would be missing from the form, which is exactly the case
    where an operator most needs to write the first one (a fresh MCP server, say).

    The lease duration is here so the button that grants one can LABEL ITSELF from the
    server. A page that spelled "30 min" into its own markup would keep saying it on a
    store configured for five, which is the same class of lie as a countdown that
    invents a window — and it is why the action is named ``allow_lease`` rather than
    after any number."""
    return {"hold_timeout": holds.HOLD_TIMEOUT,
            "lease_seconds": policy.LEASE_SECONDS,
            "client_classes": list(policy._class_names())}


@app.post("/api/saturation/ack")
def api_saturation_ack(req: AckRequest) -> dict:
    """Acknowledge over-cap rejections the operator has read.

    Server-side because a dismissal held in the page is not a dismissal: reloading
    restored the banner, which is worse than not offering the button — the operator
    believes they have cleared something and the state disagrees.

    A HIGH-WATER MARK, not a reset. Acknowledging "the 2 I read" leaves a third that
    arrived while the click was in flight still unread; zeroing the counter would
    swallow it, and rejections arrive in bursts, which is exactly when that window is
    open. Monotonic for the same reason — a lower count never un-acknowledges.

    Clamped to what has actually happened. An acknowledgement above the current total
    would suppress FUTURE rejections until they caught up, which is a governance signal
    silenced by an unvalidated client number — the same reasoning that validates
    ``pattern`` in ``resolve``, and the same answer.

    Deliberately NOT audited. The rejections themselves are already in the audit table
    with their reasons; this changes what the banner displays and touches no evidence,
    so a row here would put a non-decision in the decisions log for no gain."""
    with holds._LOCK:
        total = int(holds._SATURATION["count"])  # type: ignore[arg-type]
        acked = max(0, min(int(req.count), total))
        if acked > int(holds._SATURATION["acked"]):  # type: ignore[arg-type]
            holds._SATURATION["acked"] = acked
            holds._SATURATION["acked_ts"] = time.time()
        return {"ok": True, "acknowledged": holds._SATURATION["acked"],
                "rejections": total}


@app.post("/api/egress/rules")
def create_rule(req: RuleCreateRequest, request: Request) -> JSONResponse:
    """Write a standing rule directly, without a held request to hang it on.

    The missing verb. Until this existed, every rule in the store arrived one of two
    ways: seeded from ``policies/egress-allowlist.txt`` at first boot, or persisted as
    a side effect of resolving a hold. Both are REACTIVE — policy could only be stated
    about a host the agent had already reached for, which meant pre-authorizing a known
    registry required first letting a build block for the whole hold window, and
    writing a BLOCK before anything asked for it was not expressible at all (the
    resolve path only persists what a card was raised for).

    Two properties of the resolve path do NOT carry over, and both are why this
    endpoint is the one place in the service that validates a pattern properly:

      - the pattern is not chosen from ``policy._persist_candidates``, because there
        is no held host to derive candidates from. The bounded-choice guarantee that
        makes a persist safe is unavailable here, so its job is done instead by
        ``policy._rule_error`` — which is also why the wildcard floor lives there and
        not in a check written inline here.
      - the client class is not read off a durable approvals row, because there is no
        request whose class was already settled. It is caller-supplied and therefore
        checked against ``policy._class_names``: an unlisted class writes a rule that
        matches nothing, and an inert rule is worse than a refused one — it reads as
        policy in force in the rules view while every request it was meant to decide
        keeps being held.

    Off the authorize listener, like everything else that GRANTS (see the module
    docstring). A rule written here decides egress with no hold and no click, which
    makes this a stronger capability than ``resolve``: that one can only answer a
    question something already asked.

    Not idempotent-by-overwrite: an existing rule for the same (pattern, class) with
    the OPPOSITE action is a 409, never a silent replace — the same refusal, for the
    same reason, that ``resolve`` makes on its persist path. Replacing one is
    ``edit_rule``'s job, where it is a named operation with a before and an after in
    the record; a create that silently overwrote would be the same act with neither."""
    actor = _actor(request)
    pattern = policy._normalize_pattern(getattr(req, "pattern", "") or "")
    action = (getattr(req, "action", "") or "").strip().lower()
    client_class = (getattr(req, "client_class", "") or "").strip().lower()

    error = policy._rule_error(pattern, action)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)
    classes = policy._class_names()
    if client_class not in classes:
        # A typo here is the quiet failure: `sandox` inserts cleanly, lists cleanly,
        # and decides nothing. Named against the configured set so the refusal carries
        # its own fix.
        return JSONResponse(
            {"ok": False,
             "detail": f"client class {client_class!r} is not configured; rules can "
                       f"be scoped to: {', '.join(classes) or '(none configured)'}",
             "client_classes": list(classes)}, status_code=400)

    with store._connect() as conn:
        existing = conn.execute(
            "SELECT id, action, source FROM rules WHERE pattern=? AND client_class=?",
            (pattern, client_class)).fetchone()
        if existing is not None and existing["action"] != action:
            return JSONResponse(
                {"ok": False,
                 "detail": f"a standing rule for {pattern!r} already exists for client "
                           f"class {client_class!r} and {existing['action']}s it; "
                           f"nothing here replaces a rule. Revoke it first, or write a "
                           f"different pattern.",
                 "conflict": {"id": existing["id"], "pattern": pattern,
                              "action": existing["action"], "source": existing["source"],
                              "client_class": client_class}},
                status_code=409)
        if existing is not None:
            # Same action already in force. Not an error — the policy asked for IS the
            # policy — but reported as a non-write, so the caller can say "already in
            # place" rather than confirming a rule it did not create. ``source`` rides
            # along because it decides whether the rule can be taken back again.
            return JSONResponse({"ok": True, "created": False, "already_present": True,
                                 "id": existing["id"], "pattern": pattern,
                                 "action": action, "source": existing["source"],
                                 "client_class": client_class})
        rule_id = conn.execute(
            "INSERT INTO rules(pattern, action, source, created_at, client_class) "
            "VALUES (?,?, 'operator', ?, ?)",
            (pattern, action, time.time(), client_class)).lastrowid
        conn.commit()

    # Audited like a revocation, and for the stronger version of the same reason: this
    # writes standing policy from nothing, so the record is the only thing that can
    # answer where a rule came from once it is sitting in the table looking exactly
    # like one a human approved at a card. The NORMALIZED pattern is what is recorded,
    # because it is what was stored and therefore what decides.
    store._audit("create", stage="policy", host=pattern, client_class=client_class,
                 reason=f"{action} rule created by {actor}; {pattern} "
                        f"({policy._pattern_scope(pattern)}) now {action}s for client "
                        f"class {client_class} without being held for approval")
    return JSONResponse({"ok": True, "created": True, "already_present": False,
                         "id": rule_id, "pattern": pattern, "action": action,
                         "source": "operator", "client_class": client_class},
                        status_code=201)


@app.post("/api/egress/rules/{rule_id}/edit")
def edit_rule(rule_id: int, req: RuleEditRequest, request: Request) -> JSONResponse:
    """Change a standing rule's pattern or action in ONE operation.

    The remaining half of rule mutation. Creating and revoking were built; CHANGING one
    meant revoke-then-create, which is two audit rows for one intent and — the part that
    actually bites — a window in which the rule is gone and its host decides as unknown.
    That window failed to ``hold`` rather than to allow, which is why it was tolerable,
    but tolerable is not atomic: an operator narrowing a wildcard under load left every
    host under it held, one card at a time, for as long as the second step took.

    One UPDATE in one transaction closes it. There is no instant at which the old rule
    is gone and the new one is not yet written.

    Otherwise this carries create's exposure and therefore create's validation: the
    pattern is caller-supplied rather than drawn from ``policy._persist_candidates``, so
    ``policy._rule_error`` is the whole of what stands between this and a rule matching
    more than the operator meant.

    **Seed rules are refused**, as ``revoke_rule`` refuses them, and the reasoning is
    stronger here. A revoked seed rule at least LEAVES, and ``store._seed_if_empty``
    re-reads the file on the next empty-table start. An edited one stays, indexed and
    deciding, while ``policies/egress-allowlist.txt`` — a reviewed file under version
    control — says something else about the same host.

    ``created_at`` is deliberately not touched: this is the same rule with different
    terms, and the audit row below is where the change is dated. Rewriting it would
    erase when the policy first came into force in favour of when someone last adjusted
    it, and the rules view sorts on it.

    The class is deliberately not editable — see ``RuleEditRequest``."""
    actor = _actor(request)
    pattern = policy._normalize_pattern(getattr(req, "pattern", "") or "")
    action = (getattr(req, "action", "") or "").strip().lower()

    error = policy._rule_error(pattern, action)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)

    with store._connect() as conn:
        row = conn.execute(
            "SELECT pattern, action, source, client_class FROM rules WHERE id=?",
            (rule_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown rule"},
                                status_code=404)
        if row["source"] == "seed":
            return JSONResponse(
                {"ok": False,
                 "detail": f"{row['pattern']} came from the policy seed and cannot be "
                           f"edited here — edit policies/egress-allowlist.txt"},
                status_code=403)
        if row["pattern"] == pattern and row["action"] == action:
            # Asked for what is already in force. A non-write rather than an error, for
            # the reason ``create_rule`` reports ``already_present``: the policy asked
            # for IS the policy. Reported as such because the alternative is an audit
            # row saying standing policy moved on a request that moved nothing.
            return JSONResponse({"ok": True, "changed": False, "id": rule_id,
                                 "pattern": pattern, "action": action,
                                 "client_class": row["client_class"]})
        # A DIFFERENT rule already holding the target (pattern, class) is the 409 that
        # ``UNIQUE(pattern, client_class)`` would otherwise raise at the driver, where it
        # is an opaque 500. Excluding this rule's own id matters: without it, changing
        # only the ACTION would collide with the row being edited.
        clash = conn.execute(
            "SELECT id, action, source FROM rules "
            "WHERE pattern=? AND client_class=? AND id<>?",
            (pattern, row["client_class"], rule_id)).fetchone()
        if clash is not None:
            return JSONResponse(
                {"ok": False,
                 "detail": f"another standing rule already covers {pattern!r} for "
                           f"client class {row['client_class']!r} and "
                           f"{clash['action']}s it; nothing here merges two rules. "
                           f"Revoke one of them first.",
                 "conflict": {"id": clash["id"], "pattern": pattern,
                              "action": clash["action"], "source": clash["source"],
                              "client_class": row["client_class"]}},
                status_code=409)
        conn.execute("UPDATE rules SET pattern=?, action=? WHERE id=?",
                     (pattern, action, rule_id))
        conn.commit()

    # ONE row carrying BOTH states, which is the entire difference from
    # revoke-then-create: those were two rows that each described half of an intent,
    # with nothing tying them together and no order guaranteed between them in a busy
    # log. ``host`` is the NEW pattern, because that is what decides from now on, and
    # the reason says what it replaced.
    store._audit("edit", stage="policy", host=pattern,
                 client_class=row["client_class"],
                 reason=f"rule edited by {actor}; {row['pattern']} ({row['action']}) is "
                        f"now {pattern} ({action}, {policy._pattern_scope(pattern)}) "
                        f"for client class {row['client_class']}")
    return JSONResponse({"ok": True, "changed": True, "id": rule_id,
                         "pattern": pattern, "action": action,
                         "client_class": row["client_class"],
                         "previous": {"pattern": row["pattern"],
                                      "action": row["action"]}})


@app.post("/api/egress/rules/{rule_id}/revoke")
def revoke_rule(rule_id: int, request: Request) -> JSONResponse:
    """Remove one operator-created rule. The other half of a governance plane that
    could grant but never take back.

    **Seed rules are refused, and refused HERE rather than merely hidden in the UI.**
    Their source of truth is ``policies/egress-allowlist.txt``, a reviewed file under
    version control, and a click that left the file disagreeing with the store would
    make the file a lie. It also removes a trap for free: ``store._seed_if_empty``
    re-reads that file whenever the rules table is empty, so a store whose every rule
    could be revoked would silently resurrect the whole seed allowlist on the next
    restart. With seed rules undeletable the table cannot reach that state.

    The consequence of retiring a TRANSITIONAL seed entry (npm, PyPI, GitHub — see
    DESIGN.md) is therefore that it happens as a migration shipped beside the code
    that replaces it, not as an operator action. That is the right shape for it: it is
    a versioned, reviewed change to a declared policy.

    Deletion rather than a tombstone. The audit row below IS the history — a rules
    table carrying dead rows would have to be filtered by every reader of it,
    including ``policy._decide``, which is the one place a mistake is unrecoverable.

    Provenance is recorded exactly as ``resolve`` records it, and for the same reason:
    editing standing policy is more consequential than any single egress decision, and
    until now nothing recorded that it had happened at all. Detection, not prevention
    — the fields are forgeable by a host-local caller (see ``_actor``)."""
    actor = _actor(request)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT pattern, action, source, client_class FROM rules WHERE id=?",
            (rule_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown rule"},
                                status_code=404)
        if row["source"] == "seed":
            return JSONResponse(
                {"ok": False,
                 "detail": f"{row['pattern']} came from the policy seed and cannot "
                           f"be revoked here — edit policies/egress-allowlist.txt"},
                status_code=403)
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        conn.commit()

    # What the host reverts TO is the useful half of this record. Both actions land on
    # `hold` — an unmatched host is held for approval — but from opposite directions,
    # and only the reason line says which.
    # The class is named in both the column and the reason: a revoke touches ONE
    # client population, and a record saying only "the .github.com allow was revoked"
    # cannot answer which of two such rules went.
    store._audit("revoke", stage="policy", host=row["pattern"],
                 client_class=row["client_class"],
                 reason=f"{row['action']} rule revoked by {actor}; {row['pattern']} is "
                        f"now unknown for client class {row['client_class']} and will "
                        f"be held for approval")
    return JSONResponse({"ok": True, "pattern": row["pattern"],
                         "action": row["action"],
                         "client_class": row["client_class"]})


# ── leases (human-facing) ───────────────────────────────────────────────────
# The timed half of egress policy, and its own pair of endpoints rather than a filter
# on the rules ones — the rows are a different kind (``store._LEASES_DDL``) and they
# answer a different question. ``/api/egress/rules`` is "what have I permanently
# allowed"; this is "what am I allowing right now".

@app.get("/api/egress/leases")
def api_leases() -> list[dict]:
    """The LIVE leases — every timed grant currently deciding requests.

    Expired rows are filtered out rather than listed greyed-out: an expired lease is
    not policy, and showing it would put something in the operator's "what is granted"
    view that grants nothing. Rows are swept on the grant path (see the lease branch in
    ``resolve``), so what this filters is only what has lapsed since the last grant.

    Absolute ``expires_at``, never a remaining-seconds field, for the reason the
    saturation payload states (``holds._saturation``): a value that changes on
    every tick defeats change-detection and turns a poll into a firehose. The client
    does the arithmetic, as it already does for the hold countdown.

    Unpaginated, as ``api_rules`` is and for the same reason — this is the complete set
    of live timed grants, bounded by how many cards a human can click, and a silently
    truncated view of what is currently allowed would be worse than none."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, host, client_class, approval_id, created_at, expires_at, "
            "granted_by FROM leases WHERE expires_at > ? ORDER BY expires_at",
            (time.time(),)).fetchall()
    # Soonest to expire first: the one about to lapse is the one an operator might
    # still want to act on, and the one whose disappearance from this list next is
    # least surprising if it is at the top.
    return [dict(r) for r in rows]


@app.post("/api/egress/leases/{lease_id}/revoke")
def revoke_lease(lease_id: int, request: Request) -> JSONResponse:
    """End one timed grant early.

    Without this a lease is a grant that can only be waited out, and it is what makes
    the configured duration an ergonomics number rather than a safety floor
    (``policy.LEASE_SECONDS``). No seed exemption of the kind ``revoke_rule`` carries:
    every lease was written by a click on a card, so there is no reviewed file under
    version control for a revocation here to leave disagreeing with the store.

    Deletion rather than a tombstone — the same choice ``revoke_rule`` makes, for a
    sharper reason. This table is transient by construction, so a dead row would be the
    only long-lived thing in it and every reader, ``policy._live_lease`` included, would
    have to filter for it. The audit rows are the history: ``resolve`` records the
    grant, this records the end of it.

    An ALREADY-EXPIRED row is deleted too and reported as such rather than refused. A
    refusal would leave the operator looking at a button that failed for a reason
    indistinguishable from a bug, where the honest answer — the grant had already ended,
    and now the row is gone as well — is both true and what they were asking for.

    **What this does not do is tear down an established connection.** The proxy
    authorizes once per CONNECT tunnel, so a tunnel opened while the lease was live
    keeps carrying requests after it ends (DESIGN.md, "A lease bounds authorization,
    not connection lifetime"). Revoking stops the next connection, not this one."""
    actor = _actor(request)
    now = time.time()
    with store._connect() as conn:
        row = conn.execute(
            "SELECT host, client_class, expires_at FROM leases WHERE id=?",
            (lease_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown lease"},
                                status_code=404)
        conn.execute("DELETE FROM leases WHERE id=?", (lease_id,))
        conn.commit()
    was_live = row["expires_at"] > now
    # What the host reverts TO is the useful half of the record, exactly as it is for a
    # rule revocation: a lease ending sends the host back to being held for approval,
    # not to being denied, and only the reason line says so.
    store._audit(
        "revoke", stage="policy", host=row["host"],
        client_class=row["client_class"],
        reason=(f"lease revoked by {actor} with "
                f"{policy._short_duration(row['expires_at'] - now)} left; "
                f"{row['host']} is now unknown for client class "
                f"{row['client_class']} and will be held for approval"
                if was_live else
                f"expired lease removed by {actor}; it had already stopped deciding "
                f"requests for {row['host']}"))
    return JSONResponse({"ok": True, "host": row["host"],
                         "client_class": row["client_class"],
                         "was_live": was_live})


# ── MCP gateway policy (human-facing) ───────────────────────────────────────
# None of this waits for the gateway, and it was built before it. Tool policy is
# CONFIGURATION FIRST — an operator states what a server's tools may do before
# anything calls one — which is the opposite of the egress surface, where rules
# accumulate from approvals and direct creation was retrofitted (see "Tool policy
# gets its own table" in DESIGN.md). So the config surface is the primary path here,
# and it is built before the consumer rather than after it.
#
# Everything below is off the AUTHORIZE listener, like everything else that grants.

def _server_view(row) -> dict:
    """One `mcp_servers` row as the API serves it: `enabled` as a boolean SQLite
    cannot store, and the auth descriptor nested so it reads as the one object it is
    rather than three flat columns that happen to share a prefix."""
    return {"server": row["server"], "enabled": bool(row["enabled"]),
            "auth": {"type": row["auth_type"], "header": row["auth_header"],
                     "template": row["auth_template"]},
            "created_at": row["created_at"]}


def _descriptor(req) -> tuple[str, str, str]:
    """The auth descriptor off a request, normalized. Empty strings rather than None,
    so ``policy._auth_descriptor_error`` has one falsy value to test instead of two."""
    return ((getattr(req, "auth_type", "") or "").strip().lower(),
            (getattr(req, "auth_header", "") or "").strip(),
            (getattr(req, "auth_template", "") or "").strip())


@app.get("/api/mcp/servers")
def api_mcp_servers() -> list[dict]:
    """The registered servers, with how many tool rules each one carries.

    Unpaginated, for the reason ``api_rules`` is: this is the complete configuration
    and a truncated view of it hides exactly what the interface exists to show. The
    rule count rides along because it is what makes the list actionable — a server
    with none has nothing its tools may do, which is a fully denied surface rather
    than a broken one, and only the count says which."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT server, enabled, auth_type, auth_header, auth_template, "
            "created_at FROM mcp_servers ORDER BY server").fetchall()
        counts = {r["server"]: r["n"] for r in conn.execute(
            "SELECT server, COUNT(*) AS n FROM tool_rules GROUP BY server")}
    return [dict(_server_view(r), tool_rules=counts.get(r["server"], 0)) for r in rows]


@app.post("/api/mcp/servers")
def create_mcp_server(req: ServerCreateRequest, request: Request) -> JSONResponse:
    """Register a server so policy can be written about it. Starts nothing.

    The control plane cannot enumerate what is running — that would mean a docker
    socket on the crown-jewel container — so the operator names the server, matching
    what `mcp-servers.yml` declares. A typo is therefore possible and is not caught
    here: it produces a registered server the gateway will find nothing behind, which
    the roster reports on the gateway's next poll. That is the right failure — visible and
    granting nothing — and it is why this endpoint validates the name's SHAPE
    (``policy._server_name_error``) rather than pretending to validate its existence.

    Registered is not enabled. A fresh row is `enabled=0` with no tool rules, which is
    a server the gateway will not dial and whose every tool would be denied if it
    did — the two defaults agreeing, rather than one covering for the other."""
    actor = _actor(request)
    server = (getattr(req, "server", "") or "").strip().lower()
    auth_type, header, template = _descriptor(req)

    error = policy._server_name_error(server)
    if error is None:
        error = policy._auth_descriptor_error(auth_type, header, template)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)

    with store._connect() as conn:
        if conn.execute("SELECT 1 FROM mcp_servers WHERE server=?",
                        (server,)).fetchone() is not None:
            # Not a silent replace, for the reason ``create_rule`` refuses one: this
            # would overwrite an auth descriptor, and a descriptor changed by a call
            # that reported "registered" is a credential swap with no before in the
            # record. ``edit`` is where that has one.
            return JSONResponse(
                {"ok": False,
                 "detail": f"server {server!r} is already registered; nothing here "
                           f"replaces a registration. Edit it, or revoke it first."},
                status_code=409)
        conn.execute(
            "INSERT INTO mcp_servers(server, enabled, auth_type, auth_header, "
            "auth_template, created_at) VALUES (?, 0, ?, ?, ?, ?)",
            (server, auth_type, header or None, template or None, time.time()))
        conn.commit()

    store._audit("create", stage="mcp-server", server=server,
                 reason=f"MCP server {server} registered by {actor}; disabled, "
                        f"auth {auth_type}, no tools permitted until rules are written")
    return JSONResponse({"ok": True, "created": True, "server": server,
                         "enabled": False,
                         "auth": {"type": auth_type, "header": header or None,
                                  "template": template or None}},
                        status_code=201)


@app.post("/api/mcp/servers/{server}/edit")
def edit_mcp_server(server: str, req: ServerEditRequest,
                    request: Request) -> JSONResponse:
    """Enable or disable a server, and change how the gateway authenticates to it.

    One operation for both, because they are one configuration: a server enabled with
    a descriptor that does not resolve is a surface that fails as though policy
    refused it. Splitting them would also put an ordering trap where there is no need
    for one — enable-then-fix versus fix-then-enable, with a window either way.

    Disabling is the taking-back verb, and it is the blunt one: it stops the gateway
    dialling the server at all, where revoking a single tool rule returns that one
    tool to the default deny. Both directions are here because a governance plane
    that could only grant is the gap ``revoke_rule`` was built to close."""
    actor = _actor(request)
    server = (server or "").strip().lower()
    enabled = bool(getattr(req, "enabled", False))
    auth_type, header, template = _descriptor(req)

    error = policy._auth_descriptor_error(auth_type, header, template)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)

    with store._connect() as conn:
        row = conn.execute(
            "SELECT server, enabled, auth_type, auth_header, auth_template, "
            "created_at FROM mcp_servers WHERE server=?", (server,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown server"},
                                status_code=404)
        before = _server_view(row)
        if (before["enabled"] == enabled and row["auth_type"] == auth_type
                and (row["auth_header"] or "") == header
                and (row["auth_template"] or "") == template):
            # Asked for what is already configured — a non-write, reported as one, so
            # the audit trail does not claim the surface moved when it did not.
            return JSONResponse({"ok": True, "changed": False, **before})
        conn.execute(
            "UPDATE mcp_servers SET enabled=?, auth_type=?, auth_header=?, "
            "auth_template=? WHERE server=?",
            (1 if enabled else 0, auth_type, header or None, template or None,
             server))
        conn.commit()
        after = _server_view(conn.execute(
            "SELECT server, enabled, auth_type, auth_header, auth_template, "
            "created_at FROM mcp_servers WHERE server=?", (server,)).fetchone())

    # ONE row carrying both states, as ``edit_rule`` writes one: the two halves of a
    # change with nothing to tie them together is the shape this endpoint exists to
    # avoid. The TEMPLATE is safe to record and the secret is not in it — that is what
    # a descriptor being useless to steal buys.
    store._audit("edit", stage="mcp-server", server=server,
                 reason=f"MCP server {server} edited by {actor}; "
                        f"enabled {before['enabled']} -> {after['enabled']}, "
                        f"auth {before['auth']['type']} -> {after['auth']['type']} "
                        f"(header {before['auth']['header']} -> "
                        f"{after['auth']['header']}, template "
                        f"{before['auth']['template']} -> {after['auth']['template']})")
    return JSONResponse({"ok": True, "changed": True, **after,
                         "previous": before})


@app.post("/api/mcp/servers/{server}/revoke")
def revoke_mcp_server(server: str, request: Request) -> JSONResponse:
    """Remove a registration entirely.

    **Refused while the server still has tool rules**, rather than cascading. A
    cascade behind one POST would delete standing policy the operator cannot see from
    the button they pressed, and there is no undo in this system — the audit row would
    be the only record that a dozen decisions had been made and unmade. Deleting them
    is safe in the sense that removal never grants (an unconfigured tool is denied),
    but safe is not the same as visible, and this is the surface whose whole job is
    visibility. Disabling is the one-click way to stop a server without touching its
    policy, which is what an operator reaching for this usually wants."""
    actor = _actor(request)
    server = (server or "").strip().lower()
    with store._connect() as conn:
        row = conn.execute("SELECT server, enabled FROM mcp_servers WHERE server=?",
                           (server,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown server"},
                                status_code=404)
        rules = conn.execute("SELECT COUNT(*) FROM tool_rules WHERE server=?",
                             (server,)).fetchone()[0]
        if rules:
            return JSONResponse(
                {"ok": False,
                 "detail": f"server {server!r} still has {rules} tool rule(s); "
                           f"nothing here deletes standing policy as a side effect. "
                           f"Revoke them first, or disable the server instead.",
                 "tool_rules": rules},
                status_code=409)
        conn.execute("DELETE FROM mcp_servers WHERE server=?", (server,))
        conn.commit()

    store._audit("revoke", stage="mcp-server", server=server,
                 reason=f"MCP server {server} registration revoked by {actor}; the "
                        f"gateway will no longer dial it")
    return JSONResponse({"ok": True, "server": server})


@app.get("/api/mcp/inventory")
def api_mcp_inventory() -> dict:
    """What each server last said it exposes, for an operator choosing rules.

    Served from MEMORY and empty until a gateway has pushed — including after a
    restart of this process, which is the intended cost rather than a gap. A stored
    copy would survive a server that has been gone for a week and still read as
    current; ``seen_at`` is here so a reader can tell the difference.

    OBSERVATION, not policy. Nothing in this response is permitted, and the rules that
    decide live in ``/api/mcp/servers`` and its rule endpoints. Kept separate for that
    reason and not merely for tidiness: one is a third party's claim, the other is what
    a human decided."""
    return inventory.snapshot()


@app.get("/api/mcp/rules")
def api_mcp_rules() -> list[dict]:
    """Standing tool policy — what each registered server's tools may do.

    Grouped by server, then allow before ask before deny, so the widest grants are
    read first. That is a different order from ``api_rules``, which puts blocks first
    because ``policy._decide`` lets a block win over an allow; nothing here competes,
    since a tool matches at most one rule, so the order can serve the reader instead.

    What this view CANNOT show is the tools a server exposes that have no rule — they
    are denied, and they are also the ones most needing a decision. That join is the
    caller's to make, against ``/api/mcp/inventory``: the two are served separately
    because one is what an operator decided and the other is what a server claimed,
    and merging them here would produce a single list in which those are
    indistinguishable."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, server, tool, action, source, created_at FROM tool_rules "
            "ORDER BY server, CASE action WHEN 'allow' THEN 0 WHEN 'ask' THEN 1 "
            "ELSE 2 END, tool").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/mcp/rules")
def create_mcp_rule(req: ToolRuleCreateRequest, request: Request) -> JSONResponse:
    """Write a standing tool rule.

    An explicit ``deny`` is worth writing even though an unconfigured tool is already
    denied, and that is not redundancy: the row is what distinguishes "reviewed and
    refused" from "never looked at". Those are the same decision to the gateway and
    very different facts to an operator deciding what still needs attention."""
    actor = _actor(request)
    server = (getattr(req, "server", "") or "").strip().lower()
    tool = (getattr(req, "tool", "") or "").strip()
    action = (getattr(req, "action", "") or "").strip().lower()

    error = policy._tool_rule_error(tool, action)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)

    with store._connect() as conn:
        if conn.execute("SELECT 1 FROM mcp_servers WHERE server=?",
                        (server,)).fetchone() is None:
            # The tool-surface twin of ``create_rule``'s unknown-class refusal, and
            # the same failure it prevents: a rule for a server nobody registered
            # decides nothing while reading, in this view, as policy in force.
            return JSONResponse(
                {"ok": False,
                 "detail": f"no MCP server named {server!r} is registered; register "
                           f"it before writing policy about its tools"},
                status_code=400)
        existing = conn.execute(
            "SELECT id, action FROM tool_rules WHERE server=? AND tool=?",
            (server, tool)).fetchone()
        if existing is not None and existing["action"] != action:
            return JSONResponse(
                {"ok": False,
                 "detail": f"a rule for {tool!r} on {server!r} already exists and "
                           f"{existing['action']}s it; nothing here replaces a rule. "
                           f"Edit it, or revoke it first.",
                 "conflict": {"id": existing["id"], "server": server, "tool": tool,
                              "action": existing["action"]}},
                status_code=409)
        if existing is not None:
            return JSONResponse({"ok": True, "created": False, "already_present": True,
                                 "id": existing["id"], "server": server, "tool": tool,
                                 "action": action})
        rule_id = conn.execute(
            "INSERT INTO tool_rules(server, tool, action, source, created_at) "
            "VALUES (?,?,?, 'operator', ?)",
            (server, tool, action, time.time())).lastrowid
        conn.commit()

    store._audit("create", stage="tool-policy", server=server, tool=tool,
                 reason=f"tool rule created by {actor}; {tool} on {server} now "
                        f"{action}s")
    return JSONResponse({"ok": True, "created": True, "already_present": False,
                         "id": rule_id, "server": server, "tool": tool,
                         "action": action, "source": "operator"}, status_code=201)


@app.post("/api/mcp/rules/{rule_id}/edit")
def edit_mcp_rule(rule_id: int, req: ToolRuleEditRequest,
                  request: Request) -> JSONResponse:
    """Move a tool between deny, ask and allow — the operator's actual workflow.

    Only the action changes; see ``ToolRuleEditRequest`` for why the identity does
    not. There is no uniqueness clash to handle for the same reason: the key is
    untouched, so this cannot collide with another row."""
    actor = _actor(request)
    action = (getattr(req, "action", "") or "").strip().lower()

    with store._connect() as conn:
        row = conn.execute(
            "SELECT server, tool, action FROM tool_rules WHERE id=?",
            (rule_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown rule"},
                                status_code=404)
        error = policy._tool_rule_error(row["tool"], action)
        if error is not None:
            return JSONResponse({"ok": False, "detail": error}, status_code=400)
        if row["action"] == action:
            return JSONResponse({"ok": True, "changed": False, "id": rule_id,
                                 "server": row["server"], "tool": row["tool"],
                                 "action": action})
        conn.execute("UPDATE tool_rules SET action=? WHERE id=?", (action, rule_id))
        conn.commit()

    store._audit("edit", stage="tool-policy", server=row["server"], tool=row["tool"],
                 reason=f"tool rule edited by {actor}; {row['tool']} on "
                        f"{row['server']} was {row['action']}, now {action}")
    return JSONResponse({"ok": True, "changed": True, "id": rule_id,
                         "server": row["server"], "tool": row["tool"],
                         "action": action, "previous": {"action": row["action"]}})


@app.post("/api/mcp/rules/{rule_id}/revoke")
def revoke_mcp_rule(rule_id: int, request: Request) -> JSONResponse:
    """Remove one tool rule, returning that tool to the default.

    Which is a DENY, not a hold — the one place this differs from revoking an egress
    rule, where the host reverts to being held for approval. Revoking here can
    therefore only narrow, whatever the rule said, and the record names the
    destination rather than leaving it to be inferred from the action removed.

    There is no seed to refuse, as ``revoke_rule`` refuses one: no file seeds this
    table, so every row in it is an operator's."""
    actor = _actor(request)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT server, tool, action FROM tool_rules WHERE id=?",
            (rule_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown rule"},
                                status_code=404)
        conn.execute("DELETE FROM tool_rules WHERE id=?", (rule_id,))
        conn.commit()

    store._audit("revoke", stage="tool-policy", server=row["server"], tool=row["tool"],
                 reason=f"tool rule revoked by {actor}; {row['tool']} on "
                        f"{row['server']} was {row['action']}, now unconfigured and "
                        f"therefore denied")
    return JSONResponse({"ok": True, "id": rule_id, "server": row["server"],
                         "tool": row["tool"], "action": row["action"]})


@app.get("/status", response_class=PlainTextResponse)
def status() -> str:
    with store._connect() as conn:
        rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    return (f"dockade control plane (2b) — {rules} rules, {audits} audit rows, "
            f"{pending} pending approvals\n")


# ── entry point ─────────────────────────────────────────────────────────────

async def main() -> None:
    """Serve both listeners from one process and one event loop.

    ``uvicorn`` is imported HERE rather than at module scope so the module stays
    importable with only the stdlib — the unit suite loads this file directly with
    stub fastapi/pydantic and installs no packages (see tests/_loader.py)."""
    import uvicorn

    _assert_listeners_separated()
    _bootstrap()
    print(f"control-plane: authorize on {AUTHORIZE_BIND}:{AUTHORIZE_PORT}, "
          f"tool on {TOOL_BIND}:{TOOL_PORT}, "
          f"management on {MANAGE_BIND}:{MANAGE_PORT}", flush=True)

    # The ingest is a plain task on this loop rather than a lifespan hook, for the
    # same reason _bootstrap is not one: it belongs to the PROCESS, not to either
    # app. Holding the reference matters — asyncio keeps only a weak one, so a
    # create_task whose result nobody holds can be collected mid-flight and the
    # ingest would stop with no error anywhere.
    drain = None
    if ingest.DRAIN_INTERVAL > 0:
        drain = asyncio.create_task(ingest._audit_drain_loop())
    else:
        print("control-plane: audit ingest DISABLED "
              "(CONTROL_AUDIT_DRAIN_INTERVAL=0); locally-decided egress will "
              "appear only in the proxy's own log", flush=True)

    # Servers sharing one process means sharing one set of signal handlers,
    # and SIGTERM has to stop ALL of them or `docker compose down` waits out the grace
    # period and SIGKILLs the governance authority with holds in flight.
    #
    # uvicorn already handles this, and the mechanism is worth naming because it is
    # not obvious: each ``serve()`` wraps itself in ``capture_signals()``, so the
    # second server's handler replaces the first's — but on exit it restores what
    # it replaced and re-raises the signal it caught, which then reaches the first.
    # Measured on the pinned 0.34.0 with TWO listeners (NOTES.md): SIGTERM logged a
    # clean shutdown for each and the process was gone inside a second. The count is
    # the measurement's, not this file's — three listeners ship now, and the chain is
    # per-server rather than pairwise (each ``capture_signals`` restores and re-raises
    # for exactly one ``serve()``), so it extends by construction rather than by
    # having been re-measured. An earlier version of this function added handlers of
    # its own to "fix" the overwrite; they were inert — uvicorn installs via
    # ``signal.signal``, which displaces asyncio's — and removing them changed
    # nothing, so they are gone rather than kept as insurance.
    servers = [
        uvicorn.Server(uvicorn.Config(
            authorize_app, host=AUTHORIZE_BIND, port=AUTHORIZE_PORT,
            log_level="info")),
        uvicorn.Server(uvicorn.Config(
            tool_app, host=TOOL_BIND, port=TOOL_PORT, log_level="info")),
        uvicorn.Server(uvicorn.Config(
            app, host=MANAGE_BIND, port=MANAGE_PORT, log_level="info")),
    ]
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        if drain is not None:
            drain.cancel()


if __name__ == "__main__":
    asyncio.run(main())
