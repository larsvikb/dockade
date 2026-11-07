// SPDX-License-Identifier: Apache-2.0
// Runs a page's scenario against one copy of it in a headless browser and writes a
// transcript: after every step, the requests the page sent, what reached its console,
// and its DOM. There are no expected values here. `make ui-diff` runs this on the base
// tree and on the branch and diffs the two transcripts, so the oracle is the previous
// version of the page; approval.test.js, under `make ui-test`, is the one with expected
// values. For two runs of one page to read the same, the page's API requests are
// answered one at a time in the order it made them, except a read the backend names to
// answer last, never while the clock moves, and a step ends only once the page is quiet
// (answerQueue, in page.js).
//
//   node harness.js <served dir> <transcript out> [approval|tasks]
//
// The served dir holds the page as its image does: the UI's index.html and modules,
// and ui-common's modules beside them (`make ui-diff` assembles it). The page picks the
// fake upstream and the steps: backend.js and scenario.js for the approval page
// (control-plane-ui), tasks-backend.js and tasks-scenario.js for the tasks page.

import { writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { BROWSER, ORIGIN, launch, openPage } from "./page.js";

const PAGES = {
  approval: ["./backend.js", "./scenario.js"],
  tasks: ["./tasks-backend.js", "./tasks-scenario.js"],
};
const [uiDir, outFile, which = "approval"] = process.argv.slice(2);
if (!uiDir || !outFile || !PAGES[which]) {
  console.error("usage: node harness.js <served dir> <transcript out> [approval|tasks]");
  process.exit(2);
}
const backend = await import(PAGES[which][0]);
const { STEPS } = await import(PAGES[which][1]);
if (!existsSync(BROWSER)) {
  console.error(`no browser at ${BROWSER}. The tier-1 sandbox image has one; elsewhere, `
                + "point UI_HARNESS_BROWSER at a Chromium or headless-shell binary.");
  process.exit(2);
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
  const browser = await launch();
  // A reload replaces the page's scripts, and V8 forgets the counts of the ones it
  // replaced, so what the page ran so far is taken before each navigation and merged
  // at the end.
  const coverage = [];
  let page = null;
  const run = await openPage(browser, uiDir, backend, {
    beforeNavigate: async () => {
      coverage.push(...await page.coverage.stopJSCoverage());
      await page.coverage.startJSCoverage({ resetOnNavigation: false });
    },
  });
  ({ page } = run);
  await page.coverage.startJSCoverage({ resetOnNavigation: false });

  // A failed step ends the run but still writes what it saw: the page error that broke
  // the step is the line the diff most needs to show.
  const out = [];
  let failed = false;
  for (const [i, [name, act]] of STEPS.entries()) {
    run.beginStep();
    for (const step of [() => act(run.helpers), () => run.queue.settle()]) {
      try {
        await step();
      } catch (e) {
        failed = true;
        run.note(`step failed: ${e.message.split("\n")[0]}`);
      }
    }
    // The snapshot first, so a line logged while it is taken lands in this step.
    const dom = await page.evaluate(snapshot);
    const { log, files, unanswered } = run.take();
    out.push(`== step ${String(i + 1).padStart(2, "0")}: ${name}`,
             ...(files.length ? ["-- files", ...files.sort()] : []),
             ...(unanswered.length ? ["-- unanswered", ...unanswered.sort()] : []),
             "-- events", ...log, "-- dom", dom, "");
    if (failed) break;
  }
  process.exitCode = failed ? 1 : 0;
  await run.queue.stop();
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
