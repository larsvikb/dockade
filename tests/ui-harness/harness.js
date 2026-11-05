// SPDX-License-Identifier: Apache-2.0
// Runs a page's scenario against one copy of it in a headless browser and writes a
// transcript: after every step, the requests the page sent, what reached its console,
// and its DOM. There are no expected values. `make ui-diff` runs this on the base tree
// and on the branch and diffs the two transcripts, so the oracle is the previous
// version of the page. For two runs of one page to read the same, the page's API
// requests are answered one at a time in the order it made them, never while the
// clock moves, and a step ends only once the page is quiet (answerQueue).
//
//   node harness.js <served dir> <transcript out> [approval|tasks]
//
// The served dir holds the page as its image does: the UI's index.html and modules,
// and ui-common's modules beside them (`make ui-diff` assembles it). The page picks the
// fake upstream and the steps: backend.js and scenario.js for the approval page
// (control-plane-ui), tasks-backend.js and tasks-scenario.js for the tasks page.

import { readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright-core";
import { NOW_MS } from "./backend.js";

// Loopback, as the real UI is served, so the page is a secure context and the
// notification button takes the branch an operator's browser takes. Nothing listens
// here: every request is answered by the route below.
const ORIGIN = "http://127.0.0.1:8090";
const BROWSER = process.env.UI_HARNESS_BROWSER || "/usr/lib/chromium/chromium-headless-shell";

const PAGES = {
  approval: ["./backend.js", "./scenario.js"],
  tasks: ["./tasks-backend.js", "./tasks-scenario.js"],
};
const [uiDir, outFile, which = "approval"] = process.argv.slice(2);
if (!uiDir || !outFile || !PAGES[which]) {
  console.error("usage: node harness.js <served dir> <transcript out> [approval|tasks]");
  process.exit(2);
}
const { respond } = await import(PAGES[which][0]);
const { STEPS } = await import(PAGES[which][1]);
if (!existsSync(BROWSER)) {
  console.error(`no browser at ${BROWSER}. The tier-1 sandbox image has one; elsewhere, `
                + "point UI_HARNESS_BROWSER at a Chromium or headless-shell binary.");
  process.exit(2);
}

// Installed before the page's modules load. A route can answer a request but cannot
// hold a stream open, so the approvals feed is the one thing faked inside the page.
function fakeEventSource() {
  const open = [];
  class FakeEventSource extends EventTarget {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 2;
    constructor(url) {
      super();
      this.url = url;
      this.readyState = FakeEventSource.CONNECTING;
      this.onopen = null;
      this.onerror = null;
      open.push(this);
      queueMicrotask(() => {
        this.readyState = FakeEventSource.OPEN;
        if (this.onopen) this.onopen(new Event("open"));
      });
    }
    close() { this.readyState = FakeEventSource.CLOSED; }
  }
  window.EventSource = FakeEventSource;
  window.__harnessPush = data => {
    for (const es of open) {
      if (es.readyState !== FakeEventSource.OPEN) continue;
      es.dispatchEvent(new MessageEvent("pending", { data: JSON.stringify(data) }));
    }
  };
  // The stream ending in CLOSED, as a 502 from the relay leaves it, which the page
  // must reconnect from by itself.
  window.__harnessDrop = () => {
    for (const es of open) {
      if (es.readyState !== FakeEventSource.OPEN) continue;
      es.readyState = FakeEventSource.CLOSED;
      if (es.onerror) es.onerror(new Event("error"));
    }
  };
}

// Installed in every document before its modules load, beside fakeEventSource. Each
// fetch carries this document's id and its place in the order the page asked, which is
// how the route answers in that order; and the document keeps count of the network work
// it has not finished: a fetch until it settles, a body read until it settles. A body
// the page never reads adds nothing. A navigation starts a new document, so the stamps
// start again from 0 under a new id.
function stampFetches() {
  const doc = Math.random().toString(36).slice(2);
  let next = 0;
  const fetches = new Set();
  let reads = 0;
  const realFetch = window.fetch;
  window.fetch = function (input, init) {
    const seq = next++;
    let request;
    try {
      request = new Request(input, init);
      request.headers.set("x-harness-stamp", `${doc} ${seq}`);
    } catch (e) {
      return Promise.reject(e);
    }
    fetches.add(seq);
    return realFetch.call(window, request).finally(() => { fetches.delete(seq); });
  };
  for (const name of ["json", "text", "arrayBuffer", "blob", "formData"]) {
    const read = Response.prototype[name];
    Response.prototype[name] = function () {
      reads++;
      return read.call(this).finally(() => { reads--; });
    };
  }
  window.__harnessQueue = () => ({ doc, fetches: [...fetches], reads, now: Date.now() });
}

// The page's API requests, held at the route and answered one at a time, lowest stamp
// first, and none while time moves or the page navigates: so two runs see the same
// answers in the same order at the same fake time, whatever the network does.
// `answer(route, line, now)` answers one and logs its line with the page's fake time;
// `logLine` is the step's log, and `logAside` takes what the page asked for and never got
// an answer to, whose moment depends on the network. A step that cannot go quiet within
// `boundMs` fails.
//
// Quiet is every fetch the page has pending held here and no body read pending, at two
// looks LOOK_MS apart: the page's timers are frozen, so the only work it can still be
// doing is handling an answer, and the second look covers the turns between a body read
// and the fetch the handler sends next. The pump answers the lowest stamp each time the
// page is quiet, and `idle()` waits for quiet with nothing it may answer.
const LOOK_MS = 25;
function answerQueue(page, answer, logLine, logAside, { boundMs = 20000 } = {}) {
  const held = new Map();
  const retired = new Set();
  let doc = null;
  let unstamped = 0;
  let paused = 0;   // nested pauses: answers resume when the outermost ends
  let stopped = false;
  let lookNo = 0;
  let quietRun = 0;
  let lastLook = null;
  const waiters = [];
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // Stamps belong to one document. A navigation leaves the old one's requests
  // unanswerable, so they leave the queue, whether they reached it before the new
  // document did or after. An unstamped request stays, to fail its step.
  function dropRetired(route, line) {
    logAside(line);
    route.abort().catch(() => {});
  }
  function adopt(next) {
    if (next === doc) return;
    if (doc !== null) retired.add(doc);
    for (const h of [...held.values()]) {
      if (h.doc === next || h.doc === null) continue;
      held.delete(h.key);
      dropRetired(h.route, h.line);
    }
    doc = next;
  }

  function hold(route, line) {
    // A request arriving is the page at work, so its quiet starts again from here. A
    // conservative margin: nothing depends on it, since a step that means a request to
    // go unanswered makes it inside `navigate`.
    quietRun = 0;
    const stamp = route.request().headers()["x-harness-stamp"];
    if (!stamp) {
      // Not a fetch, so the page does not count it pending and it is never released:
      // `idle()` turns it into a failed step that names it, rather than a guess at
      // where in the order it belongs.
      const key = `unstamped ${unstamped++}`;
      held.set(key, { key, doc: null, seq: Infinity, route, line });
      return;
    }
    const [from, seq] = stamp.split(" ");
    if (retired.has(from)) return dropRetired(route, line);
    adopt(from);
    const key = `${from} ${seq}`;
    held.set(key, { key, doc: from, seq: Number(seq), route, line });
  }

  async function look() {
    let q;
    try {
      q = await page.evaluate(() => window.__harnessQueue?.() ?? null);
    } catch {
      return false;   // mid-navigation: the document it asked went away
    }
    lastLook = q;
    if (!q) return held.size === 0;
    adopt(q.doc);
    return q.reads === 0 && q.fetches.every(seq => held.has(`${q.doc} ${seq}`));
  }

  async function release(h) {
    held.delete(h.key);
    try {
      await answer(h.route, h.line, lastLook.now);
    } catch (e) {
      logLine(`  ^ the answer was not delivered: ${e.message.split("\n")[0]}`);
    }
  }

  async function pump() {
    while (!stopped) {
      await sleep(LOOK_MS);
      lookNo++;
      quietRun = await look() ? quietRun + 1 : 0;
      if (quietRun < 2) continue;
      const next = paused > 0 ? null
        : [...held.values()].filter(h => h.doc === doc).sort((a, b) => a.seq - b.seq)[0];
      if (next) {
        quietRun = 0;
        await release(next);
        continue;
      }
      for (const w of [...waiters]) {
        const quietSinceCall = Math.min(quietRun, lookNo - w.from);
        if (quietSinceCall >= 2 && (!w.empty || held.size === 0)) {
          waiters.splice(waiters.indexOf(w), 1);
          w.resolve();
        }
      }
    }
  }

  // Quiet, with a run of looks that began after the call; `empty` also wants nothing
  // held. Past the bound it throws, naming what is held, so a request the queue cannot
  // place fails its step instead of hanging the run.
  function idle({ empty }) {
    return new Promise((resolve, reject) => {
      const w = { from: lookNo, empty, resolve: () => { clearTimeout(timer); resolve(); } };
      const timer = setTimeout(() => {
        waiters.splice(waiters.indexOf(w), 1);
        const lines = [...held.values()].map(h => h.line);
        const inPage = lastLook
          ? `fetches ${JSON.stringify(lastLook.fetches)}, body reads ${lastLook.reads}`
          : "no fetch wrapper";
        reject(new Error(`the page did not go quiet in ${boundMs} ms; held: `
                         + `${lines.join(", ") || "nothing"}; pending in the page: ${inPage}`));
      }, boundMs);
      waiters.push(w);
    });
  }

  async function withoutAnswers(run) {
    paused++;
    try {
      await run();
    } finally {
      paused--;
    }
  }

  const running = pump();
  return {
    hold,
    // A step ends here: every answer the page asked for given, and the page done with it.
    settle: () => idle({ empty: true }),
    // Time moves only once every answer asked for before it is in, and nothing is
    // answered until it stops. Playwright's runFor yields a real task turn after each
    // timer it fires, so an answer let through mid-run would land at whatever fake time
    // the run had reached.
    moveTime: async run => {
      await idle({ empty: true });
      return withoutAnswers(run);
    },
    // A navigation, with nothing answered from the start of `run` until it is done, so
    // what the old document asked for in `run` goes unanswered by rule, and how fast
    // the new one loads decides nothing.
    navigate: withoutAnswers,
    // Every request the page has made held here, and none of its work left: inside
    // `navigate`, so a request reaches the queue before the browser can cancel it.
    allHeld: () => idle({ empty: false }),
    stop: async () => { stopped = true; await running; },
  };
}

// The DOM as indented lines, one element or text run per line. Properties a script
// sets without touching an attribute (a field's value, a checkbox or a radio button)
// are read off the element, since the attributes alone would not show them.
function snapshot() {
  const lines = [`title: ${document.title}`,
                 `favicon: ${document.getElementById("favicon")?.href ?? "none"}`,
                 `focus: ${describe(document.activeElement)}`];
  function describe(el) {
    if (!el) return "none";
    return el.tagName.toLowerCase() + (el.id ? `#${el.id}` : "")
      + [...el.classList].map(c => `.${c}`).join("");
  }
  function walk(node, depth) {
    for (const child of node.childNodes) {
      const pad = "  ".repeat(depth);
      if (child.nodeType === Node.TEXT_NODE) {
        const text = child.textContent.replace(/\s+/g, " ").trim();
        if (text) lines.push(pad + JSON.stringify(text));
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE || child.tagName === "SCRIPT") continue;
      const parts = [...child.attributes]
        .filter(a => a.name !== "id" && a.name !== "class")
        .map(a => `${a.name}=${JSON.stringify(a.value)}`);
      if (["INPUT", "SELECT", "TEXTAREA"].includes(child.tagName)) {
        parts.push(`.value=${JSON.stringify(child.value)}`);
      }
      if (child.type === "checkbox" || child.type === "radio") {
        parts.push(`.checked=${child.checked}`);
      }
      lines.push(pad + [describe(child), ...parts].join(" "));
      walk(child, depth + 1);
    }
  }
  walk(document.body, 0);
  return lines.join("\n");
}

async function main() {
  const browser = await chromium.launch({
    executablePath: BROWSER,
    // Why each, in the comment over claude-sandbox/Dockerfile's browser layer.
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--proxy-server=direct://"],
  });
  // A zone with an offset, and a half-hour one with no DST: in UTC, local time and UTC
  // read the same, so a move that mixed them up would diff clean (NOTES.md,
  // "`toLocaleString()` renders one instant six ways").
  const context = await browser.newContext({
    timezoneId: "Asia/Kolkata", locale: "en-GB", viewport: { width: 1280, height: 900 },
  });
  const page = await context.newPage();
  // A step whose element is not there fails the run in seconds rather than in 30.
  page.setDefaultTimeout(5000);
  await page.coverage.startJSCoverage({ resetOnNavigation: false });
  const coverage = [];
  let loaded = false;

  let log = [];
  // The page's own files, and anything off its origin: answered at once, outside the
  // queue, so the order they arrive in is the network's. Each step lists them sorted.
  let files = [];
  // What the page asked for and never got, because it navigated away first. When the
  // queue learns of it depends on the network too, so it is listed sorted as well.
  let unanswered = [];
  page.on("console", m => log.push(`console.${m.type()}: ${m.text()}`));
  page.on("pageerror", e => log.push(`pageerror: ${e.message}`));
  // Dismissed unless the step running has called `accept()`, so no confirm-guarded
  // write happens by accident, and a step's answer never carries into the next.
  let accepting = false;
  page.on("dialog", d => {
    log.push(`dialog.${d.type()} ${accepting ? "accepted" : "dismissed"}: ${d.message()}`);
    return accepting ? d.accept() : d.dismiss();
  });

  // UI_HARNESS_JITTER=<ms> delays every request on its way into the queue, and every
  // answer, by a random 0 to <ms>: a network the transcript must not show, since the
  // queue decides the order.
  const jitterMs = Number(process.env.UI_HARNESS_JITTER) || 0;
  const jitter = () => jitterMs ? new Promise(r => setTimeout(r, Math.random() * jitterMs))
    : null;

  const queue = answerQueue(page, async (route, line, now) => {
    await jitter();
    log.push(`${line} @+${now - NOW_MS}ms`);
    const req = route.request();
    const url = new URL(req.url());
    const body = req.postData();
    const answer = respond(req.method(), url.pathname, body ? JSON.parse(body) : null,
                           url.searchParams);
    if (!answer) {
      log.push(`  ^ no fixture answers this; 404`);
      return await route.fulfill({ status: 404, json: { detail: "no fixture" } });
    }
    // A request the network drops, so the page's `fetch` throws.
    if (answer.abort) return await route.abort();
    await route.fulfill({ status: answer.status, json: answer.json });
  }, line => log.push(line), line => unanswered.push(line));

  await page.route("**/*", async route => {
    const req = route.request();
    const url = new URL(req.url());
    const body = req.postData();
    const line = `${req.method()} ${url.pathname}${url.search}${body ? " " + body : ""}`;
    const file = url.pathname === "/" ? "index.html"
      : /^\/[\w-]+\.js$/.test(url.pathname) ? url.pathname.slice(1) : null;
    if (url.origin === ORIGIN && !(file && existsSync(path.join(uiDir, file)))) {
      await jitter();
      return queue.hold(route, line);
    }
    files.push(line);
    await jitter();
    if (url.origin !== ORIGIN) return await route.abort();
    await route.fulfill({
      contentType: file.endsWith(".js") ? "text/javascript" : "text/html",
      body: await readFile(path.join(uiDir, file)),
    });
  });

  // Frozen, so countdowns and stamps read the same in both runs; it moves only when a
  // step says so. `install` alone lets time flow at wall-clock pace, and the runs
  // drifted apart by a second; `pauseAt` is what stops it.
  await page.clock.install({ time: NOW_MS - 1000 });
  await page.clock.pauseAt(NOW_MS);
  await page.addInitScript(fakeEventSource);
  await page.addInitScript(stampFetches);

  const helpers = {
    page,
    push: holds => page.evaluate(h => window.__harnessPush({ holds: h }), holds),
    drop: () => page.evaluate(() => window.__harnessDrop()),
    clock: ms => queue.moveTime(() => page.clock.runFor(ms)),
    // For a step that checks what the page did with its answers before it ends.
    settle: () => queue.settle(),
    // Every navigation goes through here: `go` and whatever it does first run with no
    // answer given, so a request the page makes in it goes unanswered by rule. A reload
    // replaces the page's scripts, and V8 forgets the counts of the ones it replaced,
    // so what the page ran so far is taken first and merged at the end.
    navigate: async go => {
      if (loaded) {
        coverage.push(...await page.coverage.stopJSCoverage());
        await page.coverage.startJSCoverage({ resetOnNavigation: false });
      }
      loaded = true;
      await queue.navigate(go);
    },
    // The page from the top; `before` acts on the old document inside the navigation.
    // Answers are paused there, so `before` must not call `clock` or `settle`: either
    // waits for an empty queue, and fails at the bound if `before` left anything held.
    load: before => helpers.navigate(async () => {
      if (before) {
        await before();
        await queue.allHeld();
      }
      await page.goto(ORIGIN + "/");
    }),
    accept: () => { accepting = true; },
  };

  // A failed step ends the run but still writes what it saw: the page error that broke
  // the step is the line the diff most needs to show.
  const out = [];
  let failed = false;
  for (const [i, [name, act]] of STEPS.entries()) {
    accepting = false;
    for (const run of [() => act(helpers), () => queue.settle()]) {
      try {
        await run();
      } catch (e) {
        failed = true;
        log.push(`step failed: ${e.message.split("\n")[0]}`);
      }
    }
    // The snapshot first, so a line logged while it is taken lands in this step.
    const dom = await page.evaluate(snapshot);
    out.push(`== step ${String(i + 1).padStart(2, "0")}: ${name}`,
             ...(files.length ? ["-- files", ...files.sort()] : []),
             ...(unanswered.length ? ["-- unanswered", ...unanswered.sort()] : []),
             "-- events", ...log, "-- dom", dom, "");
    log = [];
    files = [];
    unanswered = [];
    if (failed) break;
  }
  process.exitCode = failed ? 1 : 0;
  await queue.stop();
  coverage.push(...await page.coverage.stopJSCoverage());
  await browser.close();
  await writeFile(outFile, out.join("\n"));
  for (const line of coverageReport(coverage)) console.log(line);
}

// Which of each module's functions the scenario called, and every one it never did,
// by name and line: how much of the page the transcript can speak for. Function level,
// from V8's precise coverage, where a function's first range carries its call count.
// The module body itself, the function at offset 0, is skipped. V8 does not
// list a function nested in one that never ran, so the total is a floor and the
// "never" list names only the outermost of each uncalled nest. A script loaded more
// than once, across a reload, is one module: its functions' counts are summed.
function coverageReport(entries) {
  const lines = [];
  const byUrl = new Map();
  for (const e of entries) {
    if (!e.url.startsWith(ORIGIN) || !e.url.endsWith(".js")) continue;
    const merged = byUrl.get(e.url) || { url: e.url, source: e.source, fns: new Map() };
    for (const f of e.functions) {
      const start = f.ranges[0].startOffset;
      if (start === 0) continue;
      const key = `${start}:${f.ranges[0].endOffset}`;
      const seen = merged.fns.get(key);
      merged.fns.set(key, { functionName: f.functionName, start,
                            count: (seen ? seen.count : 0) + f.ranges[0].count });
    }
    byUrl.set(e.url, merged);
  }
  const ours = [...byUrl.values()].sort((a, b) => a.url.localeCompare(b.url));
  for (const e of ours) {
    const lineAt = offset => e.source.slice(0, offset).split("\n").length;
    const fns = [...e.fns.values()].sort((a, b) => a.start - b.start);
    const called = fns.filter(f => f.count > 0);
    const missed = fns.filter(f => !f.count)
      .map(f => `${f.functionName || "(anonymous)"}:${lineAt(f.start)}`);
    lines.push(`${new URL(e.url).pathname.slice(1)}: ${called.length} of ${fns.length}`
               + " listed functions called" + (missed.length ? "; never:" : ""));
    for (let i = 0; i < missed.length; i += 6) {
      lines.push("  " + missed.slice(i, i + 6).join(" "));
    }
  }
  return lines;
}

await main();
