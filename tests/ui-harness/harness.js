// SPDX-License-Identifier: Apache-2.0
// Runs scenario.js against one copy of control-plane-ui in a headless browser and
// writes a transcript: after every step, the requests the page sent, what reached its
// console, and its DOM. There are no expected values. `make ui-diff` runs this on the
// base tree and on the branch and diffs the two transcripts, so the oracle is the
// previous version of the page.
//
//   node harness.js <control-plane-ui dir> <transcript out>

import { readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright-core";
import { NOW_MS, respond } from "./backend.js";
import { STEPS } from "./scenario.js";

// Loopback, as the real UI is served, so the page is a secure context and the
// notification button takes the branch an operator's browser takes. Nothing listens
// here: every request is answered by the route below.
const ORIGIN = "http://127.0.0.1:8090";
const BROWSER = process.env.UI_HARNESS_BROWSER || "/usr/lib/chromium/chromium-headless-shell";

const [uiDir, outFile] = process.argv.slice(2);
if (!uiDir || !outFile) {
  console.error("usage: node harness.js <control-plane-ui dir> <transcript out>");
  process.exit(2);
}
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
}

// The DOM as indented lines, one element or text run per line. Properties a script
// sets without touching an attribute (a field's value, a checkbox) are read off the
// element, since the attributes alone would not show them.
function snapshot() {
  const lines = [`title: ${document.title}`,
                 `favicon: ${document.getElementById("favicon").href}`,
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
      if (child.type === "checkbox") parts.push(`.checked=${child.checked}`);
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

  let log = [];
  let inFlight = 0;
  page.on("console", m => log.push(`console.${m.type()}: ${m.text()}`));
  page.on("pageerror", e => log.push(`pageerror: ${e.message}`));
  page.on("dialog", d => { log.push(`dialog.${d.type()}: ${d.message()}`); d.dismiss(); });

  await page.route("**/*", async route => {
    inFlight++;
    try {
      const req = route.request();
      const url = new URL(req.url());
      const body = req.postData();
      log.push(`${req.method()} ${url.pathname}${url.search}${body ? " " + body : ""}`);
      if (url.origin !== ORIGIN) return await route.abort();
      const file = url.pathname === "/" ? "index.html"
        : /^\/[\w-]+\.js$/.test(url.pathname) ? url.pathname.slice(1) : null;
      if (file && existsSync(path.join(uiDir, file))) {
        return await route.fulfill({
          contentType: file.endsWith(".js") ? "text/javascript" : "text/html",
          body: await readFile(path.join(uiDir, file)),
        });
      }
      const answer = respond(req.method(), url.pathname, body ? JSON.parse(body) : null);
      if (!answer) {
        log.push(`  ^ no fixture answers this; 404`);
        return await route.fulfill({ status: 404, json: { detail: "no fixture" } });
      }
      await route.fulfill({ status: answer.status, json: answer.json });
    } finally {
      inFlight--;
    }
  });

  // Frozen, so countdowns and stamps read the same in both runs; it moves only when a
  // step says so. `install` alone lets time flow at wall-clock pace, and the runs
  // drifted apart by a second; `pauseAt` is what stops it.
  await page.clock.install({ time: NOW_MS - 1000 });
  await page.clock.pauseAt(NOW_MS);
  await page.addInitScript(fakeEventSource);

  // Quiet means no request in flight across two looks a little apart. The page's own
  // timers are frozen, so whatever is still running is a response being handled.
  const settle = async () => {
    for (let quiet = 0; quiet < 2;) {
      await new Promise(r => setTimeout(r, 25));
      quiet = inFlight === 0 ? quiet + 1 : 0;
    }
  };
  const helpers = {
    page,
    push: holds => page.evaluate(h => window.__harnessPush({ holds: h }), holds),
    clock: ms => page.clock.runFor(ms),
    load: () => page.goto(ORIGIN + "/"),
  };

  // A failed step ends the run but still writes what it saw: the page error that broke
  // the step is the line the diff most needs to show.
  const out = [];
  let failed = false;
  for (const [i, [name, act]] of STEPS.entries()) {
    try {
      await act(helpers);
    } catch (e) {
      failed = true;
      log.push(`step failed: ${e.message.split("\n")[0]}`);
    }
    await settle();
    out.push(`== step ${String(i + 1).padStart(2, "0")}: ${name}`,
             "-- events", ...log, "-- dom", await page.evaluate(snapshot), "");
    log = [];
    if (failed) break;
  }
  process.exitCode = failed ? 1 : 0;
  const coverage = await page.coverage.stopJSCoverage();
  await browser.close();
  await writeFile(outFile, out.join("\n"));
  for (const line of coverageReport(coverage)) console.log(line);
}

// Which of each module's functions the scenario called, and every one it never did,
// by name and line: how much of the page the transcript can speak for. Function level,
// from V8's precise coverage, where a function's first range carries its call count.
// A script's first function is the module body itself, so it is skipped. V8 does not
// list a function nested in one that never ran, so the total is a floor and the
// "never" list names only the outermost of each uncalled nest.
function coverageReport(entries) {
  const lines = [];
  const ours = entries.filter(e => e.url.startsWith(ORIGIN) && e.url.endsWith(".js"))
    .sort((a, b) => a.url.localeCompare(b.url));
  for (const e of ours) {
    const lineAt = offset => e.source.slice(0, offset).split("\n").length;
    const fns = e.functions.slice(1);
    const called = fns.filter(f => f.ranges[0].count > 0);
    const missed = fns.filter(f => !f.ranges[0].count)
      .map(f => `${f.functionName || "(anonymous)"}:${lineAt(f.ranges[0].startOffset)}`);
    lines.push(`${new URL(e.url).pathname.slice(1)}: ${called.length} of ${fns.length}`
               + " listed functions called" + (missed.length ? "; never:" : ""));
    for (let i = 0; i < missed.length; i += 6) {
      lines.push("  " + missed.slice(i, i + 6).join(" "));
    }
  }
  return lines;
}

await main();
