#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""What the servers actually expose, against what policy actually decides.

The control plane holds no tool inventory and cannot build one: it has no leg on
`mcp-net`, and enumerating what runs would mean a docker socket on the crown-jewel
container. So a `tool_rules` row is a name an operator TYPED, checked for shape and
never for existence. That is safe in both directions — `policy._decide_tool` denies
when no rule matches, so a misspelled rule and an unwritten one fail the same closed
way — and it is blind in both directions too: a typo is indistinguishable from a
deny, and there is no list to pick a name from.

The gateway is the only component that ever talks to a server, so it is the only one
that can see the gap. Here it DIALS, COMPARES, REPORTS to the log, and PUSHES what it
saw to the control plane, which holds it in memory for an operator choosing rules.

The invariant that governs the push, and the reason it is safe for the crown jewel to
accept a write from here at all: discovered names are operator-facing metadata and
never reach `_decide_tool`. A tool that could write its own rule would be a server
granting itself capability, so what crosses is a CLAIM, and claims decide nothing. See
DESIGN.md, "the control plane learns a server's tools from the gateway".

NOTHING HERE GRANTS, which is what lets it be this simple. A wrong answer from a
server — a lie, a truncated list, an unreachable port — can only make this report
wrong. Execution policy is decided per call against rules an operator wrote, and a
tool absent from every list below is still denied by default if it is ever called.

Three protocol facts, MEASURED rather than assumed (NOTES.md, "Driving the server by
hand"; `make mcp-tools` is the same request by hand):

  - `tools/list` needs NO `initialize` handshake and no session header. The server
    is stateless, so enumeration is one POST.
  - The reply is SSE — `data: ` lines — not a bare JSON body.
  - The `Authorization` header is mandatory and FORMAT-checked, but enumeration
    never calls GitHub, so reading a tool surface does not spend a real credential.
    We send the real one anyway when it exists: a well-formed dummy works on this
    server and that is a property of this server, and DESIGN.md's auth handling has
    no per-server branch anywhere in it.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import ipaddress
import itertools
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator

import outcomes

#: Where each request's id comes from: one per request for the gateway's lifetime, so
#: no two requests in a session share one. The spec forbids a reused id, and a server
#: that keys each call's stream by its id loses one of two overlapping calls that share
#: it (NOTES.md, "A stateful MCP server talks back on a call's stream"). The lock covers
#: taking the next number and never a call, so calls still overlap.
_IDS = itertools.count(1)
_IDS_LOCK = threading.Lock()

#: The JSON-RPC error a server's request on a call's stream is answered with, unless it
#: is `ping`: method not found, because the gateway offers a server no client methods.
#: Root DESIGN.md, "Elicitation routes to the wrong human", says why nothing a server
#: asks reaches the agent instead.
_NO_SUCH_METHOD = {"code": -32601,
                   "message": "the gateway answers no requests from a server"}

#: How much of a server's method name a refusal's log line carries, after escaping.
_METHOD_SHOWN = 120

#: How much of why an answer was not delivered its log line carries, after escaping:
#: the 200 characters the inventory keeps of a status. The reason can quote a reason
#: phrase the server chose, of any length, once per request it sends.
_FAILURE_SHOWN = 200

#: The most an SSE reply is read in at once. A read returns what has arrived, up to
#: this, so a server's request is seen as soon as its event ends.
_CHUNK = 65536

#: A DNS label, the same shape `policy._server_name_error` holds registration to. The
#: name is used as a hostname and as a filename, so both uses want exactly this.
_SERVER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

#: A token ``post`` can send: printable ASCII, no whitespace. The task registry's
#: ``agent_api.load_secret`` rule without its minimum length, for a file the gateway
#: also reads. Past latin-1, or with a line break, http.client raises while sending.
_TOKEN_RE = re.compile(r"[!-~]+")

#: The control plane's TOOL bridge, by ADDRESS rather than by name — the one place
#: here that does not dial a name. The control plane has three legs and only its
#: tool-authorize-net one may be spoken to. The name would pick that one today, since
#: embedded DNS answers only with the legs the querier shares and these two share just
#: it; the address is used because it names the LEG rather than the container, so it
#: stays right if a second shared network is ever added, and because
#: tests/test_topology.py can hold it equal to what the control plane binds. Same
#: reason compose pins CONTROL_TOOL_BIND rather than letting that listener take a
#: wildcard.
CONTROL_URL = os.environ.get("GATEWAY_CONTROL_URL", "http://172.27.0.2:8092")

# Servers are dialled BY NAME, and that is the identity per-tool policy is keyed on
# (DESIGN.md, "Per-server identity has two different answers"). The gateway is never
# given a server's address; where the name may LEAD is ``MCP_NET``'s business. The
# port and path come from each roster entry (``check_endpoint``).

#: The path shape `policy._endpoint_error` holds registration to, used with
#: ``fullmatch`` (the comment there says why it is spelled this way). Held equal to it
#: by tests/test_tool_discovery.py.
_PATH_RE = re.compile(r"/(?:[A-Za-z0-9._~-]+/)*[A-Za-z0-9._~-]*")
_PATH_MAX_LEN = 128


def _network(raw: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Strict: masking host bits off would read ``172.28.0.0/2`` as ``128.0.0.0/2``,
    which contains sandbox-net, the network ``MCP_NET`` exists to keep credentials
    off."""
    try:
        return ipaddress.ip_network(raw)
    except ValueError as exc:
        raise SystemExit(
            f"tool-gateway: GATEWAY_MCP_NET {raw!r} is not a CIDR ({exc}). It decides "
            f"where a server's credential may be sent. Refusing to start (fail "
            f"closed).") from exc


#: The one network a server's credential may be sent to. A server is single-homed on
#: mcp-net, but its NAME is not: Docker's embedded DNS answers from every network the
#: querier shares, and the gateway also sits on sandbox-net, where the agent lives. A
#: container there carrying a server's name or alias would be handed that server's
#: bearer token. Nothing creates one today — the launchers set only `--name` — and
#: that is a fact about the launchers, so the address is checked here instead:
#: placement, not DNS, decides where a credential goes. Held equal to compose's
#: `mcp-net` subnet by tests/test_topology.py.
MCP_NET = _network(os.environ.get("GATEWAY_MCP_NET", "172.28.0.0/24"))

#: Where a server's credential lives, DERIVED from the server name and never read
#: from the store. A free-text path in a store row would let a forged config write
#: point one server at another server's credential; deriving makes that impossible
#: rather than validated-against (DESIGN.md, "The secret's path is derived").
SECRETS_DIR = os.environ.get("GATEWAY_SECRETS_DIR", "/run/dockade/secrets")

#: Every network call here is a diagnostic on a timer, so a slow peer must cost a
#: report rather than a thread. Short on purpose: these are all sibling containers on
#: a local bridge, where a second is already an eternity.
TIMEOUT = float(os.environ.get("GATEWAY_DISCOVERY_TIMEOUT", "5"))


class DiscoveryError(Exception):
    """A server could not be enumerated, with a reason fit to print.

    Carried rather than raised through: one unreachable server must not stop the
    others being reported, and "unreachable" is itself a finding an operator wants to
    see next to the rest.

    ``kind`` exists for the OUTCOME audit and for nothing else. Every caller here does
    the same thing with this exception — report it and move on — so it does not need
    subclasses; what the record needs is one bit the message cannot be trusted to
    carry. A timeout and a refused connection are both "no answer", but only one of
    them may have LANDED upstream, and for an approved write that is the difference
    between "it did not happen" and "it might have". Reading that back out of the
    message text would be the same prose-parsing the audit columns exist to avoid."""

    def __init__(self, message: str, kind: str = "transport-error"):
        super().__init__(message)
        self.kind = kind


#: What the control plane can do wrong on a call this module makes: no answer
#: (``URLError`` is an ``OSError``), a reply that is not HTTP or is cut short, and a
#: body that will not parse, too deep included (a ``RuntimeError``, not a
#: ``ValueError``). The same tuple as ``execute._ask_control``'s; anything outside it is
#: a bug, and raises.
_CONTROL_FAILURES = (OSError, http.client.HTTPException, ValueError, RecursionError)


def fetch_roster() -> list[dict]:
    """The enabled servers and their rules, from the control plane's tool bridge.

    A body that parses to something other than a list is refused with a
    ``ValueError``, the class a body that will not parse raises, so ``poll`` reports
    both the same way.

    Pulled, never pushed. The control plane has no leg on this container's networks
    and must not be given one — a push would mean the crown jewel dialling the
    agent-facing service, which is the lateral edge the bind guard exists to
    prevent."""
    request = urllib.request.Request(  # noqa: S310 - fixed http:// scheme, not user input
        f"{CONTROL_URL}/tool/roster", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        roster = json.loads(response.read())
    if not isinstance(roster, list):
        raise ValueError(f"a {type(roster).__name__} where a list was expected")
    return roster


def check_name(server: str) -> str:
    """The server name, or a refusal — validated HERE and not only at registration.

    The control plane already holds registration to a DNS label
    (``policy._server_name_error``), and this checks it again because the two are
    different processes and this one uses the string to build a FILESYSTEM PATH and a
    URL. Trusting a peer's validation for that is the shape of bug where the peer is
    later given a second write path; the check costs a regex and removes the question.
    A DNS label cannot contain ``/``, ``.`` or a null, so a name that passes cannot
    leave SECRETS_DIR."""
    if not _SERVER_RE.fullmatch(server or ""):
        raise DiscoveryError(
            f"{server!r} is not a DNS label, so it names neither a container to dial "
            f"nor a file to read")
    return server


def check_endpoint(endpoint) -> tuple[int, str]:
    """A roster entry's port and path, or a refusal — checked here for ``check_name``'s
    reason. The path is appended to the URL after the checked address, so a path that
    could end the authority would send the credential to a host ``_placed`` never saw.

    No default for a missing endpoint: the control plane always sends one, and a roster
    without it is not one this code was written against."""
    port = endpoint.get("port") if isinstance(endpoint, dict) else None
    path = endpoint.get("path") if isinstance(endpoint, dict) else None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise DiscoveryError(f"the roster gives no usable port ({port!r})")
    if (not isinstance(path, str) or len(path) > _PATH_MAX_LEN
            or not _PATH_RE.fullmatch(path) or {".", ".."} & set(path.split("/"))):
        raise DiscoveryError(f"the roster gives no usable path ({path!r})")
    return port, path


def secret_path(server: str) -> str:
    """Where a server's token lives. One definition, because it is quoted in errors as
    well as opened, and an error naming a path nobody reads is worse than none."""
    return os.path.join(SECRETS_DIR, f"{check_name(server)}.json")


def read_secret(server: str) -> str | None:
    """A server's token, or None if no file is there.

    The filename IS the server name, which is also the container name — one string
    doing all three jobs (dialled, keyed on, derived from), which is what makes
    cross-wiring impossible rather than validated-against: nothing in the roster, and
    nothing in the store behind it, contributes a path component.

    Note there is no ``mcp-`` prefix added here. The prefix is already part of the
    name (`mcp-github` is the container), so adding one would look for
    ``mcp-mcp-github.json`` — which is the file the Makefile's probe helper does NOT
    write. DESIGN.md said otherwise until this was implemented against it.

    THREE outcomes, not two, and keeping them apart is the whole point. A missing file
    is None — "configured, secret missing" is a state an operator needs reported, since
    it otherwise surfaces as an upstream 401 that reads like a policy problem
    (DESIGN.md, "What the UI can say without ever seeing a value"). An unreadable one
    and a file of the wrong SHAPE are both errors, and they name different fixes:
    permissions on the one hand, file contents on the other. A token that cannot be
    sent in a header is the wrong shape too: refused here, naming the file.

    Collapsing the third into the second is what this function did first, and it sent
    its reader looking for a file that was sitting right there."""
    path = secret_path(server)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    # RecursionError: a file nested too deep to parse, a RuntimeError, not a ValueError.
    except (OSError, ValueError, RecursionError) as exc:
        raise DiscoveryError(f"secret file {path} is unreadable: {exc}") from exc
    token = isinstance(data, dict) and data.get("token")
    if not token:
        raise DiscoveryError(
            f"{path} has no 'token'. The file holds secret material and nothing else — "
            f'{{"token": "...", "note": "..."}} — because the header name and template '
            f"are the DESCRIPTOR, and that is registered in the control plane rather "
            f"than kept beside the credential")
    # Refused here, naming the file, rather than left to fail inside http.client as a
    # bare UnicodeEncodeError or ValueError. The token itself is never quoted.
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise DiscoveryError(
            f"{path} holds a 'token' that cannot be sent in a header: it must be "
            f"printable ASCII with no spaces or line breaks")
    return token


def auth_header(auth: dict, secret: str | None, where: str = "") -> dict[str, str]:
    """The Authorization-style header a descriptor asks for, with the secret applied.

    The descriptor is the whole per-server difference — header NAME and TEMPLATE come
    from the store, so `Bearer {secret}`, `token {secret}` and an `X-Api-Key` all work
    with no branch on which server this is."""
    if auth.get("type") != "header":
        return {}
    if secret is None:
        # Names the actual path. The placeholder this used to print was unresolvable by
        # the person reading it, which is the one thing an error message must not be.
        raise DiscoveryError(
            f"auth descriptor wants {auth.get('header')!r} but there is no file at "
            f"{where or SECRETS_DIR}")
    name = auth.get("header") or "Authorization"
    template = auth.get("template") or "{secret}"
    value = template.replace("{secret}", secret)
    # Refused here because http.client's own refusal quotes the whole value, secret
    # included, and that message would reach the inventory, the audit and the agent.
    # This one quotes neither the template nor the secret.
    if "\r" in value or "\n" in value or not _latin1(value):
        raise DiscoveryError(
            f"auth descriptor's template for {name!r} builds a header value that cannot "
            f"be sent (a line break, or a character outside latin-1) for the "
            f"credential at {where or SECRETS_DIR}")
    return {name: value}


def _latin1(text: str) -> bool:
    """Whether http.client can encode ``text`` as a header value."""
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


#: An SSE line end: CRLF, CR or LF.
_LINE_END = re.compile(rb"\r\n|\r|\n")


def _lines(stream) -> Iterator[bytes]:
    """The lines of an SSE reply as they arrive, without their ends, split on bytes.

    Each read returns what has arrived, so a line is yielded as soon as it ends rather
    than when the stream does. Neither CR nor LF occurs inside a multi-byte UTF-8
    sequence, so a split here never cuts a character; a read can, which is why nothing
    is decoded until its event is whole (``_events``). A CR that ends one read and an
    LF that starts the next are one line end, not two."""
    parts: list[bytes] = []
    after_cr = False
    while chunk := stream.read1(_CHUNK):
        if after_cr and chunk.startswith(b"\n"):
            chunk = chunk[1:]
        start = 0
        for end in _LINE_END.finditer(chunk):
            yield b"".join([*parts, chunk[start:end.start()]])
            parts, start = [], end.end()
        parts.append(chunk[start:])
        after_cr = chunk.endswith(b"\r")
    if any(parts):
        yield b"".join(parts)


def _events(stream) -> Iterator[str]:
    """The data of each event in an SSE reply, by the spec's rules rather than by line,
    each decoded once it is whole: an event ends at a blank line, its ``data`` lines
    join with a newline, the space after the colon is optional, and ``event:``, ``id:``
    and ``:`` comments are not data. A last event with no blank line after it still
    counts when the stream ends."""
    data: list[bytes] = []
    for line in _lines(stream):
        if not line:
            if data:
                yield b"\n".join(data).decode("utf-8", "replace")
                data = []
        elif line.startswith(b"data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(b" ") else value)
    if data:
        yield b"\n".join(data).decode("utf-8", "replace")


def _is_request_id(value) -> bool:
    """Whether ``value`` can be a request's id: a string or an integer, and not a bool,
    which is an int to Python and not to JSON-RPC."""
    return isinstance(value, str) or (isinstance(value, int)
                                      and not isinstance(value, bool))


def _response(bodies: Iterable[str], request_id: int, on_request) -> dict:
    """The JSON-RPC response to request ``request_id`` among ``bodies``, one message
    each, handing each request a server sends to ``on_request`` as it comes, before the
    next body is read. It returns at the response and reads no further.

    THE RESPONSE, NOT THE FIRST MESSAGE. A stream may carry other messages ahead of it
    — a progress or log notification, a request from the server — and the response is
    the one with no ``method`` whose id is ours (or null, which is how a server answers
    a request it could not parse). Taking the first event read a notification as the
    reply, and recorded a call that ran as one with no result. A request has a
    ``method`` and an id that is a string or an integer; anything else, a response to
    another id included, is passed over."""
    problem = None
    for body in bodies:
        if not body.strip():
            continue
        try:
            message = json.loads(body)
        except ValueError:
            # The failure mode worth naming: a bad bearer comes back as a bare line of
            # prose with no JSON at all, so the parse error is the symptom and the
            # prose is the diagnosis. Carry the prose.
            problem = problem or f"not an MCP reply — the server said: {body[:200]!r}"
            continue
        if not isinstance(message, dict):
            # Valid JSON, wrong shape — a bare string, a list, null. Everything
            # downstream assumes an object. An AttributeError there once escaped
            # `reconcile` (which catches only DiscoveryError) and killed the reconcile
            # thread with its healthcheck still green, and once escaped `_run` past the
            # outcome record, so an approved call that answered oddly left no row.
            problem = problem or (f"not an MCP reply — expected a JSON object, the "
                                  f"server sent {type(message).__name__}: {body[:200]!r}")
            continue
        ident = message.get("id")
        if "method" not in message:
            if ident is None or (not isinstance(ident, bool) and ident == request_id):
                return message
        elif _is_request_id(ident):
            on_request(message)
    raise DiscoveryError(problem or "the reply carried no response to the request")


def _unanswered(_request: dict) -> None:
    """A request in a reply read whole: the server had ended the reply by the time it
    was read, so there is nothing left to answer it for."""


def read_reply(stream, content_type: str | None, request_id: int, on_request) -> dict:
    """The JSON-RPC response to request ``request_id`` out of a server's reply, given
    as a binary stream and its Content-Type. No network of its own: ``on_request`` is
    handed each request the server sends ahead of the response, and answers it.

    ``text/event-stream`` (with any parameters) is read event by event and left at the
    response, without waiting for the stream to end: a stateful server sends a request
    on a call's own stream and holds the call until it is answered, and may keep the
    stream open after the response (NOTES.md, "A stateful MCP server talks back on a
    call's stream"). Any other type is read whole (``response_message``)."""
    media = (content_type or "").partition(";")[0].strip().lower()
    if media == "text/event-stream":
        return _response(_events(stream), request_id, on_request)
    return response_message(stream.read(), request_id)


def response_message(raw: bytes, request_id: int) -> dict:
    """The JSON-RPC response to request ``request_id`` out of a reply read whole, SSE
    or plain JSON, by ``_response``'s rules.

    Both shapes are accepted because a reply not labelled `text/event-stream` may still
    be framed as SSE, and a bare JSON body is legal for this transport: a reader that
    silently failed on either would be worse than one that kept working. A request in
    such a body goes unanswered, since the server ended the reply before it was read."""
    if re.search(rb"(?m)^data:", raw):
        return _response(_events(io.BytesIO(raw)), request_id, _unanswered)
    body = raw.decode("utf-8", "replace").strip()
    if not body:
        raise DiscoveryError("empty reply — not an MCP response")
    return _response([body], request_id, _unanswered)


def parse_tools(message: dict) -> list[dict]:
    """The tool list out of the response to ``tools/list``, as ``post`` returns it."""
    if "error" in message:
        raise DiscoveryError(f"server returned an error: {message['error']}")
    result = message.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        # `{"result": null}`, `{"result": []}`, a `tools` that is not a list: none of
        # them is "this server exposes nothing", which is the claim an empty list
        # makes and which `reconcile` then pushes as the inventory.
        raise DiscoveryError("the reply carried no tool list")
    return tools


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so a 3xx surfaces as the HTTPError it is.

    urllib's default re-issues a redirected POST as a GET to the `Location`, with every
    header but the content ones — `Authorization` included — and resolves that host
    itself, so ``_placed`` never sees where the credential goes. Two cases make that
    matter, neither needing a compromised server: a routine redirect (a trailing-slash
    301 to `http://mcp-github:8082/mcp/`) re-resolves the NAME, reopening the DNS
    question ``_placed`` closes; and a followed GET can reach anything on the gateway's
    three networks, the control plane's tool bridge included, with the start of the
    answer handed back to the agent in an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: The opener every dial goes through: urlopen's default handlers, with redirects
#: refused and proxies off. Compose sets no proxy env on the gateway, but a Docker
#: client's `proxies` config injects one into every container, and a dial through a
#: proxy is not a dial to the address ``_placed`` chose.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _body(message: dict) -> bytes:
    """THE ONE PLACE a JSON-RPC message for an upstream server becomes request bytes.

    Every message the gateway sends upstream (``tools/list``, ``tools/call``, any
    handshake message, and each answer to a server's request on a call's stream) is
    encoded here before anything for it is dialled, and a call's
    own message before any session handshake its path performs: a call too deep to
    encode then opens nothing and dials nothing, and is a ``DiscoveryError`` (kind
    ``transport-error``), recorded as never sent rather than as a call that may have
    run.

    RecursionError only, a RuntimeError rather than a ValueError: the messages are
    built from parsed JSON, so a TypeError or ValueError here would be a gateway bug,
    left to ``execute._run``'s catch-all."""
    try:
        return json.dumps(message).encode()
    except RecursionError as exc:
        raise DiscoveryError(
            f"the message was nested too deeply to encode, so it was never sent, and "
            f"sending the same arguments again will fail the same way ({exc})") from exc


def _next_id() -> int:
    """A request id no earlier request in this process carried (``_IDS``)."""
    with _IDS_LOCK:
        return next(_IDS)


def post(entry: dict, message: dict, timeout: float | None = None) -> dict:
    """One MCP request to the server a roster entry names, returning the JSON-RPC
    response to it.

    THE ONLY PLACE ANYTHING DIALS A SERVER, which is what makes the properties below
    hold for enumeration and execution alike rather than for whichever one was written
    first. Both send the same headers, resolve the same derived secret path and get the
    same errors turned into `DiscoveryError`; the difference between them is the
    message and the timeout, which is exactly what the two arguments are. ``_dial`` is
    the dial itself, and nothing but this function and the answers it sends uses it.

    The id is stamped here, on a copy of ``message``, and is new for every request
    (``_IDS``), so no caller can send a constant or a reused one; the response is the
    one that carries it (``read_reply``). A request the server sends on the call's
    stream is answered as it arrives (``_answer``), at the address and with the
    headers the call went with.

    The URL is built from what is checked here and from nothing else (``_dial``).
    ``check_name`` and ``check_endpoint`` run on every call, so neither the name nor
    the endpoint can move the host — checked at this choke point rather than trusted
    from the roster that supplied them.
    The address it dials is decided here too, before the credential is read
    (``_placed``), and once: an answer goes to the same address, not resolved again.
    The message is encoded before anything is resolved or dialled (``_body``), and one
    too deep to encode is a ``DiscoveryError``, never sent."""
    request_id = _next_id()
    data = _body({**message, "id": request_id})
    server = entry.get("server")
    port, path = check_endpoint(entry.get("endpoint"))
    address = _placed(server, port)
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               # What urllib would have sent for the name, so the server sees the
               # request it always saw.
               "Host": f"{server}:{port}"}
    headers.update(auth_header(entry.get("auth") or {}, read_secret(server),
                               secret_path(server)))
    # Resolved once, because the timeout REFUSAL names it: a record saying "no answer
    # within None" would be worse than one that said nothing.
    deadline = TIMEOUT if timeout is None else timeout
    target = (address, port, path)
    with _dial(server, target, data, headers, deadline) as response:
        return read_reply(response, response.headers.get("Content-Type"), request_id,
                          lambda request: _answer(server, target, headers, request))


def _answer_to(request: dict) -> dict:
    """What a server's request on a call's stream is answered with: an empty result for
    ``ping``, as the spec asks, and ``_NO_SUCH_METHOD`` for anything else, quoting
    nothing of the server's. Each carries its request's id.

    Refused because the gateway declares no client capability a server could be asking
    it to use, and it must not forward a server's request to the agent instead: the
    agent is the party being governed, and a server reaching its client would ask it
    for input outside policy and the audit (root DESIGN.md, "Elicitation routes to the
    wrong human")."""
    if request["method"] == "ping":
        return {"jsonrpc": "2.0", "id": request["id"], "result": {}}
    return {"jsonrpc": "2.0", "id": request["id"], "error": dict(_NO_SUCH_METHOD)}


def _answer(server: str, target: tuple, headers: dict, request: dict) -> None:
    """Answer a server's request while its call waits on the answer: a server holds a
    call open until a request it sent on the call's stream is answered (NOTES.md, "A
    stateful MCP server talks back on a call's stream").

    To ``target`` and with ``headers``, the call's own, so it goes to the address
    ``_placed`` chose for the call through the same checked dial, carrying what the
    call carried. Its status is checked and its body left unread. A refusal is a line
    in the log, the method escaped and capped, since it is the server's text. A failed
    answer is a line as well and does not end the call: the call was sent, so its
    reply is still what the record needs, and a server left waiting ends as a timeout,
    the honest status for a call that may have run."""
    reply = _answer_to(request)
    method = outcomes.printable(request["method"])[:_METHOD_SHOWN]
    if "error" in reply:
        print(f"tool-gateway: {outcomes.printable(server)}: refused its {method} "
              f"request on a call's stream; nothing a server asks reaches the agent",
              flush=True)
    try:
        with _dial(server, target, _body(reply), headers, TIMEOUT):
            pass
    except DiscoveryError as exc:
        print(f"tool-gateway: {outcomes.printable(server)}: the answer to its {method} "
              f"request was not delivered — "
              f"{outcomes.printable(exc)[:_FAILURE_SHOWN]}", flush=True)


@contextlib.contextmanager
def _dial(server: str, target: tuple[str, int, str], data: bytes, headers: dict,
          deadline: float):
    """``data`` POSTed to ``target``, an address, port and path ``post`` checked,
    through ``_OPENER``, the response yielded open, and every way that can fail, its
    reading included, turned into a ``DiscoveryError``. ``post``'s alone, for its
    request and the answers it sends."""
    address, port, path = target
    # The URL is assembled here and nowhere else, and the scheme is a literal, so there
    # is no S310 to suppress: the address is one `_placed` has put on mcp-net, and the
    # path one `check_endpoint` let through.
    host = f"[{address}]" if ":" in address else address
    request = urllib.request.Request(f"http://{host}:{port}{path}", data=data,
                                     headers=headers)
    try:
        with _OPENER.open(request, timeout=deadline) as response:
            yield response
    except urllib.error.HTTPError as exc:
        # The reason phrase and the Location are the SERVER'S text, and this message is
        # printed to the log an operator reads: repr, so a CR or an escape sequence
        # cannot forge a line there, and the Location capped and last, so a long one
        # cannot push the explanation out of the 200 characters inventory keeps.
        if 300 <= exc.code < 400:
            location = exc.headers.get("Location")
            where = (f"to {location[:120]!r}" if location is not None
                     else "with no Location")
            raise DiscoveryError(
                f"HTTP {exc.code} from {server}, a redirect, not followed: its "
                f"credential goes only to the address it was dialled on; it pointed "
                f"{where}") from exc
        raise DiscoveryError(f"HTTP {exc.code} from {server} — {exc.reason!r}") from exc
    except (urllib.error.URLError, OSError) as exc:
        # TIMEOUT SEPARATED FIRST, because it is the only one of these that leaves the
        # call's fate unknown: the request was sent and the answer never came, so an
        # approved write may well have landed. urlopen surfaces it either bare (it is
        # an OSError subclass) or wrapped in URLError, and both spellings occur — the
        # wrapped one on a connect timeout, the bare one on a read timeout.
        timed_out = isinstance(exc, TimeoutError) or isinstance(
            getattr(exc, "reason", None), TimeoutError)
        if timed_out:
            raise DiscoveryError(f"no answer from {server} within {deadline}s",
                                 kind="timeout") from exc
        raise DiscoveryError(f"unreachable: {exc}") from exc
    except http.client.HTTPException as exc:
        # A reply that is not HTTP (`BadStatusLine` and kin), which is not an OSError,
        # so it escaped `post` and ended the whole enumeration tick, every other
        # server's included. AFTER the OSError clause, so `RemoteDisconnected`, which
        # is both, still reads as unreachable.
        raise DiscoveryError(f"not an HTTP reply from {server}: {exc!r}") from exc


def _placed(server: str, port: int) -> str:
    """The address to dial for ``server``: resolved ONCE, and only if it is on mcp-net.

    Once, and then dialled as an address, because checking one lookup and letting
    urllib do a second to connect would check one answer and use another — and a name
    on two networks can answer differently each time. The first answer on mcp-net
    wins; one elsewhere is skipped rather than fatal, since sharing a name with a
    container on another network is not this server's fault. None at all refuses the
    call before a credential is read."""
    name = check_name(server)
    try:
        infos = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DiscoveryError(f"unreachable: {server} does not resolve ({exc})") from exc
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    for address in addresses:
        if ipaddress.ip_address(address) in MCP_NET:
            return address
    raise DiscoveryError(
        f"refusing to dial {server}: it resolves to {', '.join(addresses)}, none of "
        f"it on mcp-net ({MCP_NET}), and its credential goes nowhere else")


def list_tools(entry: dict) -> tuple[list[dict], dict[str, int]]:
    """The tools a server exposes, TRIMMED, by asking it, and how many of its entries
    were malformed: ``{"entries": n, "dropped": m}``.

    One POST, no handshake — see the module docstring for why that is enough, and for
    where it was measured.

    TWO READERS, one trim. What comes back is kept to the union of what the reconcile
    report and the agent-facing surface actually read, and nothing wider: the report
    wants the name and the read-only claim, and `surface.curate` wants the name, the
    description and the schema, because an agent cannot call a tool whose arguments it
    cannot see. Trimming to only the first pair is what this did first, and it made the
    served tool list uncallable."""
    response = post(entry, {"jsonrpc": "2.0", "method": "tools/list", "params": {}})
    # Trimmed HERE, at the point of reading, rather than downstream. A real reply
    # carries inline base64 `icons` as well — measured, see the byte split
    # `make mcp-tools` prints — and nothing reads them. The narrowing that crosses to
    # the CONTROL PLANE is narrower still and belongs to the push, not here: see
    # ``_as_claim``.
    #
    # A missing schema becomes the empty object rather than being dropped. `inputSchema`
    # is required of an MCP tool, so a server that omits one is malformed — but the
    # honest reading of a malformed entry is "takes no arguments we know of", and
    # serving it lets policy decide the tool instead of this parser silently deleting it.
    #
    # An entry with no usable name is dropped: there is nothing to rule, serve or list
    # it by. Annotations that are not an object read as no read-only claim, the
    # conservative reading, and the tool is kept for the schema's reason above. Both
    # are counted, so the report says the server sent something this could not read.
    # Names are not held to TOOL_RE here: the control plane drops and counts those.
    tools, malformed, dropped = [], 0, 0
    for tool in parse_tools(response):
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name:
            malformed += 1
            dropped += 1
            continue
        # Absent and null both mean no annotations, as they did before this check.
        annotations = tool.get("annotations")
        if annotations is None:
            annotations = {}
        elif not isinstance(annotations, dict):
            malformed += 1
            annotations = {}
        tools.append({"name": name,
                      "description": str(tool.get("description") or ""),
                      "inputSchema": (tool.get("inputSchema")
                                      if isinstance(tool.get("inputSchema"), dict)
                                      else {"type": "object"}),
                      "annotations": {"readOnlyHint":
                                      bool(annotations.get("readOnlyHint"))}})
    return tools, {"entries": malformed, "dropped": dropped}


def reconcile(entry: dict) -> dict:
    """One roster entry against the server it names.

    The two directions are separate findings and each names a different operator
    action, which is why they are not one "mismatch" count:

      ruled_but_absent  — a rule decides for a tool the server does not expose. A
                          typo, or a rule outliving a server upgrade. It grants
                          nothing and never will; it is dead configuration that reads
                          as live policy in the UI.
      exposed_but_unruled — a real tool with no rule. DENIED, correctly and by
                          default, and worth surfacing because the deny is silent:
                          from the agent's side it is indistinguishable from a tool
                          that was never there."""
    server = entry["server"]
    rules = {rule["tool"]: rule["action"] for rule in entry.get("tools", [])}
    try:
        tools, malformed = list_tools(entry)
    except DiscoveryError as exc:
        # `tools` stays absent rather than empty. An empty list is a CLAIM that the
        # server exposes nothing, and pushing that on a failed dial would erase a good
        # inventory because a credential was briefly missing.
        return {"server": server, "status": str(exc), "rules": len(rules)}
    exposed = sorted({tool["name"] for tool in tools})
    return {"server": server,
            "status": "ok",
            "rules": len(rules),
            "exposed": len(exposed),
            "tools": tools,
            "malformed": malformed,
            "ruled_but_absent": sorted(set(rules) - set(exposed)),
            "exposed_but_unruled": sorted(set(exposed) - set(rules))}


def _count(n: int, noun: str) -> str:
    """``n`` of ``noun``, pluralised. Only regular nouns are passed to it."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _names(names: list) -> str:
    """Tool names from a server or the roster, each escaped before the join, so one
    that is not a string prints rather than failing the join."""
    return ", ".join(outcomes.printable(name) for name in names)


def format_report(results: list[dict]) -> list[str]:
    """The report as lines, so the caller owns where it goes.

    Stdout, deliberately, even now that the gateway has an audit path
    (``outcomes.py``): this grants nothing and records no capability, so the audit
    invariant — which exists to record capability GRANTED — does not reach it, and
    the outcome stream records how CALLS ended, which a roster report is not. It does
    use that module's stdout guard: every value here that a server or the roster
    wrote goes through ``outcomes.printable``, so a control character in one cannot
    rewrite the line, and a lone surrogate cannot fail the print and with it the
    inventory push that follows."""
    if not results:
        return ["tool-gateway: no enabled servers on the roster — nothing to reconcile"]
    lines = []
    for result in results:
        server = outcomes.printable(result["server"])
        if result["status"] != "ok":
            lines.append(f"tool-gateway: {server}: NOT ENUMERATED — "
                         f"{outcomes.printable(result['status'])}")
            continue
        # Counted nouns, because both of these are routinely 1 — a server with one
        # tool, and the first rule anyone writes. "1 rules" in the line an operator
        # reads to check their own change landed is small, and it is exactly the sort
        # of small that makes the rest of the sentence look unreviewed.
        lines.append(f"tool-gateway: {server}: {_count(result['exposed'], 'tool')} "
                     f"exposed, {_count(result['rules'], 'rule')}")
        malformed = result.get("malformed") or {}
        if malformed.get("entries"):
            # Counts only: an entry dropped for its name has no name to print.
            noun = "entry" if malformed["entries"] == 1 else "entries"
            lines.append(f"tool-gateway:   {malformed['entries']} malformed tool {noun} "
                         f"in the reply, {malformed.get('dropped', 0)} of them dropped")
        if result["ruled_but_absent"]:
            lines.append(f"tool-gateway:   rules for tools the server does not expose "
                         f"(dead policy): {_names(result['ruled_but_absent'])}")
        if result["exposed_but_unruled"]:
            lines.append(f"tool-gateway:   tools with no rule (denied by default): "
                         f"{_names(result['exposed_but_unruled'])}")
        if not result["ruled_but_absent"] and not result["exposed_but_unruled"]:
            lines.append("tool-gateway:   policy and server agree")
    return lines


def poll() -> tuple[list[dict] | None, str]:
    """The roster, or None and a reason.

    A reason is returned for no answer, a reply that is not HTTP, and a body cut
    short, that will not parse, or that parses to something other than a list. Any
    other exception is a bug, and is left to raise into ``_reconcile_forever``'s
    guard under its own class rather than read as an outage.

    Split from ``report`` because the two have different costs and therefore
    different cadences. This is one HTTP call to a sibling answering two indexed
    selects, cheap enough to run every few seconds; ``report`` dials every enabled
    server, which are the containers holding credentials. Polling the authority often
    and the servers rarely is what lets a registration show up in seconds without
    putting steady traffic on the servers.

    None means the CONTROL PLANE did not answer — not that a server is down. An
    unreachable server is a finding inside a successful report, because the roster
    arrived and the report is complete. Returned as a value rather than left for the
    caller to infer from the text: matching on a substring would stop working the day
    a message is reworded."""
    try:
        return fetch_roster(), ""
    except _CONTROL_FAILURES as exc:
        return None, f"tool-gateway: roster unavailable from {CONTROL_URL} — {exc}"


def roster_digest(roster: list[dict]) -> str:
    """What the caller compares to decide whether anything it acts on has changed.

    The WHOLE roster, not just the server names: a rule flipped from `ask` to `deny`
    changes the report without changing which servers are dialled, and an auth
    descriptor edit changes how they are dialled. Anything that can alter the report
    has to be able to trigger one.

    The serialization IS the digest rather than a hash of it. A roster is a handful of
    servers and their rules, so there is nothing to save by hashing, and no collision
    question to reason about in exchange."""
    return json.dumps(roster, sort_keys=True, separators=(",", ":"))


def reconcile_all(roster: list[dict]) -> list[dict]:
    """Dial every server on the roster and compare it with policy.

    The expensive half. Each entry is one request to a container holding a
    write-capable credential, which is why the caller runs this on change rather than
    on a short timer.

    Each server under its own guard, so one that raises something ``reconcile`` did
    not foresee reads NOT ENUMERATED, as a failed dial does, and the rest are still
    published and pushed. Broad because it backstops inputs nobody has listed, such as
    the RecursionError ``json.loads`` can raise on a deeply nested reply, at a depth
    that depends on the Python build. Never silent, since the exception's
    type is in the status every report prints and every push carries — and so is its
    message, which is why an input whose exception would quote a secret (a header
    value http.client refuses) is refused before the dial instead (``auth_header``).

    No ``rules`` count, unlike a failed dial's result: nothing reads one on a server
    that was not enumerated."""
    results = []
    for entry in roster:
        try:
            results.append(reconcile(entry))
        except Exception as exc:  # noqa: BLE001 — one server must not stop the round
            results.append({"server": entry.get("server"),
                            "status": f"gateway failed reconciling it "
                                      f"({type(exc).__name__}: {exc})"})
    return results


#: Keywords whose value is prose, dropped from a schema object before hashing, or every
#: upstream rewording would read as drift.
_PROSE = frozenset({"description", "title", "examples", "$comment"})
#: Keywords whose value is a map from a NAME to a subschema. The names are kept, since
#: they are the arguments; the values are schemas, so prose is dropped inside them.
#: `dependentRequired`, and draft-07 `dependencies` in its array form, map a name to a
#: list of names, and a name in a list passes through ``_structure`` unchanged.
_NAME_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions",
                        "dependentSchemas", "dependentRequired", "dependencies"})
#: Keywords whose value is one subschema or a list of them.
_SUBSCHEMAS = frozenset({"items", "prefixItems", "additionalItems", "contains",
                         "additionalProperties", "unevaluatedProperties",
                         "unevaluatedItems", "propertyNames", "not", "if", "then",
                         "else", "anyOf", "allOf", "oneOf"})
#: Keywords whose value is a set, so their order is not structure.
_UNORDERED = frozenset({"required", "enum", "type"})


#: The digest of a schema too deep to digest (``schema_digest``): hex-shaped, so the
#: control plane keeps it like any other, and the hash of no canonical JSON.
UNDIGESTIBLE = hashlib.sha256(b"undigestible schema").hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _structure(schema):
    """A schema with its prose dropped, for ``schema_digest``.

    Recurses only through the keywords above. Everything else is kept verbatim: `enum`,
    `const` and `default`, which are data, and every unknown or `x-` keyword, whose
    contents this cannot know to be prose. Erring toward keeping is erring toward
    reporting drift."""
    if isinstance(schema, list):
        return [_structure(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in _PROSE:
            continue
        if key in _NAME_MAPS and isinstance(value, dict):
            value = {name: _structure(sub) for name, sub in value.items()}
        elif key in _SUBSCHEMAS:
            value = _structure(value)
        if key in _UNORDERED and isinstance(value, list):
            value = [item for _, item in
                     sorted({_canonical(item): item for item in value}.items())]
        out[key] = value
    return out


def schema_digest(schema) -> str:
    """A SHA-256 of what a tool's arguments ARE: property names, types, enums, and
    which are required, without the descriptions around them.

    Fixed-length and server-text-free, which is what lets it cross to the control plane
    under ``_as_claim``'s contract: a moved digest says a tool's arguments changed, and
    the control plane audits that the way it audits a new tool.

    A schema too deep for ``_structure``, which recurses in Python, is hashed whole,
    prose included: for that tool a rewording then counts as drift, which is noise and
    never silence. Only one too deep for ``json.dumps`` as well gets ``UNDIGESTIBLE``.
    None of the three raises, since raising would end the whole push and every other
    server's audit rows with it. A tool moving between them reads as its arguments
    changing, and one never keeps its last digest, which would hide the move."""
    try:
        text = _canonical(_structure(schema))
    except RecursionError:
        try:
            text = _canonical(schema)
        except RecursionError:
            return UNDIGESTIBLE
    return hashlib.sha256(text.encode()).hexdigest()


def _as_claim(tools: list[dict]) -> list[dict]:
    """What a person choosing a rule reads: the name, the server's read-only claim,
    and a digest of the arguments.

    Narrowed at the PUSH rather than at the read, because this narrowing is about the
    reader. A description and a schema are third-party text an operator does not need
    in order to pick a tool by name, and the control plane holds this in memory to
    render a picker — so every field that crosses is one the crown jewel then keeps.
    The agent-facing surface reads the same tools and needs the schema, which is why
    the two trims are different sizes and neither can be the other's. The digest is
    the schema reduced to a fixed-length hash, so it carries none of the schema's
    text."""
    return [{"name": tool["name"], "annotations": tool.get("annotations") or {},
             "digest": schema_digest(tool.get("inputSchema") or {"type": "object"})}
            for tool in tools]


def push_inventory(results: list[dict]) -> str:
    """Report the observed surface to the control plane. Returns "" or a reason.

    The one thing this gateway WRITES anywhere, and it grants nothing: the control
    plane holds it in memory, shows it to an operator, and never consults it when
    deciding a call. Pushed because a pull is impossible — the control plane has no leg
    on this container's networks and must not be given one.

    A server that could not be enumerated is still sent, carrying its status and NO
    tool list. That is the difference between "this server exposes nothing" and "we
    could not ask", and an operator needs the second one said out loud — it is the
    state that otherwise surfaces as a 401 reading like a policy problem."""
    payload = {"servers": {r["server"]: ({"status": r["status"],
                                          "tools": _as_claim(r["tools"])}
                                         if "tools" in r else {"status": r["status"]})
                           for r in results}}
    request = urllib.request.Request(  # noqa: S310 - fixed http:// scheme, not user input
        f"{CONTROL_URL}/tool/inventory",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            answer = json.loads(response.read() or b"{}")
    except _CONTROL_FAILURES as exc:
        return f"tool-gateway: inventory not accepted by {CONTROL_URL} — {exc}"
    if not isinstance(answer, dict):
        return (f"tool-gateway: inventory not accepted by {CONTROL_URL} — "
                f"a {type(answer).__name__} where an object was expected")
    if not answer.get("ok"):
        return (f"tool-gateway: inventory refused — "
                f"{answer.get('detail') or 'no reason given'}")
    return ""
