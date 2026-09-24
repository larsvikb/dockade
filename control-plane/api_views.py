# SPDX-License-Identifier: Apache-2.0
"""The UI's read-only views — the audit record, the settings a page cannot behave
without, and /status. Nothing here changes what is allowed: the one POST, the
saturation ack, moves what the banner displays and touches no evidence.
"""
from __future__ import annotations

import os
import time

import audit
import holds
import policy
import store
from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

router = APIRouter()

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


class AckRequest(BaseModel):
    # How many over-cap rejections the operator has read. A count rather than a
    # "dismiss" flag, so a rejection arriving between the render and the click is
    # still unread afterwards. Clamped server-side — see ``api_saturation_ack``.
    count: int


def _bad_filter(exc: audit.FilterError) -> JSONResponse:
    """A refused filter, in the shape every other refusal here takes.

    400 and a sentence, rather than ignoring the parameter and serving a list. Both
    directions of a silently-dropped filter mislead: a widened one reports decisions
    the reader excluded, a narrowed one reports an empty record as a quiet system.
    Neither is visible on screen, which is why the answer is an error and not a
    best-effort list."""
    return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)


@router.get("/api/audit")
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


@router.get("/api/audit/events")
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


@router.get("/api/config")
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


@router.post("/api/saturation/ack")
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


@router.get("/status", response_class=PlainTextResponse)
def status() -> str:
    with store._connect() as conn:
        rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    return (f"dockade control plane (2b) — {rules} rules, {audits} audit rows, "
            f"{pending} pending approvals\n")
