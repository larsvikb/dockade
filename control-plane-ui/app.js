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
 * exported for the tests; everything that TOUCHES THE DOM runs from `start()`,
 * which only a browser calls. Importing this file under node must therefore have
 * no side effects — if DOM work ever migrates to import time, the node test fails
 * at `import`, which is the intended alarm.
 *
 * This is the entry module; a surface with its own file is imported below.
 */

import {
  auditRow, eventRow, coverageSummary, outageSummary,
  filterActive, auditQuery, historyPager,
} from "./audit.js";
import { revokePreview, createPreview, editPreview } from "./egress-rules.js";
import {
  SERVER_NAME_RE, serverDescriptor, serverPreview, serverEditBody,
  toolChoices, toolRulePreview, toolEditPreview, toolRevokePreview,
} from "./mcp.js";
import {
  renderableHolds, diffPending, shouldSweep, DWELL_MS,
  toolRemaining, toolOutcomeMessage, holdRemaining, countdownState, departure,
  COUNTDOWN_URGENT_S, persistPreview, requestsLabel,
  pendingAnnouncement, approvalNotices, shouldNotify, notifyButton,
} from "./holds.js";
import { payloadDisclosure, payloadHazards, payloadTokens, renderPayload }
  from "./payload.js";
import { shortActor } from "./provenance.js";
import { tsSeconds, fmtTime, fmtStamp, fmtInstant } from "./time.js";

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

// Hold-cap pressure, as a banner state. Over any of the four hold caps (cards and
// blocked requests, each global and per client — see holds.py) the control plane fails
// closed WITHOUT creating an approval, so the agent is denied and no card is ever
// raised — governance degrading to blanket-deny, and from this page indistinguishable
// from a quiet afternoon.
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

// Which of the four hold caps a rejection hit, as a phrase for the banner's detail.
//
// The control plane records the cap it actually checked, as "<who> <what>": who is
// `global` or `client <address>`, what is `cards` or `waiters` (see _reserve_hold).
// Both halves matter to the operator and they suggest different responses — "one agent
// is hammering" versus "the whole control plane is loaded", and "too many questions on
// screen" versus "too many blocked workers", which is a capacity problem rather than an
// attention one.
//
// "waiters" is rendered as "blocked-request" rather than repeated: it is the control
// plane's word for a pinned threadpool worker, which is an implementation fact the
// operator has no reason to hold. An unrecognised scope yields "" so the sentence is
// simply shorter, never a phrase invented around a word this does not know.
function capScope(scope) {
  const m = /^(global|client .+) (cards|waiters)$/.exec(String(scope || ""));
  if (!m) return "";
  const what = m[2] === "waiters" ? "blocked-request" : "card";
  return m[1] === "global"
    ? `the global ${what} cap`
    : `the per-client ${what} cap for ${m[1].replace(/^client /, "")}`;
}

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
  const inFlight = Number(sat.in_flight) || 0;
  const cards = Number(sat.cards) || 0;
  const rejections = Number(sat.rejections) || 0;
  // TWO global caps, so two gauges. Cards are always <= waiters, so the fuller one is
  // not always the same one, and reporting a fixed choice would hide the cap that is
  // actually about to deny. A cap of 0 is dropped rather than divided by: on a global
  // cap zero means "refuse everything", which the rejection notice above already
  // reports far better than a `0/0` gauge could.
  const gauges = [
    { n: inFlight, cap: Number(sat.max_waiters) || 0, unit: "requests held" },
    { n: cards, cap: Number(sat.max_pending) || 0, unit: "cards" },
  ].filter(g => g.cap > 0);
  const fullest = gauges.reduce(
    (a, b) => (b.n / b.cap > a.n / a.cap ? b : a), gauges[0] || null);

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
              (capScope(sat.last_scope) ? ` — hit ${capScope(sat.last_scope)}` : ""),
      lastTs: known ? lastTs : null,
    };
  }

  // No rejections yet: warn only once the cap is close enough that the next burst
  // would hit it. Below that this is noise on a page whose job is the queue.
  if (fullest && fullest.n >= fullest.cap * SATURATION_WARN_FRAC) {
    return {
      show: true, level: "load", count: 0,
      // Duplicates share a card, so the blocked-request count can be far above the
      // number of cards on screen. Said out loud when they differ, because "14/16
      // requests held" beside two cards otherwise reads as a queue that has stopped
      // draining.
      text: fullest.unit === "requests held" && cards > 0 && cards !== inFlight
        ? `${inFlight}/${fullest.cap} requests held on ${cards} card` +
          `${cards === 1 ? "" : "s"}`
        : `${fullest.n}/${fullest.cap} ${fullest.unit}`,
      detail: fullest.n >= fullest.cap
        ? "at the cap — further requests are denied without raising a card"
        : "near the cap — further requests would be denied without raising a card",
      lastTs: null,
    };
  }
  return hidden;
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
  stale: "Could not refresh — these are the last events loaded successfully " +
         "and may be out of date.",
  cold: "Could not load recent events — the control plane may be unreachable.",
  // Deliberately no longer a LIST of what appears here. It named "every allow, deny
  // and hold" while the vocabulary was three words; it has since grown policy edits
  // and observations, and an enumeration in an empty state is the kind of promise
  // that quietly stops being true — telling the reader the log is narrower than it is.
  empty: "Nothing recorded yet. Every governed decision, and what came of it, " +
         "appears here as it happens.",
};
// An empty list under a FILTER is not an empty record, and the difference is the same
// kind of difference as empty-versus-stale: one says the system is quiet, the other
// says the question found nothing. Getting this wrong is worse than the stale case it
// borrows from — "nothing recorded yet" in front of a full store, because the
// operator typed a host that never asked for anything, reads as governance not running.
const AUDIT_FILTERED_EMPTY_TEXT =
  "No events match these filters. The record itself is not empty — clear them, " +
  "or widen the time window, to see it.";
// When the backend refuses the filters but says nothing usable about why. Only reachable
// if the 400 body is missing or unparseable, which is why it is vague where the
// backend's own sentence is specific — but it still has to name the filter bar as the
// place to look, because the one thing we do know is that the control plane answered.
const AUDIT_REFUSED_FALLBACK =
  "These filters were refused, so the events below still answer the previous " +
  "question. Adjust them and try again.";
function auditStatus(rowCount, failed, loaded, filtered, refused) {
  // A REFUSED filter outranks every sentence below it. The query never ran, so the
  // rows on screen are the PREVIOUS question's answer — and unlike a failed poll this
  // is something the operator can act on, in the filter bar, right now. Leaving it to
  // the stale wording would blame the control plane for a parameter the page sent.
  // The backend's sentence is used verbatim (`_bad_filter` in control-plane/api_views.py)
  // because it names which filter and why, which nothing written here could.
  if (refused) return { show: true, level: "warn", text: refused };
  const s = pollStatus(AUDIT_STATUS_TEXT, rowCount, failed, loaded);
  // Only the EMPTY sentence changes. A failed poll is a failed poll whether or not a
  // filter is set, and saying so remains the more urgent fact.
  if (filtered && !failed && loaded && rowCount === 0) {
    return { ...s, text: AUDIT_FILTERED_EMPTY_TEXT };
  }
  return s;
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

// Its own wording rather than the block above with a noun swapped, for the same reason
// that one is not the decisions view's: what an EMPTY table means differs, and it is
// the opposite kind of fact. An empty egress policy holds every request for a human;
// an empty tool policy refuses every call outright, so nothing reaches anyone to
// approve and the quiet is not a queue.
const TOOL_RULES_STATUS_TEXT = {
  stale: "Could not refresh — this is the last tool policy loaded successfully and " +
         "may no longer be what the gateway is enforcing.",
  cold: "Could not load the tool policy — the control plane may be unreachable.",
  empty: "No tool rules, so every tool call is denied. An unconfigured tool is " +
         "refused rather than held, so nothing here reaches you to approve.",
};
function toolRulesStatus(rowCount, failed, loaded) {
  return pollStatus(TOOL_RULES_STATUS_TEXT, rowCount, failed, loaded);
}

// ── leases: the grants that expire ──────────────────────────────────────────
// A lease is the middle rung of the resolve ladder — this request, this host for a
// while, this pattern forever — so the button granting one has to say WHICH of the
// three it is. The duration is configuration (`policy.LEASE_SECONDS`, served by
// `/api/config`), so the label is DERIVED: a button reading "Allow for 30 min" on a
// store configured for five would be the same class of lie as a countdown inventing
// its own window, and it is why the backend action is named `allow_lease` rather than
// after any number.
//
// A non-finite or non-positive duration — including the `null` that stands for "the
// first /api/config has not answered yet" — gets a label that promises no particular
// length. The button still WORKS in that state, because the backend owns the duration
// and does not need the page to tell it: what is unknown here is only what to call it.
function leaseLabel(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return "Allow for a while";
  if (s % 3600 === 0) return `Allow for ${s / 3600} h`;
  if (s % 60 === 0) return `Allow for ${s / 60} min`;
  return `Allow for ${Math.round(s)} s`;
}

// How long a live lease has left, from its own ABSOLUTE deadline. The same discipline
// `toolRemaining` follows, for the same reason: the backend sends the instant rather
// than a remaining-seconds field, so this needs no knowledge of the configured
// duration and a stale `/api/config` cannot make it wrong.
//
// Clamped at zero instead of going negative. The backend serves only live leases, so a
// negative value here means the browser's clock and the control plane's disagree — and
// of the two readings available then, "0s" is the one that cannot mislead.
function leaseRemaining(expiresAt, nowMs) {
  const at = Number(expiresAt);
  if (!Number.isFinite(at)) return null;
  return Math.max(0, at - nowMs / 1000);
}

// A lease's remaining time as a cell: the text, and whether it is about to lapse.
// `urgent` reuses COUNTDOWN_URGENT_S so "nearly out of time" looks the same here as on
// a hold card — two thresholds would make the same colour mean two things.
function leaseCountdown(remainingS) {
  if (remainingS === null) return { text: "unknown", urgent: false };
  const whole = Math.max(0, Math.floor(remainingS));
  const mins = Math.floor(whole / 60);
  return {
    text: mins >= 1 ? `${mins}m ${String(whole % 60).padStart(2, "0")}s` : `${whole}s`,
    urgent: whole <= COUNTDOWN_URGENT_S,
  };
}

const LEASES_STATUS_TEXT = {
  stale: "Could not refresh — these are the last live leases loaded successfully, " +
         "and one may have lapsed or been revoked since.",
  cold: "Could not load the live leases — the control plane may be unreachable.",
  // Phrased so it does not read as a fault: no leases is the normal resting state of
  // this table, so the sentence says what that MEANS for the next request rather than
  // observing that a table is short.
  empty: "No timed grants in force, so every unknown host is still held for approval.",
};
function leasesStatus(rowCount, failed, loaded) {
  return pollStatus(LEASES_STATUS_TEXT, rowCount, failed, loaded);
}

// ── folding sibling hosts into one line ─────────────────────────────────────
// A lease is always the exact host (there is no breadth ladder — it answers the
// breadth question by expiring), so one site spread over `cdn.`, `static.`, `api.`
// and `assets.` is four rows for what the operator thinks of as one grant. That is
// clutter carrying no information, and it is produced by the exact-host decision
// rather than by this table.
//
// GROUPED, never truncated, and that is the load-bearing choice. A `+N more` fold
// would be less code and it is the wrong answer: a live grant hidden behind a click
// is a grant nobody revokes, which was the whole argument for showing them at all.
// Every lease stays reachable here — the summary line only defers the detail.

// The registrable domain, for display. The two-label suffix, the same shape
// `policy._persist_candidates` derives — but WITHOUT the leading dot, because nothing
// here is a pattern and nothing here grants.
//
// That difference matters for the known limitation the backend has to warn about: with
// no public-suffix list the two-label suffix of `example.co.uk` is `co.uk`. On the
// persist path that would be a grant far wider than it looks, which is why an operator
// picks it and sees it verbatim. HERE it can only put two rows under one heading, so
// the wrong answer costs a slightly odd grouping and nothing else — and the same
// applies to the loose IP check below, where the backend uses a real parser.
function leaseDomain(host) {
  const h = (host || "").toLowerCase();
  // An address has no domain to group under. Deliberately looser than the backend's
  // `ipaddress` parse: a missed literal is grouped by its last two dotted parts, which
  // is untidy rather than wrong.
  if (h.includes(":") || /^[0-9.]+$/.test(h)) return h;
  const labels = h.split(".").filter(Boolean);
  if (labels.length < 2) return h;
  return labels.slice(-2).join(".");
}

// Below this, a group renders as plain rows. A "group" of one would make every single
// lease cost a click to read, which is worse than the clutter it is meant to fix.
const LEASE_GROUP_MIN = 2;

// Live leases into display groups, soonest to expire first.
//
// The key is (client class, registrable domain) and NOT the domain alone. Two client
// populations under one heading would imply they interact, which is the exact mistake
// `api_rules` avoids by grouping the standing rules by class first: a lease for
// `api.example.com` on `sandbox` and one for `cdn.example.com` on `mcp` are two
// separate grants to two separate tenants, and folding them together would read as one.
//
// `soonest` is what a group sorts and counts down by, because it is the next thing
// about the group that will actually change.
function groupLeases(rows) {
  const byKey = new Map();
  for (const r of Array.isArray(rows) ? rows : []) {
    const domain = leaseDomain(r.host);
    const cls = r.client_class || "";
    const key = `${cls}|${domain}`;
    if (!byKey.has(key)) {
      byKey.set(key, { key, domain, clientClass: cls, leases: [] });
    }
    byKey.get(key).leases.push(r);
  }
  const groups = [];
  for (const g of byKey.values()) {
    const leases = [...g.leases].sort(
      (a, b) => Number(a.expires_at) - Number(b.expires_at));
    groups.push({
      ...g, leases,
      count: leases.length,
      soonest: Number(leases[0].expires_at),
      // Whether it is DRAWN as a group. Carried on the group rather than recomputed at
      // render time, so the threshold is applied in one place and the renderer cannot
      // disagree with the tests about where it falls.
      grouped: leases.length >= LEASE_GROUP_MIN,
    });
  }
  return groups.sort((a, b) => a.soonest - b.soonest);
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

// ── the page ────────────────────────────────────────────────────────────────

function start() {
  const esc = s => (s ?? "").toString().replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ── state the indicators read ─────────────────────────────────────────────
  let pendingCount = 0;      // approvals waiting on a human
  let streamUp = false;      // is the SSE feed actually delivering?
  let policySig = null;      // signature of the rules, to detect change
  let policyUnseen = false;  // policy changed while another view was showing
  // Seconds a hold waits before it default-denies, from GET /api/config. Stays null
  // until the backend says, and the page works without it — one fewer thing that can
  // stop an approval from reaching a human.
  let holdTimeout = null;
  // Seconds a lease lasts, also from GET /api/config, and null until it answers. Used
  // for the BUTTON LABEL only — never to compute a deadline, which every lease carries
  // as an absolute instant of its own. That split is deliberate: a wrong label is
  // cosmetic, while a locally-computed expiry could show a grant ending at a time it
  // does not, and the second failure is the one that matters.
  let leaseSeconds = null;

  // ── views ─────────────────────────────────────────────────────────────────
  const VIEWS = ["approvals", "audit", "policy", "tools"];
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
    // The inventory poll only runs while this view is up, so arriving here would
    // otherwise show whatever was last fetched — up to a poll interval old, and after
    // a long absence arbitrarily so. Asking on arrival is the same reasoning as the
    // refresh on `visibilitychange`: the stale moment to avoid is the one where
    // attention has just landed on the data.
    if (current === "tools") { refreshInventory(); }
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

  // ── desktop notifications ─────────────────────────────────────────────────
  // The one indicator that reaches OUTSIDE the tab. Everything above it needs the
  // page to be looked at; see `approvalNotices` for why this one exists and
  // `shouldNotify` for when it fires.
  const notifyEl = document.getElementById("notify");
  // tag (an approval id) -> the live Notification, so a hold that leaves the queue
  // takes its notice with it.
  const notices = new Map();
  let notifyPrimed = false;
  // Latched by a constructor that throws: some browsers expose `Notification` but
  // allow only the service-worker form, and a page that has no worker cannot get
  // there. One failure is enough to know the rest will fail the same way.
  let notifyBroken = false;

  function notifyState() {
    if (typeof Notification === "undefined" || !window.isSecureContext
        || notifyBroken) {
      return "unavailable";
    }
    return Notification.permission;
  }

  function syncNotifyButton() {
    const b = notifyButton(notifyState());
    notifyEl.hidden = b.hidden;
    notifyEl.disabled = b.disabled;
    notifyEl.textContent = b.text;
    notifyEl.title = b.title;
  }

  notifyEl.addEventListener("click", async () => {
    await Notification.requestPermission();
    syncNotifyButton();
  });

  function notifyArrivals(added, total) {
    if (!shouldNotify(notifyState(), notifyPrimed, visible(), current)) return;
    for (const n of approvalNotices(added, total)) {
      let note;
      try {
        // Deliberately NOT `requireInteraction`. Pinning the toast on screen until it
        // is dealt with reads like the right call for a hold with a ~120s fuse, but
        // Chrome answers a persistent notification with a Close button of its own —
        // unlabelable from here, and a second way to dismiss next to the one the
        // toast already has. An ordinary notification fades after a few seconds into
        // the OS notification centre, which is where an operator who was away from
        // the desk looks anyway; `closeNotice` takes it back out of there when the
        // hold is gone.
        note = new Notification(n.title, { body: n.body, tag: n.tag || undefined });
      } catch (e) {
        notifyBroken = true;
        syncNotifyButton();
        return;
      }
      note.onclick = () => {
        // Bring the console forward AND land on the queue: a notification that
        // raises the window on whatever view was last open has done half its job.
        window.focus();
        location.hash = "approvals";
        note.close();
      };
      // Kept until the HOLD departs rather than until the toast does. Dropping the
      // reference on the notification's own `close` event looks tidier and is wrong:
      // a toast that has merely faded into the notification centre is still there to
      // be read, and whether that fires a close event is the browser's business.
      // `close()` on an already-closed notification does nothing, so holding the
      // reference costs nothing and never misses.
      if (n.tag) notices.set(n.tag, note);
    }
  }

  // A notice is closed by its hold LEAVING the queue, whichever way it went — the
  // operator's own click from this page, a resolve from somewhere else, or the
  // expiry that default-denied it. All three make the question moot, and only the
  // first is one the operator already knows about. This reaches further than the
  // screen: a faded toast is still sitting in the OS notification centre, and that
  // is exactly where a stale one would be read hours later as a live question.
  function closeNotice(id) {
    const note = notices.get(id);
    if (!note) return;
    notices.delete(id);
    note.close();
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

  // The resolve ladder, in increasing order of how far the click reaches. `allow_lease`
  // carries a `null` label because its wording depends on the configured duration —
  // `leaseLabel` derives it, and `relabelLeaseButtons` fills it in again when
  // /api/config answers after a card was already drawn.
  const ACTIONS = [
    ["allow_once", "Allow once", "allow"],
    ["allow_lease", null, "allow"],
    ["allow_persist", "Allow + persist rule", "allow"],
    ["deny_once", "Deny once", "deny"],
    ["deny_persist", "Deny + persist rule", "deny"],
  ];

  // Built with createElement/textContent rather than an HTML string: the host, url
  // and client on an approval are AGENT-CONTROLLED, and a text node needs no
  // escaping to be safe, so the question of whether esc() covers every context
  // does not arise for this list at all. The persist patterns are derived from that
  // same host by the backend, so they get the same treatment.
  //: A tool ask's two answers. Deliberately not ACTIONS above: the backend keeps
  //: per-surface action sets and refuses the egress vocabulary here, so a shared list
  //: would render buttons that 400. There is no `+ persist` counterpart because the
  //: argument-shaped ladder that would derive one does not exist — and "allow this
  //: tool forever" is not a rung anyone should reach by clicking twice.
  const TOOL_CARD_ACTIONS = [["allow", "Allow", "allow"], ["deny", "Deny", "deny"]];

  // A tool ask, which shares the card's shell and almost none of its body. No persist
  // confirm panel, no duplicate badge (nothing joins a card by waiting on it), and a
  // payload where an egress card has a URL.
  //
  // Built with createElement/textContent throughout, and here that matters more than
  // it does for a host: the arguments are AGENT-AUTHORED and may contain anything a
  // model can emit. A text node needs no escaping to be safe, so the question of
  // whether an escaper covers every context does not arise.
  function buildToolCard(a) {
    const el = document.createElement("div");
    el.className = "card tool";
    el.dataset.id = a.id;

    const title = document.createElement("div");
    title.className = "host";
    title.textContent = a.tool || "(unnamed tool)";

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [a.server ? `on ${a.server}` : null,
                        `asked ${fmtTime(a.ts)}`,
                        a.client ? `from ${a.client}` : null]
      .filter(Boolean).join(" · ");

    // The payload, verbatim, one click away at worst. This is the thing being
    // approved: the tool name says what KIND of act it is and only the arguments say
    // what it does to which repository.
    // Measured, checked and escaped on the payload as sent, indented only for display:
    // the newlines indenting adds are control characters to payloadHazards.
    const disclosure = payloadDisclosure(a.args_json);
    const hazards = payloadHazards(a.args_json);
    const raw = payloadTokens(a.args_json, { breaks: true });
    const escaped = payloadTokens(hazards.escaped);
    const details = document.createElement("details");
    details.className = "payload";
    details.open = true;
    // A DANGEROUS payload is shown escaped first; a merely non-ASCII one gets the note
    // and nothing else (see payloadHazards for why the two differ).
    const danger = hazards.level === "danger";
    const summary = document.createElement("summary");
    summary.textContent = disclosure.summary;
    const pre = document.createElement("pre");
    renderPayload(pre, danger ? escaped : raw);
    details.append(summary);
    if (hazards.level !== "none") {
      // Escaped FIRST, raw on request — see payloadHazards for why not the reverse.
      const hazard = document.createElement("div");
      hazard.className = danger ? "hazard" : "hazard quiet";
      const note = document.createElement("span");
      note.textContent = hazards.note;
      const toggle = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = danger;
      box.addEventListener("change", () => {
        renderPayload(pre, box.checked ? escaped : raw);
      });
      toggle.append(box, document.createTextNode(" escaped"));
      hazard.append(note, toggle);
      details.append(hazard);
    }
    details.append(pre);

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

    const actions = document.createElement("div");
    actions.className = "actions";

    const msg = document.createElement("div");
    msg.className = "cardmsg";
    msg.hidden = true;

    const entry = {
      el, actions, msg, cd, cdText, fill,
      kind: "tool",
      state: "pending", staleAt: null, dwell: 0,
      ts: a.ts,
      deadline: Number(a.deadline),
      // The card's own window, so `countdownState` gets a total to take a fraction of
      // without the page knowing what the backend's tool timeout is set to.
      window: Number(a.deadline) - Number(a.ts),
    };

    for (const [action, label, kind] of TOOL_CARD_ACTIONS) {
      const b = document.createElement("button");
      b.className = kind;
      b.textContent = label;
      b.addEventListener("click", () => resolve(a, action));
      actions.append(b);
    }

    el.append(title, meta, details, cd, actions, msg);
    return entry;
  }

  function buildCard(a) {
    if (a.kind === "tool") return buildToolCard(a);
    const el = document.createElement("div");
    el.className = "card";
    el.dataset.id = a.id;

    const host = document.createElement("div");
    host.className = "host";
    host.textContent = a.host + (a.port ? ":" + a.port : "");

    const meta = document.createElement("div");
    meta.className = "meta";
    // The client class rides WITH the address rather than replacing it: the address
    // is what was observed and the class is what a persisted rule would be scoped to,
    // and an operator deciding a card needs both — "from 172.28.0.3 (mcp)" answers
    // both "who asked" and "who would this grant cover".
    meta.textContent = [a.proto || null, `requested ${fmtTime(a.ts)}`,
                        a.client
                          ? `from ${a.client}`
                            + (a.client_class ? ` (${a.client_class})` : "")
                          : null,
                        a.url || null]
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

    // Whether a standing rule can be written for this request at all. A rule is
    // scoped to a client class, and a client the backend could not place has none —
    // `resolve` refuses the persist with a 400. Offering the button anyway would put
    // the operator through the confirm panel to reach a refusal, so it is disabled
    // here with the reason on it, the same "say it before the click" the conflict
    // preview does. `!== false` so a backend that predates the field behaves exactly
    // as it did: absent means allowed.
    const persistable = a.persistable !== false;
    // Whether a LEASE can be granted for this request, which fails in exactly the same
    // case and for the same reason (`holds._classified`): a grant outliving the request
    // is scoped to a client class, and an unplaceable client has none. Read from its own
    // field rather than reusing `persistable`, so the page is not quietly relying on the
    // two preconditions staying identical. `!== false` so a backend that predates the
    // field behaves as it did: absent means allowed.
    const leasable = a.leasable !== false;
    for (const [action, label, kind] of ACTIONS) {
      const b = document.createElement("button");
      b.className = kind;
      // On the button so `relabelLeaseButtons` can find it later. Every action carries
      // one rather than only the lease, because a marker present on one button and
      // absent on its neighbours is the kind of asymmetry a future reader has to test
      // for to trust.
      b.dataset.action = action;
      b.textContent = label === null ? leaseLabel(leaseSeconds) : label;
      const grants = action.endsWith("persist") || action.endsWith("lease");
      if (grants && !(action.endsWith("lease") ? leasable : persistable)) {
        b.disabled = true;
        b.title = `No ${action.endsWith("lease") ? "lease" : "standing rule"} can be `
                + "written for an unclassified client — decide this request with a "
                + "once action, or map its network in CONTROL_CLIENT_CLASSES.";
        actions.append(b);
        continue;
      }
      // The two `*_persist` actions write standing policy, so they open the confirm
      // panel; the `*_once` actions decide this request only and stay a single click.
      //
      // `allow_lease` is a single click too, and that is a judgement rather than an
      // oversight. The confirm panel exists to name the PATTERN a persist would write,
      // because the choice is irreversible and nothing in this UI removes a rule; a
      // lease chooses nothing, expires on its own, and can be revoked from the table
      // below — so a second click would be friction with no question behind it.
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

  // The lease button's wording depends on a number that arrives after the first cards
  // do, so it is written twice: once at build time (with whatever is known then) and
  // again here when /api/config answers. Cheaper and less surprising than deferring the
  // whole card — the button is live and correct from the first paint either way, and
  // only its wording sharpens.
  //
  // Skips a card that is no longer pending: a resolved or stale card's buttons are
  // disabled and its message reports what already happened, so relabelling one would
  // rewrite a record of a click that has been made.
  function relabelLeaseButtons() {
    const text = leaseLabel(leaseSeconds);
    for (const entry of cards.values()) {
      if (entry.state !== "pending" && entry.state !== "confirming") continue;
      const b = entry.actions.querySelector('button[data-action="allow_lease"]');
      if (b) b.textContent = text;
    }
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
    // A tool card has no confirm panel to close: nothing it can do writes standing
    // policy, so there is no irreversible step to put a step in front of.
    if (entry.confirm) entry.confirm.hidden = true;
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
        if (entry.kind === "tool") {
          // Its own sentence, because the egress one below reports what was PERSISTED
          // and a tool ask persists nothing — the fields it reads are all absent here,
          // so it would settle on "this request only", which is both wrong and
          // reassuring about the wrong thing.
          const m = toolOutcomeMessage(d);
          setMessage(entry, m.text, m.tone);
          refreshAudit();
          return;
        }
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
        // The lease clause reads the deadline the BACKEND returned rather than adding
        // the configured duration to this machine's clock, so the time shown is the
        // time the grant actually ends. `fmtTime` for the instant and not a duration:
        // the countdown in the table below is where "how long left" belongs, and this
        // sentence is a record of what was granted.
        setMessage(entry,
          (d.outcome === "allow" ? "✓ allowed" : "✕ denied") +
          (d.persisted ? ` · standing rule written: ${d.pattern || a.host.toLowerCase()}`
            : d.already_present
              ? ` · standing rule already in place: ${d.pattern || a.host.toLowerCase()}`
              : d.leased
                ? ` · leased until ${fmtTime(d.lease_expires_at)}`
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
    // And a lease just changed what is in force RIGHT NOW, in a table on the view the
    // operator is looking at — so it appears with the click rather than up to a poll
    // interval later, which for a grant with a countdown on it would read as the table
    // having missed it.
    refreshLeases();
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
    // The same arrivals, one surface further out.
    notifyArrivals(add, list.length);
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
      // Tool cards are skipped rather than defaulted to 1: nothing blocks on an ask,
      // so a joiner adds no waiter and there is no count to keep current. A badge
      // saying "1 request" would be inventing a number the backend never sends.
      if (entry && entry.kind !== "tool"
          && (entry.state === "pending" || entry.state === "confirming")) {
        setRequests(entry, a.requests);
      }
    }
    for (const id of gone) {
      closeNotice(id);
      const entry = cards.get(id);
      if (entry.state === "pending" || entry.state === "confirming"
          || entry.state === "resolving") {
        // Say WHICH way it went. An expiry is a decision the operator failed to make
        // in time and the agent was denied for it; a card leaving with time left is
        // somebody or something else resolving it. Both used to read the same — and
        // the expiry now also stays put long enough to be read (see DWELL_MS).
        // A tool card knows its own window, so it can tell an expiry from a
        // resolved-elsewhere even when `/api/config` never answered — the egress card
        // cannot, and passes null to say so.
        const d = entry.kind === "tool"
          ? departure(toolRemaining(entry.deadline, Date.now()), entry.window)
          : departure(
            holdTimeout === null ? null
              : holdRemaining(entry.ts, holdTimeout, Date.now()),
            holdTimeout);
        markStale(entry, d);
      }
    }
    sweep();
    updateIndicators();
    // Set AFTER the first push has been rendered, never at load: until a list has
    // arrived there is nothing to distinguish "the queue was already this long" from
    // "these just came in", and only the second is worth a notification.
    notifyPrimed = true;
  }

  // Redrawn once a second for every live card. Cheap, and the only thing that makes a
  // deadline legible: the text for a reading, the bar for a glance.
  function updateCountdowns() {
    const now = Date.now();
    for (const entry of cards.values()) {
      if (entry.state === "resolved" || entry.state === "stale") continue;
      // A tool card carries its own absolute deadline, so it counts down whether or
      // not the config fetch ever succeeded. An egress card cannot — its window is
      // configuration — so it stays hidden rather than inventing a deadline.
      const tool = entry.kind === "tool";
      const remaining = tool ? toolRemaining(entry.deadline, now) : null;
      // Same rule on both surfaces: no countdown is shown for a deadline the page
      // cannot know. `countdownState` reads a null remaining as "expiring now" and
      // flags it urgent, so a card with a malformed deadline would sit there crying
      // wolf — the one thing an urgency signal must never do.
      if (tool ? remaining === null : holdTimeout === null) continue;
      const cs = tool
        ? countdownState(remaining, entry.window)
        : countdownState(holdRemaining(entry.ts, holdTimeout, now), holdTimeout);
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
  // The lease countdowns ride this tick for the same reason the saturation banner
  // does: what changes is the passage of time, not an event, so nothing is going to
  // push a redraw at the moment one is needed.
  setInterval(() => {
    updateCountdowns(); sweep(); renderSaturation(); updateLeaseCountdowns();
  }, 1000);

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
      // Validated the same way and left null when unusable, which `leaseLabel` reads as
      // "say no number". Relabelled rather than only stored, because cards drawn before
      // this first answer are already on screen with the placeholder wording on them.
      const ls = Number(c.lease_seconds);
      leaseSeconds = Number.isFinite(ls) && ls > 0 ? ls : null;
      relabelLeaseButtons();
      // The classes a rule may be scoped to, from the backend rather than guessed:
      // `create_rule` refuses one it does not know, so a guessed list offers rules that
      // cannot be written. Filtered to strings for the same reason the window above is
      // validated — this feeds a <select> whose value goes straight into a POST.
      clientClasses = (Array.isArray(c.client_classes) ? c.client_classes : [])
        .filter(x => typeof x === "string" && x);
      configState = "ok";
      renderClassOptions();
    } catch (e) {
      // No countdown, and no rule form; the cards are otherwise unaffected.
      configState = "failed";
      renderClassOptions();
    }
  }

  // ── audit + rules ─────────────────────────────────────────────────────────
  // Whether a successful load has EVER completed, and whether the most recent one
  // failed. Two facts rather than one, because "never loaded" and "loaded once, now
  // failing" want different sentences — see auditStatus.
  let auditLoaded = false;
  let auditFailed = false;
  // The refusal sentence from the last 400, or null. A third fact rather than a flavour
  // of `auditFailed`, because the transport SUCCEEDED — the control plane answered, and
  // answered with the reason. Collapsing the two would report a bad filter as an
  // unreachable control plane, which sends the operator to the wrong place entirely.
  let auditRefused = null;
  let rulesLoaded = false;
  let rulesFailed = false;
  let rulesById = new Map();
  // The same two facts for the leases poll, kept separately rather than folded into the
  // rules ones: the two views are filled by two fetches, and one of them failing while
  // the other succeeds is a state the page has to be able to describe.
  let leasesLoaded = false;
  let leasesFailed = false;
  let leasesById = new Map();
  // Which lease groups the operator has opened, by (class, domain) key. Outside the
  // render deliberately — see the toggle handler — and pruned to the live groups on
  // every refresh so a lapsed domain does not keep an expansion nobody asked for.
  const expandedLeaseGroups = new Set();
  // Populated by refreshConfig, not by the rules table: a class with no rules yet is
  // exactly the one an operator most needs to write the first rule for.
  let clientClasses = [];
  // "pending" until the first /api/config answers. Three states rather than an empty
  // list, because an empty list means two different things — not asked yet, and asked
  // and there are none — and only one of them is worth a sentence on screen.
  let configState = "pending";

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

  function renderAuditStatus(rowCount, filtered) {
    renderListStatus(auditEmpty,
                     auditStatus(rowCount, auditFailed, auditLoaded, filtered,
                                 auditRefused));
  }

  function renderRulesStatus(rowCount) {
    renderListStatus(rulesEmpty, rulesStatus(rowCount, rulesFailed, rulesLoaded));
  }

  const leasesEl = document.getElementById("leases");
  const leasesTableEl = document.getElementById("leases-table");
  const leasesEmptyEl = document.getElementById("leases-empty");
  const leasesCountEl = document.getElementById("leasecount");

  function renderLeasesStatus(rowCount) {
    renderListStatus(leasesEmptyEl, leasesStatus(rowCount, leasesFailed, leasesLoaded));
  }

  // ── the decisions view's controls ─────────────────────────────────────────
  // Rows the two views ask for. The glance stays at forty — it is read at a glance
  // and the number is what the coverage line is honest about. The record's page is
  // larger because it is read deliberately and paged, and it is bounded by the
  // backend's own ceiling regardless of what is asked for here (audit.EVENTS_LIMIT_MAX).
  const AUDIT_LIMIT = 40;
  const EVENTS_LIMIT = 100;
  // A keystroke is a query against the crown-jewel store, so the text box waits for a
  // pause. The selects do not: a click is already a deliberate act, and delaying it
  // reads as the page ignoring the click.
  const AUDIT_FILTER_DEBOUNCE_MS = 250;

  const auditQEl = document.getElementById("audit-q");
  const auditKindEl = document.getElementById("audit-kind");
  const auditWindowEl = document.getElementById("audit-window");
  const auditEveryEl = document.getElementById("audit-every");
  const auditClearEl = document.getElementById("audit-clear");
  const auditModeNote = document.getElementById("audit-mode");
  const auditGroupedTable = document.getElementById("audit-grouped-table");
  const auditEventsTable = document.getElementById("audit-events-table");
  const auditPagerEl = document.getElementById("audit-pager");
  const auditOlderEl = document.getElementById("audit-older");
  const auditNewerEl = document.getElementById("audit-newer");
  const auditPageEl = document.getElementById("audit-page");

  // Paging state for the record view. `auditCursors[i]` is the cursor that OPENS page
  // i+1 — i.e. what the backend returned as `next` while serving page i — so walking
  // back is popping an index rather than re-deriving anything. Kept as a list rather
  // than a single cursor because keyset paging is one-directional: there is no
  // "previous" cursor to compute, only one already seen.
  let auditCursors = [];
  let auditPage = 0;

  const readFilter = () => ({ q: auditQEl.value, kind: auditKindEl.value,
                              preset: auditWindowEl.value });
  const everyEvent = () => auditEveryEl.checked;
  const auditRowCount = () =>
    document.getElementById(everyEvent() ? "audit-events" : "audit").rows.length;

  function resetPaging() {
    auditCursors = [];
    auditPage = 0;
  }

  function renderGrouped(rows) {
    // Dates, not times: forty rows routinely span midnight, and a time-only stamp makes
    // them read as out of order at exactly the moment ordering matters. The `title`
    // carries the UTC instant, because the visible stamp is local and states no offset
    // — see fmtInstant.
    document.getElementById("audit").innerHTML = rows.map(r => {
      const a = auditRow(r);
      return `
        <tr${a.failClosed ? ' class="outage"' : ""}>
          <td class="ts" title="${esc(fmtInstant(a.ts))}">${esc(fmtStamp(a.ts))}</td>
          <td><span class="tag ${esc(a.kind)}">${esc(a.kind)}</span></td>
          <td>${a.stagePrefix ? `<span class="qual">${esc(a.stagePrefix)}</span>` : ""
            }${esc(a.target)}${a.repeat
              ? `<span class="rep">${esc(" " + a.repeat)}</span>` : ""}</td>
          <td class="ts">${a.clientClassPrefix
            ? `<span class="qual">${esc(a.clientClassPrefix)}</span>` : ""
            }${esc(a.client)}${a.actor
              ? `<span class="by" title="${esc(a.actorTitle)}">${esc(a.actor)}</span>`
              : ""}</td>
          <td>${esc(a.reason)}${a.firstTs
            ? esc(`${a.reason ? " · " : ""}first seen ${fmtStamp(a.firstTs)}`)
            : ""}</td></tr>`;
    }).join("");
  }

  // The record view. Same five columns as the glance, rendered from the same shaping
  // (see eventRow), plus the request itself — which is the column this view exists for
  // and the one the glance cannot carry.
  function renderEvents(rows) {
    document.getElementById("audit-events").innerHTML = rows.map(r => {
      const a = eventRow(r);
      return `
        <tr${a.failClosed ? ' class="outage"' : ""}>
          <td class="ts" title="${esc(fmtInstant(a.ts))}">${esc(fmtStamp(a.ts))}</td>
          <td><span class="tag ${esc(a.kind)}">${esc(a.kind)}</span></td>
          <td>${a.stagePrefix ? `<span class="qual">${esc(a.stagePrefix)}</span>` : ""
            }${esc(a.target)}</td>
          <td class="ts">${a.clientClassPrefix
            ? `<span class="qual">${esc(a.clientClassPrefix)}</span>` : ""
            }${esc(a.client)}${a.actor
              ? `<span class="by" title="${esc(a.actorTitle)}">${esc(a.actor)}</span>`
              : ""}</td>
          <td class="req"><code>${esc(a.request)}</code></td>
          <td>${esc(a.reason)}</td></tr>`;
    }).join("");
  }

  async function refreshAudit() {
    const f = readFilter();
    const events = everyEvent();
    const qs = auditQuery(f, {
      limit: events ? EVENTS_LIMIT : AUDIT_LIMIT,
      nowMs: Date.now(),
      // The cursor for the page being shown. Absent on page 0, which is what makes a
      // filter change (which resets paging) return to the newest rows.
      before: events ? auditCursors[auditPage - 1] : "",
    });
    let body;
    try {
      // Two calls rather than one with a computed path: the relay allowlist is
      // matched against the literal each `fetch` starts with (see the guard in
      // tests/test_control_plane_ui_js.py), and a path built by a ternary is a path
      // that test cannot see — which would take the 403-in-the-browser guard off
      // exactly the route being added.
      const res = events ? await fetch(`/api/audit/events?${qs}`)
                         : await fetch(`/api/audit?${qs}`);
      // 400 is the backend REFUSING these parameters and saying which one and why —
      // the sentence is the entire reason that response is a 400 rather than a
      // best-effort list (see `_bad_filter`). Throwing it into the catch below would
      // discard the sentence and render "could not refresh", which is both wrong about
      // the cause and unactionable. `.catch` on the parse because a refusal that
      // arrives without a readable body must still surface AS a refusal.
      if (res.status === 400) {
        const refusal = await res.json().catch(() => null);
        auditFailed = false;
        auditRefused = (refusal && refusal.detail) || AUDIT_REFUSED_FALLBACK;
        renderAuditStatus(auditRowCount(), filterActive(f));
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      body = await res.json();
    } catch (e) {
      // A failed refresh leaves the previous rows in place and SAYS SO. Silently
      // swallowing this is what let the list sit indefinitely stale while the header
      // read "live" — the stream and this poll are different transports.
      auditFailed = true;
      auditRefused = null;
      renderAuditStatus(auditRowCount(), filterActive(f));
      return;
    }
    auditFailed = false;
    auditRefused = null;
    auditLoaded = true;
    // {rows, total, filtered, next}. Tolerates a bare array from an older backend, in
    // which case `total` is undefined and coverageSummary stays silent rather than
    // guessing — and `filtered` is false, because a backend that ignored the
    // parameters served an unfiltered list whatever the controls on screen say.
    const rows = Array.isArray(body) ? body : (body && body.rows) || [];
    const total = Array.isArray(body) ? undefined : body && body.total;
    const filtered = !Array.isArray(body) && !!(body && body.filtered);
    const next = Array.isArray(body) ? null : (body && body.next) || null;

    // A page that fell off the end of the record. Reachable without anyone doing
    // anything wrong: `make audit-prune` deletes rows a cursor still points at. Snap
    // back to the newest page rather than render an empty table, which would read as
    // "nothing here" for a record that is not empty. Cannot loop — the retry is at
    // page 0, where an empty result is the honest answer.
    if (events && !rows.length && auditPage > 0) {
      resetPaging();
      return refreshAudit();
    }

    auditGroupedTable.hidden = events;
    auditEventsTable.hidden = !events;
    auditModeNote.textContent = events
      ? "· every event, newest first" : "· identical events folded";
    if (events) renderEvents(rows); else renderGrouped(rows);

    renderOutage(outageSummary(rows));
    // The two views answer "was there more?" differently, so only one of them speaks:
    // the glance has a coverage line because it silently truncates, the record has a
    // pager because it does not.
    renderCoverage(events ? { show: false, level: "none", text: "" }
                          : coverageSummary(rows, total, filtered));
    if (events) {
      // Remember the cursor that opens the NEXT page, and forget any beyond it — the
      // record can shrink under `make audit-prune`, and stale cursors would offer an
      // "older" that lands nowhere.
      if (next) auditCursors[auditPage] = next;
      else auditCursors.length = auditPage;
      const pager = historyPager(auditPage, EVENTS_LIMIT, rows.length, total,
                                 !!next, filtered);
      auditPageEl.textContent = pager.text;
      auditOlderEl.disabled = !pager.older;
      auditNewerEl.disabled = !pager.newer;
    }
    auditPagerEl.hidden = !events;
    renderAuditStatus(rows.length, filtered);
  }

  // A filter change RESETS paging, always. Keeping the cursor would apply a position
  // derived from one query to the results of another — the rows at that cursor may not
  // match the new filter at all, so the operator would land on an arbitrary page of a
  // list they just narrowed.
  let auditFilterTimer = null;
  function filtersChanged(immediate) {
    clearTimeout(auditFilterTimer);
    resetPaging();
    auditFilterTimer = setTimeout(refreshAudit,
                                  immediate ? 0 : AUDIT_FILTER_DEBOUNCE_MS);
  }

  auditQEl.addEventListener("input", () => filtersChanged(false));
  for (const el of [auditKindEl, auditWindowEl, auditEveryEl]) {
    el.addEventListener("change", () => filtersChanged(true));
  }
  auditClearEl.addEventListener("click", () => {
    auditQEl.value = "";
    auditKindEl.value = "";
    auditWindowEl.value = "";
    // The view switch is deliberately NOT cleared: it selects which record the
    // filters apply to, so resetting it would answer a question nobody asked.
    filtersChanged(true);
  });
  auditOlderEl.addEventListener("click", () => {
    if (auditCursors[auditPage] === undefined) return;   // no next page to open
    auditPage += 1;
    refreshAudit();
  });
  auditNewerEl.addEventListener("click", () => {
    if (auditPage === 0) return;
    auditPage -= 1;
    refreshAudit();
  });

  async function refreshRules() {
    let rows;
    try {
      const res = await fetch("/api/egress/rules");
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
    // Signature over pattern+action+class, not just the COUNT: a rule whose action
    // flipped is the change most worth noticing, and it leaves the count alone. The
    // class is in it for the same reason — the same pattern and action in a second
    // class is a real policy change, and without it two such rules sign identically.
    // Keyed by id so the click handler works from the ROW rather than from
    // markup it would otherwise have to parse back out of the DOM.
    rulesById = new Map(rows.map(r => [String(r.id), r]));
    const sig = JSON.stringify(
      rows.map(r => r.action + " " + r.pattern + " " + (r.client_class || "")));
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
      // Edit before revoke, in the order the operator should reach for them: changing a
      // rule is the recoverable action and taking it away is not, so the destructive one
      // is not the first button under the pointer.
      const control = p.allowed
        ? `<button type="button" class="edit" data-rule="${esc(String(r.id))}"
             >edit</button>
           <button type="button" class="revoke" data-rule="${esc(String(r.id))}"
             >revoke</button>`
        : `<span class="ts" title="${esc(p.text)}">from seed</span>`;
      return `<tr>
        <td><span class="tag ${esc(r.action)}">${esc(r.action)}</span></td>
        <td><code>${esc(r.pattern)}</code></td>
        <td class="${wild ? "wild" : "ts"}">${esc(r.scope)}</td>
        <!-- WHICH client population this rule decides for. Load-bearing rather than
             informational: the same pattern can appear twice with different actions,
             one row per class, and without this column those two rows look like a
             contradiction instead of two scoped rules. -->
        <td class="ts">${esc(r.client_class || "")}</td>
        <td class="ts">${esc(r.source)}</td>
        <td class="ts">${r.created_at ? fmtStamp(r.created_at) : ""}</td>
        <td>${control}</td></tr>`;
    }).join("");
    renderRulesStatus(rows.length);
    // The preview's conflict check reads these rows, so a rule that appeared elsewhere
    // shows up in the form rather than waiting to surface as a 409 on click.
    renderRulePreview();
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
    // Editing loads the row into the form above rather than acting here. No confirm on
    // THIS click: it changes nothing yet, and the form's own confirm is the one that
    // guards the write.
    const editBtn = ev.target.closest("button.edit");
    if (editBtn) {
      const editRow = rulesById.get(editBtn.dataset.rule);
      if (editRow) enterEditMode(editRow);
      return;
    }
    const btn = ev.target.closest("button.revoke");
    if (!btn) return;
    const row = rulesById.get(btn.dataset.rule);
    if (!row) return;
    const p = revokePreview(row);
    if (!p.allowed) return;
    if (!window.confirm(`${p.text}\n\nRevoke ${p.pattern}?`)) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/egress/rules/${encodeURIComponent(btn.dataset.rule)}/revoke`,
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

  // ── the live leases ───────────────────────────────────────────────────────
  // What is being allowed RIGHT NOW on a timer, which until this table existed was
  // the one kind of granted egress with nowhere to see it: a lease writes no rule, so
  // the standing-policy view is silent about it, and by the time an operator went
  // looking in the decisions log the grant might already have lapsed.
  //
  // It also makes the revoke button worth having. A grant nobody can see is a grant
  // nobody closes early, which would leave the configured duration doing the whole job
  // — and it is exactly that revocability that let the default be half an hour rather
  // than a few minutes.
  async function refreshLeases() {
    let rows;
    try {
      const res = await fetch("/api/egress/leases");
      // Checked like the rules poll: a 4xx body reaching .json() either throws
      // somewhere less obvious or parses into something that renders as "nothing is
      // leased", which is the reassuring reading and the wrong one.
      if (!res.ok) throw new Error(String(res.status));
      rows = await res.json();
    } catch (e) {
      // Keeps the rows and says so, rather than blanking a table whose whole subject
      // is what is in force at this moment — an empty one would read as "nothing is
      // granted" when what happened is that we stopped being able to tell.
      leasesFailed = true;
      renderLeasesStatus(document.getElementById("leases").rows.length);
      return;
    }
    leasesFailed = false;
    leasesLoaded = true;
    leasesById = new Map(rows.map(r => [String(r.id), r]));
    // The count is of LEASES, not of rendered rows: it answers "how much is granted
    // right now", and a number that shrank because four hosts folded into one line
    // would be answering a question about the table instead.
    leasesCountEl.textContent = rows.length ? `· ${rows.length} live` : "";
    leasesTableEl.hidden = rows.length === 0;
    const groups = groupLeases(rows);
    // Groups that no longer exist are dropped from the expanded set here rather than
    // left to accumulate: the key is (class, domain), so a group whose last lease
    // lapsed would otherwise keep its expansion and silently re-expand if the same
    // domain were leased again half an hour later.
    const live = new Set(groups.map(g => g.key));
    for (const key of [...expandedLeaseGroups]) {
      if (!live.has(key)) expandedLeaseGroups.delete(key);
    }
    leasesEl.innerHTML = groups.map(g => {
      if (!g.grouped) return leaseRow(g.leases[0], false);
      const open = expandedLeaseGroups.has(g.key);
      const cd = leaseCountdown(leaseRemaining(g.soonest, Date.now()));
      // The summary counts down by the group's SOONEST expiry, which is the next thing
      // about it that will change — and it carries `data-expires` like any other
      // countdown cell, so the one-second tick moves it with no special case.
      //
      // No revoke on this row. A button that ended four grants at once is a different
      // and much sharper action than the per-lease one, and it would need the confirm
      // step this table deliberately does not have; revocation stays where the thing
      // being revoked is named.
      const summary = `<tr class="lease-group">
        <td><button type="button" class="group-toggle"
              data-group="${esc(g.key)}" aria-expanded="${open}"
            >${open ? "▾" : "▸"}</button>
          <code>${esc(g.domain)}</code>
          <span class="ts">${g.count} hosts</span></td>
        <td class="ts">${esc(g.clientClass)}</td>
        <td class="lease-left${cd.urgent ? " urgent" : ""}"
            data-expires="${esc(String(g.soonest))}">${esc(cd.text)}</td>
        <td class="ts">first to lapse</td>
        <td></td></tr>`;
      return summary + g.leases.map(r => leaseRow(r, true, g.key, open)).join("");
    }).join("");
    renderLeasesStatus(rows.length);
  }

  // One lease as a row. `member` rows belong to a drawn group: indented, and hidden
  // while it is collapsed — HIDDEN rather than omitted, so expanding is a class flip on
  // rows already in the DOM and cannot race the four-second poll that would otherwise
  // have to re-render to produce them.
  //
  // `data-expires` carries the absolute deadline so the one-second tick can rewrite the
  // countdown without another fetch. The row is otherwise static, and re-polling once a
  // second to move a clock would be the firehose `/api/egress/leases` avoids by not
  // sending a remaining-seconds field at all.
  function leaseRow(r, member, groupKey, open) {
    const cd = leaseCountdown(leaseRemaining(r.expires_at, Date.now()));
    return `<tr${member ? ` class="lease-member" data-group="${esc(groupKey)}"` : ""}
        ${member && !open ? "hidden" : ""}>
      <td><code>${esc(r.host)}</code></td>
      <td class="ts">${esc(r.client_class || "")}</td>
      <td class="lease-left${cd.urgent ? " urgent" : ""}"
          data-expires="${esc(String(r.expires_at))}">${esc(cd.text)}</td>
      <td class="actor" title="${esc(r.granted_by || "")}"
        >${esc(shortActor(r.granted_by))}</td>
      <td><button type="button" class="revoke"
            data-lease="${esc(String(r.id))}">revoke</button></td></tr>`;
  }

  // Only the countdown cells, and only their text and urgency — never the row. Rebuilding
  // the table every second would drop a click landing on a revoke button at the moment
  // the tick fired, which is precisely when an operator is most likely to be pressing one.
  function updateLeaseCountdowns() {
    const now = Date.now();
    for (const cell of leasesEl.querySelectorAll("td.lease-left")) {
      const cd = leaseCountdown(leaseRemaining(cell.dataset.expires, now));
      cell.textContent = cd.text;
      cell.classList.toggle("urgent", cd.urgent);
    }
  }

  // Delegated for the reason the rules table's handler is: the tbody is replaced
  // wholesale on every poll, so a per-button listener would be re-bound every four
  // seconds and lost in between.
  //
  // No `confirm()`, where revoking a RULE has one. The asymmetry is the point: that
  // dialog guards an action with no undo, and this one has an obvious undo — the host
  // goes back to being held, so the next request raises a card and the operator can
  // grant it again. Making the reversible action as heavy as the irreversible one is
  // how a confirmation stops being read.
  document.getElementById("leases").addEventListener("click", async (ev) => {
    // Expanding a group changes nothing and reaches nothing, so it is handled before
    // the revoke path and takes none of its ceremony. The state lives OUTSIDE the
    // render (`expandedLeaseGroups`) because the table is replaced wholesale every four
    // seconds: held in the DOM alone, an expanded group would collapse itself on the
    // next poll, which is the same class of bug as re-rendering on the one-second tick.
    const toggle = ev.target.closest("button.group-toggle");
    if (toggle) {
      const key = toggle.dataset.group;
      if (expandedLeaseGroups.has(key)) expandedLeaseGroups.delete(key);
      else expandedLeaseGroups.add(key);
      const open = expandedLeaseGroups.has(key);
      // Applied to the rows already on screen rather than by re-rendering, so the click
      // does not depend on a fetch and cannot be undone by one in flight.
      toggle.textContent = open ? "▾" : "▸";
      toggle.setAttribute("aria-expanded", String(open));
      // Matched by comparing `dataset.group` rather than by an attribute SELECTOR built
      // from the key. Half the key is a host the agent chose, so a selector would need
      // escaping to be correct inside a quoted attribute value — `CSS.escape` does in
      // fact produce a string that matches there, but reasoning about why is work this
      // does not need to cost. A comparison has no escaping question to get wrong.
      for (const row of leasesEl.querySelectorAll("tr.lease-member")) {
        if (row.dataset.group === key) row.hidden = !open;
      }
      return;
    }
    const btn = ev.target.closest("button.revoke");
    if (!btn) return;
    const row = leasesById.get(btn.dataset.lease);
    if (!row) return;
    btn.disabled = true;
    try {
      const res = await fetch(
        `/api/egress/leases/${encodeURIComponent(btn.dataset.lease)}/revoke`,
        { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // A 404 here is the ordinary race, not a fault: the lease expired, or another
        // tab revoked it, between this table being drawn and the click. Said plainly
        // rather than as a failure, because the operator's intent — that host is no
        // longer leased — is now satisfied either way.
        window.alert(res.status === 404
          ? "That lease is already gone — it expired or was revoked elsewhere."
          : `Could not revoke: ${body.detail || res.status}`);
        btn.disabled = false;
        refreshLeases();
        return;
      }
    } catch (e) {
      window.alert("Could not revoke: the control plane is unreachable.");
      btn.disabled = false;
      return;
    }
    refreshLeases();
    // The revocation is an audited decision, so it belongs in the decisions view as
    // soon as it happened rather than on that view's own next poll.
    refreshAudit();
  });

  // ── writing a rule with no held request behind it ─────────────────────────
  // The config-first half of policy. Every other rule in the store is downstream of
  // something the agent already did — a seed entry, or a `+ persist` on a card — so
  // until this form existed, pre-authorizing a registry meant letting a build block
  // for the whole hold window first, and writing a block before anything asked for it
  // could not be expressed at all.
  const ruleFormEl = document.getElementById("rule-form");
  const rulePatternEl = document.getElementById("rule-pattern");
  const ruleActionEl = document.getElementById("rule-action");
  const ruleClassEl = document.getElementById("rule-class");
  const ruleAddEl = document.getElementById("rule-add");
  const ruleCancelEl = document.getElementById("rule-cancel");
  const rulePreviewEl = document.getElementById("rule-preview");
  // The id being edited, or null for the create form. Editing REUSES this form rather
  // than adding a second one: the fields are the same fields and the validation is the
  // same validation, so a separate editor would be a second place for the wildcard floor
  // and the conflict check to drift out of. It also means an operator cannot be halfway
  // through both at once.
  let editingRuleId = null;
  // What the last submit came back with. A separate fact from the preview, and it
  // OUTRANKS it: the preview describes what a click would do, and this describes what
  // the last one actually did — including the refusals this page deliberately does not
  // mirror (see createPreview).
  let ruleNotice = null;

  function renderClassOptions() {
    const chosen = ruleClassEl.value;
    ruleClassEl.innerHTML = clientClasses.map(
      c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
    // Keep the operator's choice across a refresh; otherwise a poll landing mid-type
    // silently re-scopes the rule they are composing.
    if (chosen && clientClasses.includes(chosen)) ruleClassEl.value = chosen;
    // With no classes the form cannot produce a rule the backend would accept, so it
    // is disabled and SAYS why, rather than offering an empty picker that 400s.
    const usable = clientClasses.length > 0;
    for (const el of [rulePatternEl, ruleActionEl, ruleClassEl, ruleAddEl]) {
      el.disabled = !usable;
    }
    // A config poll must not hand back a control this form deliberately locked. Without
    // this, re-rendering the class list mid-edit re-enables the picker and the operator
    // can re-scope a rule the backend will not re-scope.
    if (editingRuleId !== null) ruleClassEl.disabled = true;
    renderRulePreview();
  }

  function enterEditMode(row) {
    editingRuleId = String(row.id);
    rulePatternEl.value = row.pattern || "";
    ruleActionEl.value = row.action === "block" ? "block" : "allow";
    // The class is shown but LOCKED. Moving a rule between client classes takes policy
    // from one population and gives it to another, which the backend refuses to call an
    // edit (see RuleEditRequest) — disabled rather than hidden, so the form still says
    // who the rule decides for.
    if (row.client_class) ruleClassEl.value = row.client_class;
    ruleClassEl.disabled = true;
    ruleCancelEl.hidden = false;
    ruleAddEl.textContent = "save changes";
    // Any verdict from a previous submit is about a different rule now.
    ruleNotice = null;
    renderRulePreview();
    rulePatternEl.focus();
  }

  function leaveEditMode() {
    editingRuleId = null;
    rulePatternEl.value = "";
    ruleClassEl.disabled = !clientClasses.length;
    ruleCancelEl.hidden = true;
    ruleAddEl.textContent = "add rule";
    renderRulePreview();
  }

  function currentPreview() {
    const rules = [...rulesById.values()];
    if (editingRuleId !== null) {
      // Looked up on every render rather than captured at entry, so a rule revoked or
      // changed under the form is noticed by the preview instead of being written over.
      return editPreview(rulesById.get(editingRuleId), rulePatternEl.value,
                         ruleActionEl.value, rules);
    }
    return createPreview(rulePatternEl.value, ruleActionEl.value, ruleClassEl.value,
                         rules);
  }

  function renderRulePreview() {
    if (!clientClasses.length) {
      // Silent while the first /api/config is still in flight — a form that is briefly
      // disabled explains itself a moment later, whereas an error shown before anything
      // has failed is simply wrong.
      rulePreviewEl.hidden = configState === "pending";
      rulePreviewEl.className = "empty";
      rulePreviewEl.textContent = configState === "failed"
        ? "Could not reach the control plane, so no rule can be scoped to a client "
          + "class yet."
        : "No client classes are configured (CONTROL_CLIENT_CLASSES), so a rule "
          + "written here could not decide for anyone.";
      return;
    }
    const p = currentPreview();
    ruleAddEl.disabled = !p.ok;
    const text = ruleNotice ? ruleNotice.text : p.text;
    rulePreviewEl.hidden = !text;
    // The loosening direction is called out the same way the revoke confirm calls out
    // its own: colour is never the only cue, so the wording carries it too.
    rulePreviewEl.className =
      "empty" + ((ruleNotice ? ruleNotice.bad : p.danger) ? " wild" : "");
    rulePreviewEl.textContent = text;
  }

  for (const el of [rulePatternEl, ruleActionEl, ruleClassEl]) {
    // Any edit invalidates the last submit's verdict — leaving it up would attach a
    // refusal to a rule that is no longer the one on screen.
    el.addEventListener("input", () => { ruleNotice = null; renderRulePreview(); });
    el.addEventListener("change", () => { ruleNotice = null; renderRulePreview(); });
  }

  // Leaves the rule exactly as it was: nothing has been sent at this point, so there is
  // nothing to undo and no confirm to ask for.
  ruleCancelEl.addEventListener("click", () => { ruleNotice = null; leaveEditMode(); });

  ruleFormEl.addEventListener("submit", async (ev) => {
    // Always: the page's own CSP sends `form-action 'none'`, so a native submit is
    // refused by the browser anyway — this is what makes that a fail-closed backstop
    // rather than a broken form.
    ev.preventDefault();
    const p = currentPreview();
    if (!p.ok) return;
    if (editingRuleId !== null) {
      await submitEdit(p);
      return;
    }
    // `confirm()` for the same reason the revoke path uses one: this table only changes
    // when policy does, so a modal is the right amount of friction for a write that
    // takes effect on the agent's very next request. The pattern quoted is the
    // NORMALIZED one, which is what will actually be stored.
    if (!window.confirm(`${p.text}\n\nAdd this ${p.verb} rule for ${p.pattern}?`)) return;
    ruleAddEl.disabled = true;
    try {
      const res = await fetch("/api/egress/rules", {
        method: "POST",
        headers: { "content-type": "application/json" },
        // What was PREVIEWED and confirmed, not what is in the box: the two differ
        // whenever normalization did anything, and the confirm has to be about the
        // rule that lands. All three fields come from the one preview object for that
        // reason — `client_class` used to re-read its select here, which was a second
        // derivation of a value the operator had already been shown.
        body: JSON.stringify({ pattern: p.pattern, action: p.verb,
                               client_class: p.clientClass }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // The backend's own sentence. The refusals worth reading rather than
        // collapsing into "failed" are 400 (a pattern this page did not mirror a check
        // for) and 409 (a rule appeared under us — the table on screen was stale).
        ruleNotice = { bad: true,
                       text: `Not added: ${body.detail || `the control plane answered `
                                                        + `${res.status}`}` };
      } else if (body.created === false) {
        // A 200 that wrote nothing, because the same rule arrived between the preview
        // and the click. Reported rather than treated as success — the same distinction
        // the approval cards draw between "rule written" and "already in place".
        ruleNotice = { bad: false,
                       text: `${body.pattern} was already a standing ${body.action} `
                           + `rule for ${body.client_class}; nothing was written.` };
      } else {
        ruleNotice = { bad: false,
                       text: `Added: ${body.pattern} now ${body.action}s for `
                           + `${body.client_class}.` };
        rulePatternEl.value = "";
      }
    } catch (e) {
      ruleNotice = { bad: true,
                     text: "Not added: the control plane is unreachable." };
    }
    ruleAddEl.disabled = false;
    renderRulePreview();
    refreshRules();
  });

  // The edit half of that submit. Split out rather than branched inline because the two
  // differ in more than a URL — the confirm names a transition, the success case leaves
  // edit mode, and `changed: false` is a different sentence from `created: false`.
  async function submitEdit(p) {
    // The same friction the create path applies, for a stronger reason: this write both
    // grants and takes away, and the confirm is the only place the operator sees both
    // halves stated together.
    if (!window.confirm(`${p.text}\n\nSave this change to ${p.pattern}?`)) return;
    ruleAddEl.disabled = true;
    try {
      const res = await fetch(
        `/api/egress/rules/${encodeURIComponent(editingRuleId)}/edit`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          // The PREVIEWED pattern, not the typed one, for the reason the create path
          // gives: normalization can change what lands, and the confirm has to have been
          // about the rule that does.
          body: JSON.stringify({ pattern: p.pattern, action: p.verb }),
        });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // 404 is the one worth reading here and it has no analogue on the create path:
        // the rule was revoked while the form was open, so there is nothing to edit and
        // retrying cannot help.
        ruleNotice = { bad: true,
                       text: `Not saved: ${body.detail || `the control plane answered `
                                                        + `${res.status}`}` };
      } else if (body.changed === false) {
        ruleNotice = { bad: false,
                       text: `${body.pattern} already ${body.action}s for `
                           + `${body.client_class}; nothing was written.` };
        leaveEditMode();
      } else {
        // Names both states, as the audit row does — "saved" alone would not say which
        // of the two things an edit can change actually moved.
        const prev = body.previous || {};
        ruleNotice = { bad: false,
                       text: `Saved: ${prev.pattern} (${prev.action}) is now `
                           + `${body.pattern} (${body.action}) for `
                           + `${body.client_class}.` };
        leaveEditMode();
      }
    } catch (e) {
      ruleNotice = { bad: true,
                     text: "Not saved: the control plane is unreachable." };
    }
    ruleAddEl.disabled = false;
    renderRulePreview();
    refreshRules();
  }

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
  // Once, not polled. Server registration changes when an operator changes it,
  // and this view is the thing doing the changing — each action refreshes after
  // itself. A four-second poll here would be watching for edits nobody else makes.
  //
  // Tool rules are the same kind of state and get the same treatment. The INVENTORY
  // is not: it changes because the gateway pushed, which happens without anyone
  // touching this page, so it is the one thing in this view that is polled.
  refreshServers();
  refreshToolRules();
  refreshInventory();
    };
    es.addEventListener("pending", e => {
      const d = JSON.parse(e.data);
      lastSaturation = d.saturation || null;
      renderSaturation();
      renderPending(renderableHolds(d.holds));
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

  // ── MCP servers ───────────────────────────────────────────────────────────
  // Which of the running servers the gateway may dial. Nothing here starts a
  // container and nothing here grants: a registration lands disabled, and an enabled
  // server with no tool rules still denies every call.
  const serversBody = document.getElementById("servers");
  const serversEmpty = document.getElementById("servers-empty");
  const serverForm = document.getElementById("server-form");
  const serverName = document.getElementById("server-name");
  const serverAuth = document.getElementById("server-auth");
  const serverHeader = document.getElementById("server-header");
  const serverTemplate = document.getElementById("server-template");
  const serverPreviewEl = document.getElementById("server-preview");
  let serversFailed = false;
  const serversByName = new Map();

  // No PORT anywhere in this form, and that is the convention rather than an omission:
  // every catalogue server listens on the one the gateway dials (tool-gateway's
  // MCP_PORT), so a port here would be a field that must always hold the same value.

  const authFields = () =>
    serverDescriptor(serverAuth.value, serverHeader.value, serverTemplate.value);

  function renderServerPreview() {
    const custom = serverAuth.value === "custom";
    serverHeader.hidden = !custom;
    serverTemplate.hidden = !custom;
    serverPreviewEl.textContent = serverPreview(serverName.value, authFields()).text;
  }

  function renderServers(rows) {
    serversByName.clear();
    serversBody.replaceChildren();
    for (const row of rows) {
      serversByName.set(row.server, row);
      const tr = document.createElement("tr");
      const cell = text => {
        const td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
        return td;
      };
      cell(row.server);
      cell(row.enabled ? "enabled" : "disabled");
      cell(row.auth && row.auth.type === "header"
             ? `${row.auth.header}: ${row.auth.template}` : "none");
      cell(String(row.tool_rules));
      const actions = document.createElement("td");
      for (const [cls, label] of [["toggle", row.enabled ? "disable" : "enable"],
                                  ["revoke", "revoke"]]) {
        const b = document.createElement("button");
        b.className = cls;
        b.dataset.server = row.server;
        b.textContent = label;
        actions.appendChild(b);
      }
      tr.appendChild(actions);
      serversBody.appendChild(tr);
    }
    document.getElementById("servercount").textContent =
      rows.length ? `${rows.length} registered` : "";
    serversEmpty.hidden = !(serversFailed || rows.length === 0);
    serversEmpty.textContent = serversFailed
      ? "This list did not refresh — the control plane did not answer. What is shown " +
        "may no longer be what the gateway is dialling."
      : (rows.length ? "" :
         "No servers registered, so the gateway dials nothing. Register the container " +
         "name declared in mcp-servers.yml.");
    // The tool-policy picker below reads THIS map for its server list, and the two
    // refreshes race on load — so the picker is rebuilt here rather than left to wait
    // for whichever poll happens to run next. Without it, arriving before the servers
    // did leaves a form that says nothing is registered after everything is.
    renderToolPicker();
  }

  async function refreshServers() {
    try {
      const res = await fetch("/api/mcp/servers");
      // `res.ok` first, for the reason refreshRules checks it: a 4xx body that parses
      // would render as an empty but SUCCESSFUL list — "no servers registered" is a
      // sentence this page must not say when it simply failed to ask.
      if (!res.ok) throw new Error(String(res.status));
      serversFailed = false;
      renderServers(await res.json());
    } catch (e) {
      serversFailed = true;
      renderServers([...serversByName.values()]);
    }
  }

  serverAuth.addEventListener("change", renderServerPreview);
  serverName.addEventListener("input", renderServerPreview);
  serverHeader.addEventListener("input", renderServerPreview);
  serverTemplate.addEventListener("input", renderServerPreview);

  serverForm.addEventListener("submit", async ev => {
    ev.preventDefault();
    const name = serverName.value.trim();
    if (!SERVER_NAME_RE.test(name)) return;
    const body = JSON.stringify(Object.assign({ server: name }, authFields()));
    try {
      const res = await fetch("/api/mcp/servers", {
        method: "POST", headers: { "Content-Type": "application/json" }, body });
      const answer = await res.json().catch(() => ({}));
      if (!res.ok || !answer.ok) {
        // Verbatim. The refusals here are worth reading rather than collapsing: 409
        // says it is already registered and points at edit, and 400 says exactly which
        // half of the descriptor is malformed.
        window.alert(`Could not register: ${answer.detail || res.status}`);
        return;
      }
    } catch (e) {
      window.alert("Could not register: the control plane is unreachable.");
      return;
    }
    serverName.value = "";
    renderServerPreview();
    refreshServers();
  });

  serversBody.addEventListener("click", async ev => {
    const btn = ev.target.closest("button.toggle, button.revoke");
    if (!btn) return;
    const row = serversByName.get(btn.dataset.server);
    if (!row) return;
    const name = encodeURIComponent(row.server);
    const revoking = btn.classList.contains("revoke");
    if (revoking &&
        !window.confirm(
          `Revoke ${row.server}? The gateway stops dialling it. Its tool rules are ` +
          `not deleted — the backend refuses this while any still name it.`)) {
      return;
    }
    btn.disabled = true;
    try {
      const res = revoking
        ? await fetch(`/api/mcp/servers/${name}/revoke`, { method: "POST" })
        // The descriptor is ECHOED BACK, and it has to be. `ServerEditRequest` takes
        // the TARGET state with `auth_type` defaulting to "none", so an edit carrying
        // only `enabled` would silently strip a server's auth — the next enumeration
        // would send no credential and the 401 would read like a policy problem.
        : await fetch(`/api/mcp/servers/${name}/edit`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(serverEditBody(row, !row.enabled)) });
      const answer = await res.json().catch(() => ({}));
      if (!res.ok || !answer.ok) {
        // 409 on revoke is the one an operator most needs to read: it names how many
        // rules still point at the server and tells them to disable instead.
        window.alert(`Could not ${revoking ? "revoke" : "update"}: ` +
                     `${answer.detail || res.status}`);
        btn.disabled = false;
        return;
      }
    } catch (e) {
      window.alert("Could not reach the control plane.");
      btn.disabled = false;
      return;
    }
    refreshServers();
  });

  // ── tool policy ───────────────────────────────────────────────────────────
  // The half of this view that actually decides. The servers table above says which
  // servers the gateway may dial; this says what may be called on them, and until a
  // row exists here an enabled server with a working credential still answers nothing.
  const toolRulesBody = document.getElementById("toolrules");
  const toolRulesEmpty = document.getElementById("toolrules-empty");
  const toolRuleForm = document.getElementById("toolrule-form");
  const toolRuleServer = document.getElementById("toolrule-server");
  const toolRuleTool = document.getElementById("toolrule-tool");
  const toolRuleAction = document.getElementById("toolrule-action");
  const toolRuleAdd = document.getElementById("toolrule-add");
  const toolRuleNote = document.getElementById("toolrule-note");
  const toolRulePreviewEl = document.getElementById("toolrule-preview");
  const toolRuleCountEl = document.getElementById("toolrulecount");

  let toolRules = [];
  let toolRulesById = new Map();
  let toolRulesFailed = false;
  let toolRulesLoaded = false;
  let inventory = {};
  let inventoryFailed = false;
  // What the last submit came back with, outranking the preview while it stands — the
  // same split the egress form draws between "what this click would do" and "what the
  // last one did", including the refusals this page deliberately does not mirror.
  let toolNotice = null;

  function renderToolServerOptions() {
    const chosen = toolRuleServer.value;
    const rows = [...serversByName.values()];
    toolRuleServer.replaceChildren();
    for (const row of rows) {
      const opt = document.createElement("option");
      opt.value = row.server;
      // The disabled state rides on the option rather than being left to be read off
      // the table above: a rule written for a server the gateway has stopped dialling
      // decides nothing, and this form is where that is about to happen.
      opt.textContent = row.enabled ? row.server : `${row.server} (disabled)`;
      toolRuleServer.appendChild(opt);
    }
    // Keep the operator's choice across a refresh; otherwise a poll landing mid-edit
    // silently re-points the rule they are composing at another server.
    if (chosen && rows.some(r => r.server === chosen)) toolRuleServer.value = chosen;
  }

  function renderToolPicker() {
    renderToolServerOptions();
    const choices = toolChoices(toolRuleServer.value, inventory, toolRules);
    const chosen = toolRuleTool.value;
    toolRuleTool.replaceChildren();
    const group = (label, items) => {
      if (!items.length) return;
      const optgroup = document.createElement("optgroup");
      optgroup.label = label;
      for (const item of items) {
        const opt = document.createElement("option");
        opt.value = item.tool;
        // `textContent`, and the whole table below is built the same way: every string
        // here is SERVER-AUTHORED. An escaped template would be correct too, right up
        // until someone edits it — this cannot be got wrong later.
        opt.textContent = item.action
          ? `${item.tool} — ${item.action}${item.readOnly ? ", read-only" : ""}`
          : `${item.tool}${item.readOnly ? " — read-only" : ""}`;
        optgroup.appendChild(opt);
      }
      toolRuleTool.appendChild(optgroup);
    };
    // Unruled FIRST, which is the point of the join rather than a sorting preference:
    // those are the tools denied today, and the only ones a new rule can be written
    // for at all.
    group("no rule yet — denied", choices.unruled);
    group("already ruled", choices.ruled);
    // Keep the operator's choice across a refresh; otherwise an inventory push landing
    // mid-edit silently re-points the rule they are composing at a different tool.
    if (choices.names.includes(chosen)) toolRuleTool.value = chosen;
    // Empty because nothing has been reported, which is a state this form cannot write
    // its way out of — there is no free-text path, deliberately (see index.html). The
    // control is disabled rather than left as an empty box that looks clickable.
    toolRuleTool.disabled = choices.names.length === 0;
    // Both of these are facts about this PAGE rather than about a server, which is why
    // they are appended here instead of inside the pure helper — it would have to be
    // told about registration and about polling to say either.
    //
    // With nothing registered, "pick a server" is advice that cannot be taken, and the
    // backend would refuse the rule anyway: it will not write policy about a server
    // nobody registered. Say what to do instead.
    toolRuleNote.textContent = serversByName.size === 0
      ? "No servers are registered, so there is no tool to write a rule about. "
        + "Register one above first."
      : inventoryFailed
        ? `${choices.note} This list did not refresh — the control plane did not `
          + `answer, so what a server exposes may have changed.`
        : choices.note;
    renderToolRulePreview();
  }

  function renderToolRulePreview() {
    const p = toolRulePreview(toolRuleServer.value, toolRuleTool.value,
                              toolRuleAction.value, toolRules);
    // With no servers registered the backend refuses every rule this form could
    // produce — it will not write policy about a server nobody registered — so the
    // button is off and the note above says why. The same goes for a server that has
    // reported no tools: there is nothing the picker could have selected.
    toolRuleAdd.disabled = !p.ok || serversByName.size === 0 || toolRuleTool.disabled;
    const text = toolNotice ? toolNotice.text : p.text;
    toolRulePreviewEl.hidden = !text;
    // Colour is never the only cue here either; the wording carries the direction.
    toolRulePreviewEl.className =
      "empty" + ((toolNotice ? toolNotice.bad : p.danger) ? " wild" : "");
    toolRulePreviewEl.textContent = text;
  }

  function renderToolRules(rows) {
    // Keyed by id so the click handler works from the ROW rather than parsing it back
    // out of the DOM — the same reason the egress table keeps `rulesById`.
    toolRulesById = new Map(rows.map(r => [String(r.id), r]));
    toolRulesBody.replaceChildren();
    for (const row of rows) {
      const tr = document.createElement("tr");
      const cell = (value, cls) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (cls) td.className = cls;
        tr.appendChild(td);
      };
      cell(row.server);
      cell(row.tool);
      const actionCell = document.createElement("td");
      const tag = document.createElement("span");
      tag.className = `tag ${row.action}`;
      tag.textContent = row.action;
      actionCell.appendChild(tag);
      tr.appendChild(actionCell);
      // Whether the server still OFFERS this tool. A rule naming one it does not is
      // inert while reading here exactly like policy in force — the confusion the
      // gateway's `ruled_but_absent` report names, said next to the rule itself.
      // "unknown" and "no" are different facts: the first means nobody has asked.
      const entry = inventory[row.server];
      cell(!entry || !entry.enumerated
             ? "unknown"
             : (entry.tools || []).includes(row.tool) ? "yes" : "no", "ts");
      cell(row.created_at ? fmtStamp(row.created_at) : "", "ts");
      const actions = document.createElement("td");
      // The two actions this rule is NOT, rather than an edit mode: a tool rule's
      // identity is (server, tool) and only the action can change, so promoting one
      // between deny, ask and allow IS the whole edit. A form standing in for the row
      // would be a second place for that to be got wrong.
      for (const target of ["deny", "ask", "allow"]) {
        if (target === row.action) continue;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "edit";
        button.dataset.rule = String(row.id);
        button.dataset.action = target;
        button.textContent = `→ ${target}`;
        actions.appendChild(button);
      }
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "revoke";
      revoke.dataset.rule = String(row.id);
      revoke.textContent = "revoke";
      actions.appendChild(revoke);
      tr.appendChild(actions);
      toolRulesBody.appendChild(tr);
    }
    toolRuleCountEl.textContent =
      rows.length ? `· ${rows.length} rule${rows.length === 1 ? "" : "s"}` : "· none";
    renderListStatus(toolRulesEmpty,
                     toolRulesStatus(rows.length, toolRulesFailed, toolRulesLoaded));
    // The picker reads these rows for its join and its conflict check, so a rule that
    // appeared elsewhere shows up there rather than waiting to surface as a 409.
    renderToolPicker();
  }

  async function refreshToolRules() {
    try {
      const res = await fetch("/api/mcp/rules");
      // `res.ok` first, for the reason refreshRules checks it: a 4xx body that parsed
      // would render as an empty but SUCCESSFUL policy — and an empty tool policy is a
      // sentence meaning "everything is denied", which must not be said on a failure.
      if (!res.ok) throw new Error(String(res.status));
      toolRules = await res.json();
      toolRulesFailed = false;
      toolRulesLoaded = true;
    } catch (e) {
      // Keeps the rows and SAYS SO. A tool-policy table that quietly stops refreshing
      // misstates what the gateway is letting through, which is the one question this
      // view exists to answer.
      toolRulesFailed = true;
    }
    renderToolRules(toolRules);
  }

  async function refreshInventory() {
    try {
      const res = await fetch("/api/mcp/inventory");
      if (!res.ok) throw new Error(String(res.status));
      inventory = await res.json();
      inventoryFailed = false;
    } catch (e) {
      // The last picture stands. Unlike the rules above, nothing here decides
      // anything — the inventory only adds context — so a failed poll costs accuracy
      // in the picker and in the "exposed" column. The NOTE is where that is said;
      // the column cannot carry it without a fourth value meaning "stale", which
      // would be a distinction nobody could act on differently from "unknown".
      inventoryFailed = true;
    }
    renderToolRules(toolRules);
  }

  for (const el of [toolRuleServer, toolRuleTool, toolRuleAction]) {
    // Any edit invalidates the last submit's verdict — leaving it up would attach a
    // refusal to a rule that is no longer the one on screen.
    el.addEventListener("change", () => { toolNotice = null; renderToolPicker(); });
  }

  toolRuleForm.addEventListener("submit", async ev => {
    // Always, for the reason the egress form gives: `form-action 'none'` in the CSP
    // makes the native submit a fail-closed backstop rather than a broken form.
    ev.preventDefault();
    const p = toolRulePreview(toolRuleServer.value, toolRuleTool.value,
                              toolRuleAction.value, toolRules);
    if (!p.ok) return;
    // NO CONFIRM STEP, and dropping it was a correction rather than a relaxation. The
    // egress form confirms because its pattern is DERIVED — `example.com` and
    // `*.example.com` are different grants and the controls do not say which you picked,
    // so the dialog is the first place the breadth is visible. Nothing here is derived:
    // the server and the tool are chosen from two lists, the action from a third, and
    // `#toolrule-preview` renders this exact sentence live, BEFORE the click. A modal
    // repeating it afterwards asked the operator to read the same text twice and moved
    // the disclosure to the wrong side of the decision.
    //
    // The friction that remains is on the TABLE, where a row action is a single click
    // with no preview beside it, and only when that click widens — see the handler
    // below. Ruling a long tool list is mostly `deny` and `ask`, and neither should cost
    // a dialog.
    toolRuleAdd.disabled = true;
    try {
      const res = await fetch("/api/mcp/rules", {
        method: "POST", headers: { "content-type": "application/json" },
        // Straight off the PREVIEW, never re-read from the controls: what was
        // confirmed has to be what is sent, which is the same single-derivation
        // discipline the egress form follows for its pattern and class.
        body: JSON.stringify({ server: p.server, tool: p.tool, action: p.action }) });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // Verbatim. The refusals worth reading rather than collapsing into "failed"
        // are 400 (a tool name this page did not mirror a check for), and 409 — a rule
        // appeared under us, so the table on screen was stale.
        toolNotice = { bad: true,
                       text: `Not added: ${body.detail
                                           || `the control plane answered ${res.status}`}` };
      } else if (body.created === false) {
        // A 200 that wrote nothing, because the same rule arrived between the preview
        // and the click. Reported rather than read as a write.
        toolNotice = { bad: false,
                       text: `${body.tool} on ${body.server} was already a standing `
                           + `${body.action} rule; nothing was written.` };
      } else {
        toolNotice = { bad: false,
                       text: `Added: ${body.tool} on ${body.server} now `
                           + `${body.action}s.` };
      }
    } catch (e) {
      toolNotice = { bad: true, text: "Not added: the control plane is unreachable." };
    }
    toolRuleAdd.disabled = false;
    await refreshToolRules();
    // The servers table counts rules per server, so it is stale the moment this lands.
    refreshServers();
  });

  // Delegated, because the table is replaced wholesale on every refresh — a handler
  // bound per button would be lost with the row it was bound to.
  toolRulesBody.addEventListener("click", async ev => {
    const btn = ev.target.closest("button.edit, button.revoke");
    if (!btn) return;
    const row = toolRulesById.get(btn.dataset.rule);
    if (!row) return;
    const revoking = btn.classList.contains("revoke");
    const p = revoking ? toolRevokePreview(row) : toolEditPreview(row, btn.dataset.action);
    if (!revoking && !p.ok) return;
    // CONFIRMED ONLY WHEN THE CLICK WIDENS, which `toolEditPreview` already computes as
    // `danger` — `toolRank(to) > toolRank(from)`, so deny→ask counts as well as anything
    // →allow. The direction is the whole criterion: a row button is one click with no
    // preview beside it, so the dialog is this path's only disclosure, and it is worth
    // spending exactly where capability increases.
    //
    // Revoking never asks, and that is not an omission. Removing a tool rule returns the
    // tool to unconfigured, which DENIES — `toolRevokePreview` exists as its own function
    // because this is the one place the tool surface differs from the egress one, where
    // removing a block returns a host to being HELD and therefore to being approvable.
    // Narrowing does not need a guard rail.
    if (!revoking && p.danger
        && !window.confirm(`${p.text}\n\nSave this rule?`)) {
      return;
    }
    btn.disabled = true;
    const id = encodeURIComponent(String(row.id));
    try {
      // Two whole literal paths rather than one built from a ternary, which is the
      // shape the servers handler above uses and not an accident: the relay allowlist
      // is matched against the literal each `fetch` begins with, and a path assembled
      // by concatenation is invisible to the test that holds the two ends together.
      const res = revoking
        ? await fetch(`/api/mcp/rules/${id}/revoke`, { method: "POST" })
        : await fetch(`/api/mcp/rules/${id}/edit`, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ action: btn.dataset.action }) });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // 404 is the one worth reading: the rule was revoked in another tab, so the
        // table on screen is stale and retrying cannot help.
        window.alert(`Could not ${revoking ? "revoke" : "update"}: ` +
                     `${body.detail || res.status}`);
        btn.disabled = false;
        return;
      }
    } catch (e) {
      window.alert("Could not reach the control plane.");
      btn.disabled = false;
      return;
    }
    await refreshToolRules();
    refreshServers();
  });

  // ── wiring ────────────────────────────────────────────────────────────────
  // `visibilityState` is absent in some non-browser hosts; treat unknown as
  // visible so a missing API degrades to the old always-poll behaviour rather
  // than to a page that silently stops updating.
  const visible = () => document.visibilityState !== "hidden";

  showView(current);
  syncNotifyButton();
  connect();
  refreshAudit();
  refreshRules();
  refreshLeases();
  // Also here, not only on stream open: the rule form needs the client classes, and it
  // has to work while the SSE feed is down — which is exactly when an operator is most
  // likely to be writing policy by hand rather than clicking cards.
  refreshConfig();
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
  // Gated and paced like the other two. The COUNTDOWNS do not depend on this poll —
  // they tick from each row's own deadline on the one-second interval — so what four
  // seconds bounds is only how long a lease that was revoked elsewhere, or that has
  // just lapsed, stays listed.
  setInterval(() => { if (visible()) refreshLeases(); }, 4000);
  // Gated on the VIEW as well, which the three above deliberately are not. They feed
  // badges that have to report a hidden view's state; this feeds a picker and a column
  // nobody can see from anywhere else, so polling it while another view is up would be
  // work with no reader. Paced to the gateway's own roster tick (GATEWAY_ROSTER_INTERVAL,
  // 10s) rather than to the four seconds the others use: pushes cannot arrive faster
  // than that, so a shorter poll could only re-fetch what it already has.
  setInterval(() => {
    if (visible() && current === "tools") refreshInventory();
  }, 10000);
  // Refresh IMMEDIATELY on return, rather than leaving up to four seconds of
  // stale-but-unlabelled data on screen at the moment attention comes back to it.
  document.addEventListener("visibilitychange", () => {
    if (visible()) {
      refreshAudit();
      refreshRules();
      refreshLeases();
      if (current === "tools") refreshInventory();
    }
  });
}

// Browser: run the page. Node (the unit tests): import the pure helpers and touch
// nothing — see the header comment on why importing this file must be side-effect free.
if (typeof document !== "undefined") { start(); }
export {
  lampState, backoffDelay, saturationState,
  ackCount, capScope, auditStatus, rulesStatus, toolRulesStatus,
  leaseLabel, leaseRemaining, leaseCountdown, leasesStatus,
  leaseDomain, groupLeases, LEASE_GROUP_MIN,
  RECONNECT_MIN_MS, RECONNECT_MAX_MS,
  SATURATION_RECENT_MS, SATURATION_WARN_FRAC,
};
