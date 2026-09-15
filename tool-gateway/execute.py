#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Running a tool call, and asking permission first.

The executing half of the gateway. Every call the agent makes arrives here, and the
shape of the module is the shape of the rule: NOTHING RUNS BEFORE THE CONTROL PLANE
HAS ANSWERED. There is exactly one function that dials a server (`_run`), it is
reached from exactly two places, and both of them have an answer in hand — an `allow`
from `/tool/authorize`, or a successful claim on an ask a human approved.

THE ANSWER IS NEVER CACHED. A `deny` an operator sets that waits for a TTL is not a
deny, so every call asks, and the roster this reads for auth descriptors is the only
thing here that is polled (DESIGN.md, "The gateway pulls, and what it may cache is not
uniform"). The cost is one request to a sibling per tool call, against a tool call that
is about to cross the internet.

THREE ANSWERS, not two, and the third is what makes this different from the egress
proxy. `allow` runs; `deny` refuses; `ask` registers a question with the control plane
and answers the AGENT immediately with a pending id. Nothing is held open — not the
agent's call, not a control-plane worker — so the human's window is free to be an hour
and there is no caller to strand (DESIGN.md, "An `ask` answers immediately").

WHAT IS NOT COVERED YET, stated because it is a gap rather than a decision: the
response side. The gateway governs the REQUEST, and what steers an agent is the
third-party text that comes back — an allowed read-only tool is unaudited intake of
the same shape as WebSearch. DESIGN.md says that record belongs to the gateway's own
audit stream, ingested the way the proxy's is, and that stream does not exist. What
the control plane records today is the decision, not the payload and not the reply.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

import discovery
import surface

#: The shape of an approval id, checked before it is ever put in a URL.
#:
#: The id is the one thing on this path that comes from the AGENT, and it lands in a
#: request path on the crown-jewel bridge. A percent-encode alone would be enough to
#: keep `../` from traversing, but a bounded charset is the check that does not depend
#: on getting the encoding right — and the id is `uuid4().hex` in
#: ``control-plane/holds.py``, so nothing legitimate is excluded by saying so.
_APPROVAL_RE = re.compile(r"^[0-9a-f]{32}$")

#: Prose for the terminal states the control plane names but does not narrate. Keyed on
#: `status` rather than on the detail text, so a reworded message upstream cannot
#: silently stop matching — and anything absent here falls through to whatever the
#: control plane said, which is how the already-claimed case keeps its own sentence.
#:
#: No trailing full stop: the caller supplies one, and the reason each state is worth
#: distinguishing at all is that they name different next moves for a human reading the
#: transcript — one was refused, the other was never answered.
_TERMINAL_TEXT = {
    "denied": "A human refused this request",
    "expired": "This request expired before anyone answered it",
}

#: How long a tool call may take. Its own number, an order of magnitude above
#: ``discovery.TIMEOUT``, because the two wait for different things: enumeration is a
#: sibling answering from memory, and this is a server making a real internet request
#: through the egress proxy — which may itself be holding the request for a human.
CALL_TIMEOUT = float(os.environ.get("GATEWAY_CALL_TIMEOUT", "60"))

#: The control plane's tool bridge. Same address discovery dials, and by ADDRESS for
#: the same reason: the control plane is multi-homed and only its tool-authorize-net
#: leg may be spoken to.
CONTROL_URL = discovery.CONTROL_URL

#: How long the control plane gets to answer a decision. SHORT, and deliberately not
#: ``CALL_TIMEOUT``: this is one sibling answering two indexed selects, and a governance
#: answer that is slow is a governance answer that is broken. A call whose decision
#: cannot be obtained is refused, so this bound is the one that keeps a wedged control
#: plane from wedging the agent instead of failing it closed.
DECIDE_TIMEOUT = float(os.environ.get("GATEWAY_DECIDE_TIMEOUT", "10"))


def text_result(text: str, is_error: bool = False) -> dict:
    """An MCP ``CallToolResult`` carrying one block of text.

    Everything this module produces on its own is one of these. A refusal, a pending
    notice and a transport failure are all RESULTS rather than JSON-RPC errors, and
    that is the choice DESIGN.md makes for the pending case generalised to the rest: an
    agent can act on a result — read the reason, do something else, come back — where a
    protocol error is something it records as a failed call and carries no reason it
    can use."""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _ask_control(path: str, payload: dict) -> dict:
    """POST to the control plane's tool bridge and return its answer.

    Raises ``DiscoveryError`` when there is no answer to be had. Reusing that type
    rather than adding a second one: every caller here does the same thing with it —
    refuse the call and say why — and a second exception would be two names for one
    outcome. Its message is fit to print, which is the contract that matters.

    A NON-200 IS STILL AN ANSWER on this bridge. ``/tool/authorize`` always returns 200
    because a policy question has a policy answer, but the claim endpoint uses 404 and
    409 to distinguish refusals the gateway must tell apart, so the body is read on
    every status and the status itself decides nothing here."""
    request = urllib.request.Request(  # noqa: S310 - fixed http:// scheme, not user input
        f"{CONTROL_URL}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=DECIDE_TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except ValueError as parse_failure:
            raise discovery.DiscoveryError(
                f"HTTP {exc.code} from the control plane with no readable body"
            ) from parse_failure
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise discovery.DiscoveryError(
            f"the control plane did not answer ({exc})") from exc


def parse_call(raw: str) -> dict:
    """The ``CallToolResult`` out of a server's reply, SSE or plain JSON.

    Both shapes for the reason ``discovery.parse_tools`` accepts both: only one of them
    is measured, and a version bump that switched must not silently turn every tool
    call into a parse failure.

    A JSON-RPC ERROR IS NOT A TOOL FAILURE and is not passed off as one. The protocol
    distinguishes "the call ran and the tool reports a problem" (a result with
    ``isError``) from "the request was rejected" (an error object), and flattening them
    would tell an agent its arguments were wrong when the server was unreachable, or
    the reverse. It becomes an error-flagged result here because that is the only shape
    a tool call can answer in, but the TEXT says which it was."""
    payloads = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
    body = payloads[0] if payloads else raw.strip()
    if not body:
        raise discovery.DiscoveryError("empty reply — not an MCP response")
    try:
        message = json.loads(body)
    except ValueError as exc:
        raise discovery.DiscoveryError(
            f"not an MCP reply — the server said: {body[:200]!r}") from exc
    if "error" in message:
        return text_result(f"the server rejected the call: {message['error']}",
                           is_error=True)
    result = message.get("result")
    if not isinstance(result, dict):
        raise discovery.DiscoveryError("the reply carried no result")
    return result


def _run(server: str, tool: str, arguments: object) -> dict:
    """Dial the server and make the call. THE ONLY SIDE EFFECT IN THIS FILE.

    Deliberately reachable from two places and no others, both of which hold a decision
    — ``call`` on an `allow`, and ``resume`` on a claim the control plane granted. It
    takes no policy argument and performs no check of its own, which is the point: a
    function that decided as well as ran would have two reasons to be called, and one
    of them would eventually be wrong.

    The ARGUMENTS are the caller's to get right. On the resumption path they are the
    ones the human read, handed back by the claim rather than replayed from anything
    the agent sent."""
    entry = surface.server_entry(server)
    if entry is None:
        # Not a lookup miss. The roster stopped naming this server, which is an
        # operator disabling or revoking it — and the control plane will have said the
        # same thing already, so this is the second of two refusals rather than the
        # only one.
        return text_result(
            f"{server!r} is not on the gateway's current roster, so it cannot be "
            f"dialled. It was disabled, revoked, or never registered.", is_error=True)
    try:
        raw = discovery.post(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments if arguments else {}}},
            entry.get("auth") or {}, timeout=CALL_TIMEOUT)
        return parse_call(raw)
    except discovery.DiscoveryError as exc:
        # A failure to REACH the server, told apart from a failure reported BY it. The
        # distinction matters to whoever reads the transcript: one is an infrastructure
        # problem and the other is about the call.
        return text_result(f"could not call {tool} on {server}: {exc}", is_error=True)


def call(name: str, arguments: object, client: str | None) -> dict:
    """Decide and then run one tool call. The whole agent-facing path in one function.

    ``name`` is the flattened name the agent was shown. It is resolved back to a
    (server, tool) pair HERE, and the pair — never the flattened string — is what the
    control plane is asked about, so the rule that is read is the rule an operator
    wrote against that server's tool.

    A name that does not resolve is refused without asking anything. That is not an
    optimisation: ``split_exposed`` returning None means the string is not something
    this gateway can have produced, so there is no pair to decide about, and inventing
    one would be the gateway choosing which rule applies.

    The reason text from the control plane is passed through to the agent. It is
    written for a human and names configuration — that a server is disabled, that no
    rule exists — which tells the agent a little about policy shape. Worth it: a
    refusal an agent cannot understand is one it retries, and everything named is
    already discoverable by reading the tool list it was served."""
    if name == surface.RESUME_TOOL:
        # Routed before the split, because a native tool has no server to decide
        # against. See NATIVE_TOOLS for why the category needs its own rule.
        return resume(arguments, client)
    pair = surface.split_exposed(name)
    if pair is None:
        return text_result(
            f"{name!r} is not a tool on this gateway. Names are "
            f"'<server>__<tool>' — call tools/list for the ones you may use.",
            is_error=True)
    server, tool = pair

    try:
        answer = _ask_control("/tool/authorize",
                              {"server": server, "tool": tool,
                               "args": arguments, "client": client})
    except discovery.DiscoveryError as exc:
        # FAIL CLOSED, and loudly. A gateway that ran a call because it could not ask
        # would be a default-allow wearing an outage as a disguise.
        return text_result(
            f"refused: the gateway could not reach governance to decide this call "
            f"({exc}). Nothing ran.", is_error=True)

    decision, why = answer.get("decision"), answer.get("reason") or "no reason given"
    if decision == "allow":
        return _run(server, tool, arguments)
    if decision == "ask":
        approval_id = answer.get("approval_id")
        # A RESULT, not an error, and the instruction travels inside it — where it
        # cannot be forgotten mid-session and cannot drift from the gateway that
        # emitted it.
        return text_result(
            f"Held for human approval: {tool} on {server}. Nothing has run.\n"
            f"approval_id: {approval_id}\n"
            f"Call {surface.RESUME_TOOL} with that id to finish this call. It is "
            f"waiting on a person, so do other work and come back rather than "
            f"retrying immediately; retrying this tool instead opens a second "
            f"question for the same human."
            + ("\nAn identical request was already waiting, and this joined it."
               if answer.get("joined") else ""))
    # Every other answer is a deny, INCLUDING one this code does not recognise. The
    # control plane already refuses that way; repeating it here means a bridge that
    # answered something new could not turn into a grant on the way through.
    return text_result(f"Denied: {why}. This will not succeed on retry.", is_error=True)


def resume(arguments: object, client: str | None) -> dict:
    """Finish a call that was held: claim the approval, then run what it released.

    THE CLAIM IS THE GRANT, and it is single-use. The control plane's conditional
    update is what makes exactly one resumption of one approval perform the side
    effect, so a duplicate resumption is answered as spent rather than run twice.

    The arguments come back FROM the claim and are the ones the human read. Nothing the
    agent sends here contributes to the call beyond the id, which is what closes the
    gap between what was approved and what runs."""
    approval_id = arguments.get("approval_id") if isinstance(arguments, dict) else None
    # STRIPPED before it is checked, and that is safe in a way a looser cleanup would
    # not be: the id is bounded hex, so removing surrounding whitespace cannot turn one
    # valid id into another and cannot make an invalid one valid. It is worth doing
    # because the failure it prevents is a real one — an id copied out of a pending
    # result arrives with a stray newline or trailing space often enough, and the cost
    # is an approval a human granted that can no longer be redeemed.
    if isinstance(approval_id, str):
        approval_id = approval_id.strip()
    if not isinstance(approval_id, str) or not _APPROVAL_RE.match(approval_id):
        # One message for "missing" and for "malformed", because the agent's next move
        # is the same either way and neither tells it anything about whether some other
        # id exists.
        return text_result(
            "resume_tool_call needs the approval_id from a pending result, copied "
            "verbatim.", is_error=True)

    try:
        answer = _ask_control(f"/tool/asks/{approval_id}/claim", {"client": client})
    except discovery.DiscoveryError as exc:
        return text_result(
            f"could not reach governance to claim {approval_id} ({exc}). Nothing ran, "
            f"and the approval is untouched — try again.", is_error=True)

    if not answer.get("ok"):
        # The control plane's `detail` is machine-shaped for the states it has no prose
        # for — "not claimable (denied)" — and prose for the one it does. This surface
        # is the one an agent reads, so the machine spellings are rendered here and the
        # rest is passed through; a gateway that echoed every internal string would be
        # making the control plane responsible for wording it cannot see the reader of.
        detail = (_TERMINAL_TEXT.get(answer.get("status"))
                  or answer.get("detail") or "not claimable")
        if answer.get("terminal"):
            # `denied`, `expired`, or already spent. Said plainly and finally, because
            # an agent that cannot tell a refusal from a delay retries one forever.
            return text_result(
                f"{detail}. This is final — {approval_id} will not become runnable, "
                f"so do not retry it. Call the tool again if the work is still "
                f"needed; that raises a new question for the human.", is_error=True)
        # STILL PENDING, and therefore not an error. Nobody has answered yet, nothing
        # has run, and the id is still good.
        return text_result(
            f"Still waiting on a human for {approval_id}. Nothing has run. Come back "
            f"with the same id later — do not call the tool again, which would raise "
            f"a second question for the same person.")

    # Claimed. From here the call is authorised and the arguments are the approved
    # ones, parsed from the canonical form the digest covers and the human was shown.
    try:
        approved_args = json.loads(answer.get("args_json") or "null")
    except ValueError:
        return text_result(
            f"the approved arguments for {approval_id} could not be read, so nothing "
            f"ran. The approval is spent; raise the call again.", is_error=True)
    return _run(answer["server"], answer["tool"], approved_args)
