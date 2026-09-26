// SPDX-License-Identifier: Apache-2.0
/* Who acted, as a table cell: the forensic actor string the control plane records
 * (control-plane/provenance.py), cut to what a column can hold.
 */

// Provenance for a TABLE CELL. `_actor` builds a forensic string — peer, the address
// the relay asserts for the browser, the Origin, and a 120-char User-Agent — which is
// right for the audit `reason`, where completeness is the artefact and the column wraps
// as the last one in the row. In the leases strip it is a middle column, so ~180
// characters of Chrome version string pushed the revoke button off the table.
//
// Dropped: `origin=` and `ua=`. They are the two longest fields and the two that say
// least at a glance — both are self-reported by the client and therefore forgeable, so
// they are evidence to read in the trail rather than an answer to "who granted this".
//
// KEPT WITH THEIR LABELS: `peer=` and `via-ui=`. Picking one and showing a bare address
// would be shorter and would misrepresent it — `peer` is the socket address this
// process observed and cannot be forged by the caller, while `via-ui` is the relay's
// ASSERTION about the browser behind it, and that difference is the whole reason
// `_actor` labels its fields. A cell reading `172.18.0.1` presents an assertion as a
// fact; `via-ui=172.18.0.1` does not.
//
// The full string still goes in the cell's `title`, and the audit record is untouched:
// this shortens a rendering, never the stored value.
export function shortActor(actor) {
  const s = (actor || "").trim();
  if (!s) return "—";
  const kept = s.match(/\b(?:peer|via-ui)=\S+/g);
  // An unrecognized shape falls back to the whole string rather than to nothing — a
  // provenance cell that silently emptied itself would be worse than a wide one, and
  // the CSS width cap catches whatever length arrives.
  return kept ? kept.join(" ") : s;
}
