// SPDX-License-Identifier: Apache-2.0
// The approval page's "don't hide a failing state" machinery, against values written
// down in advance: the approvals stream dropping, erring and going silent and the page
// reconnecting, the traffic-light favicon and the conn text that follow it, the title
// and tab badges (control-plane-ui/DESIGN.md, "Tabbed views + traffic-light favicon"
// and "Reconnect"), the saturation banner (control-plane/DESIGN.md, "Dismissal is
// server-side, and it is a high-water mark"), and what each list says when its read
// fails before or after one succeeded, stale vs failed (control-plane-ui/DESIGN.md,
// "Empty vs. stale, on both polled lists"). Every expected value is worked out by hand
// from that spec, app.js's comments on lampState, backoffDelay and STREAM_SILENT_MS,
// saturation.js's saturationState and ackCount with renderSaturation and the Dismiss
// handler in app.js, status.js's sentences, and backend.js's fixture; none is copied
// from a run.
//
//   node failing-state.test.js <served dir>
//
// `make ui-test` assembles the served dir as ui-diff does and runs this. Each test opens
// a fresh browser context on a reset backend, so no test's clock or stream reaches
// another.
//
// The clock is paused at NOW from before the page loads (page.js), so the page's
// one-second intervals, the fake stream's heartbeat first and the watchdog after it,
// tick on NOW plus whole seconds, and every time below is counted from the load.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import * as backend from "./backend.js";
import { BROWSER, ORIGIN, heartbeats, launch, openPage } from "./page.js";

const uiDir = process.argv[2];
if (!uiDir) {
  console.error("usage: node failing-state.test.js <served dir>");
  process.exit(2);
}
// `make ui-test` SKIPs before it gets here; run by hand, this says why rather than
// failing every test at launch.
if (!existsSync(BROWSER)) {
  console.error(`no browser at ${BROWSER}; point UI_HARNESS_BROWSER at one`);
  process.exit(2);
}

// Long enough for a page load and twenty seconds of fake time; short enough that a
// wedged page fails the run rather than hanging it.
const TIMEOUT = { timeout: 60000 };

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser?.close(); });

// The lamp lit in a favicon: the colour of each circle at full opacity, by name. The
// colours are the three app.js draws (LAMPS) and index.html carries; anything else lit
// reads as its own markup, so it fails the comparison rather than passing as a name.
const LAMP_COLOURS = { "#d1242f": "red", "#d29922": "amber", "#2ea043": "green" };
const SVG_URI = "data:image/svg+xml,";
function litLamps(href) {
  assert.ok(href.startsWith(SVG_URI), `the favicon is not an inline SVG: ${href}`);
  const svg = decodeURIComponent(href.slice(SVG_URI.length));
  return [...svg.matchAll(/<circle\b[^>]*>/g)].map(m => m[0])
    .filter(circle => / fill-opacity="1"/.test(circle))
    .map(circle => LAMP_COLOURS[circle.match(/ fill="([^"]*)"/)?.[1]] ?? circle);
}

// The approval page loaded on a reset backend with the fake stream's heartbeats, at
// `hash`. `silent` silences the stream from its first instant: in an init script after
// the heartbeats', since the beat the fake sends on opening lands during the load.
// `beforeLoad` sets the backend up for the load's own reads. Every rules read is
// recorded in `rulesReads` with the status it was answered, every saturation
// acknowledgement's body in `acks`, and `answers(request)` the statuses every request,
// "<method> <path>", was answered with. A request in `failingReads` is answered 503 for
// as long as it is listed; a list's write shares its read's path, so the method is part
// of what is listed.
const failingReads = new Set();
const UNAVAILABLE = { status: 503, json: { detail: "upstream unavailable" } };
async function approvalPage(t, { silent = false, hash = "", beforeLoad = () => {} } = {}) {
  backend.reset();
  failingReads.clear();
  beforeLoad();
  const rulesReads = [];
  const acks = [];
  const requests = [];
  const run = await openPage(browser, uiDir, {
    respond: (method, p, body, query) => {
      const request = `${method} ${p}`;
      const answer = failingReads.has(request)
        ? UNAVAILABLE : backend.respond(method, p, body, query);
      requests.push([request, answer?.status]);
      if (method === "GET" && p === "/api/egress/rules") rulesReads.push(answer?.status);
      if (method === "POST" && p === "/api/saturation/ack") acks.push(body);
      return answer;
    },
    answeredLast: backend.answeredLast,
  }, { hash });
  t.after(() => run.close());
  const { page, helpers } = run;
  await page.addInitScript(heartbeats);
  if (silent) await page.addInitScript(() => window.__harnessSilence(true));
  await helpers.load();
  await helpers.settle();

  const lamp = async () => litLamps(await page.locator("#favicon").getAttribute("href"));
  const conn = () => page.locator("#conn").evaluate(el => [el.textContent, el.className]);
  const streams = () => page.evaluate(() => window.__harnessStreams());
  const silence = on => page.evaluate(s => window.__harnessSilence(s), on);
  // Time moves, and then what the page asked for in it is answered.
  const tick = async ms => { await helpers.clock(ms); await helpers.settle(); };
  // Each stream helper runs inside the page and then waits for the page to be quiet,
  // so a read the handler sent (onopen's config read) is answered first.
  const act = async f => { await f(); await helpers.settle(); };
  // A tab clicked, which goes through the hash; the view it shows is checked shown
  // before a test reads what showing it did.
  const show = async view => {
    await page.click(`#tab-${view}`);
    await helpers.settle();
    assert.equal(await page.locator(`#view-${view}`).isHidden(), false);
  };
  const answers = request => requests.filter(([r]) => r === request).map(([, s]) => s);
  return { page, helpers, lamp, conn, streams, silence, tick, act, show, rulesReads,
           acks, answers };
}

const LIVE = ["live", "up"];
const CONNECTING = ["connecting…", "down"];

// ── the favicon before the page's script ────────────────────────────────────

// Amber, not green, until the stream is known to be up (index.html's comment on the
// link): a context of its own with scripts off, so nothing of app.js runs.
test("the favicon the page carries before any script runs is amber", TIMEOUT,
     async t => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  t.after(() => context.close());
  const page = await context.newPage();
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN || url.pathname !== "/") return route.abort();
    return route.fulfill({ contentType: "text/html",
                           body: await readFile(path.join(uiDir, "index.html")) });
  });
  await page.goto(ORIGIN + "/");
  assert.deepEqual(litLamps(await page.locator("#favicon").getAttribute("href")),
                   ["amber"]);
});

// ── the lamp and the conn text ──────────────────────────────────────────────

// Red is the stream not delivering, which an open stream that has said nothing is not
// yet; green is delivering with nothing waiting; amber is delivering with holds waiting.
test("silent after load the lamp is red, then green on the first heartbeat, amber with "
     + "holds pending and green with the queue emptied", TIMEOUT, async t => {
  const { helpers, lamp, conn, silence, tick, act } = await approvalPage(t, { silent: true });
  assert.deepEqual(await lamp(), ["red"]);
  assert.deepEqual(await conn(), CONNECTING);

  await silence(false);
  await tick(1000);
  assert.deepEqual(await lamp(), ["green"]);
  assert.deepEqual(await conn(), LIVE);

  await act(() => helpers.push(backend.HOLDS));
  assert.deepEqual(await lamp(), ["amber"]);
  assert.deepEqual(await conn(), LIVE);

  await act(() => helpers.push([]));
  assert.deepEqual(await lamp(), ["green"]);
});

// ── the stream dropping, erring and going silent ────────────────────────────

// CLOSED, as a 502 from the relay leaves the stream: the browser will not retry it, so
// the page does, after backoffDelay(0) = 1 s. The new stream reads "connecting…" until
// it is heard from, and once it has been, the backoff starts again from 1 s.
test("a dropped stream is red and retried after 1 s; once the new one speaks it is "
     + "green, and the next drop waits 1 s again", TIMEOUT, async t => {
  const { helpers, lamp, conn, streams, silence, tick, act } = await approvalPage(t);
  assert.deepEqual(await lamp(), ["green"]);
  assert.deepEqual(await conn(), LIVE);
  assert.equal(await streams(), 1);

  await act(helpers.drop);
  assert.deepEqual(await lamp(), ["red"]);
  assert.deepEqual(await conn(), ["stream closed — retrying in 1s", "down"]);

  // Silenced, so the new stream's opening beat does not hide its "connecting…".
  await silence(true);
  await tick(900);
  assert.equal(await streams(), 1);
  assert.deepEqual(await conn(), ["stream closed — retrying in 1s", "down"]);
  await tick(100);
  assert.equal(await streams(), 2);
  assert.deepEqual(await lamp(), ["red"]);
  assert.deepEqual(await conn(), CONNECTING);

  await silence(false);
  await tick(1000);
  assert.deepEqual(await lamp(), ["green"]);
  assert.deepEqual(await conn(), LIVE);

  await act(helpers.drop);
  assert.deepEqual(await conn(), ["stream closed — retrying in 1s", "down"]);
});

test("two drops with nothing heard between wait 1 s and then 2 s", TIMEOUT, async t => {
  const { helpers, conn, streams, silence, tick, act } = await approvalPage(t);
  await silence(true);
  await act(helpers.drop);
  assert.deepEqual(await conn(), ["stream closed — retrying in 1s", "down"]);
  await tick(1000);
  assert.equal(await streams(), 2);
  assert.deepEqual(await conn(), CONNECTING);

  await act(helpers.drop);
  assert.deepEqual(await conn(), ["stream closed — retrying in 2s", "down"]);
  await tick(1900);
  assert.equal(await streams(), 2);
  await tick(100);
  assert.equal(await streams(), 3);
});

// A new stream's silence is counted from when it was made, not from the old stream's
// last event: made at 1 s, after backoffDelay(0), and never heard from, it is retired
// by the tick at 16 s, not the one at 15 s, and waits backoffDelay(1) = 2 s.
test("a reconnected stream that says nothing is timed from its own start", TIMEOUT,
     async t => {
  const { helpers, conn, streams, silence, tick, act } = await approvalPage(t);
  await silence(true);
  await act(helpers.drop);
  await tick(1000);
  assert.equal(await streams(), 2);

  await tick(14500);
  assert.deepEqual(await conn(), CONNECTING);
  await tick(1000);
  assert.deepEqual(await conn(), ["stream silent — retrying in 2s", "down"]);
});

// CONNECTING, as a network error leaves it: the browser retries by itself, so the page
// makes no stream of its own and says so; the same stream reopening reads
// "connecting…" until its next heartbeat.
test("a transient error is red and \"reconnecting…\" with no new stream; the same stream "
     + "reopened is green on its heartbeat", TIMEOUT, async t => {
  const { helpers, lamp, conn, streams, tick, act } = await approvalPage(t);
  await act(helpers.fault);
  assert.deepEqual(await lamp(), ["red"]);
  assert.deepEqual(await conn(), ["reconnecting…", "down"]);
  await tick(2000);
  assert.equal(await streams(), 1);
  assert.deepEqual(await lamp(), ["red"]);
  assert.deepEqual(await conn(), ["reconnecting…", "down"]);

  await act(helpers.reopen);
  assert.equal(await streams(), 1);
  assert.deepEqual(await lamp(), ["red"]);
  assert.deepEqual(await conn(), CONNECTING);
  await tick(1000);
  assert.deepEqual(await lamp(), ["green"]);
  assert.deepEqual(await conn(), LIVE);
});

// The last event is the stream's opening beat, at 0 on a watchdog tick, and
// STREAM_SILENT_MS is 15 s: green at 14.5 s, retired by the tick at 15 s, so red and
// waiting out backoffDelay(0) at 15.5 s; the retry at 16 s opens a new stream, whose
// opening beat, heard, turns it green.
test("a silent stream stays green until 15 s past its last event, is red and retried "
     + "after it, and a heartbeat on the new stream turns it green", TIMEOUT, async t => {
  const { lamp, conn, streams, silence, tick } = await approvalPage(t);
  await silence(true);
  await tick(14500);
  assert.deepEqual(await lamp(), ["green"]);
  assert.deepEqual(await conn(), LIVE);

  await tick(1000);
  assert.equal(await streams(), 1);
  assert.deepEqual(await lamp(), ["red"]);
  assert.deepEqual(await conn(), ["stream silent — retrying in 1s", "down"]);

  await silence(false);
  await tick(1000);
  assert.equal(await streams(), 2);
  assert.deepEqual(await lamp(), ["green"]);
  assert.deepEqual(await conn(), LIVE);
});

// Red is "SSE down or silent past the watchdog" (DESIGN), being blind to holds, which a
// failed rules read does not make the page: the lamp stays as the stream says. Today's
// page has no line that could move it here; this holds the rule against one added.
test("a failed rules poll leaves the lamp green while the stream is live", TIMEOUT,
     async t => {
  const { lamp, conn, tick, rulesReads } = await approvalPage(t);
  backend.failRules();
  const before = rulesReads.length;
  await tick(4000);
  assert.deepEqual(rulesReads.slice(before), [503]);
  assert.deepEqual(await lamp(), ["green"]);
  assert.deepEqual(await conn(), LIVE);
});

// ── the title and the tab badges ────────────────────────────────────────────

const TITLE = "dockade control plane";

test("the title and the Approvals badge count the holds pending, hot only above 0",
     TIMEOUT, async t => {
  const { page, helpers, act } = await approvalPage(t);
  const badge = () => page.locator("#badge-approvals").evaluate(
    el => [el.textContent, el.classList.contains("hot")]);
  assert.equal(await page.title(), TITLE);
  assert.deepEqual(await badge(), ["0", false]);

  await act(() => helpers.push(backend.HOLDS));
  assert.equal(await page.title(), `(2) ${TITLE}`);
  assert.deepEqual(await badge(), ["2", true]);

  await act(() => helpers.push([backend.HOLDS[1]]));
  assert.equal(await page.title(), `(1) ${TITLE}`);
  assert.deepEqual(await badge(), ["1", true]);

  await act(() => helpers.push([]));
  assert.equal(await page.title(), TITLE);
  assert.deepEqual(await badge(), ["0", false]);
});

const policyBadge = page => page.locator("#badge-policy").evaluate(
  el => [el.textContent, el.classList.contains("unseen")]);

// A failed read leaves the badge as it was (refreshRules returns before it), so a page
// whose rules have never loaded still shows index.html's "–".
test("the Policy badge reads – until the rules first load, then their count", TIMEOUT,
     async t => {
  const { page, tick, rulesReads } = await approvalPage(t, {
    beforeLoad: backend.failRules,
  });
  assert.deepEqual(rulesReads, [503]);
  assert.deepEqual(await policyBadge(page), ["–", false]);

  backend.reset();
  await tick(4000);
  assert.deepEqual(rulesReads, [503, 200]);
  assert.deepEqual(await policyBadge(page), ["2", false]);
});

// backend.js's two rules, and a third written elsewhere.
const ADDED = [...backend.RULES,
  { id: 3, action: "allow", pattern: "files.pythonhosted.org", scope: "exact host",
    client_class: "agent", source: "operator", created_at: backend.NOW_MS / 1000 }];
// The same two, the block on evil.example flipped to an allow: the count unchanged.
const FLIPPED = backend.RULES.map(r => (r.id === 2 ? { ...r, action: "allow" } : r));
// The same two, the block on evil.example moved to the fixture's other client class:
// count, pattern and action unchanged.
const RECLASSED = backend.RULES.map(r => (r.id === 2 ? { ...r, client_class: "mcp" } : r));

test("a rule added while Approvals shows marks Policy unseen, and opening Policy clears "
     + "the mark", TIMEOUT, async t => {
  const { page, tick, show } = await approvalPage(t);
  assert.deepEqual(await policyBadge(page), ["2", false]);
  backend.setRules(ADDED);
  await tick(4000);
  assert.deepEqual(await policyBadge(page), ["3", true]);

  // Read before the next poll, which would redraw the badge on its own.
  await show("policy");
  assert.deepEqual(await policyBadge(page), ["3", false]);

  // Seen, so leaving Policy and a poll with nothing changed bring no mark back.
  await show("approvals");
  await tick(4000);
  assert.deepEqual(await policyBadge(page), ["3", false]);
});

test("an action flipped at the same count marks Policy unseen", TIMEOUT, async t => {
  const { page, tick } = await approvalPage(t);
  backend.setRules(FLIPPED);
  await tick(4000);
  assert.deepEqual(await policyBadge(page), ["2", true]);
});

test("a rule moved to another client class at the same count marks Policy unseen",
     TIMEOUT, async t => {
  const { page, tick } = await approvalPage(t);
  backend.setRules(RECLASSED);
  await tick(4000);
  assert.deepEqual(await policyBadge(page), ["2", true]);
});

// A change the operator watched happen is not unseen: no mark while Policy shows, and
// none once they leave it and a poll has redrawn the badge.
test("a change while Policy shows sets no mark, after leaving it either", TIMEOUT,
     async t => {
  const { page, tick, show } = await approvalPage(t, { hash: "#policy" });
  assert.deepEqual(await policyBadge(page), ["2", false]);
  backend.setRules(FLIPPED);
  await tick(4000);
  assert.deepEqual(await policyBadge(page), ["2", false]);

  await show("approvals");
  await tick(4000);
  assert.deepEqual(await policyBadge(page), ["2", false]);
});

// ── the saturation banner ───────────────────────────────────────────────────

// The banner as a user sees it: null when it does not show, else its class, its two
// lines and whether Dismiss shows. Times in the detail are the context's Asia/Kolkata,
// NOW's 10:00:00 UTC reading 15:30:00.
async function banner(page) {
  if (!await page.locator("#saturation").isVisible()) return null;
  return {
    level: await page.locator("#saturation").getAttribute("class"),
    text: await page.locator("#sat-text").textContent(),
    detail: await page.locator("#sat-detail").textContent(),
    dismiss: await page.locator("#sat-dismiss").isVisible(),
  };
}
// A push of no holds carrying `saturation`.
const pushSaturation = (page, saturation) => page.evaluate(
  s => window.__harnessPush({ holds: [], saturation: s }), saturation);
// Dismiss clicked, and the banner read in the same turn, before any answer to the POST
// can arrive: true if it still shows.
const dismiss = page => page.evaluate(() => {
  document.getElementById("sat-dismiss").click();
  return document.getElementById("saturation").checkVisibility();
});

const NOW_S = backend.NOW_MS / 1000;
const DENIED = " denied unheard — over the hold cap, so no approval card was raised";
// backend.SATURATION as saturationState and renderSaturation read it: three unread,
// the last 55 s before NOW, counted since an hour before NOW.
const DETAIL = "last: registry.example.org — hit the per-client card cap for 172.28.0.5";
const THREE = { level: "saturation recent", text: `3 requests${DENIED}`,
                detail: `${DETAIL} at 15:29:05 · counted since 14:30:00`, dismiss: true };

test("the saturation banner is hidden before any push", TIMEOUT, async t => {
  const { page } = await approvalPage(t);
  assert.equal(await banner(page), null);
});

test("three unacknowledged rejections 55 s old read as recent, with the host, the cap, "
     + "the time and the count's start, and Dismiss", TIMEOUT, async t => {
  const { page, act } = await approvalPage(t);
  await act(() => pushSaturation(page, backend.SATURATION));
  assert.deepEqual(await banner(page), THREE);
});

test("a shown rejection banner hides on a push that carries no saturation", TIMEOUT,
     async t => {
  const { page, helpers, act } = await approvalPage(t);
  await act(() => pushSaturation(page, backend.SATURATION));
  assert.deepEqual(await banner(page), THREE);
  await act(() => helpers.push([]));
  assert.equal(await banner(page), null);
});

// The record pushed once, at load: 59 s old on the tick at 4 s, and 60 s or more, so
// past, from the tick at 5 s. Nothing but the clock moves it.
test("the rejection banner turns from recent to past by the clock alone, at 60 s",
     TIMEOUT, async t => {
  const { page, act, tick } = await approvalPage(t);
  await act(() => pushSaturation(page, backend.SATURATION));
  await tick(4000);
  assert.deepEqual(await banner(page), THREE);
  await tick(2000);
  assert.deepEqual(await banner(page), { ...THREE, level: "saturation past" });
});

// 12 of 16 requests held is the 75% that warns, and 3 of 8 cards is under it, so the
// requests are the gauge shown; the cards are fewer than the requests, so named.
test("a load near the cap reads requests held on cards with no Dismiss, and hides once "
     + "the holds drain", TIMEOUT, async t => {
  const { page, act } = await approvalPage(t);
  await act(() => pushSaturation(page, backend.LOAD));
  assert.deepEqual(await banner(page), {
    level: "saturation load", text: "12/16 requests held on 3 cards",
    detail: "near the cap — further requests would be denied without raising a card",
    dismiss: false });
  await act(() => pushSaturation(page, { ...backend.LOAD, in_flight: 2, cards: 1 }));
  assert.equal(await banner(page), null);
});

test("Dismiss hides the banner at the click and posts the rejection total once",
     TIMEOUT, async t => {
  const { page, helpers, act, tick, acks } = await approvalPage(t);
  await act(() => pushSaturation(page, backend.SATURATION));
  assert.equal(await dismiss(page), false);
  await helpers.settle();
  assert.deepEqual(acks, [{ count: 3 }]);
  assert.equal(await banner(page), null);
  await tick(1000);
  assert.equal(await banner(page), null);
});

// The acknowledgement is a high-water mark of the total (control-plane/DESIGN.md,
// "Dismissal is server-side, and it is a high-water mark"), so a click on 2 unread of 3
// posts 3.
test("with 1 of 3 acknowledged the banner reads 2, and Dismiss posts 3", TIMEOUT,
     async t => {
  const { page, helpers, act, acks } = await approvalPage(t);
  await act(() => pushSaturation(page, { ...backend.SATURATION, acknowledged: 1 }));
  assert.deepEqual(await banner(page), { ...THREE, text: `2 requests${DENIED}` });
  assert.equal(await dismiss(page), false);
  await helpers.settle();
  assert.deepEqual(acks, [{ count: 3 }]);
  assert.equal(await banner(page), null);
});

test("a backend that records 1 for a click on 3 brings the banner back reading 2",
     TIMEOUT, async t => {
  const { page, helpers, act, acks } = await approvalPage(t);
  backend.overrideAck({ status: 200, json: { ok: true, acknowledged: 1, rejections: 3 } });
  await act(() => pushSaturation(page, backend.SATURATION));
  assert.equal(await dismiss(page), false);
  await helpers.settle();
  assert.deepEqual(acks, [{ count: 3 }]);
  assert.deepEqual(await banner(page), { ...THREE, text: `2 requests${DENIED}` });
});

// The 503 carries an `acknowledged` of 3, so a page that read the body of a failed
// answer would keep the banner hidden: it is the status that must bring it back.
test("a failed acknowledgement brings the banner back as it was", TIMEOUT, async t => {
  const { page, helpers, act, acks } = await approvalPage(t);
  backend.overrideAck({ status: 503, json: { detail: "upstream unavailable",
                                             acknowledged: 3 } });
  await act(() => pushSaturation(page, backend.SATURATION));
  assert.equal(await dismiss(page), false);
  await helpers.settle();
  assert.deepEqual(acks, [{ count: 3 }]);
  assert.deepEqual(await banner(page), THREE);
});

// A fourth rejection, at NOW, pushed before the stream carries the dismissal: the
// click's own acknowledgement of 3 is what leaves 1 unread.
const FOURTH = { ...backend.SATURATION, rejections: 4, last_ts: NOW_S };
const ONE = { level: "saturation recent", text: `1 request${DENIED}`, dismiss: true };

test("a new rejection after a dismissal shows again, reading 1", TIMEOUT, async t => {
  const { page, helpers, act } = await approvalPage(t);
  await act(() => pushSaturation(page, backend.SATURATION));
  assert.equal(await dismiss(page), false);
  await helpers.settle();
  assert.equal(await banner(page), null);
  await act(() => pushSaturation(page, FOURTH));
  assert.deepEqual(await banner(page), {
    ...ONE, detail: `${DETAIL} at 15:30:00 · counted since 14:30:00` });
});

// As after a reload: a page that never clicked, given the backend's acknowledgement of
// 3 and the count's start moved to the dismissal, 30 s before NOW.
test("a fresh page given 4 rejections with 3 acknowledged reads 1", TIMEOUT, async t => {
  const { page, act, acks } = await approvalPage(t);
  await act(() => pushSaturation(page, { ...FOURTH, acknowledged: 3, since: NOW_S - 30 }));
  assert.deepEqual(await banner(page), {
    ...ONE, detail: `${DETAIL} at 15:30:00 · counted since 15:29:30` });
  assert.deepEqual(acks, []);
});

// ── stale vs failed views ───────────────────────────────────────────────────

// What a list says about itself (control-plane-ui/DESIGN.md, "Empty vs. stale, on both
// polled lists"): a read that fails with none before it succeeding reads cold, with no
// rows; one that fails after a success reads stale and keeps the rows it had; the next
// good read clears the warning. The sentences are control-plane-ui/status.js's, each
// list's its own, so a list that calls another's status function fails on the text.
// The rows are backend.js's fixture as each list draws it.
const DECISIONS = {
  name: "decisions", read: "GET /api/audit", view: "audit", status: "audit-empty",
  body: "audit",
  // AUDIT's three rows, one each in the grouped view the page opens on.
  rows: 3,
  stale: "Could not refresh — these are the last events loaded successfully and may "
       + "be out of date.",
  cold: "Could not load recent events — the control plane may be unreachable.",
};
const POLICY = {
  name: "policy", read: "GET /api/egress/rules", view: "policy", status: "rules-empty",
  body: "rules",
  // RULES' two.
  rows: 2,
  stale: "Could not refresh — this is the last policy loaded successfully and may no "
       + "longer be what is in force.",
  cold: "Could not load the standing policy — the control plane may be unreachable.",
};
const LIVE_LEASES = {
  name: "live leases", read: "GET /api/egress/leases", view: "approvals",
  status: "leases-empty", body: "leases",
  // LEASES' three: the two under cdn.example are LEASE_GROUP_MIN, so drawn as the
  // group's summary row and its two members, and files.pythonhosted.org alone.
  rows: 4,
  stale: "Could not refresh — these are the last live leases loaded successfully, and "
       + "one may have lapsed or been revoked since.",
  cold: "Could not load the live leases — the control plane may be unreachable.",
};
const TOOL_RULES = {
  name: "tool rules", read: "GET /api/mcp/rules", view: "tools",
  status: "toolrules-empty", body: "toolrules",
  // TOOL_RULES' two.
  rows: 2,
  stale: "Could not refresh — this is the last tool policy loaded successfully and may "
       + "no longer be what the gateway is enforcing.",
  cold: "Could not load the tool policy — the control plane may be unreachable.",
};
const PINS = {
  name: "pins", read: "GET /api/mcp/pins", view: "tools", status: "toolpins-empty",
  body: "toolpins",
  // PINS' two.
  rows: 2,
  stale: "Could not refresh — these are the last pins loaded successfully, and a pin "
       + "revoked or added since may be missing.",
  cold: "Could not load the pins — the control plane may be unreachable.",
};

// A list as a user sees it, its view open: its status line, null while hidden, else
// its text and whether it carries the warning; and the rows its table holds.
async function listState(page, list) {
  const status = page.locator(`#${list.status}`);
  return {
    status: await status.isVisible()
      ? { text: await status.textContent(),
          warn: await status.evaluate(el => el.classList.contains("warn")) }
      : null,
    rows: await page.locator(`#${list.body} > tr`).count(),
  };
}

// The three lists start() polls every 4 s while the tab shows, whichever view is up.
for (const list of [DECISIONS, POLICY, LIVE_LEASES]) {
  test(`the ${list.name} list whose first read fails reads as not loaded, with no rows, `
       + "until a poll succeeds", TIMEOUT, async t => {
    const { page, tick, show, answers } = await approvalPage(t, {
      beforeLoad: () => failingReads.add(list.read),
    });
    await show(list.view);
    assert.deepEqual(answers(list.read), [503]);
    assert.deepEqual(await listState(page, list),
                     { status: { text: list.cold, warn: true }, rows: 0 });

    failingReads.delete(list.read);
    await tick(4000);
    assert.deepEqual(answers(list.read), [503, 200]);
    assert.deepEqual(await listState(page, list), { status: null, rows: list.rows });
  });

  test(`the ${list.name} list whose poll fails after a success reads stale and keeps `
       + "its rows, until a poll succeeds", TIMEOUT, async t => {
    const { page, tick, show, answers } = await approvalPage(t);
    await show(list.view);
    assert.deepEqual(await listState(page, list), { status: null, rows: list.rows });

    failingReads.add(list.read);
    await tick(4000);
    assert.deepEqual(answers(list.read), [200, 503]);
    assert.deepEqual(await listState(page, list),
                     { status: { text: list.stale, warn: true }, rows: list.rows });

    failingReads.delete(list.read);
    await tick(4000);
    assert.deepEqual(answers(list.read), [200, 503, 200]);
    assert.deepEqual(await listState(page, list), { status: null, rows: list.rows });
  });
}

// The tool rules are read on load and after the page's own writes, of which a revoke
// asks no confirm (mcp.js's click handler); the fake answers it ok and changes nothing.
const revokeToolRule = page => page.locator("#toolrules button.revoke").first().click();

test("the tool rules whose first read fails read as not loaded, with no rows", TIMEOUT,
     async t => {
  const { page, show, answers } = await approvalPage(t, {
    beforeLoad: () => failingReads.add(TOOL_RULES.read),
  });
  await show("tools");
  assert.deepEqual(answers(TOOL_RULES.read), [503]);
  assert.deepEqual(await listState(page, TOOL_RULES),
                   { status: { text: TOOL_RULES.cold, warn: true }, rows: 0 });
});

test("the tool rules whose re-read after a revoke fails read stale and keep their rows, "
     + "until the next re-read succeeds", TIMEOUT, async t => {
  const { page, show, act, answers } = await approvalPage(t);
  await show("tools");
  assert.deepEqual(await listState(page, TOOL_RULES),
                   { status: null, rows: TOOL_RULES.rows });

  failingReads.add(TOOL_RULES.read);
  await act(() => revokeToolRule(page));
  assert.deepEqual(answers(TOOL_RULES.read), [200, 503]);
  assert.deepEqual(await listState(page, TOOL_RULES),
                   { status: { text: TOOL_RULES.stale, warn: true },
                     rows: TOOL_RULES.rows });

  failingReads.delete(TOOL_RULES.read);
  await act(() => revokeToolRule(page));
  assert.deepEqual(answers(TOOL_RULES.read), [200, 503, 200]);
  assert.deepEqual(await listState(page, TOOL_RULES),
                   { status: null, rows: TOOL_RULES.rows });
});

// The pins are read on load and again on every opening of Tools (showView).
test("the pins whose reads fail from the load on read as not loaded, with no rows, "
     + "until Tools is opened again with the read restored", TIMEOUT, async t => {
  const { page, show, answers } = await approvalPage(t, {
    beforeLoad: () => failingReads.add(PINS.read),
  });
  await show("tools");
  assert.deepEqual(answers(PINS.read), [503, 503]);
  assert.deepEqual(await listState(page, PINS),
                   { status: { text: PINS.cold, warn: true }, rows: 0 });

  failingReads.delete(PINS.read);
  await show("approvals");
  await show("tools");
  assert.deepEqual(answers(PINS.read), [503, 503, 200]);
  assert.deepEqual(await listState(page, PINS), { status: null, rows: PINS.rows });
});

test("the pins whose read on opening Tools fails read stale and keep their rows, until "
     + "Tools is opened again with the read restored", TIMEOUT, async t => {
  const { page, show, answers } = await approvalPage(t);
  assert.deepEqual(answers(PINS.read), [200]);

  failingReads.add(PINS.read);
  await show("tools");
  assert.deepEqual(answers(PINS.read), [200, 503]);
  assert.deepEqual(await listState(page, PINS),
                   { status: { text: PINS.stale, warn: true }, rows: PINS.rows });

  failingReads.delete(PINS.read);
  await show("approvals");
  await show("tools");
  assert.deepEqual(answers(PINS.read), [200, 503, 200]);
  assert.deepEqual(await listState(page, PINS), { status: null, rows: PINS.rows });
});
