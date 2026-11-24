# SPDX-License-Identifier: Apache-2.0
"""Standing repo policy — the git surface's rules, and the leases a repo card granted.

Rules are created and revoked here, with no fetch behind them; there is no edit,
since a rule's id then always names the rule it was created as, and a change is a
revoke and a create until something needs it as one operation. The pattern is
caller-supplied, so it is validated (``policy._normalize_repo_pattern``) where a
card's persist picks from ``policy._repo_candidates``. A lease can only be revoked
here, for the reason ``api_egress`` gives.

Every write is audited with its actor, its ``repo`` the pattern or repo it names, and
its ``host`` ``policy.REPO_HOST``.
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


class RepoRuleCreateRequest(BaseModel):
    # `github.com/owner/repo` or `github.com/owner/*`, free text, normalised and
    # validated by ``create_repo_rule``.
    pattern: str
    action: str                    # allow | block
    # No default, for the reason ``api_egress.RuleCreateRequest`` gives.
    client_class: str


def _landing(conn, pattern: str, client_class: str) -> str:
    """Where the repos ``pattern`` names land for ``client_class`` now, in
    ``policy._repo_ruling``'s words, asked on the write's own connection before it
    commits. An `owner/*` pattern is answered for the owner's repos that no exact rule
    or lease names, which only an `owner/*` rule decides."""
    if not pattern.endswith("/*"):
        ruling = policy._repo_ruling_in(conn, pattern, client_class)
        return f"for client class {client_class}, {pattern}: {ruling[1]}"
    rows = conn.execute(
        "SELECT pattern, action, client_class FROM repo_rules").fetchall()
    decided = (policy._rule_pass(rows, client_class, lambda p: p == pattern)
               or policy._held(rows, client_class, lambda p: p == pattern))
    return (f"for client class {client_class}, the repos under {pattern} that no exact "
            f"rule or lease names: {decided[1]}")


@router.get("/api/git/rules")
def api_repo_rules() -> list[dict]:
    """Every standing repo rule, unpaginated, grouped by class with blocks first: the
    order ``policy._repo_ruling`` applies, as ``api_egress.api_rules`` orders."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, pattern, action, source, created_at, client_class "
            "FROM repo_rules ORDER BY client_class, action DESC, pattern").fetchall()
    return [dict(r, scope=policy._repo_scope(r["pattern"])) for r in rows]


@router.post("/api/git/rules")
def create_repo_rule(req: RepoRuleCreateRequest, request: Request) -> JSONResponse:
    """Write a standing repo rule directly, including a block before anything has
    asked. The class is checked against ``policy._class_names``, and an existing rule
    for the same (pattern, class) with the opposite action is a 409, never a replace,
    as ``api_egress.create_rule`` does."""
    actor = provenance._actor(request)
    pattern, error = policy._normalize_repo_pattern(getattr(req, "pattern", ""))
    action = (getattr(req, "action", "") or "").strip().lower()
    client_class = (getattr(req, "client_class", "") or "").strip().lower()
    error = error or policy._repo_rule_error(action)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)
    classes = policy._class_names()
    if client_class not in classes:
        return JSONResponse(
            {"ok": False,
             "detail": f"client class {client_class!r} is not configured; repo rules "
                       f"can be scoped to: {', '.join(classes) or '(none configured)'}",
             "client_classes": list(classes)}, status_code=400)

    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, action FROM repo_rules WHERE pattern=? AND client_class=?",
            (pattern, client_class)).fetchone()
        if existing is not None and existing["action"] != action:
            conn.rollback()
            return JSONResponse(
                {"ok": False,
                 "detail": f"a standing repo rule for {pattern!r} already exists for "
                           f"client class {client_class!r} and {existing['action']}s "
                           f"it; nothing here replaces a rule. Revoke it first, or "
                           f"write a different pattern.",
                 "conflict": {"id": existing["id"], "pattern": pattern,
                              "action": existing["action"],
                              "client_class": client_class}},
                status_code=409)
        if existing is not None:
            conn.rollback()
            return JSONResponse({"ok": True, "created": False, "already_present": True,
                                 "id": existing["id"], "pattern": pattern,
                                 "action": action, "client_class": client_class})
        rule_id = conn.execute(
            "INSERT INTO repo_rules(pattern, action, source, created_at, client_class) "
            "VALUES (?,?, 'operator', ?, ?)",
            (pattern, action, time.time(), client_class)).lastrowid
        landing = _landing(conn, pattern, client_class)
        conn.commit()

    store._audit("create", stage="git-policy", host=policy.REPO_HOST, repo=pattern,
                 actor=actor,
                 reason=f"{action} repo rule created; {pattern} "
                        f"({policy._repo_scope(pattern)}); {landing}")
    return JSONResponse({"ok": True, "created": True, "already_present": False,
                         "id": rule_id, "pattern": pattern, "action": action,
                         "source": "operator", "client_class": client_class,
                         "landing": landing}, status_code=201)


@router.post("/api/git/rules/{rule_id}/revoke")
def revoke_repo_rule(rule_id: int, request: Request) -> JSONResponse:
    """Remove one repo rule. Deletion, not a tombstone, as ``api_egress.revoke_rule``.

    No expectation of what the page showed, which ``api_egress`` requires: that guards
    an id now naming a rule the page never showed, which only an edit can make, and a
    repo rule has none. Ids are never reused, so a stale page's revoke names a rule
    that is gone, and is a 404."""
    actor = provenance._actor(request)
    if not store._bindable(rule_id):
        return JSONResponse({"ok": False, "detail": "unknown repo rule"},
                            status_code=404)
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT pattern, action, client_class FROM repo_rules WHERE id=?",
            (rule_id,)).fetchone()
        if row is None:
            conn.rollback()
            return JSONResponse({"ok": False, "detail": "unknown repo rule"},
                                status_code=404)
        conn.execute("DELETE FROM repo_rules WHERE id=?", (rule_id,))
        landing = _landing(conn, row["pattern"], row["client_class"])
        conn.commit()
    store._audit("revoke", stage="git-policy", host=policy.REPO_HOST,
                 repo=row["pattern"], actor=actor,
                 reason=f"{row['action']} repo rule revoked; {landing}")
    return JSONResponse({"ok": True, "pattern": row["pattern"],
                         "action": row["action"], "client_class": row["client_class"]})


@router.get("/api/git/leases")
def api_repo_leases() -> list[dict]:
    """The live repo leases, soonest to expire first, with absolute deadlines: what
    ``api_egress.api_leases`` serves, for the repo table."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, repo, client_class, approval_id, created_at, expires_at, "
            "granted_by FROM repo_leases WHERE expires_at > ? ORDER BY expires_at",
            (time.time(),)).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/git/leases/{lease_id}/revoke")
def revoke_repo_lease(lease_id: int, request: Request) -> JSONResponse:
    """End one repo lease early, as ``api_egress.revoke_lease`` ends an egress lease;
    an already-expired row is deleted and reported as such. A fetch whose
    advertisement the lease allowed is denied its later POSTs (the upload is decided
    per request, and never held)."""
    actor = provenance._actor(request)
    now = time.time()
    if not store._bindable(lease_id):
        return JSONResponse({"ok": False, "detail": "unknown repo lease"},
                            status_code=404)
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT repo, client_class, expires_at, approval_id FROM repo_leases "
            "WHERE id=?", (lease_id,)).fetchone()
        if row is None:
            conn.rollback()
            return JSONResponse({"ok": False, "detail": "unknown repo lease"},
                                status_code=404)
        conn.execute("DELETE FROM repo_leases WHERE id=?", (lease_id,))
        decision = policy._repo_ruling_in(conn, row["repo"], row["client_class"])
        conn.commit()
    was_live = row["expires_at"] > now
    store._audit(
        "revoke", stage="git-policy", host=policy.REPO_HOST, repo=row["repo"],
        actor=actor, approval_id=row["approval_id"],
        reason=(f"repo lease revoked with "
                f"{policy._short_duration(row['expires_at'] - now)} left; for client "
                f"class {row['client_class']}, {row['repo']}: {decision[1]}"
                if was_live else
                f"expired repo lease removed; it had already stopped deciding fetches "
                f"of {row['repo']} for client class {row['client_class']}"))
    return JSONResponse({"ok": True, "repo": row["repo"],
                         "client_class": row["client_class"], "was_live": was_live})
