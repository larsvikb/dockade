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

HOW IT ENDED IS RECORDED HERE, and only here. The control plane audits the decision,
and structurally cannot audit the outcome: its claim row is written before the call, so
by the time one succeeds or fails the authority has already answered. Every path that
dials a server goes through `_run`, which writes to `outcomes.py`'s stream — so an
approved call that failed upstream is a row rather than a silence.

WHAT IS STILL NOT COVERED, stated because it is a gap rather than a decision: the
response CONTENT. The gateway governs the request and now records the fate of the
reply, but not what the reply carried — and an allowed read-only tool is unaudited
intake of the same shape as WebSearch. DESIGN.md wants size and hash at minimum; the
stream those belong in now exists, which is what made this the cheaper half.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

import discovery
import outcomes
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

#: The longest ``resume_tool_call`` will hold its own call waiting for a human, and
#: how often it re-asks while it waits.
#:
#: SIZED FOR THE STRICTEST CLIENT WE KNOW OF, not for the one in front of us. A wait
#: is just a tool call that takes a while, which is the only mechanism every MCP
#: client already has — no backgrounding, no server-initiated request, no
#: notification, nothing to negotiate. What bounds it is the tightest per-request
#: timer any client applies, and the lowest documented floor is 60 s (Claude Code's
#: first-byte timer, which cannot be configured below that; NOTES.md). Forty-five
#: leaves room for the round trip inside it and needs no client configuration
#: anywhere. A harness known to tolerate more can be given more — that is what the
#: env var is for — but the DEFAULT has to work on a client nobody can configure.
#:
#: This deliberately stays under Claude Code's two-minute backgrounding threshold,
#: so that feature never engages. It is a better mechanism and it exists in exactly
#: one harness; leaning on it would buy latency at the cost of the property this
#: design is for.
#:
#: Nothing about governance depends on any of it. The approval row is the authority,
#: the wait is latency, and an expired wait returns precisely what a zero wait
#: returns today.
MAX_RESUME_WAIT = float(os.environ.get("GATEWAY_MAX_RESUME_WAIT", "45"))
#: Re-ask cadence while waiting. A pending claim is a read that changes nothing (the
#: control plane answers "still pending" without touching the row), so this is a
#: cheap indexed select against a sibling — a second is far below what a human's
#: click latency makes worth optimising.
RESUME_POLL_INTERVAL = float(os.environ.get("GATEWAY_RESUME_POLL_INTERVAL", "1"))


def text_result(text: str, is_error: bool = False) -> dict:
    """An MCP ``CallToolResult`` carrying one block of text.

    Everything this module produces on its own is one of these. A refusal, a pending
    notice and a transport failure are all RESULTS rather than JSON-RPC errors, and
    that is the choice DESIGN.md makes for the pending case generalised to the rest: an
    agent can act on a result — read the reason, do something else, come back — where a
    protocol error is something it records as a failed call and carries no reason it
    can use."""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _first_text(result: dict) -> str | None:
    """The first text block of a ``CallToolResult``, for the audit's reason line.

    Defensive about the shape on purpose: this reads a THIRD PARTY's reply on the
    failure path, where the reply is least likely to be well formed, and a record that
    raised while describing an error would lose both the reason and the row. A result
    whose content is absent, empty, or not text yields None — the status still says
    what happened, and an absent reason is honest where a stringified dict would be
    noise."""
    for block in result.get("content") or ():
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return block["text"]
    return None


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


def parse_call(raw: str) -> tuple[dict, str]:
    """The ``CallToolResult`` out of a server's reply, SSE or plain JSON, WITH the
    audit status that says which layer answered.

    The status is returned rather than derived afterwards because it cannot be derived
    afterwards: a JSON-RPC rejection and a tool that ran and failed both leave here as
    an error-flagged result — they have to, that being the only shape a tool call can
    answer in — and by then the two are indistinguishable. The distinction is
    load-bearing for the record: one says the call never ran, the other says it ran and
    the third party refused it.

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
    if not isinstance(message, dict):
        # Valid JSON that is not a JSON-RPC message: a bare string, a list, null. The
        # membership test and `.get` below assume an object, and without this they
        # raised TypeError/AttributeError out of `_run` — past the outcome record, so
        # an approved call that ran and answered oddly left no row at all.
        raise discovery.DiscoveryError(
            f"not an MCP reply — expected a JSON object, the server sent "
            f"{type(message).__name__}: {body[:200]!r}")
    if "error" in message:
        return text_result(f"the server rejected the call: {message['error']}",
                           is_error=True), "rpc-error"
    result = message.get("result")
    if not isinstance(result, dict):
        raise discovery.DiscoveryError("the reply carried no result")
    # `isError` on a SUCCESSFUL result is the MCP convention for a tool that ran and
    # failed, and it is the case an outcome record exists for: a 403 from GitHub arrives
    # here, inside a 200, in a well-formed result. Anything but a literal True reads as
    # success, because absent and false both mean the tool is not claiming failure.
    return result, "tool-error" if result.get("isError") is True else "ok"


def _run(server: str, tool: str, arguments: object, client: str | None = None,
         approval_id: str | None = None) -> dict:
    """Dial the server, make the call, and RECORD HOW IT ENDED. The only side effect
    in this file.

    Deliberately reachable from two places and no others, both of which hold a decision
    — ``call`` on an `allow`, and ``resume`` on a claim the control plane granted. It
    takes no policy argument and performs no check of its own, which is the point: a
    function that decided as well as ran would have two reasons to be called, and one
    of them would eventually be wrong.

    The outcome audit lives HERE for the same reason, and it is what makes the record
    complete rather than best-effort: every path that dials a server passes through this
    function, so there is no way to run a tool call without writing how it went. Every
    RETURN below is audited, including the two that never reach the wire — a spent grant
    that produced nothing is the gap this exists to close, so it cannot be the one case
    that goes unrecorded. (``resume`` records the third such path, where the claim
    succeeded and the approved arguments would not parse.)

    ``client`` and ``approval_id`` are carried only for that record — nothing about the
    call itself reads them — and ``approval_id`` being present is exactly what marks a
    row as having come through a human.

    The ARGUMENTS are the caller's to get right. On the resumption path they are the
    ones the human read, handed back by the claim rather than replayed from anything
    the agent sent."""
    def recorded(result: dict, status: str, reason: str | None = None) -> dict:
        outcomes.record(status, server, tool, reason=reason, approval_id=approval_id,
                     client=client)
        return result

    entry = surface.server_entry(server)
    if entry is None:
        # Not a lookup miss. The roster stopped naming this server, which is an
        # operator disabling or revoking it — and the control plane will have said the
        # same thing already, so this is the second of two refusals rather than the
        # only one.
        #
        # Audited as a transport failure rather than passed over: for an approved call
        # this is a grant that was spent and produced nothing, which is precisely the
        # gap between "a human approved it" and "it happened".
        return recorded(
            text_result(
                f"{server!r} is not on the gateway's current roster, so it cannot be "
                f"dialled. It was disabled, revoked, or never registered.",
                is_error=True),
            "transport-error", f"{server} left the roster before the call was made")
    try:
        raw = discovery.post(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments if arguments else {}}},
            entry.get("auth") or {}, timeout=CALL_TIMEOUT)
        result, status = parse_call(raw)
    except discovery.DiscoveryError as exc:
        # A failure to REACH the server, told apart from a failure reported BY it. The
        # distinction matters to whoever reads the transcript: one is an infrastructure
        # problem and the other is about the call. `exc.kind` carries the sharper half
        # of it — a timeout may have landed upstream where a refusal cannot have.
        return recorded(
            text_result(f"could not call {tool} on {server}: {exc}", is_error=True),
            exc.kind, str(exc))
    except Exception as exc:  # noqa: BLE001 — the record's own last line
        # Anything the two layers above did not foresee. The call may well have run —
        # the request was sent before whatever this is happened — so the one thing
        # that must not follow is silence: an exception here propagated to a 500 with
        # no outcome row, which is exactly the gap this function exists to close.
        # Filed as a transport error because that is the closest word the shared
        # status vocabulary has for "the reply arrived and could not be used".
        return recorded(
            text_result(f"could not read the reply from {tool} on {server}: the "
                        f"gateway failed while handling it ({type(exc).__name__}: "
                        f"{exc})", is_error=True),
            "transport-error",
            f"gateway failed handling the reply ({type(exc).__name__}: {exc})")
    # The reason line for a failure is the server's own text, which is what makes the
    # row worth reading — "Resource not accessible by personal access token" is the
    # sentence that was missing when this was discovered. Capped in `outcomes.record`,
    # because it is third-party text.
    return recorded(result, status,
                    None if status == "ok" else _first_text(result))


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
        # No approval_id, and its absence is the signal: an outcome row without one is a
        # call policy allowed outright. Those are the rows that grow with every tool an
        # operator enables, and they are the unaudited-intake half of the response side.
        return _run(server, tool, arguments, client=client)
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


def _resume_wait(arguments: object) -> float:
    """How long this resumption may block, from the agent's request and the cap.

    OUT OF RANGE IS NOT AN ERROR, and neither is the wrong type. This parameter buys
    latency and decides nothing: every value produces the same answers, sooner or
    later, and a resumption refused over its optional argument would cost a round
    trip to say something the agent can do nothing useful with. So it is clamped —
    anything unreadable means "do not wait", which is the behaviour of every caller
    that never heard of it.

    ``bool`` is excluded explicitly because it is an ``int`` in Python, and
    ``wait_seconds: true`` meaning "one second" would be a worse reading of the
    agent's intent than "not a number"."""
    raw = arguments.get("wait_seconds") if isinstance(arguments, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return max(0.0, min(float(raw), MAX_RESUME_WAIT))


def resume(arguments: object, client: str | None) -> dict:
    """Finish a call that was held: claim the approval, then run what it released.

    THE CLAIM IS THE GRANT, and it is single-use. The control plane's conditional
    update is what makes exactly one resumption of one approval perform the side
    effect, so a duplicate resumption is answered as spent rather than run twice.

    The arguments come back FROM the claim and are the ones the human read. Nothing the
    agent sends here contributes to the call beyond the id, which is what closes the
    gap between what was approved and what runs.

    MAY WAIT, if the agent asks it to (``wait_seconds``). Re-asking is the whole
    mechanism — a pending claim is free and changes nothing — so waiting here is the
    same question the agent would ask by calling again, asked on its behalf. Which of
    the two happens is the AGENT'S call and deliberately not this module's: only the
    agent knows whether it has other work to do while a human decides. Zero is the
    default and is exactly today's behaviour."""
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

    wait = _resume_wait(arguments)
    deadline = time.monotonic() + wait
    while True:
        try:
            answer = _ask_control(f"/tool/asks/{approval_id}/claim", {"client": client})
        except discovery.DiscoveryError as exc:
            return text_result(
                f"could not reach governance to claim {approval_id} ({exc}). Nothing "
                f"ran, and the approval is untouched — try again.", is_error=True)
        # Decided either way, or out of time: stop asking. Re-asking a PENDING claim is
        # the same free question the agent would ask by calling again, which is what
        # lets this loop exist without a second endpoint and without the control plane
        # learning anything about waiting.
        if answer.get("ok") or answer.get("terminal") or time.monotonic() >= deadline:
            break
        time.sleep(min(RESUME_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))

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
        waited = (f" Waited {wait:g}s for it." if wait else "")
        return text_result(
            f"Still waiting on a human for {approval_id}. Nothing has run.{waited} "
            f"Come back with the same id later — do not call the tool again, which "
            f"would raise a second question for the same person. Pass "
            f"wait_seconds to have this call block until the answer arrives, up to "
            f"{MAX_RESUME_WAIT:g}s, if you have nothing else to do meanwhile.")

    # Claimed. From here the call is authorised and the arguments are the approved
    # ones, parsed from the canonical form the digest covers and the human was shown.
    try:
        approved_args = json.loads(answer.get("args_json") or "null")
    except ValueError:
        # The CLAIM ALREADY SUCCEEDED, so the grant is spent and the call never
        # happened — the exact shape of gap this audit exists for, and the reason it is
        # recorded here rather than left to `_run`. It keeps the property whole: every
        # spent grant produces an outcome row, so a human's approval can never end in
        # silence. (It should be unreachable — the control plane stores the canonical
        # form it serialized itself — which is precisely why it must not be silent.)
        outcomes.record("transport-error", answer.get("server") or "(unknown)",
                     answer.get("tool") or "(unknown)",
                     reason="the approved arguments could not be parsed, so the call "
                            "was never made",
                     approval_id=approval_id, client=client)
        return text_result(
            f"the approved arguments for {approval_id} could not be read, so nothing "
            f"ran. The approval is spent; raise the call again.", is_error=True)
    # The server and tool come from the CLAIM, so the outcome is filed under what the
    # human approved rather than under anything the agent named — and the id ties this
    # row to the hold, the click and the release the control plane already recorded.
    return _run(answer["server"], answer["tool"], approved_args,
                client=client, approval_id=approval_id)
