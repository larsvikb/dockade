# SPDX-License-Identifier: Apache-2.0
"""Audit VIEWS — the read queries behind the decisions interface: filters, and the
history the glance deliberately truncates.

**Two views over one table, not one view with a page parameter**, kept in the code
shape rather than in a flag (control-plane/DESIGN.md, "Browsing it: two views"):

  - ``grouped`` — the GLANCE. Folds rows whose displayed fields are identical, bounds
    its work by event count, and answers "what is happening". It cannot page, because
    a group is defined relative to a window: paging it would either split one group
    across two pages with partial counts on each, or need a second definition of what
    a group is. Filtering it is free of that problem, which is why filters land here
    and paging does not.
  - ``events`` — the RECORD, one row per decision, ordered and keyset-paged. This is
    where a question about a specific request is answered, so it serves the columns
    the glance drops (``port``/``proto``/``method``/``url``) and pages backwards
    without bound.

Both take the SAME filters, and a filter means the same thing in either view — that
is what lets an operator narrow the glance and then walk the history of what they
found without re-learning the controls.

Queries only. Nothing here shapes an HTTP response (``api_views`` does that, including
the ``fail_closed`` classification), and nothing here writes — the audit WRITE lives
in ``store._audit``, next to the schema. It imports NOTHING: every function takes the
connection its caller already holds, so this module is the SQL and the validation and
has no state of its own to get out of step with the store's.
"""
from __future__ import annotations

# The vocabulary of the ``kind`` column, so a facet can be VALIDATED rather than
# passed through to a query that returns nothing. An unknown value must not read as
# "no decisions matched": that is indistinguishable from a quiet system, which is the
# one thing a decisions list must not be ambiguous about (the same reasoning as the
# empty-versus-stale states in the frontend).
#
# Held in agreement with what the handlers actually write by a test
# (``test_every_word_the_control_plane_writes_is_filterable``), rather than shared as a
# constant with the call sites — ``store._audit`` takes ``kind`` as a plain string from
# many places, and a new word appearing there without appearing here would make its
# rows unfilterable while every other test passed.
# "observe" and "outcome" are the two that are NOT decisions, and both are here
# deliberately. `observe` records something a server claimed about itself — its tool
# surface changed. `outcome` records how a tool call ended, which only the gateway can
# know (`ingest.py`). Neither was decided by this process and neither decides anything
# in return. They share the column because they share the question an operator asks of
# this log ("what happened, and when"), and they are filterable for the same reason the
# others are: a row nobody can select for is a row nobody reads.
#
# They stay SEPARATE words rather than one "not a decision" bucket, because `observe`
# earns its worth by being rare — one writer, changes only, and an image bump that
# starts exposing a destructive tool reads as the alarm it is. Outcome rows are one per
# tool call. Folding them together would bury the rare signal under the common one.
KINDS = ("allow", "deny", "hold", "revoke", "create", "edit", "observe",
         "outcome")

# Columns ``q`` searches, PER VIEW, and the rule is that a view searches exactly what
# it DISPLAYS. Anything else produces the worst kind of result list: rows whose visible
# content does not contain what was typed, with nothing on screen to explain why they
# matched. It is the same discipline the group key follows (see ``grouped``), applied
# to search instead of to folding — and it is why ``url`` is searchable in the record
# view and not in the glance, rather than being either everywhere or nowhere. The tool
# columns are in both because an outcome row displays itself as `status · server__tool`
# where an egress row names a host. ``actor`` is in both as displayed: its cell shows
# the peer and via-ui fields and holds the whole string in its title (``shortActor``
# in control-plane-ui/app.js), so a match on the User-Agent is one hover away.
GROUPED_SEARCH = ("host", "client", "client_class", "actor", "reason",
                  "server", "tool", "status")
EVENT_SEARCH = ("host", "client", "client_class", "actor", "reason", "method",
                "url", "server", "tool", "status", "approval_id")

# Columns the record view serves. Deliberately the whole row: this is the view the
# glance defers to, so the fields it drops as noise or as unbounded (``url`` above
# all) are exactly what has to be here, or the interface still cannot answer "which
# request was it".
EVENT_COLUMNS = ("id", "ts", "kind", "stage", "host", "port", "proto", "client",
                 "client_class", "actor", "method", "url", "reason", "status",
                 # The tool columns, for the same reason ``url`` is here: this is the
                 # view that answers "which one was it", and the tool rows' identity —
                 # which server, which tool, which approval — is not in any of the
                 # columns above.
                 "server", "tool", "approval_id")

# Bound on the search needle. Not a security control — the store's own write cap
# (``store.DRAIN_MAX_FIELD``) is what keeps the COLUMNS bounded — but a LIKE pattern
# is compared against every scanned row, so an unbounded one is free work per row for
# a match that cannot exist.
Q_MAX = 200

# Rows the record view will serve in one page. Lower than the glance's ceiling on
# purpose: these rows carry ``url``, capped at ``store.DRAIN_MAX_FIELD`` each, so the
# response size is bounded by this number rather than by anything the reader intends
# to look at. Paging is what covers the rest.
EVENTS_LIMIT_MAX = 200
EVENTS_LIMIT_DEFAULT = 100


class FilterError(ValueError):
    """A filter the caller must be TOLD about rather than have ignored.

    Every raise here is a case where silently dropping the filter would answer a
    different question than the one asked — an unparseable time range widens the
    result set, an unknown decision word narrows it to nothing — and both look like
    an answer. ``api_views`` turns this into a 400 carrying the message.

    **The message is served VERBATIM to the caller** (``_bad_filter``), so interpolate
    only what the caller already has: their own parameters, and this module's constants.
    Nothing read from the store, the filesystem, or an underlying exception belongs in
    one — that is what keeps the 400 a validation sentence rather than a disclosure. A
    code scanning rule flags the ``str(exc)`` on the other end from the shape alone;
    this contract is why that is a false positive, and it holds only as long as every
    raise site below honours it."""


class Filter:
    """A parsed, validated set of audit filters, and the SQL they become.

    Carries ``active`` because the FRONTEND needs it: the coverage line under the
    decisions table compares what is shown against the recorded total, and with a
    filter applied that total is the size of the MATCHING set, not of the table. A
    reader who cannot tell the two apart reads a narrowed view as a shrunken store."""

    def __init__(self, where: str, params: list, active: bool):
        self.where = where          # "" or "WHERE ..." — never a bare fragment
        self.params = params
        self.active = active

    def and_(self, clause: str, *params) -> Filter:
        """This filter plus one more clause — used for the paging cursor, which is
        not a filter (it does not change WHAT matches, only where the page starts)
        and must therefore never reach ``active`` or the total."""
        joined = f"{self.where} AND {clause}" if self.where else f"WHERE {clause}"
        return Filter(joined, [*self.params, *params], self.active)


def _needle(q: str) -> str:
    """A LIKE pattern that matches ``q`` as a literal substring.

    The escape is load-bearing rather than tidy: unescaped, a ``q`` of ``%`` matches
    every row and one of ``_`` matches any single character, so the search box would
    quietly stop meaning "contains this text" for exactly the inputs an operator
    might paste out of a URL or a rule pattern.

    Case-insensitivity comes from SQLite's LIKE, which folds ASCII only — an
    accented host is matched case-sensitively. Stated rather than fixed: the fix is a
    custom collation or ICU, and hostnames reaching the proxy are A-labels (the
    proxy folds internationalized names at ingress), so the gap is theoretical here."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _finite(ts: float) -> bool:
    """Whether a parsed float is usable as a bound.

    The case both callers need: ``float()`` PARSES ``'nan'`` and ``'inf'`` rather than
    rejecting them, so a value that looks valid reaches the SQL and then compares
    wrongly instead of failing. What that costs differs per caller — see the two call
    sites — but the check does not, which is why it is here and not written twice."""
    return ts == ts and ts not in (float("inf"), float("-inf"))


def _time(name: str, value) -> float:
    """One epoch-seconds bound, validated. ``float('nan')`` is the case worth naming:
    every comparison against NaN is false, so it would silently return an empty list
    — a filter that answers "nothing happened" for a store full of decisions."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        raise FilterError(f"{name} must be a number of seconds since the epoch, "
                          f"not {value!r}") from None
    if not _finite(ts):
        raise FilterError(f"{name} must be a finite number of seconds since the "
                          f"epoch, not {value!r}")
    return ts


def parse(q=None, kind=None, since=None, until=None,
          search=GROUPED_SEARCH) -> Filter:
    """Validate the filter parameters and build the WHERE clause they mean.

    ``since`` is inclusive and ``until`` exclusive, which is what makes adjacent
    windows partition the record instead of double-counting the instant between them.

    Every parameter is optional and the no-filter case yields an empty WHERE, so the
    unfiltered views are the same two queries rather than a separate code path — the
    ordinary case is the one that must not be able to drift."""
    clauses: list[str] = []
    params: list = []

    text = (q or "").strip()
    if text:
        if len(text) > Q_MAX:
            raise FilterError(f"search text is longer than {Q_MAX} characters")
        needle = _needle(text)
        # COALESCE, because every searchable column is nullable: `NULL LIKE x` is
        # NULL, not false, and an OR of NULLs would drop rows that match on another
        # column. A row with no client must still be findable by its host.
        ors = " OR ".join(f"COALESCE({col},'') LIKE ? ESCAPE '\\'" for col in search)
        clauses.append(f"({ors})")
        params.extend([needle] * len(search))

    if kind:
        # Comma-separated, because "allow and deny but not hold" is the useful shape
        # of this facet — a single value would make the common "everything that was
        # refused" question two queries the UI would have to merge.
        wanted = [k.strip().lower() for k in str(kind).split(",") if k.strip()]
        unknown = [k for k in wanted if k not in KINDS]
        if unknown:
            raise FilterError(
                f"unknown kind {', '.join(repr(u) for u in unknown)} — the "
                f"recorded kinds are {', '.join(KINDS)}")
        if wanted:
            clauses.append(f"kind IN ({','.join('?' for _ in wanted)})")
            params.extend(wanted)

    lo = None if since is None else _time("since", since)
    hi = None if until is None else _time("until", until)
    if lo is not None and hi is not None and lo >= hi:
        # An inverted window matches nothing, and "nothing" is a legitimate answer to
        # a valid question — so it has to be refused here or it reads as one.
        raise FilterError(f"since ({since}) must be before until ({until})")
    if lo is not None:
        clauses.append("ts >= ?")
        params.append(lo)
    if hi is not None:
        clauses.append("ts < ?")
        params.append(hi)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return Filter(where, params, active=bool(clauses))


def total(conn, filt: Filter) -> int:
    """How many recorded decisions MATCH — the number both views measure themselves
    against. Follows the filter, and must: a narrowed view compared against the whole
    table would report itself as truncated when it is complete, on every filtered
    query.

    A `COUNT(*)` per poll, and with a text filter it is a scan the `audit_ts` index
    cannot serve (no index answers `LIKE '%x%'`). That is affordable because the
    frontend stops polling in a hidden tab, and the deployment target is one
    operator's machine, where this store is thinned by `make audit-prune` rather than
    left to grow for years."""
    # The interpolation is `Filter.where`, which is built HERE from fixed clause
    # literals — every caller value is a bound parameter in `filt.params` and no
    # request string ever reaches the SQL text. Same reasoning as `ingest._insert_for`,
    # and the same suppression; it applies to the two queries below too.
    return conn.execute(f"SELECT COUNT(*) FROM audit {filt.where}",  # noqa: S608
                        filt.params).fetchone()[0]


def grouped(conn, limit: int, filt: Filter, scan: int) -> list:
    """The glance: rows folded by exactly the fields the UI displays, newest first.

    The group key is the DISPLAYED fields, plus one that is not: ``approval_id``.

    The displayed part is why the tool columns are in it — two `ok` outcomes for
    different tools are different facts, and folding them on `reason` alone (both NULL)
    would show one row saying "2x" with no way to say what it stood for. Egress rows
    carry NULL in all of them and SQLite groups NULLs together, so they fold as if the
    tool columns were absent.

    ``approval_id`` IS THE EXCEPTION TO THAT RULE, and it is deliberate. It is grouped
    BY and not selected — the glance serves what it displays, which is why `url` and
    `port` are absent too, and a 32-character id per row would be payload nothing on
    screen could use. A grant is single-use by construction, so two approval ids are
    two distinct human decisions —
    and folding a row that stands for a SPENT GRANT hides exactly what the approval
    machinery exists to record. Two separately approved `create_pull_request` calls that
    both succeeded are two pull requests; shown as one line with a count, the count is
    the only trace that a second human decision ever happened. Outcomes with no approval
    still fold (NULLs group together), which keeps the high-volume read-only rows quiet.

    The cost is stated because it is real: two approved rows look identical on screen
    and nothing there explains why they did not merge. That is the confusion folding-by-
    displayed-fields exists to prevent, and it is accepted here — two rows that look
    alike is a far smaller wrong than two grants shown as one. The record view, one
    toggle away, shows the ids.

    The filter applies to the RAW ROWS, before folding — so ``scan`` bounds the
    matching events read rather than the events read, and a narrow filter therefore
    reaches as far back as it needs to fill one screen. That is the whole point of
    filtering a bounded window: without it, searching a quiet host would return
    nothing whenever a chatty one had filled the last few thousand rows.

    The cost of that is stated rather than hidden: a filter matching nothing walks the
    `ts` index to the end of the table. Bounded by the table, not by ``scan``."""
    return conn.execute(
        "SELECT kind, stage, host, client, client_class, actor, reason, "  # noqa: S608
        "       server, tool, status, "
        "       COUNT(*) AS n, MAX(ts) AS ts, MIN(ts) AS first_ts "
        f"FROM (SELECT * FROM audit {filt.where} ORDER BY ts DESC LIMIT ?) "
        "GROUP BY kind, stage, host, client, client_class, actor, reason, "
        "         server, tool, status, approval_id "
        "ORDER BY ts DESC LIMIT ?",
        [*filt.params, scan, limit]).fetchall()


def encode_cursor(row) -> str:
    """Where the next page starts, as an opaque-ish string the client hands back.

    ``(ts, id)`` and not ``ts`` alone: two rows can share a timestamp (`time.time()`
    is not guaranteed to advance between two writes), and a cursor that cannot break
    that tie
    either skips a row or repeats one at every page boundary. ``id`` is the store's
    own monotonic key, so the pair is a total order.

    Not an offset, deliberately. ``LIMIT ... OFFSET n`` re-counts the skipped rows on
    every page AND shifts under inserts — and this table takes an insert on every
    governed request, so an offset-paged history would drop rows between pages
    precisely while something interesting was happening."""
    return f"{float(row['ts'])!r}:{int(row['id'])}"


def decode_cursor(cursor: str) -> tuple[float, int]:
    """The ``(ts, id)`` pair ``encode_cursor`` wrote, or a refusal.

    Unparseable and non-finite are ONE error here, deliberately. ``since``/``until``
    name their specific problem because a human types them; a cursor is machine-
    generated, so the only useful thing to say about a bad one is that it did not come
    from a previous page."""
    raw = str(cursor)
    ts, _, rid = raw.rpartition(":")
    try:
        at, row_id = float(ts), int(rid)
        # Refused HERE and not left to the SQL, because a non-finite cursor does not
        # fail — it answers. NaN compares false against every row, so the page comes
        # back empty with `next: None`, which is the wire signal for end-of-record, over
        # a store that is full. An infinity compares true against every row, so it
        # serves page 1 while the pager believes it is deeper. Both are the "looks like
        # data, not like an error" outcome this refusal exists to prevent.
        ok = _finite(at)
    except ValueError:
        ok = False
    if not ok:
        raise FilterError(
            f"malformed page cursor {raw!r} — it must be the value a previous page "
            f"returned as `next`")
    return at, row_id


def events(conn, limit: int, filt: Filter, before: str | None = None) -> tuple[list, str | None]:
    """The record: one row per decision, newest first, keyset-paged.

    Returns the page and the cursor for the page AFTER it (``None`` at the end of the
    record). Asks for one row more than it serves, which is how "is there more" is
    answered without a second count — and the extra row is dropped rather than shown,
    so ``limit`` means what it says.

    ``before`` narrows through ``Filter.and_`` rather than through ``parse``, because
    a cursor is not a filter: it must not change the total, or the page counter would
    walk down to zero as the operator paged back through the history."""
    page = filt
    if before:
        ts, rid = decode_cursor(before)
        page = filt.and_("(ts < ? OR (ts = ? AND id < ?))", ts, ts, rid)
    rows = conn.execute(
        f"SELECT {', '.join(EVENT_COLUMNS)} FROM audit {page.where} "  # noqa: S608
        "ORDER BY ts DESC, id DESC LIMIT ?",
        [*page.params, limit + 1]).fetchall()
    if len(rows) > limit:
        return rows[:limit], encode_cursor(rows[limit - 1])
    return rows, None


def clamp(value, default: int, ceiling: int) -> int:
    """One row is the floor, never zero: a limit of 0 serves an empty list, which the
    frontend cannot tell from "nothing recorded". Both views' limits go through here."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, ceiling))
