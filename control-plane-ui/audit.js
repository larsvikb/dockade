// SPDX-License-Identifier: Apache-2.0
/* The record as the operator browses it: what each audit row says in five columns,
 * how consecutive rows fold and what the fold hides, the filters and the window that
 * narrow the record and the query they become, the pager over the raw events, and
 * the two summaries drawn from a page — how much of the record it shows, and whether
 * any of it is the proxy failing closed. Below them, the decisions view itself, which
 * `mountAudit` wires when `start()` calls it.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

import { esc } from "./dom.js";
import { shortActor } from "./provenance.js";
import { auditStatus, AUDIT_REFUSED_FALLBACK, renderListStatus } from "./status.js";
import { fmtInstant, fmtStamp, tsSeconds } from "./time.js";

// Nearly every governed request is a CONNECT tunnel, so `stage` is "connect" on almost
// every row and a column of it would be forty repetitions of one word. It is shown only
// when it is something else — which is exactly when it is worth seeing, because a
// plaintext HTTP decision reaching the proxy at all is unusual.
export const AUDIT_ORDINARY_STAGE = "connect";

// The port a CONNECT tunnel goes to unless something is unusual. Named so the record
// view can stay quiet about it: `:443` beside a host is the scheme restated, while
// `:8443` is a thing worth noticing. The proxy's own port gate is what ENFORCES which
// ports are reachable; this only decides what is worth printing.
const ORDINARY_PORT = 443;

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

// An outcome row's own shape. `kind` says only that this is a result; the STATUS
// is the information — `ok` against `tool-error` is the difference between "a human
// approved a PR being opened" and "a PR was opened". Same bound and charset as the
// stage shape above, widened because `transport-error` is fifteen characters.
const AUDIT_OUTCOME = "outcome";
const AUDIT_STATUS_SHAPE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$/;

// WHAT THE ROW IS ABOUT, for the `target` column.
//
// Every kind of row has one, and before this several did not use it: an egress row
// names a host, a policy row names the pattern it wrote, and every tool-shaped row —
// the call, the ask, the resumption, the outcome, the rule that governs it — names the
// tool. Those last ones left the cell EMPTY while still rendering their stage prefix,
// so the column read `tool-call · ` with nothing after the separator. The fix is not to
// drop the separator but to fill the cell: the columns to fill it from have existed
// since the audit trail became joinable.
//
// `host` wins where both are present, because a row that has one is an egress decision
// and the host IS its identity. Nothing sets both today.
function auditTarget(r) {
  return (r && r.host) || toolSubject(r) || "";
}

// The qualifier in front of the target — `http · example.com`, `tool-call ·
// mcp-github__get_me`. The prefix says HOW, the cell says WHAT.
//
// An OUTCOME row qualifies itself by STATUS instead of by stage: `tool-result` is
// implied by the `outcome` already in the tag, while `ok` against `tool-error` is the
// whole point of the row.
//
// Suppressed entirely when there is no target, because a separator with nothing on its
// right is not a separator — the same rule the grouped view's `first seen` follows.
// Belt-and-braces now that `auditTarget` fills the cell for every kind: a row carrying
// neither a host nor a tool would otherwise reintroduce the dangling prefix.
function auditPrefix(r, stage) {
  if (!auditTarget(r)) return "";
  if (r && r.kind === AUDIT_OUTCOME) {
    return r.status && AUDIT_STATUS_SHAPE.test(r.status) ? `${r.status} · ` : "";
  }
  return stage && stage !== AUDIT_ORDINARY_STAGE && AUDIT_STAGE_SHAPE.test(stage)
         ? `${stage} · ` : "";
}

// What an outcome row is ABOUT, in the column where an egress row names its host. The
// flattened `server__tool` is deliberate: it is the name the agent was served and the
// name a transcript will contain, so an operator searching for what they saw finds it.
// Falls back rather than inventing — a row missing either half says so.
function toolSubject(r) {
  const server = (r && r.server) || "";
  const tool = (r && r.tool) || "";
  if (server && tool) return `${server}__${tool}`;
  return server || tool || "";
}

// One audit row, shaped for display. Pure, and deliberately does NOT format the
// timestamp: that is locale-dependent, and a unit test should not have to pin down a
// locale to assert the parts that carry meaning.
export function auditRow(r) {
  const stage = (r && r.stage) || "";
  const client = (r && r.client) || "";
  return {
    ts: tsSeconds(r && r.ts),
    kind: (r && r.kind) || "?",
    // Prefixes the TARGET, not the kind — `http · example.com`, which reads as the
    // scheme it effectively is. The stage does not qualify the decision at all (a deny
    // at the http stage is the same deny as at connect); it describes how the request
    // was MADE, and the host cell is where the request is identified. It also keeps
    // the kind column uniform, which matters because that is the column an
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
    // An OUTCOME row qualifies itself by status rather than by stage, and the swap
    // follows the same rule: the prefix says how, the cell says what. `tool-result`
    // is what the stage would contribute and it is pure redundancy — it is implied by
    // the kind word already in the tag — while the status is the whole point of
    // the row. Left unprefixed, these rows also rendered a dangling `tool-result · `
    // against an empty host cell, since a tool call has no host.
    stagePrefix: auditPrefix(r, stage),
    target: auditTarget(r),
    // An em dash rather than an empty cell: blank reads as "this column is broken",
    // whereas the honest statement is that no client was recorded for this row. Not
    // on a row with an actor, whose line fills the cell.
    client: client || (r && r.actor ? "" : "—"),
    // The client CLASS, prefixed to the address the same way `stagePrefix` qualifies
    // the host — because it is what the decision was actually taken against, while
    // the address is only how the class was worked out. An operator scanning this
    // column wants "was this the agent or an MCP server", not a fourth octet.
    //
    // ABSENT for a row written before client classes existed, and for one the
    // backend could not place. Rendering nothing in that case is deliberate: an
    // invented label would be a claim, and these rows are evidence.
    clientClassPrefix: r && r.client_class ? `${r.client_class} · ` : "",
    // WHO ACTED, when that was not the client (`audit.actor`): the operator behind a
    // click, or the gateway behind an inventory push. Its own line under the client,
    // labelled, so "which sandbox asked" and "who did it" never share a value; the
    // title gets the whole string, as in the leases strip. The line break is CSS, so
    // the space before `by` is in the text: a copied cell reads "… 172.30.0.2 by …".
    actor: r && r.actor ? `${client ? " " : ""}by ${shortActor(r.actor)}` : "",
    actorTitle: (r && r.actor) || "",
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
export function repeatCount(r) {
  const n = Number(r && r.n);
  return Number.isFinite(n) && n > 1 ? Math.floor(n) : 1;
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
// `filtered` is the backend's own answer (`/api/audit`'s `filtered` field), not what
// the page asked for: an older backend ignores the parameters, and then the rows are
// unfiltered whatever the controls say. It changes the NOUN, and that is the whole of
// its job — "of 4,301 recorded decisions" under an active filter reads as the size of
// the store, which would make a narrowed view look like a shrunken record.
export function coverageSummary(rows, total, filtered) {
  const shown = (rows || []).reduce((n, r) => n + repeatCount(r), 0);
  const t = Number(total);
  // An absent or nonsensical total says nothing rather than guessing. A backend
  // without the field must not make this claim "0 of 0".
  if (!Number.isFinite(t) || t <= shown) {
    // With a filter on, "everything that matches is on screen" is worth stating
    // rather than leaving to inference: the reader is looking at a short list they
    // just narrowed, and silence there is indistinguishable from truncation. Without
    // a filter it stays silent, because a permanent "showing 12 of 12" is furniture.
    if (filtered && shown > 0) {
      return { show: true, level: "none", shown, total: Number.isFinite(t) ? t : null,
               text: `All ${shown.toLocaleString()} matching events are shown.` };
    }
    return { show: false, level: "none", text: "", shown, total: Number.isFinite(t) ? t : null };
  }
  return {
    show: true,
    level: "none",
    shown,
    total: t,
    text: `Showing the ${shown.toLocaleString()} most recent of `
        + `${t.toLocaleString()} ${filtered ? "matching" : "recorded"} events.`,
  };
}

// ── browsing the record: filters, and the view that pages ────────────────────
//
// The backend serves two views of the audit table and this is the page's half of that
// split (see `audit.py`): the GLANCE is folded, bounded and unpaged; the RECORD is one
// row per decision and pages backwards without bound. The filters are shared, so
// narrowing the glance and then walking the history of what it turned up does not mean
// re-typing anything — the controls do not move, only the table under them does.

// Relative windows in seconds, not a pair of date pickers. The question an operator
// has is "since when", and any absolute range they might type is one they would first
// have to work out from a clock. The keys are the <option> values in index.html.
export const AUDIT_WINDOWS = { "1h": 3600, "24h": 86400, "7d": 604800 };

// The `since` bound a preset means, in epoch SECONDS, or null for "any time".
//
// Takes the clock as an argument rather than reading it, so the arithmetic is
// assertable — the same reason the countdown helpers do. Whole seconds because the
// bound is a filter, not a measurement: a float here would make two identical clicks
// produce two different cache-missing query strings for no gain.
export function timeWindow(preset, nowMs) {
  const span = AUDIT_WINDOWS[preset];
  const now = Number(nowMs);
  if (!span || !Number.isFinite(now)) return null;
  return Math.floor(now / 1000) - span;
}

// Is anything narrowing the view? Used only to decide whether to ASK for a filtered
// query; whether one was actually applied is the backend's to report (see
// coverageSummary).
export function filterActive(f) {
  return !!(f && (((f.q || "").trim()) || f.kind || AUDIT_WINDOWS[f.preset]));
}

// The query string both views are fetched with. Pure, so the one place that decides
// what the backend is asked is testable — and so `before` cannot silently go missing,
// which would serve page 1 while the pager said page 12.
//
// Every value is percent-encoded: the search box takes free text, and `&` or `#`
// pasted out of a URL would otherwise split the query string into parameters the
// backend would then refuse (or worse, misread).
export function auditQuery(f, opts) {
  const o = opts || {};
  const parts = [];
  const limit = Number(o.limit);
  if (Number.isFinite(limit) && limit > 0) parts.push(`limit=${Math.floor(limit)}`);
  const q = ((f && f.q) || "").trim();
  if (q) parts.push(`q=${encodeURIComponent(q)}`);
  if (f && f.kind) parts.push(`kind=${encodeURIComponent(f.kind)}`);
  const since = timeWindow(f && f.preset, o.nowMs);
  if (since !== null) parts.push(`since=${since}`);
  if (o.before) parts.push(`before=${encodeURIComponent(o.before)}`);
  return parts.join("&");
}

// One raw event, shaped for the record view.
//
// Built ON TOP of auditRow rather than beside it, so the five columns the two views
// share cannot render differently in one of them — the stage prefix, the client class
// prefix, the em-dash for a missing client and the fail-closed marker are one
// implementation, and this adds only what the glance drops.
export function eventRow(r) {
  const method = ((r && r.method) || "").trim();
  const url = ((r && r.url) || "").trim();
  const port = Number(r && r.port);
  const proto = ((r && r.proto) || "").trim();
  return {
    ...auditRow(r),
    id: r && r.id !== undefined && r.id !== null ? String(r.id) : "",
    // WHAT was asked for, and EMPTY WHENEVER THAT IS THE ORDINARY THING.
    //
    // This cell used to render `:443 connect` for almost every row, because almost
    // every governed request is a CONNECT tunnel (see AUDIT_ORDINARY_STAGE) — a port
    // implied by the scheme, beside a host already in `target`. A column that repeats
    // itself down the page is noise, and noise in the view that exists for reading
    // carefully is worse than noise in the glance.
    //
    // So it follows the rule the stage prefix already follows: suppress the value that
    // is true of nearly every row, and a non-empty cell then MEANS something. What
    // survives is the minority worth stopping on —
    //
    //   a plaintext URL   which only exists because the proxy does not decrypt TLS, so
    //                     its presence is itself the finding: an unencrypted request.
    //                     It is the field that says WHICH request, and the only
    //                     agent-controlled unbounded string on the page — capped on
    //                     write (store.DRAIN_MAX_FIELD), escaped by the renderer, with
    //                     the CSP making an escaping mistake inert rather than fatal.
    //   an approval id    the outcome row's answer to "which one was it": the key
    //                     tying it to the hold, the human's answer and the claim.
    //   a non-standard port   a tunnel to :8443 is worth seeing where :443 is not.
    //
    // An outcome with no approval renders `—` rather than blank, because there the
    // absence is the information: policy allowed that call outright.
    request: r && r.kind === AUDIT_OUTCOME
             ? (r.approval_id || "—")
             : [method, url].filter(Boolean).join(" ")
               || (Number.isFinite(port) && port > 0 && port !== ORDINARY_PORT
                   ? `:${port}` : ""),
  };
}

// What the record's pager says and which of its buttons work.
//
// `page` is a zero-based index and `pageSize` the rows asked for, which together give
// the row NUMBERS — exact, because every page but the last is full by construction
// (the backend serves `limit` rows and reports a cursor only when more remain). Row
// numbers rather than a page count: "decisions 101–200 of 4,301" tells the reader
// where they are in the record, whereas "page 2" needs the page size to mean anything.
//
// `older` follows the cursor rather than the total. The total is the size of the
// matching set and the page is a keyset window into it, so arithmetic on the two would
// disagree with the record the moment a decision is written mid-read — which, on a
// table that takes an insert per governed request, is the ordinary case.
export function historyPager(page, pageSize, shown, total, hasNext, filtered) {
  const p = Math.max(0, Math.floor(Number(page)) || 0);
  const size = Math.max(1, Math.floor(Number(pageSize)) || 1);
  const n = Math.max(0, Math.floor(Number(shown)) || 0);
  const t = Number(total);
  const from = n ? p * size + 1 : 0;
  const to = n ? p * size + n : 0;
  const of = Number.isFinite(t)
    ? ` of ${t.toLocaleString()}${filtered ? " matching" : ""}`
    : "";
  return {
    older: !!hasNext,
    newer: p > 0,
    from,
    to,
    // Empty when there is nothing on screen: the list's own status line already says
    // whether that is an empty record or an unmatched filter, and a second sentence
    // saying "0–0" next to it would be noise arguing with prose.
    text: n ? `Events ${from.toLocaleString()}–${to.toLocaleString()}${of}` : "",
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
export function outageSummary(rows) {
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

// ── the decisions view ───────────────────────────────────────────────────────
// The DOM half of this surface, wired by `mountAudit`, which `start()` calls. The
// elements are looked up there rather than at import, so the node tests can still
// import this file with no DOM.
let auditEmpty, auditOutage, auditCoverage, auditQEl, auditKindEl, auditWindowEl,
    auditEveryEl, auditClearEl, auditModeNote, auditGroupedTable, auditEventsTable,
    auditPagerEl, auditOlderEl, auditNewerEl, auditPageEl;
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

// Same element contract as the two status lines, so it goes through the same
// renderer — a third hand-rolled show/hide is how the first two drifted apart.
function renderOutage(s) {
  renderListStatus(auditOutage, s);
}

function renderCoverage(s) {
  renderListStatus(auditCoverage, s);
}

function renderAuditStatus(rowCount, filtered) {
  renderListStatus(auditEmpty,
                   auditStatus(rowCount, auditFailed, auditLoaded, filtered,
                               auditRefused));
}

// ── the decisions view's controls ────────────────────────────────────────────
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

export async function refreshAudit() {
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

export function mountAudit() {
  auditEmpty = document.getElementById("audit-empty");
  auditOutage = document.getElementById("audit-outage");
  auditCoverage = document.getElementById("audit-coverage");
  auditQEl = document.getElementById("audit-q");
  auditKindEl = document.getElementById("audit-kind");
  auditWindowEl = document.getElementById("audit-window");
  auditEveryEl = document.getElementById("audit-every");
  auditClearEl = document.getElementById("audit-clear");
  auditModeNote = document.getElementById("audit-mode");
  auditGroupedTable = document.getElementById("audit-grouped-table");
  auditEventsTable = document.getElementById("audit-events-table");
  auditPagerEl = document.getElementById("audit-pager");
  auditOlderEl = document.getElementById("audit-older");
  auditNewerEl = document.getElementById("audit-newer");
  auditPageEl = document.getElementById("audit-page");

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
}
