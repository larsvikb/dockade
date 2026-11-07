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
and answers the AGENT immediately with a resume token. Nothing is held open — not the
agent's call, not a control-plane worker — so the human's window is free to be an hour
and there is no caller to strand (DESIGN.md, "An `ask` answers immediately").

HOW IT ENDED IS RECORDED HERE, and only here. The control plane audits the decision,
and structurally cannot audit the outcome: its claim row is written before the call, so
by the time one succeeds or fails the authority has already answered. Every path that
dials a server goes through `_run`, which writes to `outcomes.py`'s stream — so an
approved call that failed upstream is a row rather than a silence.

WHAT THE REPLY CARRIED is recorded in the same place. An allowed read-only tool is
intake of the same shape as WebSearch, and the egress proxy sees where a server's
traffic goes, not what it carries. So the result `_run` returns goes to
`outcomes.record` whole: the outcome line gets its size and hash and the dial's
duration, and the gateway's own content file gets the body (`outcomes.py` says where
each goes and why).
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request

import discovery
import outcomes
import protocol
import surface

#: The shape of a resume token, checked before it is ever put in a URL, and before
#: one the control plane hands back is passed on to the agent. A call token, minted
#: the same way, is checked against it before it is written on an outcome line.
#:
#: The token is the one thing on this path that comes from the AGENT, and it lands in
#: a request path on the crown-jewel bridge. A percent-encode alone would be enough to
#: keep `../` from traversing, but a bounded charset is the check that does not depend
#: on getting the encoding right — and the token is 32 random hex characters
#: (``store._new_resume_token`` in the control plane), so nothing legitimate is
#: excluded by saying so.
#:
#: The gateway is never given an ask's approval id, which is the operator's: so no
#: result, outcome record or log line of its can carry one.
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

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
    "withdrawn": "This request was withdrawn",
}

#: An ``expired`` ask a human had approved: the grant lapsed unclaimed. Its own
#: constant rather than a key above, because ``_TERMINAL_TEXT`` is keyed on real
#: statuses and the row's status is still ``expired``; ``_closed_text`` picks it.
_LAPSED_TEXT = "A human approved this request, but the approval lapsed before it was resumed"

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
#: Re-ask cadence while waiting. A claim answered as pending is a read that changed
#: nothing (the control plane answers "still pending" without touching the row; under
#: a policy that now denies, it retires the ask and answers terminal instead), so this
#: is a cheap indexed select against a sibling — a second is far below what a human's
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


class Unanswered(discovery.DiscoveryError):
    """The request reached the control plane and no usable answer came back, so
    whatever it does on receipt may have happened. For a decision that is one more
    reason to refuse; for a CLAIM it means the approval may be spent."""


def _ask_control(path: str, payload: dict) -> dict:
    """POST to the control plane's tool bridge and return its answer.

    Raises ``DiscoveryError`` when there is no answer to be had, and the subclass
    ``Unanswered`` when the request was DELIVERED first. A decision is refused either
    way, so ``call`` catches the base type; ``resume`` needs them apart, because only an
    undelivered claim leaves the approval certainly untouched. The line between them is
    urllib's own: it wraps a failure to connect or to SEND in ``URLError``, and lets
    everything after the request is out — a read timeout, a reset, an unreadable body —
    through unwrapped. Messages are fit to print, which is the contract that matters.

    A NON-200 IS STILL AN ANSWER on this bridge. ``/tool/authorize`` always returns 200
    because a policy question has a policy answer, but the claim endpoint uses 404 and
    409 to distinguish refusals the gateway must tell apart, so the body is read on
    every status and the status itself decides nothing here."""
    request = urllib.request.Request(  # noqa: S310 - fixed http:// scheme, not user input
        f"{CONTROL_URL}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=DECIDE_TIMEOUT) as response:  # noqa: S310
            answer = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            answer = json.loads(exc.read() or b"{}")
        except (OSError, http.client.HTTPException, ValueError) as parse_failure:
            raise Unanswered(f"HTTP {exc.code} with no readable body") from parse_failure
    except urllib.error.URLError as exc:
        raise discovery.DiscoveryError(
            f"the control plane could not be reached ({exc.reason})") from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise Unanswered(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(answer, dict):
        raise Unanswered(f"a {type(answer).__name__} where an object was expected")
    return answer


def parse_call(raw: str) -> tuple[dict, str]:
    """The ``CallToolResult`` out of a server's reply, SSE or plain JSON, WITH the
    audit status that says which layer answered.

    The status is returned rather than derived afterwards because it cannot be derived
    afterwards: a JSON-RPC rejection and a tool that ran and failed both leave here as
    an error-flagged result — they have to, that being the only shape a tool call can
    answer in — and by then the two are indistinguishable. The distinction is
    load-bearing for the record: one says the call never ran, the other says it ran and
    the third party refused it.

    Which message in the reply is the response is ``discovery.response_message``'s
    call, shared with ``tools/list`` so the two cannot read the same stream differently.

    A JSON-RPC ERROR IS NOT A TOOL FAILURE and is not passed off as one. The protocol
    distinguishes "the call ran and the tool reports a problem" (a result with
    ``isError``) from "the request was rejected" (an error object), and flattening them
    would tell an agent its arguments were wrong when the server was unreachable, or
    the reverse. It becomes an error-flagged result here because that is the only shape
    a tool call can answer in, but the TEXT says which it was."""
    message = discovery.response_message(raw)
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


def _unsendable(status: str, exc: Exception) -> tuple[dict, str]:
    """What the agent gets, and the record's reason, in place of a result the gateway
    cannot send: ``exc`` is what ``protocol.encode`` raised on it.

    Fixed sentences that never quote the result, so the substitute is encodable by
    construction. Not repaired and delivered instead: escaping or nulling the bad
    values would hand the agent content the server did not send. Two results can fail
    to encode: a server's own, and an rpc-error text quoting a server's string error
    (every other result's text is fixed or carries no server-supplied string). Only
    the first RAN, which the agent must hear: an approved write that landed is
    otherwise taken for one that did not."""
    # `UnicodeEncodeError` first: it is a `ValueError`.
    if isinstance(exc, UnicodeEncodeError):
        why = "it held a lone surrogate, which UTF-8 cannot carry"
    elif isinstance(exc, ValueError):
        why = "it held NaN or an infinity, which JSON cannot carry"
    else:
        why = "the gateway failed while encoding it"
    if status in ("ok", "tool-error"):
        text = (f"The call ran on the server, but its result could not be passed on: "
                f"{why}. Do not assume the call did not happen.")
        reason = (f"the call ran on the server; its {status} result could not be "
                  f"passed on: {why}")
    else:
        text = f"The result of this call could not be passed on: {why}."
        reason = f"the {status} result could not be passed on: {why}"
    return text_result(text, is_error=True), reason


def _run(server: str, tool: str, arguments: object, client: str | None = None,
         resume_token: str | None = None, call_token: str | None = None) -> dict:
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

    ``client``, ``resume_token`` and ``call_token`` are carried only for that record —
    nothing about the call itself reads them. A ``resume_token`` marks a row as having
    come through a human's card, and a ``call_token`` as answered by a pin a human
    made from one; a row with neither is a call policy allowed outright.

    The ARGUMENTS are the caller's to get right, and are sent as given: a default
    substituted here would run something other than what the approval card showed. On
    the resumption path they are the ones the human read, handed back by the claim
    rather than replayed from anything the agent sent."""
    # The record describes the result returned, on every path: its size, hash and
    # body. ``duration`` describes the dial, so it stays None until one starts.
    started: float | None = None

    def recorded(result: dict, status: str, reason: str | None = None) -> dict:
        # DECIDED HERE, before anything is written: unchecked, a result `app.py` cannot
        # send would get a 500 there after an `ok` row here, and the record would hide
        # the failure it exists to show. Encoded with the reply's own serializer, and on failure
        # the substitute is both what is recorded and what is returned, so the row
        # describes what the agent received. Any exception, not only the two named:
        # one that escaped here would skip the record and leave a 500 with no row.
        try:
            protocol.encode(result)
        except Exception as exc:  # noqa: BLE001 — must reach the record below
            result, reason = _unsendable(status, exc)
            status = "transport-error"
        outcomes.record(status, server, tool, reason=reason, resume_token=resume_token,
                        call_token=call_token, client=client, result=result,
                        duration=None if started is None
                        else time.monotonic() - started)
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
    started = time.monotonic()
    try:
        raw = discovery.post(
            entry,
            {"jsonrpc": "2.0", "id": discovery.REQUEST_ID, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments}},
            timeout=CALL_TIMEOUT)
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


def _served_digest(server: str, tool: str) -> str | None:
    """The digest of the arguments the gateway last enumerated for ``tool``, which a
    pin in the control plane is bound to, or None when there is none worth binding to.

    None for a tool no enumeration named, and for an ``UNDIGESTIBLE`` schema, whose
    digest stays put whatever changes inside it: a pin bound to that would widen with
    nothing to stop it, where None only sends the call to a card."""
    schema = surface.served_schema(server, tool)
    digest = None if schema is None else discovery.schema_digest(schema)
    return None if digest == discovery.UNDIGESTIBLE else digest


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
    # Routed before the split, because a native tool has no server to decide against.
    # See NATIVE_TOOLS for why the category needs its own rule.
    if name == surface.RESUME_TOOL:
        return resume(arguments, client)
    if name == surface.WITHDRAW_TOOL:
        return withdraw(arguments, client)
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
                               "args": arguments, "client": client,
                               "schema_digest": _served_digest(server, tool)})
    except discovery.DiscoveryError as exc:
        # FAIL CLOSED, and loudly. A gateway that ran a call because it could not ask
        # would be a default-allow wearing an outage as a disguise.
        return text_result(
            f"refused: the gateway got no answer from governance to decide this call "
            f"({exc}). Nothing ran.", is_error=True)

    decision, why = answer.get("decision"), answer.get("reason") or "no reason given"
    if decision == "allow":
        # No resume token. A call token comes back only when a pin answered, and is
        # kept for the outcome line alone, never shown to the agent; one not in the
        # shape the control plane mints is dropped, and the outcome names no card. An
        # outcome with neither token is a call policy allowed outright: the rows that
        # grow with every tool an operator enables, the unaudited-intake half of the
        # response side.
        call_token = answer.get("call_token")
        if not isinstance(call_token, str) or not _TOKEN_RE.match(call_token):
            call_token = None
        return _run(server, tool, arguments, client=client, call_token=call_token)
    if decision == "ask":
        # The token and nothing else: an answer's ``approval_id`` is never read, so a
        # control plane older than resume tokens cannot hand the agent the operator's
        # id through here. It gets this refusal instead, and nothing runs.
        resume_token = answer.get("resume_token")
        if not isinstance(resume_token, str) or not _TOKEN_RE.match(resume_token):
            return text_result(
                f"Held for human approval: {tool} on {server}, but governance gave "
                f"no usable resume token for it, so it cannot be finished from here. "
                f"Nothing has run.", is_error=True)
        # A RESULT, not an error, and the instruction travels inside it — where it
        # cannot be forgotten mid-session and cannot drift from the gateway that
        # emitted it.
        return text_result(
            f"Held for human approval: {tool} on {server}. Nothing has run.\n"
            f"resume_token: {resume_token}\n"
            f"Call {surface.RESUME_TOOL} with that token to finish this call. It is "
            f"waiting on a person, so do other work and come back rather than "
            f"retrying immediately; calling this tool again cannot finish it, and "
            f"with any change in the arguments it opens a second question for the "
            f"same human. If you no longer want the call, {surface.WITHDRAW_TOOL} "
            f"with the same token takes the question back."
            + _answer_window(answer.get("deadline"), time.time())
            + _grant_window(answer.get("grant_window"))
            + ("\nAn identical request was already waiting, and this joined it."
               if answer.get("joined") else ""))
    # Every other answer is a deny, INCLUDING one this code does not recognise. The
    # control plane already refuses that way; repeating it here means a bridge that
    # answered something new could not turn into a grant on the way through.
    return text_result(f"Denied: {why}. This will not succeed on retry.", is_error=True)


def _answer_window(deadline: object, now: float) -> str:
    """The sentence saying until when a human may answer, or "" if ``deadline`` is
    not an instant.

    BOTH FORMS, because each covers the other's weakness: the UTC instant does not go
    stale in the agent's context, and the time left needs no clock arithmetic. Both
    are TRUNCATED, so neither overstates what is left. The time left is reckoned
    against this gateway's clock and ``deadline`` against the control plane's; they
    agree because both are containers on the same Docker host, which is a deployment
    fact rather than a guarantee, and the instant is the authority either way.

    Wording, not authority, as with ``_resume_wait``: a deadline that is missing or
    unreadable drops the sentence and nothing else, because the token is still good."""
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return ""
    try:
        instant = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(deadline))
    except (OverflowError, OSError, ValueError):  # NaN, infinite, or out of range
        return ""
    hours, minutes = divmod(int((deadline - now) // 60), 60)
    parts = ([_count(hours, "hour")] if hours else []) \
        + ([_count(minutes, "minute")] if minutes else [])
    if deadline <= now:
        left = "now"
    elif not parts:
        left = "less than a minute from now"
    else:
        left = " ".join(parts) + " from now"
    return (f"\nA human can answer until {instant} ({left}); if none has by then, the "
            f"question expires and the call is refused.")


def _grant_window(seconds: object) -> str:
    """The sentence saying how long an approval stays redeemable, or "" if ``seconds``
    is not a usable duration.

    Worded as ``_answer_window``'s time left, truncated, with seconds under a minute.
    It promises no new question once the approval lapses: a pinned approval leaves
    its pin, which may let the same call through again without one.

    Wording, not authority: a missing or malformed value drops the sentence only."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
            or not math.isfinite(seconds) or seconds < 1:
        return ""
    hours, minutes = divmod(int(seconds // 60), 60)
    parts = ([_count(hours, "hour")] if hours else []) \
        + ([_count(minutes, "minute")] if minutes else [])
    within = " ".join(parts) if parts else _count(int(seconds), "second")
    return (f"\nOnce a human approves, resume within {within}; an approval not "
            f"resumed by then lapses, and nothing runs under it.")


def _lapsed(answer: dict) -> bool:
    """True if a refusal is a lapsed grant: approved, then not resumed in time.

    Both it and an unanswered ask are ``expired``, and only the control plane's
    ``lapsed`` flag differs. The flag counts only beside ``expired`` and only as
    ``True``, so a malformed one reads as the status alone."""
    return answer.get("status") == "expired" and answer.get("lapsed") is True


def _closed_text(answer: dict) -> str | None:
    """The prose for a closed ask's answer, or None if its status has none here."""
    return _LAPSED_TEXT if _lapsed(answer) else _TERMINAL_TEXT.get(answer.get("status"))


def _count(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def _resume_token(arguments: object) -> str | None:
    """The token an agent passed to a native tool, or None if it is missing or
    malformed.

    Read from ``resume_token``, or from ``approval_id`` when that key is absent. The
    second is an ALIAS for the same value under the parameter's old name, and it is
    needed because a client keeps the schema it fetched at session start (protocol.py
    says why `listChanged` is not advertised): a session that spans the upgrade sends
    every token under the old key. When both keys are present, ``resume_token`` is
    the one read. The alias can go once no client session predates the upgrade.

    STRIPPED before it is checked, and that is safe in a way a looser cleanup would not
    be: the token is bounded hex, so removing surrounding whitespace cannot turn one
    valid token into another and cannot make an invalid one valid. It is worth doing
    because the failure it prevents is a real one — a token copied out of a pending
    result arrives with a stray newline or trailing space often enough, and the cost is
    an approval a human granted that can no longer be redeemed."""
    if not isinstance(arguments, dict):
        return None
    key = "resume_token" if "resume_token" in arguments else "approval_id"
    token = arguments.get(key)
    if isinstance(token, str):
        token = token.strip()
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        return None
    return token


def _bad_token(tool: str) -> dict:
    """One message for "missing" and for "malformed", because the agent's next move is
    the same either way and neither tells it anything about whether some other token
    exists."""
    return text_result(
        f"{tool} needs the resume_token from a pending result, copied verbatim.",
        is_error=True)


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


#: Resume tokens whose claim was delivered and got no usable answer, in this process.
#: Wording only: a later "already claimed" for one of these may be that lost claim, and
#: the agent should hear so rather than assume its call ran. "May", because a
#: concurrent resumption of the same token could have been the one that won. Each
#: token leaves on its next settled answer; a restart forgets them and costs only the
#: sentence.
_UNSETTLED: set[str] = set()


def _pending(answer: dict) -> bool:
    """A claim answer that means "a human has not decided yet", and nothing else.
    ``terminal`` is checked as well as the status, so a terminal answer is never read
    as a delay, whatever status it names."""
    return (not answer.get("ok") and not answer.get("terminal")
            and answer.get("status") == "pending")


def _unsettled(resume_token: str, client: str | None, why: str) -> dict:
    """A claim the control plane received and did not usably answer. It may have
    landed — the claim is written before its answer is sent — so the grant may be
    spent with nothing run, and that is recorded as an outcome for the reason every
    spent grant is: an approval must not end in silence. Server and tool are the
    claim's to supply and it supplied nothing; the token is what joins the row to the
    ask."""
    _UNSETTLED.add(resume_token)
    outcomes.record("transport-error", "(unknown)", "(unknown)",
                    reason=f"the claim reached the control plane but got no usable "
                           f"answer ({why}), so the call was never made; the approval "
                           f"may be spent",
                    resume_token=resume_token, client=client)
    return text_result(
        f"governance received the claim for {resume_token} but gave no usable answer "
        f"({why}). Nothing ran. The claim may have gone through anyway, so try again "
        f"with the same token; if that says it was already claimed, the call needs a "
        f"new approval.", is_error=True)


def resume(arguments: object, client: str | None) -> dict:
    """Finish a call that was held: claim the approval, then run what it released.

    THE CLAIM IS THE GRANT, and it is single-use. The control plane's conditional
    update is what makes exactly one resumption of one approval perform the side
    effect, so a duplicate resumption is answered as spent rather than run twice.

    The arguments come back FROM the claim and are the ones the human read. Nothing the
    agent sends here contributes to the call beyond the token, which is what closes the
    gap between what was approved and what runs.

    MAY WAIT, if the agent asks it to (``wait_seconds``). Re-asking is the whole
    mechanism — a claim answered as pending was free and changed nothing — so waiting
    here is the same question the agent would ask by calling again, asked on its
    behalf. Which of the two happens is the AGENT'S call and deliberately not this
    module's: only the agent knows whether it has other work to do while a human
    decides. Zero is the default and is exactly today's behaviour."""
    resume_token = _resume_token(arguments)
    if resume_token is None:
        return _bad_token(surface.RESUME_TOOL)

    wait = _resume_wait(arguments)
    deadline = time.monotonic() + wait
    while True:
        try:
            answer = _ask_control(f"/tool/asks/{resume_token}/claim", {"client": client})
        except Unanswered as exc:
            return _unsettled(resume_token, client, str(exc))
        except discovery.DiscoveryError as exc:
            # Never delivered, so this one IS certain: the control plane did not see it.
            return text_result(
                f"could not reach governance to claim {resume_token} ({exc}). Nothing "
                f"ran, and the approval is untouched — try again.", is_error=True)
        # Only PENDING is worth asking again. Re-asking it is the same free question the
        # agent would ask by calling again, which is what lets this loop exist without a
        # second endpoint and without the control plane learning anything about waiting.
        if not _pending(answer) or time.monotonic() >= deadline:
            break
        time.sleep(min(RESUME_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))

    if not answer.get("ok"):
        # The control plane's `detail` is machine-shaped for the states it has no prose
        # for — "not claimable (denied)" — and prose for the one it does. This surface
        # is the one an agent reads, so the machine spellings are rendered here and the
        # rest is passed through; a gateway that echoed every internal string would be
        # making the control plane responsible for wording it cannot see the reader of.
        # A refusal BY POLICY carries its reason in the detail, and the status the
        # ask was left in: for a retired one `expired`, whose text would say nobody
        # answered rather than why.
        by_policy = answer.get("by_policy") is True
        detail = ((by_policy and answer.get("detail")) or _closed_text(answer)
                  or answer.get("detail") or "not claimable")
        if answer.get("terminal"):
            # `denied`, `expired`, or already spent. Said plainly and finally, because
            # an agent that cannot tell a refusal from a delay retries one forever.
            lost = (" An earlier claim of it got no usable answer, so it may have been "
                    "spent by that claim without its call running."
                    if answer.get("spent") and resume_token in _UNSETTLED else "")
            _UNSETTLED.discard(resume_token)
            # A lapsed approval may have left a pin, which lets the same call through
            # again with no question: the tail must not promise one.
            again = ("policy decides it afresh, and it may need a new approval"
                     if _lapsed(answer) else "that raises a new question for the human")
            return text_result(
                f"{detail}.{lost} This is final — {resume_token} will not become "
                f"runnable, so do not retry it. Call the tool again if the work is "
                f"still needed; {again}.",
                is_error=True)
        if not _pending(answer):
            # Neither a verdict nor a delay. Reading it as pending would tell the agent
            # a human is still deciding; it is the same unknown as a lost answer.
            return _unsettled(resume_token, client, "a reply with no verdict in it")
        # STILL PENDING, and therefore not an error. Nobody has answered yet, nothing
        # has run, and the token is still good.
        waited = (f" Waited {wait:g}s for it." if wait else "")
        return text_result(
            f"Still waiting on a human for {resume_token}. Nothing has run.{waited} "
            f"Come back with the same token later — do not call the tool again, which "
            f"cannot finish this one and, with any change in the arguments, raises a "
            f"second question for the same person. Pass "
            f"wait_seconds to have this call block until the answer arrives, up to "
            f"{MAX_RESUME_WAIT:g}s, if you have nothing else to do meanwhile. If you "
            f"no longer want the call, {surface.WITHDRAW_TOOL} takes the question "
            f"back.")

    # Claimed. From here the call is authorised and the arguments are the approved
    # ones, parsed from the canonical form the digest covers and the human was shown.
    _UNSETTLED.discard(resume_token)
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
                     resume_token=resume_token, client=client)
        return text_result(
            f"the approved arguments for {resume_token} could not be read, so nothing "
            f"ran. The approval is spent; raise the call again.", is_error=True)
    # The server and tool come from the CLAIM, so the outcome is filed under what the
    # human approved rather than under anything the agent named — and the token ties this
    # row to the hold, the click and the release the control plane already recorded.
    return _run(answer["server"], answer["tool"], approved_args,
                client=client, resume_token=resume_token)


def withdraw(arguments: object, client: str | None) -> dict:
    """Take back an ask the agent no longer wants: ``withdraw_tool_call``.

    Nothing runs here and no outcome is recorded, because no grant is spent: the
    control plane retires the ask or answers that it was already closed. Repeating a
    withdrawal is harmless, which is why every failure to get an answer, delivered or
    not, says the same thing — try again."""
    resume_token = _resume_token(arguments)
    if resume_token is None:
        return _bad_token(surface.WITHDRAW_TOOL)
    try:
        answer = _ask_control(f"/tool/asks/{resume_token}/withdraw", {"client": client})
    except discovery.DiscoveryError as exc:
        return text_result(
            f"could not get an answer from governance to withdraw {resume_token} "
            f"({exc}). Nothing ran; try again — withdrawing twice is harmless.",
            is_error=True)
    if answer.get("ok"):
        if answer.get("was") == "allowed":
            # The human had already answered: what was dropped is their approval.
            return text_result(
                f"Withdrawn: {resume_token}. The approval a human gave is dropped, and "
                f"nothing will run under this token.")
        return text_result(
            f"Withdrawn: {resume_token}. The human is no longer asked, and nothing will "
            f"run under this token.")
    if answer.get("spent"):
        # The one answer that is not "it is gone": the claim is written before the call,
        # so the agent must find out from the result of that resume whether it ran.
        return text_result(
            f"{resume_token} was already resumed, so its call may have run; withdrawing "
            f"it changed nothing.", is_error=True)
    closed = _closed_text(answer)
    if closed:
        # Closed already, which is what the agent wanted: not an error.
        return text_result(
            f"{closed}, so there was nothing left to withdraw. "
            f"Nothing will run under {resume_token}.")
    return text_result(
        f"{answer.get('detail') or 'not withdrawable'}: {resume_token}.", is_error=True)
