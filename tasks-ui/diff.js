// SPDX-License-Identifier: Apache-2.0
/* What changed between two texts, as runs the page draws: the same, added and removed.
 * A shortest edit script over words (Myers, "An O(ND) Difference Algorithm"), or over
 * lines for a list such as a footprint. Equality is on the raw text, so a change only a
 * hidden character makes is a change here; the page spells it where it draws the run.
 *
 * Imports nothing and draws nothing, so the unit tests import it under node
 * (tests/test_tasks_ui_js.py).
 */

// The most edits a diff looks for before it calls the text rewritten. The search costs
// time in the texts' length times the edits found, and memory in the edits squared, so
// this is what keeps agent text from hanging the tab; a plan's caps
// (task-registry/plans.py) bound the length.
export const EDIT_MAX = 1000;

// Words and the whitespace between them, each a token, so a change to the spacing is
// a change too.
export function wordDiff(before, after) {
  return diff(tokens(before, /\s+|\S+/g), tokens(after, /\s+|\S+/g));
}

// One token a line, as a footprint is one path a line. Each line carries its break, so
// runs drawn one after another keep their lines, and the last line is the same line
// whether a break follows it or not.
export function lineDiff(before, after) {
  const lines = text => (String(text ?? "") === "" ? []
    : String(text).replace(/\n$/, "").split("\n").map(line => `${line}\n`));
  return diff(lines(before), lines(after));
}

function tokens(text, re) {
  return String(text ?? "").match(re) || [];
}

// `{ runs, rewritten }`: each run `{ op: "same" | "add" | "del", text }`, consecutive
// tokens of one op joined; past EDIT_MAX, the whole of each side, rewritten.
function diff(a, b) {
  const ops = editScript(a, b);
  if (ops === null) {
    return { runs: [{ op: "del", text: a.join("") }, { op: "add", text: b.join("") }]
               .filter(r => r.text !== ""),
             rewritten: true };
  }
  const runs = [];
  for (const [op, token] of ops) {
    const last = runs[runs.length - 1];
    if (last && last.op === op) last.tokens.push(token);
    else runs.push({ op, tokens: [token] });
  }
  return { runs: grouped(runs.map(r => ({ op: r.op, text: r.tokens.join("") }))),
           rewritten: false };
}

// Each change as what it removed, then what it added. Whitespace alone between two
// changes is part of them: kept apart, it would interleave the words of a rewritten
// phrase one by one.
function grouped(runs) {
  const out = [];
  let del = "", add = "";
  const flush = () => {
    if (del) out.push({ op: "del", text: del });
    if (add) out.push({ op: "add", text: add });
    del = add = "";
  };
  runs.forEach((r, i) => {
    const between = r.op === "same" && /^\s+$/.test(r.text) && (del || add)
      && i + 1 < runs.length && runs[i + 1].op !== "same";
    if (r.op === "same" && !between) {
      flush();
      out.push(r);
      return;
    }
    if (r.op !== "add") del += r.text;
    if (r.op !== "del") add += r.text;
  });
  flush();
  return out;
}

// The edits from `a` to `b`, in order, as `[op, token]`, or null past EDIT_MAX.
function editScript(a, b) {
  const n = a.length, m = b.length;
  const max = Math.min(n + m, EDIT_MAX);
  const off = max + 1;
  const v = new Array(2 * max + 3).fill(0);
  // Before each round d, the furthest x on diagonals -d-1..d+1: what walking back needs.
  const trace = [];
  for (let d = 0; d <= max; d++) {
    trace.push(v.slice(off - d - 1, off + d + 2));
    for (let k = -d; k <= d; k += 2) {
      let x = k === -d || (k !== d && v[off + k - 1] < v[off + k + 1])
        ? v[off + k + 1] : v[off + k - 1] + 1;
      let y = x - k;
      while (x < n && y < m && a[x] === b[y]) { x++; y++; }
      v[off + k] = x;
      if (x >= n && y >= m) return walkBack(trace, a, b);
    }
  }
  return null;
}

function walkBack(trace, a, b) {
  const ops = [];
  let x = a.length, y = b.length;
  for (let d = trace.length - 1; d > 0; d--) {
    const at = k => trace[d][k + d + 1];
    const k = x - y;
    const down = k === -d || (k !== d && at(k - 1) < at(k + 1));
    const prevK = down ? k + 1 : k - 1;
    const prevX = at(prevK);
    const prevY = prevX - prevK;
    while (x > prevX && y > prevY) { x--; y--; ops.push(["same", a[x]]); }
    if (down) ops.push(["add", b[prevY]]);
    else ops.push(["del", a[prevX]]);
    x = prevX;
    y = prevY;
  }
  while (x > 0 && y > 0) { x--; y--; ops.push(["same", a[x]]); }
  return ops.reverse();
}
