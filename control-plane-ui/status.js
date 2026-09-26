// SPDX-License-Identifier: Apache-2.0
/* What each polled table says about itself: empty, stale, or not yet loaded, which
 * render identically and mean different things. One decision (`pollStatus`) over
 * four tables, each supplying only its sentences — so none of them can quietly lose
 * the honesty the others have.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

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
export function pollStatus(texts, rowCount, failed, loaded) {
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
export const AUDIT_REFUSED_FALLBACK =
  "These filters were refused, so the events below still answer the previous " +
  "question. Adjust them and try again.";
export function auditStatus(rowCount, failed, loaded, filtered, refused) {
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
export function rulesStatus(rowCount, failed, loaded) {
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
export function toolRulesStatus(rowCount, failed, loaded) {
  return pollStatus(TOOL_RULES_STATUS_TEXT, rowCount, failed, loaded);
}

// Stale is the one that matters: a pin the table no longer shows may still be
// answering calls with no card.
const TOOL_PINS_STATUS_TEXT = {
  stale: "Could not refresh — these are the last pins loaded successfully, and a " +
         "pin revoked or added since may be missing.",
  cold: "Could not load the pins — the control plane may be unreachable.",
  empty: "No pins, so every call to an ask tool raises a card.",
};
export function toolPinsStatus(rowCount, failed, loaded) {
  return pollStatus(TOOL_PINS_STATUS_TEXT, rowCount, failed, loaded);
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
export function leasesStatus(rowCount, failed, loaded) {
  return pollStatus(LEASES_STATUS_TEXT, rowCount, failed, loaded);
}
