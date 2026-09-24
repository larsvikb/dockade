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

# How many recent matching rows /api/audit folds into its grouped view
# (``audit.grouped``). The slice rides the ``audit_ts`` index.
AUDIT_GROUP_SCAN = int(os.environ.get("CONTROL_AUDIT_GROUP_SCAN", "5000"))


# The reason the egress proxy writes when it denies because it could not reach THIS
# service (``_authorize`` in proxies/egress/addon.py): fail-closed working, not policy,
# though both show as a red `deny` against a host. A prefix, because the proxy appends
# the underlying exception. The two services share no module, so a test pins this
# against addon.py's literal; if they drift anyway, the row reverts to an ordinary
# deny, which is the safe direction.
FAIL_CLOSED_REASON = "control-plane unreachable"


class AckRequest(BaseModel):
    count: int


def _bad_filter(exc: audit.FilterError) -> JSONResponse:
    """A refused filter: 400 and the error's sentence, served verbatim, never a
    best-effort list. A dropped filter misleads either way — widened, the list shows
    rows the reader excluded; narrowed, an empty record passes for a quiet system."""
    return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)


@router.get("/api/audit")
def api_audit(limit: int = 50, q: str | None = None, kind: str | None = None,
              since: float | None = None, until: float | None = None) -> dict:
    """The glance: recent decisions, newest first, for the UI's decisions table.

    Rows fold by the fields on screen plus ``approval_id``, over the latest
    ``AUDIT_GROUP_SCAN`` matching rows; ``n`` and ``first_ts`` say what each folded.
    ``audit.grouped`` explains the key, and DESIGN.md, "The list groups; the record
    does not", the grouping. The columns are fewer than the table's: ``port``/
    ``proto``/``method``/``url`` are left to ``api_audit_events``, since ``url`` is
    agent-controlled and unbounded.

    ``client`` is the peer address the proxy observed; grouped, it is an address's
    history rather than one sandbox's. ``client_class`` is read from the stored column
    rather than re-derived, so a row keeps the class it was decided under after the
    CIDR map changes (NULL on rows older than the column). ``total`` counts what
    matches the filter, and ``filtered`` says whether the parameters made one, which
    the browser cannot tell from what it sent."""
    try:
        filt = audit.parse(q=q, kind=kind, since=since, until=until,
                           search=audit.GROUPED_SEARCH)
    except audit.FilterError as exc:
        return _bad_filter(exc)
    limit = audit.clamp(limit, 50, 500)
    with store._connect() as conn:
        rows = audit.grouped(conn, limit, filt, AUDIT_GROUP_SCAN)
        # Without the total, forty rows pass for the whole record. It costs a COUNT(*)
        # per poll, affordable because the page stops polling in a hidden tab.
        total = audit.total(conn, filt)
    return {"rows": [_audit_view(r) for r in rows], "total": total,
            "filtered": filt.active}


@router.get("/api/audit/events")
def api_audit_events(limit: int = audit.EVENTS_LIMIT_DEFAULT, q: str | None = None,
                     kind: str | None = None, since: float | None = None,
                     until: float | None = None, before: str | None = None) -> dict:
    """The record: one row per decision, newest first, paged backwards without bound.

    Serves the columns the glance drops, ``url`` among them: capped per field on write
    (``store.DRAIN_MAX_FIELD``), bounded per page here (``audit.EVENTS_LIMIT_MAX``),
    and escaped by the UI under its CSP.

    Paged by a ``(ts, id)`` cursor, not an offset (``audit.encode_cursor`` says why);
    ``next`` is null at the end. ``total`` is the matching set and stays put as pages
    advance, or paging back would look like the record shrinking."""
    try:
        filt = audit.parse(q=q, kind=kind, since=since, until=until,
                           search=audit.EVENT_SEARCH)
        limit = audit.clamp(limit, audit.EVENTS_LIMIT_DEFAULT, audit.EVENTS_LIMIT_MAX)
        with store._connect() as conn:
            rows, nxt = audit.events(conn, limit, filt, before)
            total = audit.total(conn, filt)
    except audit.FilterError as exc:
        # Includes a malformed `before`: served as page 1, it would pass for page 12.
        return _bad_filter(exc)
    return {"rows": [_audit_view(r) for r in rows], "total": total,
            "filtered": filt.active, "next": nxt}


def _audit_view(row) -> dict:
    """One audit row as the UI receives it, grouped or raw, plus ``fail_closed``:
    whether a denial was an outage rather than policy. Decided here, not in the
    browser, so the marker sits beside the test that pins it."""
    out = dict(row)
    reason = out.get("reason") or ""
    out["fail_closed"] = reason.startswith(FAIL_CLOSED_REASON)
    return out


@router.get("/api/config")
def api_config() -> dict:
    """The settings the UI cannot behave correctly without. Read-only and non-secret;
    served rather than written into the page, so the page shows what is configured."""
    return {
        # A held request default-denies after this, and a card that cannot say how
        # long is left cannot tell hold-for-approval from a slow deny.
        "hold_timeout": holds.HOLD_TIMEOUT,
        # So the lease button labels itself from the server. For the same reason the
        # action is ``allow_lease``, not named after a number.
        "lease_seconds": policy.LEASE_SECONDS,
        # ``create_rule`` refuses a class it does not know. Not derived from the rules:
        # a class with none yet, the one most in need of its first, would be missing.
        "client_classes": list(policy._class_names()),
    }


@router.post("/api/saturation/ack")
def api_saturation_ack(req: AckRequest) -> dict:
    """Record that the operator has read ``count`` over-cap rejections.

    A high-water mark, not a reset, so a rejection landing while the click is in
    flight stays unread. Monotonic, and clamped to the rejections there have been, so
    a stale or inflated count cannot silence future ones. Not audited: it changes what
    the banner shows and no evidence. DESIGN.md, "Dismissal is server-side, and it is
    a high-water mark", has the reasoning."""
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
