// SPDX-License-Identifier: Apache-2.0
/* Hold-cap pressure as the operator sees it: when the control plane is refusing
 * requests unheard, the banner that says so, and what dismissing it acknowledges.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

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
export const SATURATION_RECENT_MS = 60000;
export const SATURATION_WARN_FRAC = 0.75;

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
export function capScope(scope) {
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
export function saturationState(sat, nowMs, dismissedCount = 0) {
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

// What a Dismiss click acknowledges. A one-line function only because it must be the
// SAME expression the optimistic local hide uses and the one the POST body carries:
// with the number written twice, the two can disagree, and the failure is silent —
// the banner hides, the POST returns 200, and the dismissal simply does not persist.
// As a pure function it is also the only part of the dismiss path a unit test can
// reach, `start()` being deliberately unverified.
export function ackCount(sat) {
  return sat ? Math.max(0, Number(sat.rejections) || 0) : 0;
}
