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
                 "#6 Refined one edit spec");
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
       + "show it keeps the scroll", TIMEOUT, async t => {
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
    assert.match(await text(p.page.locator(".notice-slot")), /^show it/);
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
