// SPDX-License-Identifier: Apache-2.0
/* The timed grants as the page shows them: the button label derived from the
 * configured duration, each lease's own countdown, and sibling hosts folded into one
 * line. A lease's expiry is the instant the backend recorded, never computed here.
 * Below them, the live leases table itself, which `mountLeases` wires when `start()`
 * calls it.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

import { refreshAudit } from "./audit.js";
import { esc } from "./dom.js";
import { COUNTDOWN_URGENT_S } from "./holds.js";
import { shortActor } from "./provenance.js";
import { leasesStatus, renderListStatus } from "./status.js";
import { durationWords } from "./time.js";

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
  const words = durationWords(seconds);
  return words ? `Allow for ${words}` : "Allow for a while";
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
// a hold card — two thresholds would make the same colour mean two things. The units
// are the backend's reasons' (`policy._short_duration`).
export function leaseCountdown(remainingS) {
  if (remainingS === null) return { text: "unknown", urgent: false };
  const whole = Math.max(0, Math.floor(remainingS));
  const mins = Math.floor(whole / 60);
  const pad = n => String(n).padStart(2, "0");
  return {
    text: whole >= 3600 ? `${Math.floor(whole / 3600)}h ${pad(mins % 60)}m`
        : mins >= 1 ? `${mins}m ${pad(whole % 60)}s` : `${whole}s`,
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

// ── the live leases ─────────────────────────────────────────────────────────
// What is being allowed RIGHT NOW on a timer, which until this table existed was
// the one kind of granted egress with nowhere to see it: a lease writes no rule, so
// the standing-policy view is silent about it, and by the time an operator went
// looking in the decisions log the grant might already have lapsed.
//
// It also makes the revoke button worth having. A grant nobody can see is a grant
// nobody closes early, which would leave the configured duration doing the whole job
// — and it is exactly that revocability that let the default be half an hour rather
// than a few minutes.
//
// The DOM half of this surface, wired by `mountLeases`, which `start()` calls. The
// elements are looked up there rather than at import, so the node tests can still
// import this file with no DOM.
let leasesEl, leasesTableEl, leasesEmptyEl, leasesCountEl;
// Whether a load has ever succeeded, and whether the last one failed (`pollStatus`
// in status.js says what each pair means on screen). Kept apart from the rules
// poll's pair: the two views are filled by two fetches, and one of them failing
// while the other succeeds is a state the page has to be able to describe.
let leasesLoaded = false;
let leasesFailed = false;
let leasesById = new Map();
// Which lease groups the operator has opened, by (class, domain) key. Outside the
// render deliberately — see the toggle handler — and pruned to the live groups on
// every refresh so a lapsed domain does not keep an expansion nobody asked for.
const expandedLeaseGroups = new Set();

function renderLeasesStatus(rowCount) {
  renderListStatus(leasesEmptyEl, leasesStatus(rowCount, leasesFailed, leasesLoaded));
}

export async function refreshLeases() {
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
export function updateLeaseCountdowns() {
  const now = Date.now();
  for (const cell of leasesEl.querySelectorAll("td.lease-left")) {
    const cd = leaseCountdown(leaseRemaining(cell.dataset.expires, now));
    cell.textContent = cd.text;
    cell.classList.toggle("urgent", cd.urgent);
  }
}

export function mountLeases() {
  leasesEl = document.getElementById("leases");
  leasesTableEl = document.getElementById("leases-table");
  leasesEmptyEl = document.getElementById("leases-empty");
  leasesCountEl = document.getElementById("leasecount");

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
}
