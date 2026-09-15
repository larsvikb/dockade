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

import json
import os
import re
import urllib.error
import urllib.request

#: A DNS label, the same shape `policy._server_name_error` holds registration to. The
#: name is used as a hostname and as a filename, so both uses want exactly this.
_SERVER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

#: The control plane's TOOL bridge, by ADDRESS rather than by name — the one place
#: here that does not dial a name. The control plane is multi-homed and only its
#: tool-authorize-net leg may be spoken to; `control-plane` resolves to whichever leg
#: Docker's DNS feels like returning, and three of the four would be the wrong one.
#: The address is the only thing that names a LEG. This is the same reason compose
#: pins CONTROL_TOOL_BIND rather than letting that listener take a wildcard.
CONTROL_URL = os.environ.get("GATEWAY_CONTROL_URL", "http://172.27.0.2:8092")

#: Servers are dialled BY NAME, and that is the identity per-tool policy is keyed on
#: (DESIGN.md, "Per-server identity has two different answers"). A server is
#: single-homed on mcp-net, so its name resolves to the one leg it has — the
#: ambiguity that forces an address above does not exist here. The gateway needs no
#: address for a server and is deliberately never given one.
#:
#: Port and path are one convention rather than per-server config, matching the
#: Makefile's MCP_PORT/MCP_PATH: 8082 and /mcp are what the first server listens on,
#: and a second server that differs makes these a lookup. Two constants beat a
#: lookup until that server exists.
MCP_PORT = int(os.environ.get("GATEWAY_MCP_PORT", "8082"))
MCP_PATH = os.environ.get("GATEWAY_MCP_PATH", "/mcp")

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
    see next to the rest."""


def fetch_roster() -> list[dict]:
    """The enabled servers and their rules, from the control plane's tool bridge.

    Pulled, never pushed. The control plane has no leg on this container's networks
    and must not be given one — a push would mean the crown jewel dialling the
    agent-facing service, which is the lateral edge the bind guard exists to
    prevent."""
    request = urllib.request.Request(  # noqa: S310 - fixed http:// scheme, not user input
        f"{CONTROL_URL}/tool/roster", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        return json.loads(response.read())


def check_name(server: str) -> str:
    """The server name, or a refusal — validated HERE and not only at registration.

    The control plane already holds registration to a DNS label
    (``policy._server_name_error``), and this checks it again because the two are
    different processes and this one uses the string to build a FILESYSTEM PATH and a
    URL. Trusting a peer's validation for that is the shape of bug where the peer is
    later given a second write path; the check costs a regex and removes the question.
    A DNS label cannot contain ``/``, ``.`` or a null, so a name that passes cannot
    leave SECRETS_DIR."""
    if not _SERVER_RE.match(server or ""):
        raise DiscoveryError(
            f"{server!r} is not a DNS label, so it names neither a container to dial "
            f"nor a file to read")
    return server


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
    permissions on the one hand, file contents on the other.

    Collapsing the third into the second is what this function did first, and it sent
    its reader looking for a file that was sitting right there."""
    path = secret_path(server)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"secret file {path} is unreadable: {exc}") from exc
    token = isinstance(data, dict) and data.get("token")
    if not token:
        raise DiscoveryError(
            f"{path} has no 'token'. The file holds secret material and nothing else — "
            f'{{"token": "...", "note": "..."}} — because the header name and template '
            f"are the DESCRIPTOR, and that is registered in the control plane rather "
            f"than kept beside the credential")
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
    return {name: template.replace("{secret}", secret)}


def parse_tools(raw: str) -> list[dict]:
    """The tool list out of an MCP reply, SSE or plain JSON.

    Both shapes are accepted because only one of them is measured. The server answers
    SSE today; a bare JSON body is legal for this transport and a version bump could
    start sending one, and a discovery report that silently emptied itself on that day
    would be worse than one that kept working."""
    payloads = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
    body = payloads[0] if payloads else raw.strip()
    if not body:
        raise DiscoveryError("empty reply — not an MCP response")
    try:
        message = json.loads(body)
    except ValueError as exc:
        # The failure mode worth naming: a bad bearer comes back as a bare line of
        # prose with no JSON at all, so the parse error is the symptom and the prose
        # is the diagnosis. Carry the prose.
        raise DiscoveryError(f"not an MCP reply — the server said: {body[:200]!r}") from exc
    if "error" in message:
        raise DiscoveryError(f"server returned an error: {message['error']}")
    return message.get("result", {}).get("tools", [])


def post(server: str, message: dict, auth: dict, timeout: float | None = None) -> str:
    """One MCP request to a server, returning its raw reply body.

    THE ONLY PLACE ANYTHING DIALS A SERVER, which is what makes the properties below
    hold for enumeration and execution alike rather than for whichever one was written
    first. Both send the same headers, resolve the same derived secret path and get the
    same errors turned into `DiscoveryError`; the difference between them is the
    message and the timeout, which is exactly what the two arguments are.

    The URL is assembled here and nowhere else. ``check_name`` runs on every call, so a
    server name that is not a DNS label cannot become a host, a path or a filename —
    checked at this choke point rather than trusted from the roster that supplied it."""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    headers.update(auth_header(auth, read_secret(server), secret_path(server)))
    # The scheme is a literal here, so there is no S310 to suppress: the only variable
    # part is a server NAME off the roster, which is what this is supposed to dial.
    request = urllib.request.Request(
        f"http://{check_name(server)}:{MCP_PORT}{MCP_PATH}",
        data=json.dumps(message).encode(), headers=headers)
    try:
        with urllib.request.urlopen(  # noqa: S310
                request, timeout=TIMEOUT if timeout is None else timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise DiscoveryError(f"HTTP {exc.code} from {server} — {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DiscoveryError(f"unreachable: {exc}") from exc


def list_tools(server: str, auth: dict) -> list[dict]:
    """The tools a server exposes, TRIMMED, by asking it.

    One POST, no handshake — see the module docstring for why that is enough, and for
    where it was measured.

    TWO READERS, one trim. What comes back is kept to the union of what the reconcile
    report and the agent-facing surface actually read, and nothing wider: the report
    wants the name and the read-only claim, and `surface.curate` wants the name, the
    description and the schema, because an agent cannot call a tool whose arguments it
    cannot see. Trimming to only the first pair is what this did first, and it made the
    served tool list uncallable."""
    raw = post(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                        "params": {}}, auth)
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
    return [{"name": tool["name"],
             "description": str(tool.get("description") or ""),
             "inputSchema": (tool.get("inputSchema")
                             if isinstance(tool.get("inputSchema"), dict)
                             else {"type": "object"}),
             "annotations": {"readOnlyHint":
                             bool((tool.get("annotations") or {}).get("readOnlyHint"))}}
            for tool in parse_tools(raw)
            if isinstance(tool, dict) and tool.get("name")]


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
        tools = list_tools(server, entry.get("auth") or {})
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
            "ruled_but_absent": sorted(set(rules) - set(exposed)),
            "exposed_but_unruled": sorted(set(exposed) - set(rules))}


def _count(n: int, noun: str) -> str:
    """``n`` of ``noun``, pluralised. Only regular nouns are passed to it."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def format_report(results: list[dict]) -> list[str]:
    """The report as lines, so the caller owns where it goes.

    Stdout today: this grants nothing and records no capability, so the audit
    invariant — which exists to record capability GRANTED — does not reach it. When
    the gateway has an audit path, this is a natural second sink rather than a
    rewrite."""
    if not results:
        return ["tool-gateway: no enabled servers on the roster — nothing to reconcile"]
    lines = []
    for result in results:
        server = result["server"]
        if result["status"] != "ok":
            lines.append(f"tool-gateway: {server}: NOT ENUMERATED — {result['status']}")
            continue
        # Counted nouns, because both of these are routinely 1 — a server with one
        # tool, and the first rule anyone writes. "1 rules" in the line an operator
        # reads to check their own change landed is small, and it is exactly the sort
        # of small that makes the rest of the sentence look unreviewed.
        lines.append(f"tool-gateway: {server}: {_count(result['exposed'], 'tool')} "
                     f"exposed, {_count(result['rules'], 'rule')}")
        if result["ruled_but_absent"]:
            lines.append(f"tool-gateway:   rules for tools the server does not expose "
                         f"(dead policy): {', '.join(result['ruled_but_absent'])}")
        if result["exposed_but_unruled"]:
            lines.append(f"tool-gateway:   tools with no rule (denied by default): "
                         f"{', '.join(result['exposed_but_unruled'])}")
        if not result["ruled_but_absent"] and not result["exposed_but_unruled"]:
            lines.append("tool-gateway:   policy and server agree")
    return lines


def poll() -> tuple[list[dict] | None, str]:
    """The roster, or None and a reason. Never raises.

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
    except (urllib.error.URLError, OSError, ValueError) as exc:
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
    on a short timer."""
    return [reconcile(entry) for entry in roster]


def _as_claim(tools: list[dict]) -> list[dict]:
    """What a person choosing a rule reads: the name, and the server's read-only claim.

    Narrowed at the PUSH rather than at the read, because this narrowing is about the
    reader. A description and a schema are third-party text an operator does not need
    in order to pick a tool by name, and the control plane holds this in memory to
    render a picker — so every field that crosses is one the crown jewel then keeps.
    The agent-facing surface reads the same tools and needs the schema, which is why
    the two trims are different sizes and neither can be the other's."""
    return [{"name": tool["name"], "annotations": tool.get("annotations") or {}}
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
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"tool-gateway: inventory not accepted by {CONTROL_URL} — {exc}"
    if not answer.get("ok"):
        return (f"tool-gateway: inventory refused — "
                f"{answer.get('detail') or 'no reason given'}")
    return ""
