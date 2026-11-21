// SPDX-License-Identifier: Apache-2.0
// The approval page's cards, against values written down in advance: what a confirm
// panel says it will write and what the click then sends, the countdowns, and what a
// card says once its resolve is answered. ui-diff's oracle is the previous version of
// the page; this one's is the fixture in backend.js and the spec in the page's comments,
// so every expected value below is worked out from those by hand and none is copied
// from a run.
//
//   node approval.test.js <served dir>
//
// `make ui-test` assembles the served dir as ui-diff does and runs this. Each test opens
// a fresh browser context on a reset backend, so no test's clicks or clock reach another.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import * as backend from "./backend.js";
import { existsSync } from "node:fs";
import { BROWSER, heartbeats, launch, openPage } from "./page.js";

const uiDir = process.argv[2];
if (!uiDir) {
  console.error("usage: node approval.test.js <served dir>");
  process.exit(2);
}
// `make ui-test` SKIPs before it gets here; run by hand, this says why rather than
// failing every test at launch.
if (!existsSync(BROWSER)) {
  console.error(`no browser at ${BROWSER}; point UI_HARNESS_BROWSER at one`);
  process.exit(2);
}

// Long enough for a page load and two and a half minutes of fake time (an allowed tool
// card's wait and its dwell, with a heartbeat and a tick each second); short enough
// that a wedged page fails the run rather than hanging it.
const TIMEOUT = { timeout: 60000 };

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser?.close(); });

// The page loaded with backend.js's two holds on it (the egress hold on
// registry.example.org and the tool ask on mcp-github's create_pull_request), and every
// resolve it sends recorded in `sent`, in order. The config serves the outcome wait,
// as the control plane does, unless `wait` is false. With `heartbeat`, the stream
// speaks every second (page.js's `heartbeats`), so it reads live past the watchdog's
// 15 s for as long as `__harnessSilence` leaves it on; without, it goes quiet after the
// last push. `answer` stands in for backend.js's answer to a request, when it returns
// one.
async function approvals(t, { wait = true, heartbeat = false, answer = () => null } = {}) {
  backend.reset();
  if (wait) backend.serveOutcomeWait();
  const sent = [];
  const run = await openPage(browser, uiDir, {
    respond: (method, path, body, query) => {
      if (method === "POST" && path.startsWith("/approvals/")) sent.push({ path, body });
      return answer(method, path) || backend.respond(method, path, body, query);
    },
    answeredLast: backend.answeredLast,
  });
  t.after(() => run.close());
  const { page, helpers } = run;
  if (heartbeat) await page.addInitScript(heartbeats);
  await helpers.load();
  await helpers.settle();
  await helpers.push(backend.HOLDS);
  await helpers.settle();
  const egress = page.locator('#pending .card[data-id="egress-1"]');
  const tool = page.locator('#pending .card[data-id="tool-1"]');
  // Clicks go through here so the page has handled each, and answered what it sent,
  // before the test reads anything.
  const click = async locator => { await locator.click(); await helpers.settle(); };
  // A push that also says how the tool asks allowed lately stand.
  const grant = async (holds, granted) => {
    await page.evaluate(([h, g]) => window.__harnessPush({ holds: h, granted: g }),
                        [holds, granted]);
    await helpers.settle();
  };
  return { run, page, helpers, sent, egress, tool, click, grant };
}

const text = async locator => (await locator.textContent()).trim();

// ── the persist panel: its preview against the select's value ───────────────

// For each persist action and each pattern the hold offers: what the panel says it
// will write, its warning, its confirm button, and the pattern the confirm sends.
const PERSISTS = [
  ["allow_persist", "allow", "registry.example.org", "exact host", ""],
  ["allow_persist", "allow", ".example.org", "example.org and subdomains",
   "⚠ Wildcard: this covers example.org and every subdomain of it, including hosts "
   + "nothing has requested yet."],
  ["deny_persist", "block", "registry.example.org", "exact host", ""],
  ["deny_persist", "block", ".example.org", "example.org and subdomains",
   "⚠ Wildcard: this covers example.org and every subdomain of it, including hosts "
   + "nothing has requested yet."],
];

for (const [action, verb, pattern, scope, warning] of PERSISTS) {
  test(`${action} with ${pattern}: the panel names it and the confirm sends it`, TIMEOUT,
       async t => {
    const { egress, sent, click } = await approvals(t);
    await click(egress.locator(`button[data-action="${action}"]`));
    const panel = egress.locator(".confirm");
    // The other option first, so a preview that stayed on the first one drawn is caught
    // whichever option this case is for.
    const other = PERSISTS.find(p => p[0] === action && p[2] !== pattern)[2];
    await panel.locator("select").selectOption(other);
    await panel.locator("select").selectOption(pattern);
    assert.equal(await text(panel.locator(".cwhat")),
                 `Writes a standing rule to ${verb} ${pattern} — ${scope}.`);
    const warn = panel.locator(".cwarn");
    assert.equal(await warn.isVisible(), Boolean(warning));
    if (warning) assert.equal(await text(warn), warning);
    const go = panel.locator("button.confirmgo");
    assert.equal(await text(go), `Confirm — ${verb} ${pattern} from now on`);
    assert.equal(await go.getAttribute("class"),
                 `confirmgo ${verb === "allow" ? "allow" : "deny"}`);
    assert.deepEqual(sent, []);
    await click(go);
    assert.deepEqual(sent, [{ path: "/approvals/egress-1/resolve",
                              body: { action, pattern } }]);
  });
}

test("the persist panel opens on the narrowest pattern", TIMEOUT, async t => {
  const { egress, click } = await approvals(t);
  await click(egress.locator('button[data-action="allow_persist"]'));
  const panel = egress.locator(".confirm");
  assert.equal(await panel.locator("select").inputValue(), "registry.example.org");
  assert.equal(await text(panel.locator(".cwhat")),
               "Writes a standing rule to allow registry.example.org — exact host.");
});

// ── the confirm panel: cancel, confirm, the pins sent ───────────────────────

test("cancel keeps the pattern picked and sends nothing; confirm then sends it once",
     TIMEOUT, async t => {
  const { egress, sent, click, helpers } = await approvals(t);
  const persist = egress.locator('button[data-action="allow_persist"]');
  const panel = egress.locator(".confirm");
  await click(persist);
  // While the panel is open the action row is off, so a stray click cannot send another
  // action past it.
  assert.deepEqual(await egress.locator(".actions button").evaluateAll(
    bs => bs.filter(b => !b.disabled).map(b => b.textContent)), []);
  await panel.locator("select").selectOption(".example.org");
  await click(panel.getByRole("button", { name: "Cancel" }));
  assert.equal(await panel.isVisible(), false);
  assert.equal(await persist.isEnabled(), true);
  assert.deepEqual(sent, []);

  await click(persist);
  assert.equal(await panel.locator("select").inputValue(), ".example.org");
  assert.equal(await text(panel.locator(".cwhat")),
               "Writes a standing rule to allow .example.org — example.org and subdomains.");
  // A double click on Confirm: the first sends, and the card takes no second, whether
  // because the panel is gone or because the card is already resolving.
  await panel.locator("button.confirmgo").dblclick();
  await helpers.settle();
  assert.deepEqual(sent, [{ path: "/approvals/egress-1/resolve",
                            body: { action: "allow_persist", pattern: ".example.org" } }]);
});

test("Escape backs out of the persist panel and sends nothing", TIMEOUT, async t => {
  const { egress, sent, click, helpers } = await approvals(t);
  await click(egress.locator('button[data-action="deny_persist"]'));
  const panel = egress.locator(".confirm");
  await panel.locator("select").press("Escape");
  await helpers.settle();
  assert.equal(await panel.isVisible(), false);
  assert.equal(await egress.locator('button[data-action="deny_once"]').isEnabled(), true);
  assert.deepEqual(sent, []);
});

test("the pin panel starts with nothing ticked and nothing to confirm", TIMEOUT,
     async t => {
  const { tool, click } = await approvals(t);
  await click(tool.getByRole("button", { name: "Allow + pin…" }));
  const panel = tool.locator(".confirm");
  assert.deepEqual(await panel.locator(".cpins input").evaluateAll(
    boxes => boxes.map(b => [b.value, b.checked])), [["owner", false], ["repo", false]]);
  assert.equal(await text(panel.locator(".cwhat")),
               "Tick the fields a later call must match exactly.");
  const [timed, forGood] = await panel.locator("button.confirmgo").all();
  assert.equal(await text(timed), "Pin for 1 h");
  assert.equal(await text(forGood), "Pin for good");
  assert.equal(await timed.isEnabled(), false);
  assert.equal(await forGood.isEnabled(), false);
});

test("the pin panel's preview follows the boxes, and the ticked ones are the pins sent",
     TIMEOUT, async t => {
  const { tool, sent, click } = await approvals(t);
  await click(tool.getByRole("button", { name: "Allow + pin…" }));
  const panel = tool.locator(".confirm");
  const [owner, repo] = await panel.locator(".cpins input").all();
  const [timed, forGood] = await panel.locator("button.confirmgo").all();

  await owner.check();
  assert.equal(await text(panel.locator(".cwhat")),
               "Allows this call, and every later create_pull_request call on mcp-github "
               + "with owner = acme runs without a card while the pin lasts. Every other "
               + "argument is free: repo, title, and any this call did not set. "
               + "Pinned for good it answers every sandbox; for 1 h it answers only "
               + "sandbox a1b2c3d4, the one this call came from.");
  assert.equal(await text(forGood), "Confirm — allow and pin owner for good");
  assert.equal(await text(timed), "Confirm — allow and pin owner for 1 h");

  await repo.check();
  assert.equal(await text(panel.locator(".cwhat")),
               "Allows this call, and every later create_pull_request call on mcp-github "
               + "with owner = acme and repo = gadgets runs without a card while the pin "
               + "lasts. Every other argument is free: title, and any this call did not "
               + "set. "
               + "Pinned for good it answers every sandbox; for 1 h it answers only "
               + "sandbox a1b2c3d4, the one this call came from.");
  assert.equal(await text(forGood), "Confirm — allow and pin owner, repo for good");

  // Ending on a box unticked, so what is sent is the boxes ticked rather than every
  // field offered.
  await owner.uncheck();
  assert.equal(await text(panel.locator(".cwhat")),
               "Allows this call, and every later create_pull_request call on mcp-github "
               + "with repo = gadgets runs without a card while the pin lasts. Every other "
               + "argument is free: owner, title, and any this call did not set. "
               + "Pinned for good it answers every sandbox; for 1 h it answers only "
               + "sandbox a1b2c3d4, the one this call came from.");
  assert.equal(await text(forGood), "Confirm — allow and pin repo for good");
  assert.deepEqual(sent, []);

  await click(forGood);
  assert.deepEqual(sent, [{ path: "/approvals/tool-1/resolve",
                            body: { action: "allow_pinned", pins: ["repo"] } }]);
});

test("the timed pin sends its own action with the one box ticked", TIMEOUT, async t => {
  const { tool, sent, click } = await approvals(t);
  await click(tool.getByRole("button", { name: "Allow + pin…" }));
  const panel = tool.locator(".confirm");
  await panel.locator(".cpins input").nth(1).check();
  await click(panel.locator("button.confirmgo").first());
  assert.deepEqual(sent, [{ path: "/approvals/tool-1/resolve",
                            body: { action: "allow_pinned_lease", pins: ["repo"] } }]);
});

test("cancel on the pin panel sends nothing and gives the buttons back", TIMEOUT,
     async t => {
  const { tool, sent, click } = await approvals(t);
  await click(tool.getByRole("button", { name: "Allow + pin…" }));
  const panel = tool.locator(".confirm");
  await panel.locator(".cpins input").first().check();
  assert.deepEqual(await tool.locator(".actions button").evaluateAll(
    bs => bs.filter(b => !b.disabled).map(b => b.textContent)), []);
  await click(panel.getByRole("button", { name: "Cancel" }));
  assert.equal(await panel.isVisible(), false);
  assert.equal(await tool.getByRole("button", { name: "Deny", exact: true }).isEnabled(),
               true);
  assert.deepEqual(sent, []);
});

// ── the countdowns ──────────────────────────────────────────────────────────

// The page loads with its clock paused at NOW, so its one-second tick falls on NOW plus
// whole seconds, and answers are held while time moves, so every tick reads a settled
// page. The egress hold was requested at NOW - 10 against /api/config's 120 s window;
// the tool ask's deadline is NOW + 115, raised at NOW - 5, a 120 s window of its own.
// The bar is the fraction left of the window, to one decimal, which the style reads
// back without a trailing ".0".
test("each card counts down its own window from the fake clock", TIMEOUT, async t => {
  const { egress, tool, helpers } = await approvals(t);
  const read = async card => [await card.locator(".countdown").isVisible(),
                              await text(card.locator(".cdtext")),
                              await card.locator(".bar i").evaluate(i => i.style.width),
                              await card.locator(".countdown").evaluate(
                                cd => cd.classList.contains("urgent"))];
  await helpers.clock(1000);
  assert.deepEqual(await read(egress), [true, "expires in 109s", "90.8%", false]);
  assert.deepEqual(await read(tool), [true, "expires in 114s", "95%", false]);

  await helpers.clock(1000);
  assert.deepEqual(await read(egress), [true, "expires in 108s", "90%", false]);
  assert.deepEqual(await read(tool), [true, "expires in 113s", "94.2%", false]);

  // Ninety seconds on: the egress hold is inside its last 20 s and says so; the tool
  // ask, five seconds behind it, is not yet.
  await helpers.clock(90000);
  assert.deepEqual(await read(egress), [true, "expires in 18s", "15%", true]);
  assert.deepEqual(await read(tool), [true, "expires in 23s", "19.2%", false]);
});

// ── the round trip: what the card says is what the backend answered ─────────

const outcome = async card => [await text(card.locator(".cardmsg")),
                               await card.locator(".cardmsg").getAttribute("class"),
                               await card.evaluate(el => ["done-allow", "done-deny"]
                                 .filter(c => el.classList.contains(c)))];

test("a persist reports the pattern the backend stored", TIMEOUT, async t => {
  const { egress, click } = await approvals(t);
  await click(egress.locator('button[data-action="allow_persist"]'));
  await egress.locator(".confirm select").selectOption(".example.org");
  await click(egress.locator(".confirm button.confirmgo"));
  assert.deepEqual(await outcome(egress),
                   ["✓ allowed · standing rule written: .example.org", "cardmsg ok",
                    ["done-allow"]]);
});

test("a persist the backend stored under another pattern reports that one", TIMEOUT,
     async t => {
  const { egress, sent, click } = await approvals(t);
  backend.overrideResolve(answer => ({ ...answer, pattern: "registry.example.org" }));
  await click(egress.locator('button[data-action="allow_persist"]'));
  await egress.locator(".confirm select").selectOption(".example.org");
  await click(egress.locator(".confirm button.confirmgo"));
  assert.equal(sent[0].body.pattern, ".example.org");
  assert.equal(await text(egress.locator(".cardmsg")),
               "✓ allowed · standing rule written: registry.example.org");
});

// The lease's end as the backend answered it, in the context's own zone. The fixture's
// answer is NOW + 1800 s, which is also /api/config's lease length, so a card adding the
// configured length to its own clock would read the same; here the backend says
// NOW + 900 instead: 10:15 UTC, 15:45 in Asia/Kolkata.
test("a lease reports when the backend says it ends", TIMEOUT, async t => {
  const { egress, sent, click } = await approvals(t);
  backend.overrideResolve(answer => ({ ...answer,
                                       lease_expires_at: backend.NOW_MS / 1000 + 900 }));
  await click(egress.locator('button[data-action="allow_lease"]'));
  assert.deepEqual(sent, [{ path: "/approvals/egress-1/resolve",
                            body: { action: "allow_lease" } }]);
  assert.deepEqual(await outcome(egress),
                   ["✓ allowed · leased until 15:45:00", "cardmsg ok", ["done-allow"]]);
});

test("allow once and deny once each send their action and say so", TIMEOUT, async t => {
  const { egress, helpers, sent, click } = await approvals(t);
  await click(egress.locator('button[data-action="deny_once"]'));
  assert.deepEqual(sent, [{ path: "/approvals/egress-1/resolve",
                            body: { action: "deny_once" } }]);
  assert.deepEqual(await outcome(egress),
                   ["✕ denied · this request only", "cardmsg bad", ["done-deny"]]);

  await helpers.push([{ ...backend.HOLDS[0], id: "egress-2" }]);
  await helpers.settle();
  const second = egress.page().locator('#pending .card[data-id="egress-2"]');
  await click(second.locator('button[data-action="allow_once"]'));
  assert.deepEqual(sent[1], { path: "/approvals/egress-2/resolve",
                              body: { action: "allow_once" } });
  assert.deepEqual(await outcome(second),
                   ["✓ allowed · this request only", "cardmsg ok", ["done-allow"]]);
});

test("a tool deny sends deny and says the call will not run", TIMEOUT, async t => {
  const { tool, sent, click } = await approvals(t);
  await click(tool.getByRole("button", { name: "Deny", exact: true }));
  assert.deepEqual(sent, [{ path: "/approvals/tool-1/resolve", body: { action: "deny" } }]);
  assert.deepEqual(await outcome(tool),
                   ["✕ denied · the call will not run", "cardmsg bad", ["done-deny"]]);
});

// ── an allowed tool card follows its call ───────────────────────────────────

// What an allowed card shows besides its message: no buttons and no countdown, since
// nothing is left to decide and its deadline is the page's.
const settled = async card => [await card.locator(".actions").isVisible(),
                               await card.locator(".countdown").isVisible()];

test("a tool allow says it waits for the call, not that it ran, with nothing to press",
     TIMEOUT, async t => {
  const { tool, sent, click, helpers } = await approvals(t);
  await click(tool.getByRole("button", { name: "Allow", exact: true }));
  assert.deepEqual(sent, [{ path: "/approvals/tool-1/resolve", body: { action: "allow" } }]);
  const waiting = ["✓ allowed · waiting for its result", "cardmsg ok", ["done-allow"]];
  assert.deepEqual(await outcome(tool), waiting);
  assert.deepEqual(await settled(tool), [false, false]);
  // Ticks redraw the countdowns of live cards; this one has none to show.
  await helpers.clock(1000);
  await helpers.clock(1000);
  assert.deepEqual(await outcome(tool), waiting);
  assert.deepEqual(await settled(tool), [false, false]);
});

test("a pin reports the fields the backend pinned, not the boxes ticked", TIMEOUT,
     async t => {
  const { tool, sent, click, helpers } = await approvals(t);
  backend.overrideResolve(answer => ({ ...answer,
                                       pin: { ...answer.pin, fields: ["owner", "repo"] } }));
  await click(tool.getByRole("button", { name: "Allow + pin…" }));
  await tool.locator(".confirm .cpins input").first().check();
  await click(tool.locator(".confirm button.confirmgo").nth(1));
  assert.deepEqual(sent[0].body, { action: "allow_pinned", pins: ["owner"] });
  const said = ["✓ allowed · pinned for good on owner, repo (pin 5) · waiting for its "
                + "result · its pin may answer other calls", "cardmsg ok", ["done-allow"]];
  assert.deepEqual(await outcome(tool), said);
  await helpers.clock(1000);
  assert.deepEqual(await outcome(tool), said);
  assert.deepEqual(await settled(tool), [false, false]);
});

// The stream's entry for the fixture's tool ask in each state (holds._list_granted).
// Its grant lapses at NOW + 900 s: 15:45:00 in the context's Asia/Kolkata.
const entry = (state, extra = {}) => ({ id: "tool-1", pinned: false, state, ...extra });
const WAITING = entry("waiting", { ends_at: backend.NOW_MS / 1000 + 900 });

test("the stream's word for an allowed card is what it says", TIMEOUT, async t => {
  const { tool, click, grant } = await approvals(t);
  await click(tool.getByRole("button", { name: "Allow", exact: true }));
  await grant([], [WAITING]);
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · waiting for the agent", "cardmsg ok", ["done-allow"]]);
  await grant([], [entry("ended", { status: "tool-error",
                                    reason: "<b>x</b> & <img src=x onerror=alert(1)>",
                                    result_bytes: 1234, duration: 0.5 })]);
  // The reason is third-party text, so it is shown as text and makes no element.
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · tool-error · <b>x</b> & <img src=x onerror=alert(1)> · "
                    + "1,234 bytes · 0.50 s", "cardmsg bad", ["done-allow"]]);
  assert.equal(await tool.locator(".cardmsg *").count(), 0);
});

test("a final card stays for its dwell, then goes", TIMEOUT, async t => {
  const { page, tool, click, grant, helpers } = await approvals(t);
  await click(tool.getByRole("button", { name: "Allow", exact: true }));
  await grant([], [entry("ended", { status: "ok", reason: null, result_bytes: null,
                                    duration: null })]);
  assert.deepEqual(await outcome(tool), ["✓ allowed · ok", "cardmsg ok", ["done-allow"]]);
  await page.mouse.move(0, 0);
  await helpers.clock(59000);
  assert.equal(await tool.count(), 1);
  // A later push cannot change a final card.
  await grant([], [WAITING]);
  assert.deepEqual(await outcome(tool), ["✓ allowed · ok", "cardmsg ok", ["done-allow"]]);
  await helpers.clock(1000);
  assert.equal(await tool.count(), 0);
});

test("at its deadline a live card names when the grant lapses, then goes", TIMEOUT,
     async t => {
  const { page, tool, click, grant, helpers } = await approvals(t, { heartbeat: true });
  await click(tool.getByRole("button", { name: "Allow", exact: true }));
  await grant([], [WAITING]);
  await page.mouse.move(0, 0);
  await helpers.clock(119000);
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · waiting for the agent", "cardmsg ok", ["done-allow"]]);
  await helpers.clock(1000);
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · not claimed yet · the agent has until 15:45:00 · the "
                    + "result will be in the record under tool-1", "cardmsg",
                    ["done-allow"]]);
  await helpers.clock(7000);
  assert.equal(await tool.count(), 1);
  await helpers.clock(1000);
  assert.equal(await tool.count(), 0);
});

// The same card with the stream silenced (the only heartbeat, page.js's): the watchdog
// retires it after 15 s, the stream stays quiet, and the kept entry may be stale.
test("at its deadline a card on a silent stream points at the record", TIMEOUT,
     async t => {
  const { page, tool, click, grant, helpers } = await approvals(t, { heartbeat: true });
  await click(tool.getByRole("button", { name: "Allow", exact: true }));
  await grant([], [WAITING]);
  await page.evaluate(() => window.__harnessSilence(true));
  await helpers.clock(119000);
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · waiting for the agent", "cardmsg ok", ["done-allow"]]);
  await helpers.clock(1000);
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · no result on this card · see the record under tool-1",
                    "cardmsg", ["done-allow"]]);
});

// The resolve held while the stream says the ask was allowed, as when the answer is
// slow: the card follows the call from the push, and the answer, when it comes, adds
// what it pinned and nothing else.
async function pushBeforeTheAnswer(t, answer) {
  const it = await approvals(t, { answer });
  const { run, page, tool, click } = it;
  // A tick first, so the card's countdown is drawn and the push has one to hide.
  await it.helpers.clock(1000);
  assert.equal(await tool.locator(".countdown").isVisible(), true);
  await click(tool.getByRole("button", { name: "Allow + pin…" }));
  await tool.locator(".confirm .cpins input").first().check();
  let before;
  await run.queue.navigate(async () => {
    await tool.locator(".confirm button.confirmgo").nth(1).click();
    await run.queue.allHeld();
    await page.evaluate(g => window.__harnessPush({ holds: [], granted: g }), [WAITING]);
    before = await outcome(tool);
  });
  await it.helpers.settle();
  return { before, after: await outcome(tool), settled: await settled(tool) };
}

test("a push naming a resolving card follows the call; a late answer adds its pin",
     TIMEOUT, async t => {
  const { before, after, settled: shown } = await pushBeforeTheAnswer(t);
  assert.deepEqual(before,
                   ["✓ allowed · waiting for the agent", "cardmsg ok", ["done-allow"]]);
  assert.deepEqual(after,
                   ["✓ allowed · pinned for good on owner (pin 5) · waiting for the agent "
                    + "· its pin may answer other calls", "cardmsg ok", ["done-allow"]]);
  assert.deepEqual(shown, [false, false]);
});

// A refused answer, and one the network drops, so the page's fetch throws.
for (const [how, failed] of [["refused", { status: 503,
                                           json: { detail: "upstream unavailable" } }],
                             ["dropped", { abort: true }]]) {
  test(`a push naming a resolving card follows the call; a late answer ${how} `
       + "changes nothing", TIMEOUT, async t => {
    const { before, after, settled: shown } = await pushBeforeTheAnswer(
      t, (method, path) => (method === "POST" && path === "/approvals/tool-1/resolve"
        ? failed : null));
    const waiting = ["✓ allowed · waiting for the agent", "cardmsg ok", ["done-allow"]];
    assert.deepEqual(before, waiting);
    assert.deepEqual(after, waiting);
    assert.deepEqual(shown, [false, false]);
  });
}

// A card resolving here leaves the queue with the stream naming no grant for it, or
// with the page not knowing the wait: it departs as any card resolved elsewhere does.
for (const [why, wait, granted] of [["names no grant for it", true, []],
                                    ["has no wait to follow it by", false, [WAITING]]]) {
  test(`a resolving card leaving on a push that ${why} departs as before`, TIMEOUT,
       async t => {
    const { run, page, tool } = await approvals(t, { wait });
    let left;
    await run.queue.navigate(async () => {
      await tool.getByRole("button", { name: "Allow", exact: true }).click();
      await run.queue.allHeld();
      await page.evaluate(g => window.__harnessPush({ holds: [], granted: g }), granted);
      left = await outcome(tool);
    });
    assert.deepEqual(left, ["no longer pending — resolved elsewhere, or the control "
                            + "plane restarted", "cardmsg bad", []]);
  });
}

// With the heartbeat, so the stream is still open for the late push.
test("a result after the deadline stays for its own dwell", TIMEOUT, async t => {
  const { page, tool, click, grant, helpers } = await approvals(t, { heartbeat: true });
  await click(tool.getByRole("button", { name: "Allow", exact: true }));
  await page.mouse.move(0, 0);
  await helpers.clock(120000);
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · no result on this card · see the record under tool-1",
                    "cardmsg", ["done-allow"]]);
  // Inside the 8 s that card would stay, the stream brings the call's ending: it is
  // read for as long as any result is.
  await helpers.clock(7000);
  await grant([], [entry("lapsed")]);
  await helpers.clock(59000);
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · not claimed in time; it will not run", "cardmsg bad",
                    ["done-allow"]]);
  await helpers.clock(1000);
  assert.equal(await tool.count(), 0);
});

// The config's wait absent, as from a control plane older than it, or unusable.
for (const [why, wait] of [["absent", undefined], ["zero", 0]]) {
  test(`with the outcome wait ${why}, an allow reads and goes as before`, TIMEOUT,
       async t => {
    const config = (method, path) => (method === "GET" && path === "/api/config"
      ? { status: 200, json: { ...backend.respond(method, path).json,
                               tool_outcome_wait: wait } }
      : null);
    const { page, tool, click, grant, helpers } = await approvals(
      t, { wait: false, answer: config });
    await click(tool.getByRole("button", { name: "Allow", exact: true }));
    const said = ["✓ allowed · runs when the agent returns for it", "cardmsg ok",
                  ["done-allow"]];
    assert.deepEqual(await outcome(tool), said);
    await grant([], [WAITING]);
    assert.deepEqual(await outcome(tool), said);
    await page.mouse.move(0, 0);
    await helpers.clock(8000);
    assert.equal(await tool.count(), 0);
  });
}
