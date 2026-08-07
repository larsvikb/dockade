// SPDX-License-Identifier: Apache-2.0
/* dockade control-plane UI — page behaviour.
 *
 * Split out of index.html rather than left inline for two substantive reasons:
 *   - it lets the Content-Security-Policy this app now sends say `script-src 'self'`
 *     instead of `'unsafe-inline'`, which is the difference between a CSP that
 *     constrains an injection and one that merely decorates the response headers;
 *   - it makes the logic testable. `tests/test_control_plane_ui_js.py` runs the pure
 *     helpers below under node; script inline in an HTML file cannot be reached at all.
 *
 * The structure follows that second point, and mirrors the split the Python side
 * already uses: everything that DECIDES something is a pure function at the top,
 * exported for the tests; everything that TOUCHES THE DOM lives inside `start()`,
 * which only runs in a browser. Requiring this file under node must therefore have
 * no side effects — if DOM work ever migrates to the top level, the node test fails
 * at `require`, which is the intended alarm.
 */

// ── pure decision helpers (unit-tested) ─────────────────────────────────────

// Which traffic-light lamp is lit. RED is the stream being down, not a denial:
// being blind is worth more alarm than being busy, because a hold nobody sees
// default-denies when CONTROL_HOLD_TIMEOUT elapses.
function lampState(streamUp, pendingCount) {
  return !streamUp ? "red" : pendingCount ? "amber" : "green";
}

// Reconnect delay for the approvals stream: doubling from 1s, capped at 30s.
// No jitter on purpose — jitter exists to de-synchronise a fleet of clients, and
// this page has exactly one operator, so determinism is worth more than herd
// avoidance (and makes the delay assertable).
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
function backoffDelay(attempt) {
  return Math.min(RECONNECT_MIN_MS * 2 ** Math.max(0, attempt), RECONNECT_MAX_MS);
}

// What changed between the pending list on screen and the one just pushed, keyed by
// approval id so a surviving card is kept and updated IN PLACE.
//
// This is load-bearing, not a rendering nicety. The old code assigned the whole list
// to `innerHTML` on every push (up to once a second), so a card leaving the middle of
// the queue — which happens on its own, since an unresolved hold expires after
// ~120s — shifted every card below it upwards between the operator's eye and their
// click, on a row of buttons that GRANT EGRESS. It also discarded the `disabled`
// state a resolve in flight had just set.
function diffPending(shownIds, list) {
  const incoming = new Set(list.map(a => a.id));
  return {
    add: list.filter(a => !shownIds.includes(a.id)),
    gone: shownIds.filter(id => !incoming.has(id)),
  };
}

// A card that has left the pending queue (its hold expired, or someone else resolved
// it) is marked stale in place rather than yanked out, and swept only once removing
// it cannot move a button under the pointer — i.e. when nothing in the list is
// hovered or focused. The age cap keeps a cursor parked over the list from freezing
// it permanently.
const STALE_MAX_MS = 15000;

// How long a departed card stays on screen REGARDLESS of where the pointer is, by why
// it left. This is not cosmetic: the sweep above triggers as soon as removal cannot
// move a button under the pointer, which for anyone whose cursor is not inside the list
// means the very next 1s tick — so the `expired — default-denied` marker, the one
// departure that reports a GOVERNANCE FAILURE, was on screen for about a second and
// only readable by an operator who happened to be hovering. The audit row survives
// either way, but the card is where it would be noticed.
//
// An expiry therefore gets long enough to actually be read. A resolve gets less (the
// operator performed it and knows the outcome, but may move the mouse away before
// reading the confirmation), and a card resolved elsewhere least — it is purely
// informational. Longer than these and stale cards would compete with real pending
// holds for attention on what is fundamentally a work queue; the Decisions tab is the
// durable record.
const DWELL_MS = { expired: 60000, resolved: 8000, gone: 5000 };

function shouldSweep(busy, ageMs, dwellMs = 0) {
  if (ageMs < dwellMs) return false;   // must have been readable this long
  if (!busy) return true;              // nothing can shift under anyone
  // A parked cursor must not freeze the list forever. No need to also max() this
  // against dwellMs: the check above already returned for everything younger than the
  // floor, so by here ageMs >= dwellMs and a longer floor has already had its say.
  return ageMs >= STALE_MAX_MS;
}

// How long this hold has left, in seconds. A held request BLOCKS the agent and is
// default-denied when CONTROL_HOLD_TIMEOUT elapses, and that deadline was the one
// thing a card could not tell you: it showed `requested 14:32:05` and then vanished,
// so 100 seconds left and 4 seconds left looked identical.
//
// Clamped at BOTH ends, because these are two different clocks — `requestedTs` is the
// backend's time.time(), `nowMs` the browser's. They agree in the intended deployment
// (both are the operator's host), and where they don't, a skewed clock should make the
// countdown wrong rather than absurd: never more than the whole window, never negative.
function holdRemaining(requestedTs, holdTimeout, nowMs) {
  return Math.max(0, Math.min(holdTimeout, requestedTs + holdTimeout - nowMs / 1000));
}

// Below this many seconds left the countdown is called out, because by then the
// decision is being made for the operator rather than by them.
const COUNTDOWN_URGENT_S = 20;
function countdownState(remainingS, holdTimeout) {
  return {
    // At zero this says "expiring", not "expired": the BACKEND's clock decides, so a
    // click may still land. If it doesn't, the 409 path reports that honestly.
    text: remainingS < 1 ? "expiring now" : `expires in ${Math.ceil(remainingS)}s`,
    frac: holdTimeout > 0
      ? Math.max(0, Math.min(1, remainingS / holdTimeout)) : 0,
    urgent: remainingS <= COUNTDOWN_URGENT_S,
  };
}

// Why a card left the pending queue — the wording AND the kind, from one decision, so
// the message and how long it dwells (DWELL_MS above) cannot disagree about what
// happened. Worth distinguishing rather than covering both cases with one hedge: an
// EXPIRY is a governance outcome — the agent was denied because nobody looked in time —
// whereas a card vanishing with time still on the clock means something else resolved
// it. Falls back to the hedge when the window is unknown (/api/config unreachable),
// because then we genuinely cannot tell, and a card we cannot classify must not claim
// the expiry wording.
function departure(remainingS, holdTimeout) {
  if (holdTimeout === null || holdTimeout === undefined) {
    return { kind: "gone", dwellMs: DWELL_MS.gone,
             text: "no longer pending — the hold expired (default-denied) or was " +
                   "resolved elsewhere" };
  }
  if (remainingS <= 0) {
    return { kind: "expired", dwellMs: DWELL_MS.expired,
             text: `expired — no decision within ${Math.round(holdTimeout)}s, ` +
                   "so the request was default-denied" };
  }
  return { kind: "gone", dwellMs: DWELL_MS.gone,
           text: "no longer pending — resolved elsewhere, or the control plane restarted" };
}

// What a `+ persist` click is about to write, for the confirm step. The pattern is
// reported VERBATIM next to its scope rather than described, because exact-host and
// whole-subtree differ by one leading dot and that dot is the entire grant.
function persistPreview(action, option) {
  const pattern = (option && option.pattern) || "";
  return {
    // Defaults to the SAFER reading if the action is somehow absent: a preview that
    // says "block" where an allow was meant is caught by the operator; the reverse
    // is the mistake this whole step exists to prevent.
    verb: (action || "").startsWith("allow") ? "allow" : "block",
    pattern,
    scope: (option && option.scope) || "",
    // Flagged separately so the UI can shout about it: a wildcard covers hosts that
    // have never been requested, and nothing in this UI removes a rule once written.
    wild: pattern.startsWith("."),
    // A standing rule already holding this pattern. `conflict` is the case the backend
    // REFUSES: nothing here replaces a rule, so persisting the opposite action would
    // have written nothing while reporting success. `redundant` is harmless — the
    // policy asked for is already in force — but worth saying so the operator is not
    // told a rule was written when none was.
    existing: (option && option.existing) || null,
    conflict: !!(option && option.existing
                 && option.existing !== ((action || "").startsWith("allow")
                                         ? "allow" : "block")),
    redundant: !!(option && option.existing
                  && option.existing === ((action || "").startsWith("allow")
                                          ? "allow" : "block")),
  };
}

// Hold-cap pressure, as a banner state. Over CONTROL_MAX_PENDING(_PER_CLIENT) the
// control plane fails closed WITHOUT creating an approval, so the agent is denied and
// no card is ever raised — governance degrading to blanket-deny, and from this page
// indistinguishable from a quiet afternoon.
//
// The rejection RECORD is what this reports, not the live level. Saturation is a
// burst: holds drain in seconds, so a gauge alone shows a healthy number to anyone who
// looks a moment later. The gauge is here too, but only as the secondary reading.
//
// Deliberately says nothing about WHICH cap fired in its headline. The operator's
// response is identical either way — a request was refused unheard — and per-client
// detail would be the first thing in this UI to expose client identity. It is carried
// in `detail` for when the distinction between "one agent hammering" and "the whole
// control plane loaded" is the question being asked.
const SATURATION_RECENT_MS = 60000;
const SATURATION_WARN_FRAC = 0.75;

// `dismissedCount` is the rejection total already acknowledged — held in the CONTROL
// PLANE, not here, because a dismissal that a reload undoes is worse than no button at
// all: the operator believes they cleared something and the page disagrees the moment
// they refresh. The banner therefore reports what has happened SINCE the dismissal, and
// returns on the next rejection rather than staying shut. There is no time-based
// auto-clear on purpose: the failure being reported is precisely "nobody was looking",
// so expiring the notice after a minute would re-create the bug for the one population
// it exists to serve. Emphasis decays; the notice does not.
function saturationState(sat, nowMs, dismissedCount = 0) {
  const hidden = { show: false, level: "none", count: 0, text: "", detail: "", lastTs: null };
  if (!sat) return hidden;
  const cap = Number(sat.max_pending) || 0;
  const inFlight = Number(sat.in_flight) || 0;
  const cards = Number(sat.cards) || 0;
  const rejections = Number(sat.rejections) || 0;

  // The UNREAD count, not the lifetime total: after dismissing at 2, a third rejection
  // reads "1 request denied unheard", and the `since` stamp beside it has moved to the
  // dismissal — so the number and the window it covers always describe the same span.
  const unread = rejections - dismissedCount;
  if (unread > 0) {
    const lastTs = Number(sat.last_ts);
    const known = Number.isFinite(lastTs) && lastTs > 0;
    // Age from an ABSOLUTE stamp, computed here — the payload never carries an
    // elapsed value (see _saturation in the control plane).
    const recent = known && (nowMs - lastTs * 1000) < SATURATION_RECENT_MS;
    const plural = unread === 1 ? "" : "s";
    return {
      show: true,
      level: recent ? "recent" : "past",
      count: unread,
      text: `${unread} request${plural} denied unheard — over the hold cap, ` +
            "so no approval card was raised",
      // Never renders a bare zero anywhere (the banner is hidden at zero), and says
      // SINCE WHEN, because this counter is in-memory and a restart silently resets
      // it. "3 since 14:02" cannot be misread as "3 ever".
      detail: (sat.last_host ? `last: ${sat.last_host}` : "last: unknown host") +
              (sat.last_scope
                ? " — hit " + (sat.last_scope === "global"
                    ? "the global cap"
                    : `the per-client cap for ${String(sat.last_scope).replace(/^client /, "")}`)
                : ""),
      lastTs: known ? lastTs : null,
    };
  }

  // No rejections yet: warn only once the cap is close enough that the next burst
  // would hit it. Below that this is noise on a page whose job is the queue.
  if (cap > 0 && inFlight >= cap * SATURATION_WARN_FRAC) {
    return {
      show: true, level: "load", count: 0,
      // The cap counts BLOCKED REQUESTS, and duplicates share a card — so this number
      // can be far above the number of cards on screen. Said out loud when they
      // differ, because "14/16 holds in flight" beside two cards otherwise reads as a
      // queue that has stopped draining.
      text: cards > 0 && cards !== inFlight
        ? `${inFlight}/${cap} requests held on ${cards} card${cards === 1 ? "" : "s"}`
        : `${inFlight}/${cap} holds in flight`,
      detail: inFlight >= cap
        ? "at the cap — further requests are denied without raising a card"
        : "near the cap — further requests would be denied without raising a card",
      lastTs: null,
    };
  }
  return hidden;
}

// How a card announces that one click decides more than one blocked request.
//
// This exists because grouping changed what the buttons MEAN. Duplicate requests share
// a card (see _group_key in the control plane), so "Allow once" can release four
// blocked requests — and an operator granting egress to four while believing it is one
// is exactly the surprise this system is built to prevent. Empty string for the
// ordinary single-request card, so the badge is ABSENT rather than reading "1 request"
// on every row: a marker that appears on every card is one nobody sees on the card
// where it matters.
// No default for a missing or junk count: NaN > 1 is false, so anything we were not
// told renders nothing, which is the same outcome as being told "1". A `|| 1` fallback
// here looked more careful and was unobservable — the two differ in no case this
// function can return.
function requestsLabel(n) {
  return Number(n) > 1
    ? `${Number(n)} identical requests — one decision releases all of them`
    : "";
}

// Nearly every governed request is a CONNECT tunnel, so `stage` is "connect" on almost
// every row and a column of it would be forty repetitions of one word. It is shown only
// when it is something else — which is exactly when it is worth seeing, because a
// plaintext HTTP decision reaching the proxy at all is unusual.
const AUDIT_ORDINARY_STAGE = "connect";

// What may be rendered AS a stage prefix. The proxy sends one of two literals
// (`connect`, `http`) and the field is unvalidated free text all the way to the
// column, so this bounds the shape rather than the vocabulary — a stage a future
// hook adds still shows, which is the point of displaying the unusual ones at all.
//
// A SHAPE bound, not a security control: `/authorize` is reachable only from
// control-net, so the agent cannot reach it, and a compromised proxy could do far
// worse than mislabel a row. The reason is that the prefix sits immediately before
// the host and `.qual` does not wrap, so an unbounded value would run into the host
// it precedes, or push it out of view. This makes "the host cell shows the host" a
// structural property instead of an argued one.
// Case is deliberately NOT constrained. Both current stages are lowercase, but
// silently suppressing a future `TLS` would be a confusing debug for no benefit —
// case does nothing to make a value blend into the host beside it.
const AUDIT_STAGE_SHAPE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,11}$/;

// One audit row, shaped for display. Pure, and deliberately does NOT format the
// timestamp: that is locale-dependent, and a unit test should not have to pin down a
// locale to assert the parts that carry meaning.
function auditRow(r) {
  const stage = (r && r.stage) || "";
  return {
    ts: tsSeconds(r && r.ts),
    decision: (r && r.decision) || "?",
    // Prefixes the HOST, not the decision — `http · example.com`, which reads as the
    // scheme it effectively is. The stage does not qualify the decision at all (a deny
    // at the http stage is the same deny as at connect); it describes how the request
    // was MADE, and the host cell is where the request is identified. It also keeps
    // the decision column uniform, which matters because that is the column an
    // operator scans vertically.
    //
    // Carries its own SEPARATOR rather than relying on a CSS margin. The margin spaced
    // it on screen while `textContent` read "denyhttp" — which is what an operator
    // gets copying a row into a ticket, and what a screen reader says. In a table
    // whose purpose is to be quotable evidence, the copied text is the artefact.
    //
    // ABSENT means "connect, or not recorded" — the two are not distinguished here on
    // purpose. This is a hint that something is unusual, not evidence; the audit table
    // holds the exact value and `make logs-cp` prints it.
    stagePrefix: stage && stage !== AUDIT_ORDINARY_STAGE
                 && AUDIT_STAGE_SHAPE.test(stage) ? `${stage} · ` : "",
    host: (r && r.host) || "",
    // An em dash rather than an empty cell: blank reads as "this column is broken",
    // whereas the honest statement is that no client was recorded for this row.
    client: (r && r.client) || "—",
    reason: (r && r.reason) || "",
    // /api/audit groups rows by exactly the fields rendered here, so `n` is how many
    // identical decisions this line stands for. Empty at n<=1, which is the ordinary
    // case and must look exactly as it did before grouping existed — a bare "1x" on
    // every row would be noise on the majority to annotate the minority.
    //
    // Not a bare count: `firstTs` is what turns it into information. "47x" alone
    // cannot distinguish a burst from a client retrying once a minute for a day, and
    // those call for different responses from whoever is reading.
    repeat: repeatCount(r) > 1 ? `${repeatCount(r)}x` : "",
    // The span's start, unformatted — auditRow stays out of formatting (see above),
    // and the renderer uses fmtStamp rather than a time: the scan behind a group can
    // cover days, so a time-only start reads as minutes ago when it is not.
    firstTs: repeatCount(r) > 1 ? tsSeconds(r && r.first_ts) : null,
    // Denied because the proxy could not REACH the control plane, rather than
    // because policy said no. Same red `deny` tag, entirely different meaning: one
    // is governance working, the other is governance absent and everything being
    // refused. Classified by the backend (see `_audit_view`), never matched on the
    // reason text here — and defaulting to false means a backend that does not send
    // the field renders exactly as it did before this existed.
    failClosed: !!(r && r.fail_closed),
  };
}

// How many raw decisions a grouped row folds. Absent (an ungrouped payload, or a
// backend older than grouping) reads as 1, so the row renders exactly as it always
// did rather than as a broken group.
function repeatCount(r) {
  const n = Number(r && r.n);
  return Number.isFinite(n) && n > 1 ? Math.floor(n) : 1;
}

// What revoking a rule is about to do, for the confirm step — and whether it is the
// safe direction or the dangerous one.
//
// The two are opposites and a single "delete this?" hides that. Revoking an ALLOW
// tightens: the host reverts to unknown and the next request is held for a human.
// Revoking a BLOCK loosens — an explicit operator denial becomes "unknown", which
// can then be approved by someone who never knew it had been deliberately refused.
// Both land on `hold`; only the direction differs, so only wording can carry it.
//
// Says the CONSEQUENCE rather than the pattern alone, the same discipline
// persistPreview uses. "Delete .github.com?" asks about a row. "Requests to
// .github.com will no longer be allowed" asks about the world.
//
// Seed rules are reported as not revocable at all, with the reason. The backend
// refuses them too — this is the explanation, not the control.
function revokePreview(rule) {
  const pattern = (rule && rule.pattern) || "";
  const action = (rule && rule.action) || "";
  if (rule && rule.source === "seed") {
    return {
      allowed: false,
      pattern,
      danger: false,
      text: `${pattern} comes from the policy seed and cannot be revoked here. `
          + `Edit policies/egress-allowlist.txt and rebuild.`,
    };
  }
  // Unknown action is treated as the dangerous direction: a confirm that
  // under-warns is the failure worth avoiding, and an over-warned allow costs the
  // operator one extra sentence.
  const loosening = action !== "allow";
  return {
    allowed: true,
    pattern,
    danger: loosening,
    text: loosening
      ? `Requests to ${pattern} will no longer be blocked. They will be held for `
        + `approval instead — and can then be allowed.`
      : `Requests to ${pattern} will no longer be allowed. They will be held for `
        + `approval instead.`,
  };
}

// What a screen reader should hear when the pending queue changes.
//
// Announced from a SEPARATE element rather than by making the card list a live
// region, which is the obvious move and would be actively hostile: every card holds
// a countdown that rewrites once a second, so the region would read a timer aloud
// instead of the arrival, continuously, for the life of the hold. `aria-relevant`
// could suppress some of that, but only by relying on how each screen reader
// classifies a text replacement inside an existing node.
//
// ARRIVALS only. A departure is either the operator's own click, or an expiry the
// card already announces in place and keeps on screen long enough to read (DWELL_MS).
//
// The host is in the message because the host IS the decision. "One approval
// pending" says something is waiting; it does not say whether the agent is asking
// for the package registry or for an address nobody recognises, which is the whole
// question the operator is being woken up to answer.
//
// KNOWN LIMIT: identical consecutive text is not re-announced by a live region, so
// two arrivals for the same host with the same total would speak once. The total
// differs in almost every real case, and the alternative — salting the string to
// force a change — makes the region announce noise on purpose.
function pendingAnnouncement(added, total) {
  if (!added || !added.length) return "";
  if (added.length === 1) {
    const host = (added[0] && added[0].host) || "an unnamed host";
    return total > 1
      ? `Approval needed for ${host}. ${total} pending.`
      : `Approval needed for ${host}.`;
  }
  return `${added.length} new approvals needed. ${total} pending.`;
}

// What the decisions list is a WINDOW ONTO, in words.
//
// The list shows the most recent groups and has always silently truncated. Grouping
// made that harder to notice rather than easier: forty rows carrying "47x" read like
// a complete account, because the counts appear to explain the volume away.
//
// Compares raw decisions with raw decisions — the folded count on screen against the
// total recorded — so the two numbers are the same kind of thing. Rows-versus-
// decisions would be a ratio of unlike quantities and would read as truncation even
// when nothing was truncated.
//
// Silent when the window covers everything, which is the ordinary state on a quiet
// system and the state where a permanent "showing 12 of 12" is pure furniture.
function coverageSummary(rows, total) {
  const shown = (rows || []).reduce((n, r) => n + repeatCount(r), 0);
  const t = Number(total);
  // An absent or nonsensical total says nothing rather than guessing. A backend
  // without the field must not make this claim "0 of 0".
  if (!Number.isFinite(t) || t <= shown) {
    return { show: false, level: "none", text: "", shown, total: Number.isFinite(t) ? t : null };
  }
  return {
    show: true,
    level: "none",
    shown,
    total: t,
    text: `Showing the ${shown.toLocaleString()} most recent of `
        + `${t.toLocaleString()} recorded decisions.`,
  };
}

// What the decisions list should say about a run of FAIL-CLOSED denials, over and
// above marking the rows themselves.
//
// The rows alone are not enough. They are red `deny` lines among other red `deny`
// lines, and the situation this exists for is precisely the one where nobody is
// suspicious yet — governed egress refusing everything while the page, the rules and
// the health checks all look normal. A count stated in words is what the per-row
// edge can only imply, and it is also what keeps colour from being the sole cue.
//
// SCOPED TO WHAT WAS SERVED, deliberately. It counts the rows on screen rather than
// querying the store, so it can never claim more than the reader can scroll to, and
// it says "in the decisions below" rather than "now" — these rows may be an outage
// that has already ended, and a banner asserting a live failure that has since
// recovered would be its own kind of lie.
//
// `repeatCount` and not the row count: the grouped view folds identical denials, so
// one line can stand for hundreds of refused requests, which is exactly the number
// that conveys the scale of an outage.
function outageSummary(rows) {
  const hit = (rows || []).filter(r => r && r.fail_closed);
  if (!hit.length) return { show: false, level: "none", text: "", decisions: 0, hosts: 0 };
  const decisions = hit.reduce((n, r) => n + repeatCount(r), 0);
  const hosts = new Set(hit.map(r => (r && r.host) || "")).size;
  return {
    show: true,
    level: "warn",
    decisions,
    hosts,
    text: `${decisions} request${decisions === 1 ? "" : "s"} to `
        + `${hosts} host${hosts === 1 ? "" : "s"} below `
        + `${decisions === 1 ? "was" : "were"} denied because the proxy could not `
        + `reach the control plane. That is an outage, not policy — governed egress `
        + `fails closed, so ${decisions === 1 ? "it was" : "they were"} refused `
        + `without any rule being consulted.`,
  };
}

// What the decisions list should say ABOUT ITSELF. It exists because an empty table
// and a failed poll rendered identically — and the header cannot disambiguate them
// either, since `conn` reports the SSE stream while this list is filled by a separate
// poll that can be failing while the stream is healthy.
//
// A failed refresh does NOT clear the rows. Stale decisions with a warning above them
// are more useful than an empty table, provided the staleness is stated — which is the
// entire difference between this and what it replaces.
// The three states are the same for EVERY polled list, so the logic lives once and
// each view supplies only its sentences. Both views are filled by their own poll and
// both can therefore be stale while the header reads `live`; sharing this is what
// stops one of them growing the honesty the other has (the decisions table got it
// first, and the policy table then sat silently swallowing failures for as long).
function pollStatus(texts, rowCount, failed, loaded) {
  if (failed) {
    return { show: true, level: "warn",
             text: loaded ? texts.stale : texts.cold };
  }
  // Before the first response there is nothing to claim in either direction; saying
  // "none yet" here would be a positive all-clear the page has not earned.
  if (!loaded) return { show: false, level: "none", text: "" };
  if (rowCount === 0) return { show: true, level: "none", text: texts.empty };
  return { show: false, level: "none", text: "" };
}

const AUDIT_STATUS_TEXT = {
  stale: "Could not refresh — these are the last decisions loaded successfully " +
         "and may be out of date.",
  cold: "Could not load recent decisions — the control plane may be unreachable.",
  empty: "No decisions recorded yet. Every allow, deny and hold appears here " +
         "as it happens.",
};
function auditStatus(rowCount, failed, loaded) {
  return pollStatus(AUDIT_STATUS_TEXT, rowCount, failed, loaded);
}

// The policy view's wording is NOT the decisions view's with a noun swapped, because
// the consequence of staleness differs. A stale decisions table is old history, which
// is merely unhelpful. A stale policy table misstates WHAT IS CURRENTLY ALLOWED — an
// operator deciding a hold reads this to see what already stands, so it has to say
// plainly that it may no longer be in force.
const RULES_STATUS_TEXT = {
  stale: "Could not refresh — this is the last policy loaded successfully and may " +
         "no longer be what is in force.",
  cold: "Could not load the standing policy — the control plane may be unreachable.",
  // Not a neutral "no rules": with an empty table nothing matches, so `_decide`
  // returns hold for every host. That is a fact about what happens next, which is
  // what an operator needs, rather than an observation about a table being short.
  empty: "No standing rules, so every request is unknown and will be held for " +
         "approval.",
};
function rulesStatus(rowCount, failed, loaded) {
  return pollStatus(RULES_STATUS_TEXT, rowCount, failed, loaded);
}

// What a Dismiss click acknowledges. A one-line function only because it must be the
// SAME expression the optimistic local hide uses and the one the POST body carries:
// with the number written twice, the two can disagree, and the failure is silent —
// the banner hides, the POST returns 200, and the dismissal simply does not persist.
// As a pure function it is also the only part of the dismiss path a unit test can
// reach, `start()` being deliberately unverified.
function ackCount(sat) {
  return sat ? Math.max(0, Number(sat.rejections) || 0) : 0;
}

// ── timestamps ──────────────────────────────────────────────────────────────
// Formatted HERE rather than deferred to the viewer's locale, and that is a
// correctness choice rather than a preference. `toLocaleString()` with no locale
// argument takes its format from the BROWSER's language preference — not the OS
// regional setting and not the page's `lang` — so one operator read an audit row as
// `8/6/2026` while another read the same row as `06/08/2026`. Those are different
// dates. A record whose whole purpose is to say WHEN something happened cannot mean
// two things depending on who opened the page.
//
// So: fixed ISO-8601 ordering, local time, 24-hour. Unambiguous, sortable, identical
// on every machine — and pinnable by a test, which a locale-driven format deliberately
// was not.
const pad2 = n => String(n).padStart(2, "0");

// Epoch seconds, or null if there is no usable value.
//
// The explicit null/undefined/"" rejection is the whole reason this is a function.
// `Number(null)` and `Number("")` are 0, NOT NaN — so a missing timestamp sails
// through every plausible numeric guard and renders as `1970-01-01`, which in an audit
// row reads as a real (if absurd) decision time rather than as missing data. A test
// caught exactly that.
function tsSeconds(ts) {
  if (ts === null || ts === undefined || ts === "") return null;
  const n = Number(ts);
  return Number.isFinite(n) ? n : null;
}

// A Date, or null if the input cannot make one. Every formatter below returns "" for
// that case rather than the string "Invalid Date", which is what `new Date(NaN)`
// renders and reads like a decision the system made.
function tsDate(ts) {
  const n = tsSeconds(ts);
  if (n === null) return null;
  const d = new Date(n * 1000);
  return Number.isNaN(d.getTime()) ? null : d;
}

// HH:MM:SS — for things happening NOW, where the date is today by construction: a
// card's hold countdown, the saturation banner's "last rejection at".
function fmtTime(ts) {
  const d = tsDate(ts);
  return d ? `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
           : "";
}

// YYYY-MM-DD HH:MM:SS — for rows that persist and are read in order. Rules live for
// weeks and forty audit rows routinely span midnight, where a time-only stamp reads
// as out of order at exactly the moment ordering matters.
function fmtStamp(ts) {
  const d = tsDate(ts);
  return d ? `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} `
             + fmtTime(ts)
           : "";
}

// The unambiguous instant, for a `title` on audit rows. The displayed stamp is LOCAL
// and carries no offset, which is fine while an operator is reading their own screen
// and stops being fine the moment a row is correlated against `make logs-cp` or pasted
// into a security advisory.
function fmtInstant(ts) {
  const d = tsDate(ts);
  return d ? d.toISOString() : "";
}

// ── the page ────────────────────────────────────────────────────────────────

function start() {
  const esc = s => (s ?? "").toString().replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ── state the indicators read ─────────────────────────────────────────────
  let pendingCount = 0;      // approvals waiting on a human
  let streamUp = false;      // is the SSE feed actually delivering?
  let policySig = null;      // signature of the rules, to detect change
  let policyUnseen = false;  // policy changed while another view was showing
  // Seconds a hold waits before it default-denies, from GET /api/config. Stays null
  // until the backend says, and the page works without it — one fewer thing that can
  // stop an approval from reaching a human.
  let holdTimeout = null;

  // ── views ─────────────────────────────────────────────────────────────────
  const VIEWS = ["approvals", "decisions", "policy"];
  const viewFromHash = () =>
    VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "approvals";
  let current = viewFromHash();

  function showView(name) {
    current = VIEWS.includes(name) ? name : "approvals";
    for (const v of VIEWS) {
      document.getElementById("view-" + v).hidden = v !== current;
      const tab = document.getElementById("tab-" + v);
      tab.setAttribute("aria-selected", String(v === current));
      tab.tabIndex = v === current ? 0 : -1;
    }
    // Opening the policy view IS the acknowledgement that its change was seen.
    if (current === "policy") { policyUnseen = false; }
    updateIndicators();
  }

  for (const v of VIEWS) {
    document.getElementById("tab-" + v).addEventListener("click", () => {
      // Drive through the hash so a reload, a bookmark and the back button all
      // land on the same view; hashchange calls showView.
      if (viewFromHash() === v) showView(v); else location.hash = v;
    });
  }
  window.addEventListener("hashchange", () => showView(viewFromHash()));

  // Arrow-key navigation between tabs, as the tablist role implies.
  document.querySelector("nav.tabs").addEventListener("keydown", e => {
    const i = VIEWS.indexOf(current);
    let next = null;
    if (e.key === "ArrowRight") next = VIEWS[(i + 1) % VIEWS.length];
    if (e.key === "ArrowLeft") next = VIEWS[(i - 1 + VIEWS.length) % VIEWS.length];
    if (e.key === "Home") next = VIEWS[0];
    if (e.key === "End") next = VIEWS[VIEWS.length - 1];
    if (!next) return;
    e.preventDefault();
    location.hash = next;
    document.getElementById("tab-" + next).focus();
  });

  // ── indicators (favicon + title + badges) ─────────────────────────────────
  // A traffic light whose three lamps map onto the three states that matter (see
  // lampState). All three keep their OWN colour at every state and only the
  // brightness moves: with the inactive lamps greyed out the icon is a dark
  // rectangle carrying one small coloured dot and stops reading as a traffic light
  // at 16px, which is the size that actually matters. The cost is honest —
  // dim-to-bright is a weaker peripheral signal than grey-to-colour, so the `(n)`
  // title prefix does most of the work of catching the eye in a background tab.
  const LAMP_DIM = 0.26;
  const LAMPS = [["red", 9, "#d1242f"], ["amber", 16, "#d29922"],
                 ["green", 23, "#2ea043"]];
  const favicon = document.getElementById("favicon");

  function faviconURI(state) {
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">` +
      `<rect x="8" y="1" width="16" height="30" rx="5" fill="#0f1115"` +
      ` stroke="#39404e" stroke-width="1.5"/>` +
      LAMPS.map(([name, cy, colour]) =>
        `<circle cx="16" cy="${cy}" r="4" fill="${colour}"` +
        ` fill-opacity="${state === name ? 1 : LAMP_DIM}"/>`).join("") +
      `</svg>`;
    // encodeURIComponent, not raw: '#' in the colours would otherwise start a
    // fragment and truncate the SVG.
    return "data:image/svg+xml," + encodeURIComponent(svg);
  }

  let faviconState = null;

  function updateIndicators() {
    const state = lampState(streamUp, pendingCount);
    // Only touch the <link> when the state actually CHANGES. This runs on every
    // poll (~15x a minute, forever), and browsers treat each href assignment as a
    // fresh favicon load — which is wasted work at best and a visibly flickering
    // tab icon at worst.
    if (state !== faviconState) {
      faviconState = state;
      favicon.href = faviconURI(state);
    }
    // Title prefix so a BACKGROUND tab shows the count in the tab strip — the
    // case that matters, since nobody watches this page while working.
    document.title = (pendingCount ? `(${pendingCount}) ` : "") +
      "dockade control plane";

    const pa = document.getElementById("badge-approvals");
    pa.textContent = String(pendingCount);
    pa.classList.toggle("hot", pendingCount > 0);

    const pb = document.getElementById("badge-policy");
    pb.classList.toggle("unseen", policyUnseen && current !== "policy");
  }

  // ── pending approvals, keyed by approval id ───────────────────────────────
  const pendingEl = document.getElementById("pending");
  const emptyEl = document.getElementById("pending-empty");
  const pendingLive = document.getElementById("pending-live");
  // id -> entry (see buildCard for the shape)
  //   state: pending → resolving → resolved
  //          pending → confirming → resolving → resolved   (the *_persist actions)
  //          any of the above → stale
  const cards = new Map();

  const ACTIONS = [
    ["allow_once", "Allow once", "allow"],
    ["allow_persist", "Allow + persist rule", "allow"],
    ["deny_once", "Deny once", "deny"],
    ["deny_persist", "Deny + persist rule", "deny"],
  ];

  // Built with createElement/textContent rather than an HTML string: the host, url
  // and client on an approval are AGENT-CONTROLLED, and a text node needs no
  // escaping to be safe, so the question of whether esc() covers every context
  // does not arise for this list at all. The persist patterns are derived from that
  // same host by the backend, so they get the same treatment.
  function buildCard(a) {
    const el = document.createElement("div");
    el.className = "card";
    el.dataset.id = a.id;

    const host = document.createElement("div");
    host.className = "host";
    host.textContent = a.host + (a.port ? ":" + a.port : "");

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [a.proto || null, `requested ${fmtTime(a.ts)}`,
                        a.client ? `from ${a.client}` : null, a.url || null]
      .filter(Boolean).join(" · ");

    // The hold countdown, hidden until /api/config has told us the window — the page
    // does not invent a deadline it cannot know.
    const cd = document.createElement("div");
    cd.className = "countdown";
    cd.hidden = true;
    const cdText = document.createElement("span");
    cdText.className = "cdtext";
    const bar = document.createElement("span");
    bar.className = "bar";
    const fill = document.createElement("i");
    bar.append(fill);
    cd.append(cdText, bar);

    // Immediately above the buttons, not up with the host line: it qualifies what the
    // click does, so it belongs where the eye already is at the moment of clicking.
    const dup = document.createElement("div");
    dup.className = "dup";
    dup.hidden = true;

    const actions = document.createElement("div");
    actions.className = "actions";

    const msg = document.createElement("div");
    msg.className = "cardmsg";
    msg.hidden = true;

    const entry = {
      el, actions, msg, cd, cdText, fill, dup,
      state: "pending", staleAt: null, dwell: 0,
      ts: a.ts,
      // What a persist may write, as the BACKEND will accept it. A backend that has
      // not been restarted into this change sends no options; fall back to the exact
      // host, which is also what resolve() defaults to when no pattern is sent.
      options: (a.persist_options && a.persist_options.length)
        ? a.persist_options
        : [{ pattern: (a.host || "").toLowerCase(), scope: "exact host" }],
      action: null,            // which *_persist action the confirm panel is for
    };

    for (const [action, label, kind] of ACTIONS) {
      const b = document.createElement("button");
      b.className = kind;
      b.textContent = label;
      // The two `*_persist` actions write standing policy, so they open the confirm
      // panel; the `*_once` actions decide this request only and stay a single click.
      b.addEventListener("click", () => action.endsWith("persist")
        ? askPersist(a, action)
        : resolve(a, action));
      actions.append(b);
    }

    buildConfirm(entry, a);
    setRequests(entry, a.requests);
    el.append(host, meta, cd, dup, actions, entry.confirm, msg);
    return entry;
  }

  function setRequests(entry, n) {
    const text = requestsLabel(n);
    entry.dup.textContent = text;
    entry.dup.hidden = !text;
  }

  // ── the confirm step for a `+ persist` ────────────────────────────────────
  // Justified by IRREVERSIBILITY rather than by risk. A persisted rule outlives the
  // session, is what makes every future request to that host skip the hold entirely,
  // and nothing in this UI removes it — undoing a mis-click means hand-editing SQLite
  // in a named volume. So the click that writes policy is now two clicks, and the
  // second one names the pattern and lets the operator narrow or widen it.
  //
  // Laid out BELOW the action row, which stays exactly where it was. That is
  // deliberate, and the same concern that drove keyed rendering: if the confirm button
  // appeared where the pointer already is, a double-click on "Allow + persist rule"
  // would sail straight through the confirmation it had just opened.
  function buildConfirm(entry, a) {
    const box = document.createElement("div");
    box.className = "confirm";
    box.hidden = true;

    const what = document.createElement("div");
    what.className = "cwhat";

    const label = document.createElement("label");
    label.className = "cscope";
    label.append(document.createTextNode("rule "));
    const select = document.createElement("select");
    for (const opt of entry.options) {
      const o = document.createElement("option");
      o.value = opt.pattern;
      o.textContent = `${opt.pattern} — ${opt.scope}`;
      select.append(o);
    }
    // Narrowest first from the backend, so the default selection is the safest one.
    select.selectedIndex = 0;
    select.addEventListener("change", () => renderPreview(entry));
    label.append(select);

    const warn = document.createElement("div");
    warn.className = "cwarn";
    warn.hidden = true;

    const cactions = document.createElement("div");
    cactions.className = "cactions";
    const go = document.createElement("button");
    go.className = "confirmgo";
    const cancel = document.createElement("button");
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => cancelPersist(entry));
    go.addEventListener("click", () => resolve(a, entry.action, select.value));
    cactions.append(go, cancel);

    // Escape backs out, as a dialog-shaped thing should.
    box.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); cancelPersist(entry); }
    });

    box.append(what, label, warn, cactions);
    Object.assign(entry, { confirm: box, cwhat: what, cwarn: warn,
                           select, confirmBtn: go });
  }

  function renderPreview(entry) {
    const opt = entry.options.find(o => o.pattern === entry.select.value)
      || entry.options[0];
    const p = persistPreview(entry.action, opt);
    // Rebuilt as nodes so the pattern (derived from an agent-chosen host) is text,
    // never markup — same reason as the card body above.
    entry.cwhat.replaceChildren();
    entry.cwhat.append(document.createTextNode(`Writes a standing rule to ${p.verb} `));
    const code = document.createElement("code");
    code.textContent = p.pattern;
    entry.cwhat.append(code, document.createTextNode(` — ${p.scope}.`));
    // A conflict outranks the wildcard warning: the wildcard caution is about a rule
    // that would be too broad, while a conflict means the click cannot write a rule at
    // all. Saying the second is more urgent than saying the first.
    const warn = p.conflict
      ? `⚠ ${p.pattern} is already a standing ${p.existing.toUpperCase()} rule, and ` +
        "nothing here replaces one. This will be refused — decide the request with " +
        "a one-off action, or pick a different pattern."
      : p.redundant
        ? `Already a standing ${p.existing.toUpperCase()} rule — confirming decides ` +
          "this request and leaves policy unchanged."
        : p.wild
          ? `⚠ Wildcard: this covers ${p.pattern.slice(1)} and every subdomain of ` +
            "it, including hosts nothing has requested yet."
          : "";
    entry.cwarn.hidden = !warn;
    entry.cwarn.textContent = warn;
    entry.cwarn.className = "cwarn" + (p.conflict ? " bad" : "");
    // Disabled rather than merely warned about, because the backend will refuse it:
    // letting the click through would spend a round trip to arrive at the same place.
    // The panel still opens, so the operator can read WHY and pick another pattern.
    entry.confirmBtn.disabled = p.conflict;
    entry.confirmBtn.textContent = p.conflict
      ? `Cannot ${p.verb} ${p.pattern} — already a ${p.existing} rule`
      : `Confirm — ${p.verb} ${p.pattern} from now on`;
    entry.confirmBtn.className = "confirmgo " + (p.verb === "allow" ? "allow" : "deny");
  }

  function askPersist(a, action) {
    const entry = cards.get(a.id);
    if (!entry || entry.state !== "pending") return;
    entry.state = "confirming";
    entry.action = action;
    entry.el.classList.add("confirming");
    // The action row is disabled while the panel is open, so the only live buttons are
    // Confirm and Cancel — a stray second click cannot fire a different action.
    disableActions(entry, true);
    renderPreview(entry);
    entry.confirm.hidden = false;
    // Focus lands on the pattern select when Confirm is disabled by a conflict —
    // focusing a disabled button drops focus to the body, which would strand a
    // keyboard operator outside the panel that just opened, with Escape (bound on the
    // panel) no longer reaching anything.
    (entry.confirmBtn.disabled ? entry.select : entry.confirmBtn).focus();
  }

  function cancelPersist(entry) {
    if (entry.state !== "confirming") return;
    entry.state = "pending";
    entry.action = null;
    entry.confirm.hidden = true;
    entry.el.classList.remove("confirming");
    disableActions(entry, false);
  }

  function setMessage(entry, text, kind) {
    entry.msg.hidden = false;
    entry.msg.textContent = text;
    entry.msg.className = "cardmsg" + (kind ? " " + kind : "");
  }

  function disableActions(entry, off) {
    entry.actions.querySelectorAll("button").forEach(b => { b.disabled = off; });
  }

  // Takes the whole `{text, dwellMs}` from `departure()` rather than the two
  // separately, so the message and how long it stays readable cannot be passed apart —
  // omitting the dwell would silently give an expiry the 5s treatment and undo the fix
  // above, with nothing failing. The 409 caller below builds the same shape by hand.
  function markStale(entry, d) {
    entry.state = "stale";
    entry.staleAt = Date.now();
    entry.dwell = d.dwellMs;
    entry.el.classList.remove("busy", "confirming");
    entry.el.classList.add("stale");
    // A card nobody can act on any more shows neither a deadline nor a half-finished
    // confirmation — the message below replaces both.
    entry.confirm.hidden = true;
    entry.cd.hidden = true;
    disableActions(entry, true);
    setMessage(entry, d.text, "bad");
  }

  // `pattern` is sent only for the `*_persist` actions, and only ever a value the
  // backend itself offered on this approval (it re-derives and re-validates the set,
  // so the choice is bounded there too, not merely here).
  async function resolve(a, action, pattern) {
    const entry = cards.get(a.id);
    if (!entry || (entry.state !== "pending" && entry.state !== "confirming")) return;
    entry.state = "resolving";
    entry.confirm.hidden = true;
    entry.el.classList.remove("confirming");
    entry.el.classList.add("busy");
    disableActions(entry, true);
    setMessage(entry, "resolving…");
    try {
      const r = await fetch(`/approvals/${encodeURIComponent(a.id)}/resolve`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pattern ? { action, pattern } : { action }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        // Report the outcome HERE instead of waiting for the stream to drop the
        // card. The card disappearing on the next SSE tick used to be the ONLY
        // feedback, so with the feed down — precisely the state the reconnect logic
        // below exists for — a successful approval was indistinguishable from a
        // hung click.
        entry.state = "resolved";
        entry.staleAt = Date.now();
        // Long enough to read the outcome after moving the mouse off the list, which is
        // the natural thing to do right after clicking — before this, the card could go
        // in about a second and take the confirmation with it.
        entry.dwell = DWELL_MS.resolved;
        entry.el.classList.remove("busy");
        entry.cd.hidden = true;
        entry.el.classList.add(d.outcome === "allow" ? "done-allow" : "done-deny");
        // The pattern comes back from the BACKEND, so this reports what was stored
        // rather than what was clicked — and it is the exact string an operator would
        // have to go and delete.
        //
        // `persisted` now means A ROW WAS WRITTEN, read from the insert's rowcount.
        // The comment that used to sit here reasoned that "standing rule" was true
        // even when nothing was written, "because that only happens when the identical
        // rule was already there". That was the bug: it also happened when the
        // OPPOSITE rule was there, and the card cheerfully confirmed a block that
        // policy had discarded. The conflicting case is refused outright now, and the
        // already-in-place case says so in its own words.
        setMessage(entry,
          (d.outcome === "allow" ? "✓ allowed" : "✕ denied") +
          (d.persisted ? ` · standing rule written: ${d.pattern || a.host.toLowerCase()}`
            : d.already_present
              ? ` · standing rule already in place: ${d.pattern || a.host.toLowerCase()}`
              : " · this request only"),
          d.outcome === "allow" ? "ok" : "bad");
      } else if (r.status === 409 && d.conflict) {
        // A persist that would contradict an existing rule. NOT a stale card: the
        // approval is deliberately left pending so the operator can choose again, so
        // the buttons come back — the same treatment as a rejected pattern.
        entry.state = "pending";
        entry.el.classList.remove("busy");
        disableActions(entry, false);
        setMessage(entry, d.detail || "that rule already exists", "bad");
      } else if (r.status === 409) {
        // Backend says it is no longer pending (expired, or resolved elsewhere).
        // Re-enabling the buttons would only invite a second failing click.
        markStale(entry, {
          dwellMs: DWELL_MS.gone,
          text: "no longer pending — the hold expired or was already resolved" });
      } else {
        entry.state = "pending";
        entry.el.classList.remove("busy");
        disableActions(entry, false);
        setMessage(entry, `could not resolve: ${d.detail || "HTTP " + r.status}`, "bad");
      }
    } catch (e) {
      entry.state = "pending";
      entry.el.classList.remove("busy");
      disableActions(entry, false);
      setMessage(entry, `request failed: ${e}`, "bad");
    }
    refreshAudit();
    // A "+ persist" action just changed standing policy. Refresh it at once so the
    // Policy badge marks the change while you are still on the approvals view.
    refreshRules();
  }

  function listBusy() {
    return pendingEl.matches(":hover") ||
      (document.activeElement !== null && pendingEl.contains(document.activeElement));
  }

  function sweep() {
    const busy = listBusy();
    const now = Date.now();
    for (const [id, entry] of cards) {
      if (entry.staleAt === null) continue;
      if (shouldSweep(busy, now - entry.staleAt, entry.dwell)) {
        entry.el.remove();
        cards.delete(id);
      }
    }
    emptyEl.hidden = cards.size > 0;
  }

  // ── saturation banner ─────────────────────────────────────────────────────
  const satEl = document.getElementById("saturation");
  const satText = document.getElementById("sat-text");
  const satDetail = document.getElementById("sat-detail");
  const satDismiss = document.getElementById("sat-dismiss");
  let lastSaturation = null;
  // Optimistic acknowledgement, held only until the stream echoes the backend's own.
  // The POST plus the next push is up to a second, and a banner that lingers after the
  // click reads as a button that did not work.
  let localAck = 0;

  satDismiss.addEventListener("click", async () => {
    // One expression for both the optimistic hide and the POST body, so they cannot
    // disagree. Acknowledging a COUNT rather than sending a "dismiss" is what lets a
    // rejection that landed between the render and the click survive as unread.
    const n = ackCount(lastSaturation);
    localAck = n;
    renderSaturation();
    try {
      const r = await fetch("/api/saturation/ack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: n }),
      });
      if (!r.ok) throw new Error(String(r.status));
      // Take the acknowledgement the backend actually RECORDED, not the one we hoped
      // to send — the same reason a resolved card reports `d.pattern` from the
      // response. If this page ever sends the wrong count, the banner reappears
      // immediately instead of the dismissal quietly failing to persist.
      localAck = Number((await r.json()).acknowledged) || 0;
      renderSaturation();
    } catch (e) {
      // The POST is the whole point — it is what survives a reload. If it failed, put
      // the banner back rather than leaving the operator believing it stuck.
      localAck = 0;
      renderSaturation();
    }
  });

  function renderSaturation() {
    // Whichever acknowledgement is further along: the backend's, or the click we have
    // not had confirmed yet.
    const acked = Math.max(
      localAck, lastSaturation ? Number(lastSaturation.acknowledged) || 0 : 0);
    const s = saturationState(lastSaturation, Date.now(), acked);
    satEl.hidden = !s.show;
    if (!s.show) return;
    satEl.className = "saturation " + s.level;
    satText.textContent = s.text;
    // The time is formatted HERE rather than in the pure helper, which returns the raw
    // stamp: locale formatting is not something a unit test should have to pin down.
    satDetail.textContent = s.lastTs
      ? `${s.detail} at ${fmtTime(s.lastTs)} · counted since ${fmtTime(lastSaturation.since)}`
      : s.detail;
    // Only the acknowledgeable state offers the button; a load warning clears itself
    // when the holds drain, so dismissing it would mean nothing.
    satDismiss.hidden = s.level === "load";
  }

  function renderPending(list) {
    pendingCount = list.length;
    const { add, gone } = diffPending([...cards.keys()], list);
    // Before the DOM work, so the announcement reflects what is arriving rather than
    // racing the cards that carry it.
    const say = pendingAnnouncement(add, list.length);
    if (say) pendingLive.textContent = say;
    for (const a of add) {
      const entry = buildCard(a);
      cards.set(a.id, entry);
      pendingEl.append(entry.el);
    }
    // The one field on a SURVIVING card that changes while it waits: how many blocked
    // requests it now speaks for. Updated in place on every push, because a retry can
    // join between the render and the click — a count frozen at first paint would
    // understate what the button does at the moment it is pressed. Left alone once the
    // card is resolving or stale: by then the number is history, and rewriting it
    // under a click that has already been sent would be a lie in the other direction.
    for (const a of list) {
      const entry = cards.get(a.id);
      if (entry && (entry.state === "pending" || entry.state === "confirming")) {
        setRequests(entry, a.requests);
      }
    }
    for (const id of gone) {
      const entry = cards.get(id);
      if (entry.state === "pending" || entry.state === "confirming"
          || entry.state === "resolving") {
        // Say WHICH way it went. An expiry is a decision the operator failed to make
        // in time and the agent was denied for it; a card leaving with time left is
        // somebody or something else resolving it. Both used to read the same — and
        // the expiry now also stays put long enough to be read (see DWELL_MS).
        const d = departure(
          holdTimeout === null ? null
            : holdRemaining(entry.ts, holdTimeout, Date.now()),
          holdTimeout);
        markStale(entry, d);
      }
    }
    sweep();
    updateIndicators();
  }

  // Redrawn once a second for every live card. Cheap, and the only thing that makes a
  // deadline legible: the text for a reading, the bar for a glance.
  function updateCountdowns() {
    if (holdTimeout === null) return;
    const now = Date.now();
    for (const entry of cards.values()) {
      if (entry.state === "resolved" || entry.state === "stale") continue;
      const cs = countdownState(
        holdRemaining(entry.ts, holdTimeout, now), holdTimeout);
      entry.cd.hidden = false;
      entry.cdText.textContent = cs.text;
      entry.fill.style.width = `${(cs.frac * 100).toFixed(1)}%`;
      entry.cd.classList.toggle("urgent", cs.urgent);
    }
  }

  // Sweeping is also driven by the operator leaving the list, so a stale card goes
  // as soon as removing it is safe rather than on the next tick.
  pendingEl.addEventListener("mouseleave", sweep);
  pendingEl.addEventListener("focusout", () => setTimeout(sweep, 0));
  // renderSaturation rides this tick too, because its emphasis decays with time
  // rather than with events: a rejection that stops being "recent" must fade on its
  // own, and the stream is silent precisely when nothing is arriving.
  setInterval(() => { updateCountdowns(); sweep(); renderSaturation(); }, 1000);

  // ── config (the hold window behind the countdown) ─────────────────────────
  async function refreshConfig() {
    try {
      const c = await (await fetch("/api/config")).json();
      const t = Number(c.hold_timeout);
      // Validated rather than trusted: a zero or absent window would divide the
      // progress bar by zero and, worse, imply a deadline that isn't there. Anything
      // unusable leaves holdTimeout null, which just means no countdown.
      holdTimeout = Number.isFinite(t) && t > 0 ? t : null;
      updateCountdowns();
    } catch (e) { /* no countdown; the cards are otherwise unaffected */ }
  }

  // ── audit + rules ─────────────────────────────────────────────────────────
  // Whether a successful load has EVER completed, and whether the most recent one
  // failed. Two facts rather than one, because "never loaded" and "loaded once, now
  // failing" want different sentences — see auditStatus.
  let auditLoaded = false;
  let auditFailed = false;
  let rulesLoaded = false;
  let rulesFailed = false;
  let rulesById = new Map();

  // One renderer for both, because the element contract is identical and the two
  // states drifting apart is precisely what happened last time.
  function renderListStatus(el, s) {
    el.hidden = !s.show;
    el.textContent = s.text;
    el.className = "empty" + (s.level === "warn" ? " warn" : "");
  }

  const auditEmpty = document.getElementById("audit-empty");
  const rulesEmpty = document.getElementById("rules-empty");
  const auditOutage = document.getElementById("audit-outage");

  // Same element contract as the two status lines, so it goes through the same
  // renderer — a third hand-rolled show/hide is how the first two drifted apart.
  function renderOutage(s) {
    renderListStatus(auditOutage, s);
  }

  const auditCoverage = document.getElementById("audit-coverage");

  function renderCoverage(s) {
    renderListStatus(auditCoverage, s);
  }

  function renderAuditStatus(rowCount) {
    renderListStatus(auditEmpty, auditStatus(rowCount, auditFailed, auditLoaded));
  }

  function renderRulesStatus(rowCount) {
    renderListStatus(rulesEmpty, rulesStatus(rowCount, rulesFailed, rulesLoaded));
  }

  async function refreshAudit() {
    let rows, total;
    try {
      const res = await fetch("/api/audit?limit=40");
      if (!res.ok) throw new Error(String(res.status));
      // {rows, total}. Tolerates a bare array from an older backend, in which case
      // `total` is undefined and coverageSummary stays silent rather than guessing.
      const body = await res.json();
      rows = Array.isArray(body) ? body : (body && body.rows) || [];
      total = Array.isArray(body) ? undefined : body && body.total;
    } catch (e) {
      // A failed refresh leaves the previous rows in place and SAYS SO. Silently
      // swallowing this is what let the list sit indefinitely stale while the header
      // read "live" — the stream and this poll are different transports.
      auditFailed = true;
      renderAuditStatus(document.getElementById("audit").rows.length);
      return;
    }
    auditFailed = false;
    auditLoaded = true;
    // Dates, not times: forty rows routinely span midnight, and a time-only stamp makes
    // them read as out of order at exactly the moment ordering matters. The `title`
    // carries the UTC instant, because the visible stamp is local and states no offset
    // — see fmtInstant.
    document.getElementById("audit").innerHTML = rows.map(r => {
      const a = auditRow(r);
      return `
        <tr${a.failClosed ? ' class="outage"' : ""}>
          <td class="ts" title="${esc(fmtInstant(a.ts))}">${esc(fmtStamp(a.ts))}</td>
          <td><span class="tag ${esc(a.decision)}">${esc(a.decision)}</span></td>
          <td>${a.stagePrefix ? `<span class="qual">${esc(a.stagePrefix)}</span>` : ""
            }${esc(a.host)}${a.repeat
              ? `<span class="rep">${esc(" " + a.repeat)}</span>` : ""}</td>
          <td class="ts">${esc(a.client)}</td>
          <td>${esc(a.reason)}${a.firstTs
            ? esc(` · first seen ${fmtStamp(a.firstTs)}`) : ""}</td></tr>`;
    }).join("");
    renderOutage(outageSummary(rows));
    renderCoverage(coverageSummary(rows, total));
    renderAuditStatus(rows.length);
  }

  async function refreshRules() {
    let rows;
    try {
      const res = await fetch("/api/rules");
      // `res.ok` checked, not just the parse: a 4xx/5xx body would otherwise flow
      // into .json() and either throw somewhere less obvious or — worse — parse
      // into something that renders as an empty but SUCCESSFUL policy.
      if (!res.ok) throw new Error(String(res.status));
      rows = await res.json();
    } catch (e) {
      // A failed refresh keeps the rows and SAYS SO, rather than swallowing it as
      // "transient; next poll retries" — which it is, right up until it is not.
      // Three things stop here and none of them looked stopped: the table kept
      // showing rules that might no longer be in force, the count froze, and
      // `policySig` stopped advancing, so the change badge silently stopped firing.
      // Leaving `policySig` alone IS correct — we cannot claim a change we did not
      // see — but the operator has to be told the view is not moving.
      rulesFailed = true;
      renderRulesStatus(document.getElementById("rules").rows.length);
      return;
    }
    rulesFailed = false;
    rulesLoaded = true;
    // Signature over pattern+action, not just the COUNT: a rule whose action
    // flipped is the change most worth noticing, and it leaves the count alone.
    // Keyed by id so the click handler works from the ROW rather than from
    // markup it would otherwise have to parse back out of the DOM.
    rulesById = new Map(rows.map(r => [String(r.id), r]));
    const sig = JSON.stringify(rows.map(r => r.action + " " + r.pattern));
    if (policySig !== null && sig !== policySig && current !== "policy") {
      policyUnseen = true;
    }
    policySig = sig;

    document.getElementById("badge-policy").textContent = String(rows.length);
    document.getElementById("rulecount").textContent =
      rows.length ? `· ${rows.length} rule${rows.length === 1 ? "" : "s"}` : "· none";
    document.getElementById("rules").innerHTML = rows.map(r => {
      const wild = (r.pattern || "").startsWith(".");
      const p = revokePreview(r);
      // A seed rule shows WHY it has no control rather than an empty cell, so
      // nobody has to wonder whether the button failed to render. The id is on the
      // button because revocation keys on it, never on the pattern.
      const control = p.allowed
        ? `<button type="button" class="revoke" data-rule="${esc(String(r.id))}"
             >revoke</button>`
        : `<span class="ts" title="${esc(p.text)}">from seed</span>`;
      return `<tr>
        <td><span class="tag ${esc(r.action)}">${esc(r.action)}</span></td>
        <td><code>${esc(r.pattern)}</code></td>
        <td class="${wild ? "wild" : "ts"}">${esc(r.scope)}</td>
        <td class="ts">${esc(r.source)}</td>
        <td class="ts">${r.created_at ? fmtStamp(r.created_at) : ""}</td>
        <td>${control}</td></tr>`;
    }).join("");
    renderRulesStatus(rows.length);
    updateIndicators();
  }

  // Delegated, because the table is replaced wholesale on every poll — a handler
  // bound per button would be re-bound every four seconds and lost in between.
  //
  // `confirm()` rather than the inline two-step the approval cards use. The cards
  // needed inline confirmation because the button row moves under the pointer as
  // holds expire; this table only changes when policy does, and a modal that steals
  // focus is the right amount of friction for an action with no undo.
  document.getElementById("rules").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button.revoke");
    if (!btn) return;
    const row = rulesById.get(btn.dataset.rule);
    if (!row) return;
    const p = revokePreview(row);
    if (!p.allowed) return;
    if (!window.confirm(`${p.text}\n\nRevoke ${p.pattern}?`)) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/rules/${encodeURIComponent(btn.dataset.rule)}/revoke`,
                              { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // Says what the backend said. The refusals that matter here are 403 (a seed
        // rule, which the UI should not have offered) and 404 (already revoked, or
        // the table on screen is stale) — both are worth reading rather than
        // collapsing into "failed".
        window.alert(`Could not revoke: ${body.detail || res.status}`);
        btn.disabled = false;
        return;
      }
    } catch (e) {
      window.alert("Could not revoke: the control plane is unreachable.");
      btn.disabled = false;
      return;
    }
    refreshRules();
  });

  // ── the approvals feed, and the reconnect it used to lack ─────────────────
  const conn = document.getElementById("conn");
  let es = null;
  let attempt = 0;
  let retryTimer = null;

  function setConn(text, cls) { conn.textContent = text; conn.className = cls; }

  function connect() {
    clearTimeout(retryTimer);
    es = new EventSource("/approvals/stream");
    es.onopen = () => {
      attempt = 0;
      streamUp = true;
      setConn("live", "up");
      updateIndicators();
      // Re-read the config on every (re)connect rather than once at load: a card can
      // only exist because this stream delivered it, so this is the earliest useful
      // moment — and a reconnect may be a restarted backend with a different window.
      refreshConfig();
    };
    es.addEventListener("pending", e => {
      const d = JSON.parse(e.data);
      lastSaturation = d.saturation || null;
      renderSaturation();
      renderPending(d.holds || []);
    });
    es.onerror = () => {
      // The feed is the only thing that tells us about pending approvals, so losing
      // it means we are blind, not idle — say so in the light rather than sitting on
      // a stale green.
      streamUp = false;
      updateIndicators();
      // The distinction the old code missed: EventSource retries BY ITSELF only from
      // CONNECTING. A non-200 response or a wrong MIME type puts it in CLOSED for
      // good — and that is reachable in ordinary operation, because the relay answers
      // 502 while the backend restarts. The page then sat on "reconnecting…"
      // indefinitely: blind, and claiming otherwise. Reconnect by hand from CLOSED.
      if (es.readyState !== EventSource.CLOSED) {
        setConn("reconnecting…", "down");
        return;
      }
      es.close();
      const delay = backoffDelay(attempt++);
      setConn(`stream closed — retrying in ${Math.round(delay / 1000)}s`, "down");
      retryTimer = setTimeout(connect, delay);
    };
  }

  // ── wiring ────────────────────────────────────────────────────────────────
  // `visibilityState` is absent in some non-browser hosts; treat unknown as
  // visible so a missing API degrades to the old always-poll behaviour rather
  // than to a page that silently stops updating.
  const visible = () => document.visibilityState !== "hidden";

  showView(current);
  connect();
  refreshAudit();
  refreshRules();
  // Both keep polling regardless of which VIEW is showing — otherwise the badges
  // could not report a hidden view's state, which is the whole reason they exist.
  //
  // A hidden TAB is a different matter. Nobody is reading either list, the badges
  // are not on screen either, and the cost is real now that /api/audit counts the
  // table on every call: left ungated this is a COUNT(*) every four seconds for as
  // long as the page is open on a machine that never closes it. The SSE stream is
  // deliberately NOT gated — a hold has a ~120s fuse and default-denies, so arrivals
  // must keep landing whether or not the tab is in front, and the title prefix is
  // how they get noticed.
  setInterval(() => { if (visible()) refreshAudit(); }, 4000);
  setInterval(() => { if (visible()) refreshRules(); }, 4000);
  // Refresh IMMEDIATELY on return, rather than leaving up to four seconds of
  // stale-but-unlabelled data on screen at the moment attention comes back to it.
  document.addEventListener("visibilitychange", () => {
    if (visible()) { refreshAudit(); refreshRules(); }
  });
}

// Browser: run the page. Node (the unit tests): export the pure helpers and touch
// nothing — see the header comment on why requiring this file must be side-effect free.
if (typeof document !== "undefined") { start(); }
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    lampState, backoffDelay, diffPending, shouldSweep,
    holdRemaining, countdownState, departure, persistPreview, saturationState,
    ackCount, requestsLabel, auditRow, auditStatus, rulesStatus, repeatCount,
    outageSummary, pendingAnnouncement, coverageSummary, revokePreview,
    fmtTime, fmtStamp, fmtInstant,
    AUDIT_ORDINARY_STAGE,
    RECONNECT_MIN_MS, RECONNECT_MAX_MS, STALE_MAX_MS, COUNTDOWN_URGENT_S,
    DWELL_MS, SATURATION_RECENT_MS, SATURATION_WARN_FRAC,
  };
}
