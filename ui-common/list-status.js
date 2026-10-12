// SPDX-License-Identifier: Apache-2.0
/* What a polled list says about itself: empty, stale, or not yet loaded, which render
 * identically and mean different things. One decision (`pollStatus`) and one renderer
 * (`renderListStatus`) for every list on every page, each list supplying only its
 * sentences (the approval page's are control-plane-ui/status.js).
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

// What a list should say ABOUT ITSELF. It exists because an empty table and a failed
// poll rendered identically — and a page's header cannot tell them apart either: the
// approval page's `conn` reports the SSE stream, while its lists are filled by polls
// that can be failing while the stream is healthy.
//
// A failed refresh does NOT clear the rows. Stale rows with a warning above them are
// more useful than an empty table, provided the staleness is stated.
// The three states are the same for EVERY polled list, so the logic lives once and
// each list supplies only its sentences; sharing it is what stops one list growing the
// honesty another lacks (the decisions table got it first, and the policy table then
// sat silently swallowing failures for as long).
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

// The DOM half of every status line, one renderer for all of them: the element
// contract is identical, and two copies drifting apart is precisely what happened once.
export function renderListStatus(el, s) {
  el.hidden = !s.show;
  el.textContent = s.text;
  el.className = "empty" + (s.level === "warn" ? " warn" : "");
}
