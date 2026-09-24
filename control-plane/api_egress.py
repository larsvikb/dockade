# SPDX-License-Identifier: Apache-2.0
"""Standing egress policy — the rules, and the leases a card granted.

Rules can be written, edited and revoked here directly, with no request behind them.
The pattern is then caller-supplied, so it is validated (``policy._rule_error``) where
a card's persist path picks from derived candidates. A lease can only be revoked
here: a timed grant with no card behind it would be a standing rule somebody forgets
they wrote.
"""
from __future__ import annotations

import time

import policy
import provenance
import store
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


class RuleCreateRequest(BaseModel):
    # The widest input in the service: free text, not a pick from derived candidates.
    # ``create_rule`` validates and normalizes it.
    pattern: str
    action: str                    # allow | block
    # No default: a wrong guess would be a rule deciding for a population the operator
    # did not name, and a mis-scoped rule looks correct in the rules view.
    client_class: str
    # No ``source``: it is server-set to 'operator'. A caller that could set 'seed'
    # could write a rule ``revoke_rule`` refuses to delete.


class RuleEditRequest(BaseModel):
    # The TARGET state, not a delta, so both fields are required. An absent field
    # meaning "leave this alone" looks the same as one the caller meant to send, and
    # for ``action`` that ambiguity decides egress.
    pattern: str
    action: str                    # allow | block
    # No ``source``, as in ``RuleCreateRequest``. No ``client_class``: moving a rule
    # between classes takes policy from one population and gives it to another, two
    # changes under one audit row.


@router.get("/api/egress/rules")
def api_rules() -> list[dict]:
    """Every standing rule — the policy that decides every request.

    UNPAGINATED: a truncated view of the complete policy would hide exactly what it
    exists to show, and rules are bounded in practice, unlike the audit table.

    Grouped by CLIENT CLASS, then blocks before allows, which is the order
    ``policy._decide`` applies. Alphabetical order would put one pattern's rules for
    two classes side by side, as if they interacted, which they never do."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, pattern, action, source, created_at, client_class FROM rules "
            "ORDER BY client_class, action DESC, pattern").fetchall()
    # `id` is served so edit and revoke name the exact row, rather than a pattern
    # that would have to normalize the same way twice to find it.
    return [dict(r, scope=policy._pattern_scope(r["pattern"])) for r in rows]


@router.post("/api/egress/rules")
def create_rule(req: RuleCreateRequest, request: Request) -> JSONResponse:
    """Write a standing rule directly, with no held request behind it — including a
    block before anything has asked.

    A stronger capability than ``api_approvals.resolve``, which can only answer a
    question something already asked, and without the two things that make a persist
    safe there:

      - The pattern is not picked from ``policy._persist_candidates``, so
        ``policy._rule_error`` does that job, wildcard floor included.
      - The class is not read off an approvals row, so it is checked against
        ``policy._class_names``. An unlisted class writes a rule that matches nothing
        while reading as policy in force.

    An existing rule for the same (pattern, class) with the OPPOSITE action is a 409,
    never a silent replace, as on ``resolve``'s persist path. Changing a rule is
    ``edit_rule``, which records a before and an after."""
    actor = provenance._actor(request)
    pattern = policy._normalize_pattern(getattr(req, "pattern", "") or "")
    action = (getattr(req, "action", "") or "").strip().lower()
    client_class = (getattr(req, "client_class", "") or "").strip().lower()

    error = policy._rule_error(pattern, action)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)
    classes = policy._class_names()
    if client_class not in classes:
        # `sandox` would insert, list and decide nothing. The refusal names the
        # configured classes, so it carries its own fix.
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
            # The UI shows `detail` verbatim, so it names a fix that works: a seed rule
            # can be neither edited nor revoked here.
            fix = ("Change it in policies/egress-allowlist.txt"
                   if existing["source"] == "seed" else "Edit it, or revoke it first")
            return JSONResponse(
                {"ok": False,
                 "detail": f"a standing rule for {pattern!r} already exists for client "
                           f"class {client_class!r} and {existing['action']}s it; "
                           f"nothing here replaces a rule. {fix}, or write a "
                           f"different pattern.",
                 "conflict": {"id": existing["id"], "pattern": pattern,
                              "action": existing["action"], "source": existing["source"],
                              "client_class": client_class}},
                status_code=409)
        if existing is not None:
            # Already in force: not an error, but a non-write, so the caller does not
            # confirm a rule it did not create. ``source`` says whether it can be
            # revoked.
            return JSONResponse({"ok": True, "created": False, "already_present": True,
                                 "id": existing["id"], "pattern": pattern,
                                 "action": action, "source": existing["source"],
                                 "client_class": client_class})
        rule_id = conn.execute(
            "INSERT INTO rules(pattern, action, source, created_at, client_class) "
            "VALUES (?,?, 'operator', ?, ?)",
            (pattern, action, time.time(), client_class)).lastrowid
        conn.commit()

    # Once written, this rule looks exactly like one approved at a card, so the record
    # is the only thing that says where it came from. The NORMALIZED pattern, because
    # that is what was stored and what decides.
    store._audit("create", stage="policy", host=pattern, client_class=client_class,
                 reason=f"{action} rule created by {actor}; {pattern} "
                        f"({policy._pattern_scope(pattern)}) now {action}s for client "
                        f"class {client_class} without being held for approval")
    return JSONResponse({"ok": True, "created": True, "already_present": False,
                         "id": rule_id, "pattern": pattern, "action": action,
                         "source": "operator", "client_class": client_class},
                        status_code=201)


@router.post("/api/egress/rules/{rule_id}/edit")
def edit_rule(rule_id: int, req: RuleEditRequest, request: Request) -> JSONResponse:
    """Change a standing rule's pattern or action in ONE operation.

    One UPDATE in one transaction, so there is no instant with the old rule gone and
    the new one not yet written. Revoke-then-create had that window: two audit rows
    for one intent, and every host under a wildcard being narrowed held meanwhile.
    The pattern is caller-supplied, so ``policy._rule_error`` validates it as in
    ``create_rule``.

    **Seed rules are refused**, as ``revoke_rule`` refuses them, and more firmly: an
    edited seed rule would stay, deciding, while ``policies/egress-allowlist.txt``
    says something else about the same host.

    ``created_at`` is untouched. It is the same rule on new terms, the audit row dates
    the change, and the rules view sorts on when policy came into force. The class is
    not editable (``RuleEditRequest``)."""
    actor = provenance._actor(request)
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
            # Already in force: a non-write, as in ``create_rule``, so no audit row
            # says policy moved when nothing did.
            return JSONResponse({"ok": True, "changed": False, "id": rule_id,
                                 "pattern": pattern, "action": action,
                                 "client_class": row["client_class"]})
        # Another rule on the target (pattern, class): a 409 here, where
        # ``UNIQUE(pattern, client_class)`` would raise an opaque 500. Excluding this
        # rule's own id is what lets an action-only change through.
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

    # ONE row carrying both states. ``host`` is the NEW pattern, since that decides
    # from now on, and the reason says what it replaced.
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


@router.post("/api/egress/rules/{rule_id}/revoke")
def revoke_rule(rule_id: int, request: Request) -> JSONResponse:
    """Remove one operator-created rule.

    **Seed rules are refused here, not merely hidden in the UI.** Their source of
    truth is ``policies/egress-allowlist.txt``, reviewed and versioned, which a click
    must not leave disagreeing with the store. It also closes a trap:
    ``store._seed_if_empty`` re-reads the file whenever the table is empty, so if every
    rule could be revoked, the next restart would bring the whole seed back. Retiring
    a transitional seed entry is therefore a migration shipped with the code that
    replaces it.

    Deletion, not a tombstone: the audit row is the history, and dead rows would have
    to be filtered by every reader, ``policy._decide`` included."""
    actor = provenance._actor(request)
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

    # The reason says where the host lands: held for approval, whichever action was
    # revoked. The class is named too, since a revoke touches ONE population and two
    # classes can each have a `.github.com` allow.
    store._audit("revoke", stage="policy", host=row["pattern"],
                 client_class=row["client_class"],
                 reason=f"{row['action']} rule revoked by {actor}; {row['pattern']} is "
                        f"now unknown for client class {row['client_class']} and will "
                        f"be held for approval")
    return JSONResponse({"ok": True, "pattern": row["pattern"],
                         "action": row["action"],
                         "client_class": row["client_class"]})


# ── leases (human-facing) ───────────────────────────────────────────────────
# The timed half of egress policy: its own table (``store._LEASES_DDL``), and its own
# endpoints, answering "what am I allowing right now" where the rules answer "what
# have I permanently allowed".

@router.get("/api/egress/leases")
def api_leases() -> list[dict]:
    """The LIVE leases — every timed grant currently deciding requests.

    Expired rows are left out, not greyed out: they grant nothing. The grant path
    sweeps them (the lease branch of ``api_approvals.resolve``), so this filters only
    what lapsed since the last grant.

    Absolute ``expires_at``, never remaining seconds, which would change on every tick
    and defeat change-detection (``holds._saturation`` says the same). Unpaginated,
    like ``api_rules``, and bounded by how many cards a human can click."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, host, client_class, approval_id, created_at, expires_at, "
            "granted_by FROM leases WHERE expires_at > ? ORDER BY expires_at",
            (time.time(),)).fetchall()
    # Soonest to expire first: the one an operator might still act on, and the next
    # to disappear.
    return [dict(r) for r in rows]


@router.post("/api/egress/leases/{lease_id}/revoke")
def revoke_lease(lease_id: int, request: Request) -> JSONResponse:
    """End one timed grant early.

    This is what makes ``policy.LEASE_SECONDS`` an ergonomics number rather than a
    safety floor: without it a lease could only be waited out. No seed exemption, as
    every lease came from a click on a card. Deletion, not a tombstone, as in
    ``revoke_rule``; ``api_approvals.resolve`` records the grant and this its end.

    An ALREADY-EXPIRED row is deleted too and reported as such, not refused: the grant
    had ended, and a refusal would look like a bug.

    **It does not tear down an established connection.** The proxy authorizes once
    per CONNECT tunnel, so a tunnel opened while the lease was live keeps carrying
    requests (DESIGN.md, "A lease bounds authorization, not connection lifetime").
    Revoking stops the next connection, not this one."""
    actor = provenance._actor(request)
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
    # As for a rule, the reason says where the host lands: held, not denied.
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
