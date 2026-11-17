// SPDX-License-Identifier: Apache-2.0
// The tasks page against values written down in advance, as approval.test.js holds the
// approval page: every expected value below is worked out by hand from the fixture in
// tasks-backend.js and the page's comments, none copied from a run. Unlike the approval
// page's, some of these read layout boxes and scroll offsets, since what they hold is
// where things are: the list or one task, never both, and the list's place kept.
//
//   node tasks.test.js <served dir>
//
// `make ui-test` assembles the served dir as ui-diff does and runs this. Each test opens
// a fresh browser context on a reset backend. A later task's tests go in a section of
// their own below, on the helpers here.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import * as backend from "./tasks-backend.js";
import { existsSync } from "node:fs";
import { BROWSER, ORIGIN, launch, openPage } from "./page.js";

const uiDir = process.argv[2];
if (!uiDir) {
  console.error("usage: node tasks.test.js <served dir>");
  process.exit(2);
}
// `make ui-test` SKIPs before it gets here; run by hand, this says why rather than
// failing every test at launch.
if (!existsSync(BROWSER)) {
  console.error(`no browser at ${BROWSER}; point UI_HARNESS_BROWSER at one`);
  process.exit(2);
}

const TIMEOUT = { timeout: 60000 };
const DESKTOP = { width: 1920, height: 1080 };
const PHONE = { width: 390, height: 844 };
// Wide and short, so the inbox scrolls by hundreds of pixels, not the few tens it
// scrolls at 1080 high, which a shell drawing it slightly shorter could take away.
const SHORT = { width: 1920, height: 700 };
// main's padding, either side: what an open task's width is short of the window's.
const MARGIN = 20;
// The task-registry's ID_MAX plus one, as digits.
const PAST_ID_MAX = "9223372036854775808";

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser?.close(); });

// ── helpers ─────────────────────────────────────────────────────────────────

// The page loaded at `hash` on a reset backend and settled. `reads` is the id of every
// task read it sent (`GET /api/tasks/<id>`), `dialogs` the type of every dialog it
// raised, `errors` every uncaught error, all in order. Dialogs are dismissed unless
// `helpers.accept()` was called (page.js).
async function tasksPage(t, { viewport = DESKTOP, hash = "" } = {}) {
  backend.reset();
  const reads = [];
  const run = await openPage(browser, uiDir, {
    respond: (method, path, body, query) => {
      const read = method === "GET" && path.match(/^\/api\/tasks\/(\d+)$/);
      if (read) reads.push(read[1]);
      return backend.respond(method, path, body, query);
    },
  }, { viewport, hash });
  t.after(() => run.close());
  const { page, helpers } = run;
  const dialogs = [];
  const errors = [];
  page.on("dialog", d => dialogs.push(d.type()));
  page.on("pageerror", e => errors.push(e.message));
  await helpers.load();
  await helpers.settle();

  const settle = () => helpers.settle();
  const click = async (selector, options) => {
    await page.click(selector, options);
    await settle();
  };
  const press = async key => {
    await page.keyboard.press(key);
    await settle();
  };
  const go = async how => {
    await helpers.navigate(() => (how === "back" ? page.goBack() : page.goForward()));
    await settle();
  };
  // A poll interval passed, and the page done with what it read.
  const poll = async () => {
    await helpers.clock(16000);
    await settle();
  };
  const setHash = async hash => {
    await page.evaluate(h => { location.hash = h; }, hash);
    await settle();
  };
  // Scrolled as far as asked or as the page goes, and the offset it reached.
  const scrollTo = y => page.evaluate(to => { window.scrollTo(0, to); return window.scrollY; },
                                      y);
  const state = () => page.evaluate(() => ({
    hash: location.hash,
    list: !document.getElementById("list-view").hidden,
    task: !document.getElementById("task-view").hidden,
    y: window.scrollY,
  }));
  // A task's row in a list, by the cell named: a cell with no control in it.
  const row = (list, id) => `${list} tr:has(button[data-task="${id}"])`;
  return { page, helpers, reads, dialogs, errors, settle, click, press, go, poll, setHash,
           scrollTo, state, row };
}

const text = async locator => (await locator.textContent()).trim();

// What is on screen, as boxes: each view's, and the window's width without a scrollbar.
async function boxes(page) {
  return page.evaluate(() => {
    const box = id => {
      const el = document.getElementById(id);
      const r = el.getBoundingClientRect();
      return { shown: el.getClientRects().length > 0, left: r.left, width: r.width };
    };
    return { list: box("list-view"), task: box("task-view"),
             window: document.documentElement.clientWidth };
  });
}

// The task view alone, the window's width less the page's margin either side.
async function assertTaskAlone(page) {
  const b = await boxes(page);
  assert.equal(b.list.shown, false, "the list is not drawn beside the task");
  assert.equal(b.task.shown, true);
  assert.equal(b.task.left, MARGIN);
  assert.equal(b.task.width, b.window - 2 * MARGIN);
}

// ── opening a task ──────────────────────────────────────────────────────────

for (const [name, viewport] of [["desktop", DESKTOP], ["narrow", PHONE]]) {
  test(`${name}: a click on a row's title opens the task alone, in the inbox`, TIMEOUT,
       async t => {
    const p = await tasksPage(t, { viewport });
    await p.click(`${p.row("#inbox", 6)} td:nth-child(2)`);
    await assertTaskAlone(p.page);
    assert.equal((await p.state()).hash, "#task=6");
    assert.deepEqual(p.reads, ["6"]);
    assert.equal(await text(p.page.locator("#task-detail h3").first()),
                 "#6 Refined one show source edit spec");
  });

  test(`${name}: a click on a row opens the task alone, in All tasks`, TIMEOUT,
       async t => {
    const p = await tasksPage(t, { viewport });
    await p.click("#view-all");
    await p.page.selectOption("#tasks-repo", "dockade");
    await p.settle();
    await p.page.selectOption("#tasks-state", "open");
    await p.settle();
    await p.click(`${p.row("#tasks", 2)} td:nth-child(4)`);
    await assertTaskAlone(p.page);
    assert.equal((await p.state()).hash, "#view=all&task=2");
    assert.deepEqual(p.reads, ["2"]);
  });

  test(`${name}: a click on a note's text opens its task alone, in Notes for you`,
       TIMEOUT, async t => {
    const p = await tasksPage(t, { viewport });
    await p.click('#flagged-notes tr:has(input[data-flagged-note="15"]) .prose');
    await assertTaskAlone(p.page);
    assert.equal((await p.state()).hash, "#task=6");
    assert.deepEqual(p.reads, ["6"]);
  });
}

test("a task opened from the scrolled inbox, or from another scrolled task, starts at "
     + "the top", TIMEOUT, async t => {
  const p = await tasksPage(t, { viewport: SHORT });
  assert.ok(await p.scrollTo(10000) > 200, "the inbox is well taller than the window");
  // Task 3 is taller than the inbox, so a page that kept the list's offset would show.
  await p.click(`${p.row("#inbox", 3)} td:nth-child(2)`);
  assert.deepEqual(await p.state(), { hash: "#task=3", list: false, task: true, y: 0 });
  assert.equal(await p.scrollTo(900), 900);
  await p.setHash("#task=6");
  assert.deepEqual(await p.state(), { hash: "#task=6", list: false, task: true, y: 0 });
});

test("a click on a flagged note's box, on its cell's padding, or ending a drag that "
     + "selected text, opens nothing", TIMEOUT, async t => {
  const p = await tasksPage(t);
  const box = '#flagged-notes input[data-flagged-note="15"]';
  await p.click(box);
  assert.equal(await p.page.isChecked(box), true);
  await p.click('#flagged-notes tr:has(input[data-flagged-note="15"]) td.pick',
                { position: { x: 1, y: 1 } });
  const prose = p.page.locator('#flagged-notes tr:has(input[data-flagged-note="16"]) .prose');
  const r = await prose.boundingBox();
  await p.page.mouse.move(r.x + 1, r.y + r.height / 2);
  await p.page.mouse.down();
  await p.page.mouse.move(r.x + r.width - 1, r.y + r.height / 2, { steps: 5 });
  await p.page.mouse.up();
  await p.settle();
  assert.notEqual(await p.page.evaluate(() => String(window.getSelection())), "");
  assert.deepEqual(await p.state(), { hash: "", list: true, task: false, y: 0 });
  assert.deepEqual(p.reads, []);
});

for (const [name, viewport] of [["desktop", DESKTOP], ["narrow", PHONE]]) {
  test(`${name}: the strip and the changed line stay in view on a scrolled task, and `
       + "read it again keeps the scroll", TIMEOUT, async t => {
    const p = await tasksPage(t, { viewport, hash: "#task=3" });
    const y = await p.scrollTo(1500);
    assert.equal(y, 1500);
    backend.editElsewhere(3);
    await p.poll();
    const at = await p.page.evaluate(() => {
      const rect = sel => document.querySelector(sel).getBoundingClientRect();
      return { strip: rect(".task-strip"), slot: rect(".notice-slot"),
               back: rect("#task-back"), height: innerHeight };
    });
    assert.equal(at.strip.top, 0);
    assert.ok(at.back.top >= 0 && at.back.bottom <= at.strip.bottom);
    assert.ok(at.slot.top >= at.strip.bottom, "the line sits under the strip");
    assert.ok(at.slot.bottom <= at.height);
    assert.match(await text(p.page.locator(".notice-slot")), /^read it again/);
    await p.click("#task-detail button[data-show]");
    assert.equal((await p.state()).y, y);
    assert.deepEqual(p.reads, ["3", "3", "3"]);
  });
}

// ── back to the list ────────────────────────────────────────────────────────

// Each way back from task 2, which is shorter than the window: the list at the offset it
// was left at, which the page restores, since a short task holds no scroll to keep.
const BACK = {
  control: p => p.click("#task-back"),
  Esc: p => p.press("Escape"),
  "the browser's back": p => p.go("back"),
};

for (const [how, back] of Object.entries(BACK)) {
  test(`${how} returns to the inbox where it was, the row last read marked`, TIMEOUT,
       async t => {
    const p = await tasksPage(t, { viewport: SHORT });
    const y = await p.scrollTo(10000);
    assert.ok(y > 200, `the inbox scrolls only ${y} px`);
    await p.click(`${p.row("#inbox", 2)} td:nth-child(2)`);
    assert.deepEqual(await p.state(), { hash: "#task=2", list: false, task: true, y: 0 });
    await back(p);
    assert.deepEqual(await p.state(), { hash: "", list: true, task: false, y });
    const marked = () => p.page.locator("#list-view tr[aria-current]").evaluateAll(
      rows => rows.map(r => r.querySelector("button[data-task]").dataset.task));
    assert.deepEqual(await marked(), ["2"]);
    // A poll redraws the inbox, and the mark with it.
    await p.poll();
    assert.deepEqual(await marked(), ["2"]);
    assert.deepEqual(p.dialogs, []);
  });

  test(`${how} returns to All tasks with its filters and search as they were`, TIMEOUT,
       async t => {
    const p = await tasksPage(t);
    await p.click("#view-all");
    await p.page.selectOption("#tasks-repo", "dockade");
    await p.settle();
    await p.page.selectOption("#tasks-state", "open");
    await p.settle();
    await p.page.fill("#tasks-search", "store");
    await p.page.press("#tasks-search", "Enter");
    await p.settle();
    await p.click(`${p.row("#tasks", 2)} td:nth-child(2)`);
    assert.equal((await p.state()).hash, "#view=all&task=2");
    await back(p);
    assert.deepEqual(await p.state(), { hash: "#view=all", list: true, task: false, y: 0 });
    assert.equal(await p.page.inputValue("#tasks-repo"), "dockade");
    assert.equal(await p.page.inputValue("#tasks-state"), "open");
    assert.equal(await p.page.inputValue("#tasks-search"), "store");
    assert.deepEqual(await p.page.locator("#tasks button[data-task]").evaluateAll(
      bs => bs.map(b => b.dataset.task)), ["2"]);
    assert.equal(await text(p.page.locator("#task-back")), "← All tasks");
  });
}

test("forward after the browser's back opens the task again", TIMEOUT, async t => {
  const p = await tasksPage(t);
  await p.click(`${p.row("#inbox", 2)} td:nth-child(2)`);
  await p.go("back");
  await p.go("forward");
  await assertTaskAlone(p.page);
  assert.equal((await p.state()).hash, "#task=2");
  assert.deepEqual(p.reads, ["2", "2"]);
});

test("Esc in a text field stays on the task", TIMEOUT, async t => {
  const p = await tasksPage(t, { hash: "#task=3" });
  await p.page.click("#task-detail textarea[data-note]");
  await p.press("Escape");
  const { hash, list, task } = await p.state();
  assert.deepEqual({ hash, list, task }, { hash: "#task=3", list: false, task: true });
});

// Narrow, where the inbox scrolls far enough that the task's offset, clamped to the
// list's height, would not land on the list's own.
test("the list's offset is the one it was left at, not the last task's", TIMEOUT,
     async t => {
  const p = await tasksPage(t, { viewport: PHONE });
  const y = await p.scrollTo(300);
  assert.equal(y, 300);
  await p.click(`${p.row("#inbox", 3)} td:nth-child(2)`);
  await p.scrollTo(1200);
  await p.setHash("#task=6");
  await p.click("#task-back");
  assert.deepEqual(await p.state(), { hash: "", list: true, task: false, y });
});

test("a reload on a task's URL shows that task alone", TIMEOUT, async t => {
  const p = await tasksPage(t);
  await p.click(`${p.row("#inbox", 3)} td:nth-child(2)`);
  await p.helpers.navigate(() => p.page.reload());
  await p.settle();
  await assertTaskAlone(p.page);
  assert.equal((await p.state()).hash, "#task=3");
  assert.equal(await text(p.page.locator("#task-back")), "← Inbox");
});

// ── a task the page cannot open ─────────────────────────────────────────────

// Each loaded as a link would, the list's URL in its place after. Only the one that is
// an id is read; the registry has no task 999.
const NO_TASK = [
  ["999", "The registry has no task #999.", ["999"]],
  ["abc", 'The link names no task: "abc" is not a task id.', []],
  ["0", 'The link names no task: "0" is not a task id.', []],
  [PAST_ID_MAX, `The link names no task: "${PAST_ID_MAX}" is not a task id.`, []],
];

for (const [given, said, reads] of NO_TASK) {
  test(`#task=${given} shows the list, saying so, under the list's URL`, TIMEOUT,
       async t => {
    const p = await tasksPage(t, { hash: `#task=${given}` });
    assert.deepEqual(await p.state(), { hash: "", list: true, task: false, y: 0 });
    assert.equal(p.page.url(), `${ORIGIN}/`);
    assert.equal(await text(p.page.locator("#list-notice")), said);
    assert.deepEqual(p.reads, reads);
    assert.deepEqual(p.errors, []);
  });

  test(`#task=${given} reached from task 2: back returns to task 2`, TIMEOUT, async t => {
    const p = await tasksPage(t, { hash: "#task=2" });
    await p.setHash(`#task=${given}`);
    assert.deepEqual(await p.state(), { hash: "", list: true, task: false, y: 0 });
    assert.equal(await text(p.page.locator("#list-notice")), said);
    await p.go("back");
    assert.deepEqual(await p.state(), { hash: "#task=2", list: false, task: true, y: 0 });
    assert.deepEqual(p.errors, []);
  });
}

test("the notice is in view on a list that comes back scrolled", TIMEOUT, async t => {
  const p = await tasksPage(t, { viewport: PHONE });
  assert.equal(await p.scrollTo(300), 300);
  await p.setHash("#task=2");
  await p.setHash("#task=abc");
  const at = await p.page.evaluate(() => ({
    y: window.scrollY, top: document.getElementById("list-notice").getBoundingClientRect().top,
  }));
  // The list where it was left; past it by the notice's height when the browser's
  // scroll anchoring keeps the rows still as the notice appears above them.
  assert.ok(at.y >= 300, `the list came back at ${at.y}`);
  assert.equal(at.top, 0);
});

test("the notice is gone, back on the list, after the next open", TIMEOUT, async t => {
  const p = await tasksPage(t, { hash: "#task=abc" });
  assert.equal(await p.page.isVisible("#list-notice"), true);
  await p.click(`${p.row("#inbox", 2)} td:nth-child(2)`);
  await p.click("#task-back");
  assert.equal(await p.page.isVisible("#list-notice"), false);
});

test("a read that fails otherwise says it could not load the task", TIMEOUT, async t => {
  const p = await tasksPage(t, { hash: "#task=13" });
  assert.deepEqual(await p.state(), { hash: "#task=13", list: false, task: true, y: 0 });
  assert.equal(await text(p.page.locator("#task-detail")), "Could not load task #13.");
  await p.click("#task-back");
  assert.deepEqual(await p.state(), { hash: "", list: true, task: false, y: 0 });
});

// A read the stopped registry failed leaves the task view up with no task open; back
// while it is stopped, the page follows the list's hash once the registry answers.
test("back from a read the stopped registry failed shows the list once it answers",
     TIMEOUT, async t => {
  const p = await tasksPage(t);
  backend.stopRegistry();
  await p.setHash("#task=2");
  await p.go("back");
  backend.startRegistry();
  await p.poll();
  const { hash, list, task } = await p.state();
  assert.deepEqual({ hash, list, task }, { hash: "", list: true, task: false });
  assert.equal(await p.page.isVisible("#list-view"), true);
});

// ── leaving with a spec edit open ───────────────────────────────────────────

// Task 3 opened, its spec edit opened and its body changed by real key input, as
// Chromium counts a gesture before it shows beforeunload; then scrolled to an offset
// the list does not have. The list was left at its own offset, and task 6 opened from
// task 3 and gone back from first, so forward has a task to go to.
async function editing(t) {
  const p = await tasksPage(t, { viewport: SHORT });
  const listY = await p.scrollTo(10000);
  assert.ok(listY > 200, `the inbox scrolls only ${listY} px`);
  await p.click(`${p.row("#inbox", 3)} td:nth-child(2)`);
  await p.setHash("#task=6");
  await p.go("back");
  assert.equal((await p.state()).hash, "#task=3");
  await p.click("#task-detail button[data-spec-open]");
  const body = p.page.locator('#task-detail textarea[data-spec="body"]');
  await body.click();
  await p.page.keyboard.press("End");
  await p.page.keyboard.type(" More.");
  await p.settle();
  // Esc goes back only outside a text field.
  await p.page.evaluate(() => document.activeElement.blur());
  const y = await p.scrollTo(700);
  assert.equal(y, 700);
  assert.notEqual(y, listY);
  const draft = () => body.inputValue();
  const written = await draft();
  assert.match(written, / More\.$/);
  return { ...p, y, listY, draft, written };
}

const LEAVE = {
  "the back control": p => p.click("#task-back"),
  Esc: p => p.press("Escape"),
  "the browser's back": p => p.go("back"),
  "the browser's forward, to a task": p => p.go("forward"),
  "a hash naming another task": p => p.setHash("#task=6"),
};

for (const [how, leave] of Object.entries(LEAVE)) {
  test(`${how} with an edit's changes unsaved asks once; dismissed, nothing moves`,
       TIMEOUT, async t => {
    const p = await editing(t);
    const reads = p.reads.length;
    await leave(p);
    // A declined back or forward is undone by a traversal of its own, which may land
    // after the page went quiet. It asks nothing more and reads nothing.
    await p.page.waitForFunction(() => location.hash === "#task=3");
    await p.settle();
    assert.equal(p.reads.length, reads);
    assert.deepEqual(p.dialogs, ["confirm"]);
    assert.deepEqual(await p.state(), { hash: "#task=3", list: false, task: true, y: p.y });
    assert.equal(await p.draft(), p.written);
    // History reads as it did before: once the edit is discarded, one back from the
    // task reaches the list it was opened from.
    await p.click("#task-detail button[data-spec-discard]");
    await p.go("back");
    assert.deepEqual(await p.state(), { hash: "", list: true, task: false, y: p.listY });
    assert.deepEqual(p.dialogs, ["confirm"]);
  });

  test(`${how} with an edit's changes unsaved asks once; accepted, it is gone`, TIMEOUT,
       async t => {
    const p = await editing(t);
    p.helpers.accept();
    await leave(p);
    assert.deepEqual(p.dialogs, ["confirm"]);
    const now = await p.state();
    assert.notEqual(now.hash, "#task=3");
    await p.setHash("#task=3");
    assert.equal(await p.page.locator('#task-detail textarea[data-spec="body"]').count(), 0);
    assert.deepEqual(p.dialogs, ["confirm"]);
  });
}

// Two entries at once: the undo goes as far, not one step, and leaves every entry as
// it was: back from task 3 is task 6, and back from that the list.
test("a declined history.go(-2) asks once and leaves every entry as it was", TIMEOUT,
     async t => {
  const p = await tasksPage(t);
  await p.click(`${p.row("#inbox", 6)} td:nth-child(2)`);
  await p.setHash("#task=3");
  await p.click("#task-detail button[data-spec-open]");
  await p.page.locator('#task-detail textarea[data-spec="body"]').click();
  await p.page.keyboard.type(" More.");
  await p.settle();
  const reads = p.reads.length;
  await p.helpers.navigate(() => p.page.evaluate(() => window.history.go(-2)));
  await p.page.waitForFunction(() => location.hash === "#task=3");
  await p.settle();
  assert.deepEqual(p.dialogs, ["confirm"]);
  assert.equal(p.reads.length, reads);
  const { hash, list, task } = await p.state();
  assert.deepEqual({ hash, list, task }, { hash: "#task=3", list: false, task: true });
  await p.click("#task-detail button[data-spec-discard]");
  await p.go("back");
  assert.equal((await p.state()).hash, "#task=6");
  await p.go("back");
  assert.deepEqual(await p.state(), { hash: "", list: true, task: false, y: 0 });
  assert.deepEqual(p.dialogs, ["confirm"]);
});

test("an edit with no changes is left unasked", TIMEOUT, async t => {
  const p = await tasksPage(t, { hash: "#task=3" });
  await p.click("#task-detail button[data-spec-open]");
  await p.click("#task-back");
  assert.deepEqual(p.dialogs, []);
  assert.deepEqual(await p.state(), { hash: "", list: true, task: false, y: 0 });
});

test("closing the page with an edit's changes unsaved asks", TIMEOUT, async t => {
  const p = await editing(t);
  const asked = p.page.waitForEvent("dialog");
  await p.page.close({ runBeforeUnload: true });
  assert.equal((await asked).type(), "beforeunload");
});

test("closing the page with no edit, or one with no changes, asks nothing", TIMEOUT,
     async t => {
  for (const open of [false, true]) {
    const p = await tasksPage(t, { hash: "#task=3" });
    if (open) await p.click("#task-detail button[data-spec-open]");
    // A gesture, so a page that would ask could.
    await p.page.click("#task-detail h3");
    const closed = p.page.waitForEvent("close");
    await p.page.close({ runBeforeUnload: true });
    await closed;
    assert.deepEqual(p.dialogs, []);
  }
});

// ── the moves' labels, primary and caution ─────────────────────────────────

// The detail's action controls, in order: approve's row, then the moves and the break,
// each as [main, second, marks], its marks "primary" and "caution" as drawn; with the
// line naming the holder above them and the line approve's row holds without one.
async function controls(page) {
  return page.evaluate(() => {
    const text = el => (el ? el.textContent.trim() : "");
    const buttons = [...document.querySelectorAll(
      '#task-detail button[data-write="approve"], #task-detail button[data-write="move"], '
      + '#task-detail button[data-write="break"]')];
    return {
      holder: text(document.querySelector("#task-detail [data-holder]")),
      incomplete: text(document.querySelector("#task-detail [data-approve-row] > span")),
      buttons: buttons.map(b => [text(b.querySelector(".main")),
                                 text(b.querySelector(".second")),
                                 ["primary", "caution"].filter(c => b.classList.contains(c))
                                   .join(" ")]),
    };
  });
}

const HOLDER = "claude-3f2a9c1e-0000-4000-8000-000000000001 holds this task: a state "
  + "move or a break ends the claim; an edit or a note leaves it.";
const SUPERSEDES = n => `supersedes plan ${n}; no move restores it`;
const ENDS = "ends the claim";
// The moves every state shares, by the state they reach.
const TO = {
  landing: ["mark ready to land", "", ""],
  deferred: ["defer", "", ""],
  closed: ["close the task", "", ""],
  refined: ["move to refined", "", ""],
};

// Each state's controls, worked out by hand from tasks-backend.js's tasks and the
// scheme (tasks-ui/DESIGN.md, "One scheme of labels"). `act` brings the choices to the
// case named.
const STATES = [
  ["proposed, its plan superseded", 1, null, {
    holder: "", incomplete: "", buttons: [
      ["open without a plan", "", ""], TO.landing, TO.deferred, TO.closed] }],
  ["refined with no decisions", 6, null, {
    holder: "", incomplete: "", buttons: [
      ["approve and open", "revision 7 · plan 2", "primary"],
      ["send back for a new plan", SUPERSEDES(2), "caution"],
      TO.landing, TO.deferred, TO.closed] }],
  ["refined with its decisions as assumed", 9, null, {
    holder: "", incomplete: "", buttons: [
      ["approve and open", "revision 3 · plan 4", "primary"],
      ["send back for a new plan", SUPERSEDES(4), "caution"],
      TO.landing, TO.deferred, TO.closed] }],
  ["refined with a choice that differs", 10,
   p => p.page.check('#task-detail input[data-decision="0"][data-option="1"]'), {
     holder: "", incomplete: "", buttons: [
       ["approve with these answers, sending it back",
        `revision 3 · ${SUPERSEDES(5)}`, "primary"],
       ["send back without answering", SUPERSEDES(5), "caution"],
       TO.landing, TO.deferred, TO.closed] }],
  ["refined with an answer incomplete", 10, async p => {
    await p.page.uncheck('#task-detail input[data-decision="1"][data-option="0"]');
    await p.page.uncheck('#task-detail input[data-decision="1"][data-option="1"]');
  }, {
    holder: "",
    incomplete: "No approve until every decision is answered: decision 2 not yet. A "
      + 'picked "other" needs its text.',
    buttons: [["send back without answering", SUPERSEDES(5), "caution"],
              TO.landing, TO.deferred, TO.closed] }],
  ["refined with its plan answered", 15, null, {
    holder: "", incomplete: "", buttons: [
      ["send back for a new plan", SUPERSEDES(11), "caution"],
      ["reopen", "plan 11 as approved · revision 5", "primary"],
      TO.landing, TO.deferred, TO.closed] }],
  ["open and claimed", 3, null, {
    holder: HOLDER, incomplete: "", buttons: [
      ["move to proposed", `${SUPERSEDES(2)} · ${ENDS}`, "caution"],
      ["move to refined", ENDS, "caution"], ["mark ready to land", ENDS, "caution"],
      ["defer", ENDS, "caution"], ["close the task", ENDS, "caution"],
      ["break the claim", ENDS, "caution"]] }],
  ["open, not claimed, with no plan", 2, null, {
    holder: "", incomplete: "", buttons: [
      ["move to proposed", "", ""], TO.landing, TO.deferred, TO.closed] }],
  ["landing", 5, null, {
    holder: "", incomplete: "", buttons: [
      ["move to proposed", SUPERSEDES(2), "caution"], TO.refined,
      ["move to open", "", ""], TO.deferred, ["close the task", "", "primary"]] }],
  ["deferred with no plan", 7, null, {
    holder: "", incomplete: "", buttons: [
      ["move to proposed", "", ""], ["open without a plan", "", ""], TO.landing,
      TO.closed] }],
  ["deferred with a plan approved", 16, null, {
    holder: "", incomplete: "", buttons: [
      ["move to proposed", SUPERSEDES(12), "caution"], TO.refined,
      ["reopen", "plan 12 as approved · revision 4", ""], TO.landing, TO.closed] }],
  ["deferred with a plan no approve names", 17, null, {
    holder: "", incomplete: "", buttons: [
      ["move to proposed", SUPERSEDES(13), "caution"], TO.refined,
      ["open with plan 13",
       "no approve of plan 13 found: move it to refined to approve it", ""],
      TO.landing, TO.closed] }],
  ["closed with its plan answered", 14, null, {
    holder: "", incomplete: "", buttons: [
      ["move to proposed", SUPERSEDES(10), "caution"], TO.refined,
      ["reopen", "plan 10 as approved · revision 6", ""], TO.landing, TO.deferred] }],
];

for (const [name, id, act, expected] of STATES) {
  test(`${name}: each control's words, primary and caution`, TIMEOUT, async t => {
    const p = await tasksPage(t, { hash: `#task=${id}` });
    if (act) {
      await act(p);
      await p.settle();
    }
    assert.deepEqual(await controls(p.page), expected);
  });
}

// What the classes draw, as the browser computes it: the warning colour's border on a
// caution, as drawn and under the pointer, and the accent's fill on the primary. Task 5,
// landing, has both.
test("a caution is bordered in the warning colour, hovered too, and the primary filled",
     TIMEOUT, async t => {
  const p = await tasksPage(t, { hash: "#task=5" });
  const HOLD = "rgb(210, 153, 34)";
  const ACCENT = "rgb(59, 130, 246)";
  const caution = p.page.locator('#task-detail button.caution[data-value="proposed"]');
  const primary = p.page.locator('#task-detail button.primary[data-value="closed"]');
  const style = (loc, prop) => loc.evaluate((el, name) => getComputedStyle(el)[name], prop);
  assert.equal(await style(caution, "borderTopColor"), HOLD);
  await caution.hover();
  assert.equal(await style(caution, "borderTopColor"), HOLD, "hovered");
  assert.equal(await style(primary, "backgroundColor"), ACCENT);
});

test("no control on any task reads a bare close", TIMEOUT, async t => {
  const p = await tasksPage(t);
  for (const [, id] of STATES) {
    await p.setHash(`#task=${id}`);
    const words = await p.page.locator("#task-detail button").evaluateAll(
      bs => bs.map(b => (b.querySelector(".main") || b).textContent.trim()));
    assert.ok(words.length > 0);
    assert.ok(!words.includes("close"), `task ${id}: ${words.join(", ")}`);
  }
});

// A move or an approve whose words are not its verb is named in its outcome as its
// button named it.
test("an outcome names its write by the words on its button", TIMEOUT, async t => {
  const p = await tasksPage(t, { hash: "#task=7" });
  await p.click('#task-detail button[data-write="move"][data-value="open"]');
  const outcome = () => p.page.evaluate(() => {
    const head = [...document.querySelectorAll("#task-detail h3")]
      .find(h => h.textContent === "Moves");
    return head.nextElementSibling.textContent.trim();
  });
  assert.equal(await outcome(), "Open without a plan: done.");
  await p.setHash("#task=6");
  await p.click('#task-detail button[data-verb="approve"]');
  assert.equal(await outcome(), "Approve and open: done. The task is open.");
});

// An approve that opens leaves its plan approved (`approved_at`), so moved back to
// refined it is reopened, never approved again, and the registry refuses a second one.
test("a plan approved and moved back to refined offers reopen, not approve", TIMEOUT,
     async t => {
  const p = await tasksPage(t, { hash: "#task=8" });
  await p.click('#task-detail button[data-verb="approve"]');
  await p.click('#task-detail button[data-write="move"][data-value="refined"]');
  assert.deepEqual(await controls(p.page), {
    holder: "", incomplete: "", buttons: [
      ["send back for a new plan", SUPERSEDES(2), "caution"],
      ["reopen", "plan 2 as approved · revision 4", ""],
      TO.landing, TO.deferred, TO.closed] });
  assert.equal(await p.page.locator("#task-detail [data-approve-row]").count(), 0);
  assert.match(await text(p.page.locator("#task-detail div.plan > div.qual")),
               / · approved \d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/);
  const again = backend.respond("POST", "/api/tasks/8/approve",
                                { revision: 4, answers: [] });
  assert.equal(again.status, 400);
  assert.match(again.json.detail, /approved already/);
});

test("a hidden note is unhidden, and a note's writes say they stay", TIMEOUT, async t => {
  const p = await tasksPage(t, { hash: "#task=3" });
  // Note 11 is hidden on the timeline's first page.
  const unhide = p.page.locator('#task-detail button[data-write="hide"][data-note="11"]');
  assert.equal(await text(unhide.locator(".main")), "unhide");
  assert.equal(await text(unhide.locator(".second")), "agents read it again");
  const stays = "agents read it; it can be hidden, never removed";
  assert.equal(await text(p.page.locator(
    '#task-detail button[data-write="note"] .second')), stays);
  await p.click('#task-detail button[data-reply-open="14"]');
  assert.equal(await text(p.page.locator(
    '#task-detail button[data-write="handle"][data-value="reply"][data-note="14"] .second')),
               stays);
  await p.click('#task-detail button[data-write="hide"][data-note="11"]');
  const said = await p.page.evaluate(() => {
    const timeline = [...document.querySelectorAll("#task-detail h3")]
      .find(h => h.textContent.startsWith("Timeline"));
    return timeline.previousElementSibling.textContent.trim();
  });
  assert.match(said, /^Unhide note #11: done\./);
});

test("the slot's way to the newer read says read it again", TIMEOUT, async t => {
  const p = await tasksPage(t, { hash: "#task=6" });
  backend.editElsewhere(6);
  await p.poll();
  assert.equal(await text(p.page.locator("#task-detail button[data-show]")),
               "read it again");
});

test("the save says the revision, and that the claim stays on a claimed task", TIMEOUT,
     async t => {
  const p = await tasksPage(t, { hash: "#task=12" });
  await p.click("#task-detail button[data-spec-open]");
  const save = p.page.locator('#task-detail button[data-write="edit"]');
  assert.equal(await text(save.locator(".main")), "save the edit");
  assert.equal(await text(save.locator(".second")), "revision 5 · the claim stays");
  assert.equal(await text(p.page.locator("#task-detail button[data-spec-discard]")),
               "discard the edit");
  await p.setHash("#task=11");
  await p.click("#task-detail button[data-spec-open]");
  assert.equal(await text(save.locator(".second")), "revision 2");
});

// Every state's moves, and the note's add, inside the window, and no write control in
// the detail clipping its words, at a desktop width and a phone's. The timeline's own
// note controls are left out of the first: at 390 px its table is wider than the window
// whatever they say.
for (const [name, viewport] of [["1280", { width: 1280, height: 900 }], ["390", PHONE]]) {
  test(`${name} px: every move lies in the window and no control clips its label`,
       TIMEOUT, async t => {
    const p = await tasksPage(t, { viewport });
    for (const [state, id, act] of STATES) {
      await p.setHash(`#task=${id}`);
      if (act) {
        await act(p);
        await p.settle();
      }
      const bad = await p.page.evaluate(() => {
        const width = document.documentElement.clientWidth;
        const outside = [...document.querySelectorAll(
          '#task-detail button[data-write="approve"], #task-detail button[data-write="move"], '
          + '#task-detail button[data-write="break"], #task-detail button[data-write="note"]')]
          .filter(b => {
            const r = b.getBoundingClientRect();
            return r.left < 0 || r.right > width;
          });
        const clipped = [...document.querySelectorAll("#task-detail button[data-write]")]
          .filter(b => b.getClientRects().length)
          .filter(b => b.scrollWidth > b.clientWidth
                  || [...b.children].some(c => c.scrollWidth > c.clientWidth));
        // Approve's two lines inside its row, whose height is fixed.
        const row = document.querySelector("#task-detail [data-approve-row]");
        const approve = row && row.querySelector("button");
        const spills = approve && (approve.getBoundingClientRect().top
                                   < row.getBoundingClientRect().top
                                   || approve.getBoundingClientRect().bottom
                                   > row.getBoundingClientRect().bottom);
        return [...outside.map(b => `outside: ${b.textContent.trim()}`),
                ...clipped.map(b => `clipped: ${b.textContent.trim()}`),
                ...(spills ? [`spills from its row: ${approve.textContent.trim()}`] : [])];
      });
      assert.deepEqual(bad, [], `${state}, task ${id}`);
    }
  });
}

// The boxes of every move, the break and every heading in the detail, in the page's
// coordinates, so a scroll that brings a choice into view moves none: what must stay
// put while approve is relabelled.
async function stillBoxes(page) {
  return page.evaluate(() => {
    const els = [...document.querySelectorAll(
      '#task-detail button[data-write="move"], #task-detail button[data-write="break"], '
      + "#task-detail h3")];
    return els.map(el => {
      const r = el.getBoundingClientRect();
      return [el.textContent.startsWith("Timeline") ? "Timeline" : el.dataset.value ?? "",
              r.left + window.scrollX, r.top + window.scrollY,
              el.matches("h3") ? 0 : r.width, r.height];
    });
  });
}

test("relabelling approve and send back, or an answer going incomplete and back, moves "
     + "no other box", TIMEOUT, async t => {
  for (const viewport of [DESKTOP, PHONE]) {
    const p = await tasksPage(t, { viewport, hash: "#task=10" });
    const before = await stillBoxes(p.page);
    const steps = [
      () => p.page.check('#task-detail input[data-decision="0"][data-option="1"]'),
      () => p.page.uncheck('#task-detail input[data-decision="1"][data-option="0"]'),
      () => p.page.uncheck('#task-detail input[data-decision="1"][data-option="1"]'),
      () => p.page.check('#task-detail input[data-decision="1"][data-option="0"]'),
      () => p.page.check('#task-detail input[data-decision="0"][data-option="0"]'),
    ];
    for (const [i, step] of steps.entries()) {
      await step();
      await p.settle();
      assert.deepEqual(await stillBoxes(p.page), before, `${viewport.width} px, step ${i}`);
    }
  }
});

test("the edit holding the moves moves no box", TIMEOUT, async t => {
  for (const viewport of [DESKTOP, PHONE]) {
    const p = await tasksPage(t, { viewport, hash: "#task=11" });
    await p.click("#task-detail button[data-spec-open]");
    const before = await stillBoxes(p.page);
    await p.page.fill('#task-detail input[data-spec="title"]', "Rotate");
    await p.settle();
    assert.equal(await p.page.locator('#task-detail button[data-verb="approve"]')
      .isDisabled(), true);
    assert.deepEqual(await stillBoxes(p.page), before, `${viewport.width} px`);
  }
});

// ── agent markdown ──────────────────────────────────────────────────────────

// Task 18's fields drawn as markdown, in the order the detail draws them, each as its
// drawn form's textContent and its count of hidden characters: the spelled source less
// exactly the delimiters markdownBlock drops (a heading's hashes and the space after
// them, a bullet and the space after it, emphasis and backtick delimiters, a fence's
// backticks or tildes), worked out by hand from tasks-backend.js.
const DRAWN = [
  ["the body", "Head\\u202eing ##\n"
    + "\n"
    + "A em\\u200b and strong and co\\u202ede, a [link](https://example.invalid/x).\n"
    + "![img](https://example.invalid/i.png) <img src=\"https://example.invalid/j.png\"> "
    + "<!-- a \\u200bcomment -->\n"
    + "\n"
    + "bul\\u200blet\n"
    + "  nested\n"
    + "1. one\n2. two\n5. five\\u202e\n"
    + "\n"
    + "sh\\u200b\n"
    + "echo \\u202ehi\n"
    + "\n"
    + " ascii-info\n"
    + "plain\n"
    + "\n"
    + "Line with CR LF\\u000d\n"
    + "end", 9],
  ["the question", "Which store\\u202e?", 1],
  ["why", "Because x.", 0],
  ["option 1", "sqlite", 0],
  ["its meaning", "Small.", 0],
  // A label is drawn inline only: a heading or an item in it is drawn as typed.
  ["option 2", "# not a heading\n- nor a list", 0],
  ["its meaning", "a\nb", 0],
  ["approach", "Plan\nKeep x\\u200b and y.", 1],
  ["tests", "one\ntwo", 0],
  ["out of scope", "None.", 0],
  ["risks", "None known.", 0],
  ["review verdict", "Passes.", 0],
  ["the plan's note", "Planned.", 0],
  ["the note", "lower_case\nPlan\nSee this.\nx\n\ny", 0],
];

// Every markdown field in the detail, read on its drawn form's own element, not the
// field's, which holds the source form and the note too: its textContent, its innerText
// with runs of line breaks made one (innerText adds breaks around blocks, and is what
// CSS shows, a text-transform included), the count its note gives, and whether the
// field and each form are drawn.
async function mdFields(page) {
  return page.evaluate(() => [...document.querySelectorAll("#task-detail .md-field")]
    .map(field => {
      const drawn = field.querySelector(":scope > .md");
      const source = field.querySelector(":scope > .md-source");
      const note = field.querySelector(":scope > .empty.warn");
      return { text: drawn.textContent,
               inner: drawn.innerText.replace(/\n+/g, "\n").replace(/^\n|\n$/g, ""),
               hidden: note ? Number(note.textContent.split(" ")[0]) : 0,
               shown: field.getClientRects().length > 0,
               drawn: drawn.getClientRects().length > 0,
               source: source.getClientRects().length > 0 };
    }));
}

test("each markdown field draws its spelled source less exactly its delimiters, "
     + "and counts what it spells", TIMEOUT, async t => {
  const offOrigin = [];
  const p = await tasksPage(t, { hash: "#task=18" });
  p.page.on("request", r => {
    if (!r.url().startsWith(ORIGIN) && !r.url().startsWith("data:")) offOrigin.push(r.url());
  });
  await p.click('#task-detail button[data-plan-view="whole"]');
  const fields = await mdFields(p.page);
  assert.deepEqual(fields.map(f => [f.text, f.hidden]),
                   DRAWN.map(([, drawn, hidden]) => [drawn, hidden]));
  assert.deepEqual(fields.map(f => f.inner),
                   DRAWN.map(([, drawn]) => drawn.replace(/\n+/g, "\n").replace(/\n$/, "")));
  assert.ok(fields.every(f => f.drawn && !f.source), "the drawn form shows, the source not");
  // Built of the subset's elements, and of nothing that shows or fetches anything else.
  const made = await p.page.evaluate(() => {
    const detail = document.getElementById("task-detail");
    const body = detail.querySelector(".task-body > .md");
    const comments = document.createTreeWalker(detail, NodeFilter.SHOW_COMMENT);
    return {
      missing: ["h2", "p", "ul", "ol", "li", "em", "strong", "code", "pre"]
        .filter(tag => !body.querySelector(tag)),
      foreign: [...detail.querySelectorAll("a, img, script, iframe, object, embed")]
        .map(el => el.localName),
      comment: comments.nextNode() !== null,
      markers: [...detail.querySelectorAll(".md li")]
        .map(li => getComputedStyle(li).listStyleType),
      lists: [...detail.querySelectorAll(".md :is(ul, ol)")]
        .map(l => [l.localName, l.children.length, getComputedStyle(l).paddingLeft,
                   getComputedStyle(l).marginTop]),
      labels: detail.querySelectorAll(".option-label :is(h1, li)").length,
      pre: [...body.querySelectorAll("pre")].map(el => getComputedStyle(el).whiteSpace),
      // Every drawn element's text in a colour of its own, over its background, and at
      // a size: text no colour or size can hide.
      faint: [...detail.querySelectorAll(".md, .md *")]
        .filter(el => el.getClientRects().length > 0)
        .filter(el => {
          const style = getComputedStyle(el);
          let under = "rgb(15, 17, 21)";
          for (let up = el; up; up = up.parentElement) {
            const fill = getComputedStyle(up).backgroundColor;
            if (fill !== "rgba(0, 0, 0, 0)") {
              under = fill;
              break;
            }
          }
          return style.color === under || /, 0\)$/.test(style.color)
            || !(parseFloat(style.fontSize) > 0);
        })
        .map(el => `${el.localName}.${el.className}`),
      // A bullet item shows a disc where its bullet was, which the stylesheet draws.
      discs: [...body.querySelectorAll("ul > li > .md-bullet")]
        .map(b => getComputedStyle(b, "::before").content),
    };
  });
  assert.deepEqual(made.missing, [], "every element of the subset is drawn");
  assert.deepEqual(made.foreign, []);
  assert.equal(made.comment, false);
  assert.deepEqual(made.markers, Array(11).fill("none"), "no browser marker on any item");
  // The body's bullets, then its numbers; an option's meaning; the plan's tests; the
  // note's two items, a blank line apart. None indented or spaced by the browser.
  assert.deepEqual(made.lists, [["ul", 2, "0px", "0px"], ["ol", 3, "0px", "0px"],
                                ["ul", 2, "0px", "0px"], ["ul", 2, "0px", "0px"],
                                ["ul", 1, "0px", "0px"], ["ul", 1, "0px", "0px"]]);
  assert.equal(made.labels, 0, "a label draws no heading and no list");
  assert.deepEqual(made.pre, ["pre-wrap", "pre-wrap"]);
  assert.deepEqual(made.faint, [], "no drawn text is hidden by its colour or size");
  assert.deepEqual(made.discs, ['"• "', '"• "']);
  await p.poll();
  assert.deepEqual(offOrigin, []);
});

test("an agent's heading never looks like the detail's own", TIMEOUT, async t => {
  const p = await tasksPage(t, { hash: "#task=18" });
  const sizes = await p.page.evaluate(() => {
    const own = getComputedStyle(document.querySelector("#task-detail > h3")).fontSize;
    const agents = [...document.querySelectorAll(
      "#task-detail .md-field :is(h1, h2, h3, h4, h5, h6)")];
    const text = getComputedStyle(document.querySelector("#task-detail .md p"));
    // A size of its own, apart from the detail's 14px; nothing else of the page's heading
    // rules: no capitals, spacing, dimming or margin.
    return { own, agents: agents.map(h => {
      const style = getComputedStyle(h);
      return [h.localName, h.textContent, style.fontSize, style.textTransform,
              style.letterSpacing, style.color === text.color, style.marginTop,
              style.marginBottom];
    }) };
  });
  assert.equal(sizes.own, "14px");
  assert.deepEqual(sizes.agents, [
    ["h2", "Head\\u202eing ##\n", "17px", "none", "normal", true, "0px", "0px"],
    ["h3", "Plan\n", "15px", "none", "normal", true, "0px", "0px"],
    ["h2", "lower_case\n", "17px", "none", "normal", true, "0px", "0px"],
    ["h3", "Plan\n", "15px", "none", "normal", true, "0px", "0px"],
  ]);
});

for (const [name, viewport] of [["desktop", DESKTOP], ["narrow", PHONE]]) {
  test(`${name}: show source shows every field as typed and back, keeping what was typed `
       + "and the plan's view, and its box stays put", TIMEOUT, async t => {
    const p = await tasksPage(t, { viewport, hash: "#task=18" });
    const toggle = "#task-detail button[data-source-toggle]";
    // In the page's coordinates: a click scrolls what it clicks into view.
    const box = () => p.page.locator(toggle).evaluate(el => {
      const r = el.getBoundingClientRect();
      return [r.left + window.scrollX, r.top + window.scrollY, r.width, r.height];
    });
    await p.click('#task-detail button[data-plan-view="whole"]');
    await p.click('#task-detail input[data-decision="0"][data-option="other"]');
    await p.page.fill('#task-detail textarea[data-other="0"]', "Neither: *both*.");
    await p.settle();
    const at = await box();
    await p.click(toggle);
    assert.deepEqual(await box(), at);
    assert.equal(await text(p.page.locator(toggle)), "show rendered");
    assert.ok((await mdFields(p.page)).every(f => !f.drawn && f.source),
              "the source shows, the drawn form not");
    assert.equal(await p.page.locator("#task-detail .task-body > .md-source").textContent(),
                 "## Head\\u202eing ##\n\nA *em\\u200b* and **strong** and `co\\u202ede`, a "
                 + "[link](https://example.invalid/x).\n![img](https://example.invalid/i.png) "
                 + "<img src=\"https://example.invalid/j.png\"> <!-- a \\u200bcomment -->\n\n"
                 + "- bul\\u200blet\n  - nested\n1. one\n2. two\n5. five\\u202e\n\n"
                 + "```sh\\u200b\necho \\u202ehi\n```\n~~~ ascii-info\nplain\n~~~\n"
                 + "Line with CR LF\\u000d\nend");
    await p.click(toggle);
    assert.deepEqual(await box(), at);
    assert.equal(await text(p.page.locator(toggle)), "show source");
    assert.ok((await mdFields(p.page)).every(f => f.drawn && !f.source));
    // Nothing was redrawn: the other text and the whole plan are as they were left.
    assert.equal(await p.page.inputValue('#task-detail textarea[data-other="0"]'),
                 "Neither: *both*.");
    assert.equal(await text(p.page.locator("#task-detail button[data-plan-view]")),
                 "show what changed");
    // Shown as source, it stays so through opening an edit, which redraws the detail.
    await p.click(toggle);
    await p.click("#task-detail button[data-spec-open]");
    assert.deepEqual(await box(), at);
    assert.equal(await text(p.page.locator(toggle)), "show rendered");
    // The plan is redrawn on what changed, so its whole view's fields are not shown.
    const redrawn = (await mdFields(p.page)).filter(f => f.shown);
    assert.equal(redrawn.length, 8);
    assert.ok(redrawn.every(f => !f.drawn && f.source));
  });
}
