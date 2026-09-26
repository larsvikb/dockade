// SPDX-License-Identifier: Apache-2.0
/* The pending queue's decisions: which cards a payload yields, what changed between
 * two pushes, when a departed card may be swept, how a hold's countdown reads, what a
 * persist or pin click is about to write, and how an arrival is announced. Everything here
 * decides whether a hold reaches a human and what they are told; the DOM work that
 * draws the answer is in app.js.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */
import { escapePayload } from "./payload.js";

// What changed between the pending list on screen and the one just pushed, keyed by
// approval id so a surviving card is kept and updated IN PLACE.
//
// This is load-bearing, not a rendering nicety. The old code assigned the whole list
// to `innerHTML` on every push (up to once a second), so a card leaving the middle of
// the queue — which happens on its own, since an unresolved hold expires after
// ~120s — shifted every card below it upwards between the operator's eye and their
// click, on a row of buttons that GRANT EGRESS. It also discarded the `disabled`
// state a resolve in flight had just set.
// Card kinds this page knows how to draw. The queue is ONE list over two builders
// (holds.py, `_pending_payload`), so the payload can carry a kind this version of the
// script has never seen — a backend one step ahead of the page, which in this repo
// means a container rebuilt while the browser tab stayed open.
//
// Filtered ONCE, where the payload arrives, rather than defensively at each consumer.
// That is the whole point: the count, the announcement and the rendered cards all read
// the same list, so the page can never say "3 waiting" over two visible cards. Showing
// fewer than exist is a compromise either way — but a wrong NUMBER is the one that
// makes an operator think they have finished a queue they have not.
export const RENDERABLE_KINDS = ["egress", "tool"];

// A card with no `kind` at all is treated as egress, and that is not leniency for its
// own sake: it is the shape every payload had before the tool surface existed, so a
// page held in cache across that upgrade keeps working instead of blanking its queue.
export function renderableHolds(list) {
  return (list || []).filter(a => !a.kind || RENDERABLE_KINDS.includes(a.kind));
}

// How much of a tool ask's window is left. Read off the card's OWN `deadline`, which
// is absolute and durable, rather than derived from `ts` plus a configured window the
// way an egress hold's countdown is. Two consequences, both wanted: a tool card can
// count down before `/api/config` has answered, and its window can differ per card
// without the page knowing anything about `CONTROL_TOOL_HOLD_TIMEOUT`.
export function toolRemaining(deadline, nowMs) {
  if (!Number.isFinite(deadline)) return null;
  return Math.max(0, deadline - nowMs / 1000);
}

// What a decided tool ask says on the card. "Allowed" is NOT "ran": the gateway
// executes on resumption, when the agent comes back and claims the approval, so a
// message reading like a completed action would misreport the one property that keeps
// an approved side effect from happening with nobody to receive it.
export function toolOutcomeMessage(d) {
  if (!d || d.outcome !== "allow") {
    return { text: "✕ denied · the call will not run", tone: "bad" };
  }
  // The fields come back from the BACKEND, so this reports what was pinned rather
  // than what was ticked, and says so when nothing new was written.
  const pin = d.pin;
  const pinned = !pin ? ""
    : ` · ${pin.created ? "pinned" : "already pinned"} on `
      + `${(pin.fields || []).join(", ")} (pin ${pin.id})`;
  return { text: `✓ allowed${pinned} · runs when the agent returns for it`,
           tone: "ok" };
}

// What "Allow + pin" is about to write, for the confirm panel: which calls it will
// answer from now on, and which arguments stay free. `options` is the card's
// `pin_options` (policy._pin_candidates), so every field and value here is one the
// backend offered for this call; `chosen` is the field names ticked.
//
// Values are spelled with every non-ASCII character escaped, as the pins table shows
// them (`pinText` in mcp.js): a pin value is an identifier, and this is the last look
// at it before it becomes standing policy.
export function pinPreview(options, chosen, tool, server) {
  const offered = (options && options.fields) || [];
  const ticked = new Set(chosen || []);
  const picked = offered.filter(f => ticked.has(f.field));
  if (!picked.length) {
    return { ok: false, fields: [],
             text: "Tick the fields a later call must match exactly.",
             label: "Pick a field to pin" };
  }
  const free = [...offered.filter(f => !ticked.has(f.field)).map(f => f.field),
                ...((options && options.unpinnable) || []).map(u => u.field)];
  const conditions = picked.map(f => `${f.field} = ${escapePayload(f.value)}`)
    .join(" and ");
  const rest = free.length
    ? `Every other argument is free: ${free.join(", ")}, and any this call did not set.`
    : "Every argument this call carries is pinned; one it did not set is still free.";
  return { ok: true, fields: picked.map(f => f.field),
           text: `Allows this call, and from now on every ${tool} call on ${server} `
               + `with ${conditions} runs without a card. ${rest}`,
           label: `Confirm — allow and pin ${picked.map(f => f.field).join(", ")}` };
}

export function diffPending(shownIds, list) {
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
export const STALE_MAX_MS = 15000;

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
// holds for attention on what is fundamentally a work queue; the Audit tab is the
// durable record.
export const DWELL_MS = { expired: 60000, resolved: 8000, gone: 5000 };

export function shouldSweep(busy, ageMs, dwellMs = 0) {
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
export function holdRemaining(requestedTs, holdTimeout, nowMs) {
  return Math.max(0, Math.min(holdTimeout, requestedTs + holdTimeout - nowMs / 1000));
}

// Below this many seconds left the countdown is called out, because by then the
// decision is being made for the operator rather than by them.
export const COUNTDOWN_URGENT_S = 20;
export function countdownState(remainingS, holdTimeout) {
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
export function departure(remainingS, holdTimeout) {
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
export function persistPreview(action, option) {
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
export function requestsLabel(n) {
  return Number(n) > 1
    ? `${Number(n)} identical requests — one decision releases all of them`
    : "";
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
// What a card is ABOUT, in words, for anyone reading the queue rather than looking at
// it. Per kind, because the two surfaces name their subject differently and the
// announcement is the only place a screen-reader user learns which decision arrived:
// a tool ask read out as "an unnamed host" is worse than no announcement, since it
// describes the wrong sort of thing entirely.
export function cardSubject(a) {
  if (!a) return "an unnamed host";
  if (a.kind === "tool") {
    const tool = a.tool || "an unnamed tool";
    return a.server ? `${tool} on ${a.server}` : tool;
  }
  return a.host || "an unnamed host";
}

export function pendingAnnouncement(added, total) {
  if (!added || !added.length) return "";
  if (added.length === 1) {
    const subject = cardSubject(added[0]);
    return total > 1
      ? `Approval needed for ${subject}. ${total} pending.`
      : `Approval needed for ${subject}.`;
  }
  return `${added.length} new approvals needed. ${total} pending.`;
}

// ── desktop notification for an arriving approval ───────────────────────────
//
// The same ARRIVALS the live region announces, one surface further out. The `(n)`
// title prefix and the favicon lamp only reach a tab someone can see; a hold with a
// two-minute fuse on a page nobody watches needs to reach past the window.
//
// One notice per arrival, tagged with the approval id, which buys two things the
// tag-less form does not: the OS replaces rather than stacks on a repeat, and
// `renderPending` can CLOSE the notice when its hold leaves. That second one matters
// here in a way it does not for mail — a notification still asking for a decision
// that already default-denied is worse than no notification at all.
//
// Above NOTIFY_MAX arrivals in a single push that stops being true: a dozen separate
// notices is not information. They collapse into one summary, which carries no id and
// is therefore closed for none of them — the count is the message by then, and the
// page is where the detail is.
export const NOTIFY_MAX = 3;

// The title NAMES THE APP, which a title inside the page never has to. The OS
// attributes the notification to the browser — "Google Chrome" — so "Approval needed"
// arrives from an application that has dozens of pages open and says nothing about
// which one is asking, or that the thing asking is a governance decision at all.
export function approvalNotices(added, total) {
  if (!added || !added.length) return [];
  if (added.length > NOTIFY_MAX) {
    return [{ tag: "", title: `${added.length} Dockade approvals needed`,
              body: added.map(cardSubject).join(", ") }];
  }
  // Only when there is a backlog BEYOND what this notice is about. Two arrivals on an
  // otherwise empty queue saying "· 2 pending" is the notice restating itself.
  const pending = total > added.length ? ` · ${total} pending` : "";
  return added.map(a => ({ tag: a.id || "", title: "Dockade approval needed",
                           body: cardSubject(a) + pending }));
}

// Whether an arrival may raise a notification, in one place because the rule is four
// conditions and three of them are easy to get subtly wrong:
//
//   `granted` only. `default` must not notify — asking needs a user gesture in Firefox
//   and Safari, and the header button is what supplies one.
//
//   `primed` is false until the first push has rendered. A reload delivers every
//   waiting hold as an arrival, and a browser restoring the tab in the background
//   would fire a notification per hold for approvals that arrived while it was shut.
//   Nothing NEW happened; the queue was simply read for the first time.
//
//   Visible AND on the approvals view means the card is already on screen. Visible is
//   not focused — a second monitor counts — but in both cases the operator is looking
//   at the thing the notification would point them to.
export function shouldNotify(permission, primed, visible, view) {
  if (permission !== "granted" || !primed) return false;
  return !(visible && view === "approvals");
}

// The header button, by permission state. Split from the DOM so the states are
// legible in one glance, since three of the four are states nobody hits by accident:
//
//   granted     nothing to offer — it works.
//   default     the only clickable state, and the gesture the API requires.
//   denied      SHOWN, disabled. Script cannot undo a denial; only the browser's own
//               site settings can. Hiding it is how a misclicked "Block" turns into a
//               feature that is silently gone forever with nothing on screen to say so.
//   unavailable no API here. Notification needs a SECURE CONTEXT: compose publishes
//               this UI on 127.0.0.1, which counts as one over plain HTTP, so the
//               ordinary deployment is fine — but front it under another name and the
//               API is either absent or auto-denies. That is precisely the operator
//               who needs telling, hence the title text.
const NOTIFY_BUTTON = {
  granted: { hidden: true, disabled: false, text: "", title: "" },
  default: { hidden: false, disabled: false, text: "notify me",
             title: "Raise a desktop notification when an approval arrives" },
  denied: { hidden: false, disabled: true, text: "notifications blocked",
            title: "This browser is blocking notifications for this site. "
                 + "Only its site settings can undo that." },
  unavailable: { hidden: false, disabled: true, text: "notifications unavailable",
                 title: "Notifications need a secure origin. This page is served over "
                      + "plain HTTP under a name the browser does not treat as one — "
                      + "reach it on localhost instead." },
};

export function notifyButton(state) {
  return NOTIFY_BUTTON[state] || NOTIFY_BUTTON.unavailable;
}
