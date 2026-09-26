// SPDX-License-Identifier: Apache-2.0
/* Timestamps as the page shows them. Formatted here, in the page's own locale, and
 * the reasons are below.
 */

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
export function tsSeconds(ts) {
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
export function fmtTime(ts) {
  const d = tsDate(ts);
  return d ? `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
           : "";
}

// YYYY-MM-DD HH:MM:SS — for rows that persist and are read in order. Rules live for
// weeks and forty audit rows routinely span midnight, where a time-only stamp reads
// as out of order at exactly the moment ordering matters.
export function fmtStamp(ts) {
  const d = tsDate(ts);
  return d ? `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} `
             + fmtTime(ts)
           : "";
}

// The unambiguous instant, for a `title` on audit rows. The displayed stamp is LOCAL
// and carries no offset, which is fine while an operator is reading their own screen
// and stops being fine the moment a row is correlated against `make logs-cp` or pasted
// into a security advisory.
export function fmtInstant(ts) {
  const d = tsDate(ts);
  return d ? d.toISOString() : "";
}
