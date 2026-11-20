// SPDX-License-Identifier: Apache-2.0
// The approval page's "don't hide a failing state" machinery, against values written
// down in advance: the approvals stream dropping, erring and going silent and the page
// reconnecting, the traffic-light favicon and the conn text that follow it, and the
// title and tab badges (control-plane-ui/DESIGN.md, "Tabbed views + traffic-light
// favicon" and "Reconnect"). Every expected value is worked out by hand from that spec,
// app.js's comments on lampState, backoffDelay and STREAM_SILENT_MS, and backend.js's
// fixture; none is copied from a run.
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
// recorded in `rulesReads` with the status it was answered.
async function approvalPage(t, { silent = false, hash = "", beforeLoad = () => {} } = {}) {
  backend.reset();
  beforeLoad();
  const rulesReads = [];
  const run = await openPage(browser, uiDir, {
    respond: (method, p, body, query) => {
      const answer = backend.respond(method, p, body, query);
      if (method === "GET" && p === "/api/egress/rules") rulesReads.push(answer?.status);
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
  return { page, helpers, lamp, conn, streams, silence, tick, act, show, rulesReads };
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
