// SPDX-License-Identifier: Apache-2.0
/* The timed grants as the page shows them: the button label derived from the
 * configured duration, each lease's own countdown, and sibling hosts folded into one
 * line. A lease's expiry is the instant the backend recorded, never computed here.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

import { COUNTDOWN_URGENT_S } from "./holds.js";

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
export function leaseLabel(seconds) {
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
export function leaseRemaining(expiresAt, nowMs) {
  const at = Number(expiresAt);
  if (!Number.isFinite(at)) return null;
  return Math.max(0, at - nowMs / 1000);
}

// A lease's remaining time as a cell: the text, and whether it is about to lapse.
// `urgent` reuses COUNTDOWN_URGENT_S so "nearly out of time" looks the same here as on
// a hold card — two thresholds would make the same colour mean two things.
export function leaseCountdown(remainingS) {
  if (remainingS === null) return { text: "unknown", urgent: false };
  const whole = Math.max(0, Math.floor(remainingS));
  const mins = Math.floor(whole / 60);
  return {
    text: mins >= 1 ? `${mins}m ${String(whole % 60).padStart(2, "0")}s` : `${whole}s`,
    urgent: whole <= COUNTDOWN_URGENT_S,
  };
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
export function leaseDomain(host) {
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
export const LEASE_GROUP_MIN = 2;

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
export function groupLeases(rows) {
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
