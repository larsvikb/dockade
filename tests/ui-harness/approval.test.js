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
import { BROWSER, launch, openPage } from "./page.js";

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

// Long enough for a page load and a minute and a half of fake time; short enough that a
// wedged page fails the run rather than hanging it.
const TIMEOUT = { timeout: 60000 };

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser?.close(); });

// The page loaded with backend.js's two holds on it (the egress hold on
// registry.example.org and the tool ask on mcp-github's create_pull_request), and every
// resolve it sends recorded in `sent`, in order.
async function approvals(t) {
  backend.reset();
  const sent = [];
  const run = await openPage(browser, uiDir, {
    respond: (method, path, body, query) => {
      if (method === "POST" && path.startsWith("/approvals/")) sent.push({ path, body });
      return backend.respond(method, path, body, query);
    },
    answeredLast: backend.answeredLast,
  });
  t.after(() => run.close());
  const { page, helpers } = run;
  await helpers.load();
  await helpers.settle();
  await helpers.push(backend.HOLDS);
  await helpers.settle();
  const egress = page.locator('#pending .card[data-id="egress-1"]');
  const tool = page.locator('#pending .card[data-id="tool-1"]');
  // Clicks go through here so the page has handled each, and answered what it sent,
  // before the test reads anything.
  const click = async locator => { await locator.click(); await helpers.settle(); };
  return { page, helpers, sent, egress, tool, click };
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
               + "argument is free: repo, title, and any this call did not set.");
  assert.equal(await text(forGood), "Confirm — allow and pin owner for good");
  assert.equal(await text(timed), "Confirm — allow and pin owner for 1 h");

  await repo.check();
  assert.equal(await text(panel.locator(".cwhat")),
               "Allows this call, and every later create_pull_request call on mcp-github "
               + "with owner = acme and repo = gadgets runs without a card while the pin "
               + "lasts. Every other argument is free: title, and any this call did not "
               + "set.");
  assert.equal(await text(forGood), "Confirm — allow and pin owner, repo for good");

  // Ending on a box unticked, so what is sent is the boxes ticked rather than every
  // field offered.
  await owner.uncheck();
  assert.equal(await text(panel.locator(".cwhat")),
               "Allows this call, and every later create_pull_request call on mcp-github "
               + "with repo = gadgets runs without a card while the pin lasts. Every other "
               + "argument is free: owner, title, and any this call did not set.");
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

test("a tool allow says it runs when the agent returns, not that it ran", TIMEOUT,
     async t => {
  const { tool, sent, click } = await approvals(t);
  await click(tool.getByRole("button", { name: "Allow", exact: true }));
  assert.deepEqual(sent, [{ path: "/approvals/tool-1/resolve", body: { action: "allow" } }]);
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · runs when the agent returns for it", "cardmsg ok",
                    ["done-allow"]]);
});

test("a pin reports the fields the backend pinned, not the boxes ticked", TIMEOUT,
     async t => {
  const { tool, sent, click } = await approvals(t);
  backend.overrideResolve(answer => ({ ...answer,
                                       pin: { ...answer.pin, fields: ["owner", "repo"] } }));
  await click(tool.getByRole("button", { name: "Allow + pin…" }));
  await tool.locator(".confirm .cpins input").first().check();
  await click(tool.locator(".confirm button.confirmgo").nth(1));
  assert.deepEqual(sent[0].body, { action: "allow_pinned", pins: ["owner"] });
  assert.deepEqual(await outcome(tool),
                   ["✓ allowed · pinned for good on owner, repo (pin 5) · runs when the "
                    + "agent returns for it", "cardmsg ok", ["done-allow"]]);
});
