// SPDX-License-Identifier: Apache-2.0
// A page of the UI in a headless browser, set up so that what it does can be read the
// same way every time: its API requests answered by a fake backend one at a time in
// the order it made them, its clock frozen until a step moves it, and its approvals
// feed faked inside the page. harness.js runs a scenario on it and writes a transcript;
// approval.test.js and tasks.test.js assert on it. None repeats any of this.
//
//   const browser = await launch();
//   const p = await openPage(browser, servedDir, { respond, answeredLast });
//   await p.helpers.load(); ...; await p.close();

import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { NOW_MS } from "./backend.js";

// Loopback, as the real UI is served, so the page is a secure context and the
// notification button takes the branch an operator's browser takes. Nothing listens
// here: every request is answered by the route below.
export const ORIGIN = "http://127.0.0.1:8090";
export const BROWSER = process.env.UI_HARNESS_BROWSER
  || "/usr/lib/chromium/chromium-headless-shell";

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
// Request order alone hides a page bug that depends on the order answers arrive in, so
// `last(method, path)` names requests to answer after every other one held: the backend's
// `answeredLast`. Among those, lowest stamp first again.
//
// Quiet is every fetch the page has pending held here and no body read pending, at two
// looks LOOK_MS apart: the page's timers are frozen, so the only work it can still be
// doing is handling an answer, and the second look covers the turns between a body read
// and the fetch the handler sends next. The pump answers the lowest (last, stamp) each
// time the page is quiet, and `idle()` waits for quiet with nothing it may answer.
const LOOK_MS = 25;
function answerQueue(page, answer, logLine, logAside,
                     { boundMs = 20000, last = () => false } = {}) {
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
      held.set(key, { key, doc: null, seq: Infinity, last: false, route, line });
      return;
    }
    const [from, seq] = stamp.split(" ");
    if (retired.has(from)) return dropRetired(route, line);
    adopt(from);
    const key = `${from} ${seq}`;
    const req = route.request();
    const isLast = Boolean(last(req.method(), new URL(req.url()).pathname));
    held.set(key, { key, doc: from, seq: Number(seq), last: isLast, route, line });
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
        : [...held.values()].filter(h => h.doc === doc)
          .sort((a, b) => (a.last - b.last) || (a.seq - b.seq))[0];
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

// playwright-core is imported here rather than at the top, so `make ui-test` can read
// BROWSER from this module before the install it might skip.
export async function launch() {
  const { chromium } = await import("playwright-core");
  return chromium.launch({
    executablePath: BROWSER,
    // Why each, in the comment over claude-sandbox/Dockerfile's browser layer.
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--proxy-server=direct://"],
  });
}

// A new context on `browser` with the page in it, served from `uiDir` and answered by
// `backend`: `respond(method, path, body, query)` and optionally `answeredLast(method,
// path)`, as backend.js exports them. `beforeNavigate` runs at the start of every
// navigation but the first, before anything of it happens. `viewport` sizes the window
// (1280 by 900 unless given), and `hash` is what `load` opens the page at ("" unless
// given), so a test can load on a URL a link or a reload would.
//
// What the page did, since the last `take()`: `log` the events in order, `files` what
// it loaded outside the queue and `unanswered` what it asked for and never got, both in
// the network's order.
export async function openPage(browser, uiDir, { respond, answeredLast = () => false },
                               { beforeNavigate = async () => {},
                                 viewport = { width: 1280, height: 900 },
                                 hash = "" } = {}) {
  // A zone with an offset, and a half-hour one with no DST: in UTC, local time and UTC
  // read the same, so a move that mixed them up would diff clean (NOTES.md,
  // "`toLocaleString()` renders one instant six ways").
  const context = await browser.newContext({
    timezoneId: "Asia/Kolkata", locale: "en-GB", viewport,
  });
  const page = await context.newPage();
  // A step whose element is not there fails the run in seconds rather than in 30.
  page.setDefaultTimeout(5000);
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
  }, line => log.push(line), line => unanswered.push(line), { last: answeredLast });

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
    // answer given, so a request the page makes in it goes unanswered by rule.
    navigate: async go => {
      if (loaded) await beforeNavigate();
      loaded = true;
      await queue.navigate(go);
    },
    // The page from the top, at `hash`; `before` acts on the old document inside the
    // navigation. Answers are paused there, so `before` must not call `clock` or
    // `settle`: either waits for an empty queue, and fails at the bound if `before`
    // left anything held.
    load: before => helpers.navigate(async () => {
      if (before) {
        await before();
        await queue.allHeld();
      }
      await page.goto(ORIGIN + "/" + hash);
    }),
    accept: () => { accepting = true; },
  };

  return {
    context, page, queue, helpers,
    // A step starts with dialogs dismissed, so its `accept()` never carries over.
    beginStep: () => { accepting = false; },
    note: line => log.push(line),
    take: () => {
      const seen = { log, files, unanswered };
      log = [];
      files = [];
      unanswered = [];
      return seen;
    },
    close: async () => {
      await queue.stop();
      await context.close();
    },
  };
}
