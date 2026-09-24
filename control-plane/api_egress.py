# SPDX-License-Identifier: Apache-2.0
"""Standing egress policy — the rules, and the leases a card granted.

Standing policy is also editable directly, which is the half that does not begin with
a request: POST /api/egress/rules writes a rule (``create_rule``), POST
/api/egress/rules/{id}/edit changes one (``edit_rule``) and POST
/api/egress/rules/{id}/revoke takes one back (``revoke_rule``). The pattern there IS
caller-supplied — there is no held host to derive candidates from — so that path
validates it (``policy._rule_error``) where the persist path constrains it. A lease has
only the taking-back half, POST /api/egress/leases/{id}/revoke (``revoke_lease``):
nothing creates one without a card to grant it from, because a timed grant with no
request behind it is just a standing rule somebody will forget they wrote.
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


@router.get("/api/egress/rules")
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


@router.post("/api/egress/rules")
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
    docstring in app.py). A rule written here decides egress with no hold and no
    click, which makes this a stronger capability than ``resolve``: that one can only
    answer a question something already asked.

    Not idempotent-by-overwrite: an existing rule for the same (pattern, class) with
    the OPPOSITE action is a 409, never a silent replace — the same refusal, for the
    same reason, that ``resolve`` makes on its persist path. Replacing one is
    ``edit_rule``'s job, where it is a named operation with a before and an after in
    the record; a create that silently overwrote would be the same act with neither."""
    actor = provenance._actor(request)
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


@router.post("/api/egress/rules/{rule_id}/edit")
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


@router.post("/api/egress/rules/{rule_id}/revoke")
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
    — the fields are forgeable by a host-local caller (see ``provenance._actor``)."""
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

@router.get("/api/egress/leases")
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


@router.post("/api/egress/leases/{lease_id}/revoke")
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
