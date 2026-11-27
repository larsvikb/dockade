// SPDX-License-Identifier: Apache-2.0
/* Agent markdown, read as the subset the tasks page draws (tasks-ui/DESIGN.md, "Agent
 * markdown is drawn, never hidden"): ATX headings, paragraphs, bullet and numbered
 * items, `*`/`_` emphasis, code spans and fenced code blocks. Everything else is source:
 * links, images, HTML and its comments, entities, backslash escapes, block quotes,
 * tables, rules, setext underlines, indented code, unmatched delimiters.
 *
 * Lossless: `tokenize(text)` returns blocks of tokens `{kind, text}` whose texts, joined
 * in order, are `text` exactly, markers, line ends and CR LF included. So a mirror built
 * over a textarea lines up with it, and the page decides what it drops and spells the
 * rest. Only LF or CR LF ends a line; a lone CR or U+2028 is inside one.
 *
 * Linear in the text: the spec editor runs it per keystroke, on an agent's text. And
 * spans nest NEST_MAX deep at most, so what is drawn from it stays cheap to lay out.
 *
 * Imports nothing and draws nothing, so the unit tests import it under node
 * (tests/test_tasks_ui_js.py).
 */

// A block's kinds: "heading" (with `level`), "paragraph", "item" (with `ordered` and
// `indent`, the columns before its marker), "code" (fenced), "blank", "source".
// A token's kinds: "indent", "heading-marker" (the hashes and the space or tab after
// them), "bullet" (the bullet and the space or tab after it), "number" (`1.` or `1)`),
// "text", "eol", "em-open", "em-close", "strong-open", "strong-close", "code-open",
// "code", "code-close", "fence", "info", "source".

const BLANK = /^[ \t]*$/;
const HEADING = /^( {0,3})(#{1,6}(?:[ \t]|$))/;
const FENCE = /^( {0,3})(`{3,}|~{3,})([^]*)$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const SETEXT = /^ {0,3}=+[ \t]*$/;
const ITEM = /^([ \t]*)(?:([-*+])([ \t]|$)|([0-9]{1,9}[.)])(?=[ \t]|$))/;
// A block quote, a table row, an HTML block: drawn as typed.
const SOURCE_LINE = /^ {0,3}(?:>|\||<[A-Za-z!?/])/;

// Columns of leading whitespace, a tab to the next multiple of four.
function columns(ws) {
  let col = 0;
  for (const ch of ws) col = ch === "\t" ? col + 4 - (col % 4) : col + 1;
  return col;
}

// The text as lines, each with the line end that closes it: "\n", "\r\n", or "" for
// the last when nothing ends it.
function lines(text) {
  const out = [];
  let at = 0;
  while (at < text.length) {
    const lf = text.indexOf("\n", at);
    if (lf < 0) {
      out.push({ text: text.slice(at), eol: "" });
      break;
    }
    const cr = lf > at && text[lf - 1] === "\r";
    out.push({ text: text.slice(at, cr ? lf - 1 : lf), eol: cr ? "\r\n" : "\n" });
    at = lf + 1;
  }
  return out;
}

export function tokenize(text) {
  const s = typeof text === "string" ? text : "";
  const blocks = [];
  // The block a plain line continues: a paragraph's or an item's, until a blank line or
  // another block. A fenced block takes every line until its closing fence.
  let open = null;
  let fence = null;
  // Whether the last block but blank lines was an item, or text continuing one: an item
  // indented four columns or more is nested there, and indented code elsewhere.
  let inList = false;
  for (const { text: line, eol } of lines(s)) {
    if (fence) {
      const close = line.match(/^( {0,3})(`{3,}|~{3,})([ \t]*)$/);
      if (close && close[2][0] === fence.char && close[2].length >= fence.length) {
        fence.block.tokens.push(...piece("indent", close[1]), { kind: "fence", text: close[2] },
                                ...piece("text", close[3]), ...piece("eol", eol));
        fence = null;
      } else {
        fence.block.tokens.push(...piece("code", line), ...piece("eol", eol));
      }
      continue;
    }
    if (BLANK.test(line)) {
      blocks.push({ kind: "blank", tokens: [...piece("text", line), ...piece("eol", eol)] });
      open = null;
      continue;
    }
    const opening = line.match(FENCE);
    if (opening && !(opening[2][0] === "`" && opening[3].includes("`"))) {
      const block = { kind: "code", tokens: [...piece("indent", opening[1]),
                                             { kind: "fence", text: opening[2] },
                                             ...piece("info", opening[3]),
                                             ...piece("eol", eol)] };
      blocks.push(block);
      fence = { block, char: opening[2][0], length: opening[2].length };
      open = null;
      inList = false;
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      blocks.push({ kind: "heading", level: heading[2].trimEnd().length,
                    prefix: [...piece("indent", heading[1]),
                             { kind: "heading-marker", text: heading[2] }],
                    content: line.slice(heading[0].length) + eol });
      open = null;
      inList = false;
      continue;
    }
    if (RULE.test(line) || SETEXT.test(line)) {
      blocks.push(sourceLine(line, eol));
      open = null;
      inList = false;
      continue;
    }
    const item = line.match(ITEM);
    const indent = item ? columns(item[1]) : 0;
    if (item && (indent < 4 || inList)) {
      const marker = item[2]
        ? { kind: "bullet", text: item[2] + item[3] }
        : { kind: "number", text: item[4] };
      open = { kind: "item", ordered: !item[2], indent,
               prefix: [...piece("indent", item[1]), marker],
               content: line.slice(item[0].length) + eol };
      blocks.push(open);
      inList = true;
      continue;
    }
    if (SOURCE_LINE.test(line)) {
      blocks.push(sourceLine(line, eol));
      open = null;
      inList = false;
      continue;
    }
    if (open) {
      open.content += line + eol;
      continue;
    }
    const lead = columns(line.match(/^[ \t]*/)[0]);
    if (lead >= 4 && !inList) {
      // Indented code: drawn as typed.
      blocks.push(sourceLine(line, eol));
      continue;
    }
    open = { kind: "paragraph", prefix: [], content: line + eol };
    blocks.push(open);
    inList = inList && lead > 0;
  }
  return blocks.map(b => (b.content === undefined ? b : {
    kind: b.kind,
    ...(b.kind === "heading" ? { level: b.level } : {}),
    ...(b.kind === "item" ? { ordered: b.ordered, indent: b.indent } : {}),
    tokens: [...b.prefix, ...inline(b.content)],
  }));
}

function piece(kind, text) {
  return text ? [{ kind, text }] : [];
}

function sourceLine(line, eol) {
  return { kind: "source", tokens: [{ kind: "source", text: line }, ...piece("eol", eol)] };
}

// ── inline ──────────────────────────────────────────────────────────────────

const PUNCT = /^[!-/:-@[-`{-~]$/;
// Each matched where it starts, sticky; none can backtrack past the next `<`, `[` or
// `]`, so a text of unclosed openers costs what it is long. TAG takes a URL's autolink
// too, which starts with a letter; an address's may not.
const EMAIL = /<[^\s<>@]+@[^\s<>@]+>/y;
const TAG = /<\/?[A-Za-z][^<>]*>|<[!?][^<>]*>/y;
const LINK = /!?\[[^[\]]*\](?:\([^()\s]*(?:[ \t]+(?:"[^"]*"|'[^']*'))?\)|\[[^[\]]*\])/y;
const ENTITY = /&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});/y;

const WS = " \t\r\n";
// Emphasis is stricter than CommonMark's, so paths and globs written outside backticks
// stay as written: a run opens only after whitespace, a line's start or one of these,
// before a non-space; and closes only after a non-space, before whitespace, a line's
// end or one of these, or a `.` that ends the line or comes before whitespace. A run
// that could do both, as in `"*"` or `(**)`, does neither; and none opens before a `.`
// or a `/`, or closes after a `/`: there it is a glob's or a path's, as in `*.py` and
// `src/*`.
const OPENS_AFTER = "([\"'";
const CLOSES_BEFORE = ",;:!?)]\"'";

function sticky(re, s, at) {
  re.lastIndex = at;
  const m = re.exec(s);
  return m ? m[0].length : 0;
}

// Where `needle` next occurs at or after `from`, remembered: the scan only moves
// forward, so each search starts past the last, and one that failed is never run again.
function finder(s, needle) {
  let found = -2;
  return from => {
    if (found === -1 || (found >= from)) return found;
    found = s.indexOf(needle, from);
    return found;
  };
}

// Every backtick run of `s`, by length, in order; `next(n, after)` is the first of
// length n starting after `after`. The scan only moves forward, so each length's cursor
// does too.
function backtickRuns(s) {
  const byLength = new Map();
  const re = /`+/g;
  let m;
  while ((m = re.exec(s)) !== null) {
    const n = m[0].length;
    if (!byLength.has(n)) byLength.set(n, { starts: [], at: 0 });
    byLength.get(n).starts.push(m.index);
  }
  return (n, after) => {
    const runs = byLength.get(n);
    if (!runs) return -1;
    while (runs.at < runs.starts.length && runs.starts[runs.at] <= after) runs.at++;
    return runs.at < runs.starts.length ? runs.starts[runs.at] : -1;
  };
}

function inline(s) {
  const tokens = [];
  const delims = [];
  let text = "";
  const flush = () => {
    if (text) tokens.push({ kind: "text", text });
    text = "";
  };
  const push = (kind, run) => {
    flush();
    tokens.push({ kind, text: run });
  };
  const nextRun = backtickRuns(s);
  const commentEnd = finder(s, "-->");
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    if (c === "\n" || (c === "\r" && s[i + 1] === "\n")) {
      const eol = c === "\n" ? "\n" : "\r\n";
      push("eol", eol);
      i += eol.length;
      continue;
    }
    if (c === "`") {
      let n = 1;
      while (s[i + n] === "`") n++;
      const close = nextRun(n, i);
      if (close < 0) {
        text += s.slice(i, i + n);
      } else {
        push("code-open", s.slice(i, i + n));
        if (close > i + n) push("code", s.slice(i + n, close));
        push("code-close", s.slice(close, close + n));
        n = close + n - i;
      }
      i += n;
      continue;
    }
    if (c === "\\" && i + 1 < s.length && PUNCT.test(s[i + 1])) {
      push("source", s.slice(i, i + 2));
      i += 2;
      continue;
    }
    let n = 0;
    if (c === "<") {
      if (s.startsWith("<!--", i)) {
        const end = commentEnd(i + 4);
        n = end < 0 ? 0 : end + 3 - i;
      }
      n = n || sticky(EMAIL, s, i) || sticky(TAG, s, i);
    } else if (c === "[" || (c === "!" && s[i + 1] === "[")) {
      n = sticky(LINK, s, i);
    } else if (c === "&") {
      n = sticky(ENTITY, s, i);
    }
    if (n) {
      push("source", s.slice(i, i + n));
      i += n;
      continue;
    }
    if (c === "*" || c === "_") {
      n = 1;
      while (s[i + n] === c) n++;
      const before = i === 0 ? "\n" : s[i - 1];
      const after = i + n < s.length ? s[i + n] : "\n";
      const ws = ch => WS.includes(ch);
      const opens = (ws(before) || OPENS_AFTER.includes(before)) && !ws(after);
      const closes = !ws(before) && (ws(after) || CLOSES_BEFORE.includes(after)
        || (after === "." && (i + n + 1 >= s.length || ws(s[i + n + 1]))));
      const both = opens && closes;
      const canOpen = opens && !both && after !== "." && after !== "/";
      const canClose = closes && !both && before !== "/";
      flush();
      delims.push({ token: tokens.length, char: c, count: n, canOpen, canClose,
                    opens: [], closes: [] });
      tokens.push(null);
      i += n;
      continue;
    }
    text += c;
    i += 1;
  }
  flush();
  matchEmphasis(delims);
  // Each run as what it closes, from its left; what is left of it, as text; and what
  // it opens, outermost first, from its right.
  for (const d of delims) {
    const run = [];
    for (const use of d.closes) run.push({ kind: use === 2 ? "strong-close" : "em-close", use });
    if (d.count) run.push({ kind: "text", use: d.count });
    for (const use of [...d.opens].reverse()) {
      run.push({ kind: use === 2 ? "strong-open" : "em-open", use });
    }
    tokens[d.token] = run.map(r => ({ kind: r.kind, text: d.char.repeat(r.use) }));
  }
  return flattened(tokens.flat()).reduce((out, token) => {
    const last = out[out.length - 1];
    if (token.kind === "text" && last && last.kind === "text") last.text += token.text;
    else out.push({ ...token });
    return out;
  }, []);
}

// How deep spans nest before the ones inside are text. A body at the registry's cap can
// nest about 8000 deep, which took Chromium 0.8 s to lay out; 16000 crashed it
// (tasks-ui/NOTES.md, "Nested spans cost the square of their depth"). No agent writes
// more than a few.
export const NEST_MAX = 16;

// `tokens` with every pair opened past NEST_MAX as text, both its ends: pairs nest, so
// a close past it is the close of an open past it.
function flattened(tokens) {
  let depth = 0;
  return tokens.map(token => {
    const opens = token.kind.endsWith("-open");
    const closes = token.kind.endsWith("-close");
    if (opens) depth += 1;
    const deep = depth > NEST_MAX && (opens || closes);
    if (closes) depth -= 1;
    return deep ? { kind: "text", text: token.text } : token;
  });
}

// CommonMark's "process emphasis", over `delims` in order: each closer takes the
// nearest opener of its character below it, two characters for strong when both have
// them, one for emphasis otherwise, and the delimiters between them drop out. A search
// that finds nothing records where it stopped, by the closer's character, so the next
// search for that character stops there: nothing below can open it. That bound keeps a
// text of unmatched delimiters linear. CommonMark's rule of three is left out: it parts
// pairs where a run could both open and close, and here no run does both.
function matchEmphasis(delims) {
  delims.forEach((d, k) => {
    d.index = k;
    d.prev = delims[k - 1] || null;
    d.next = delims[k + 1] || null;
  });
  const remove = d => {
    if (d.prev) d.prev.next = d.next;
    if (d.next) d.next.prev = d.prev;
  };
  const bottom = new Map();
  let closer = delims[0] || null;
  while (closer) {
    if (!closer.canClose) {
      closer = closer.next;
      continue;
    }
    const floor = bottom.has(closer.char) ? bottom.get(closer.char) : -1;
    let opener = closer.prev;
    while (opener && opener.index > floor
           && !(opener.char === closer.char && opener.canOpen)) {
      opener = opener.prev;
    }
    if (opener && opener.index > floor) {
      const use = opener.count >= 2 && closer.count >= 2 ? 2 : 1;
      opener.count -= use;
      closer.count -= use;
      opener.opens.push(use);
      closer.closes.push(use);
      opener.next = closer;
      closer.prev = opener;
      if (!opener.count) remove(opener);
      if (!closer.count) {
        remove(closer);
        closer = closer.next;
      }
    } else {
      bottom.set(closer.char, closer.prev ? closer.prev.index : -1);
      closer = closer.next;
    }
  }
}

// ── highlighting ────────────────────────────────────────────────────────────

// A block quote's line, as SOURCE_LINE reads one. Only a source line of its own starts
// so: no construct inside a line opens with a `>`.
const QUOTE_LINE = /^ {0,3}>/;

// The spec editor's colouring of `text` (the mirror behind the body's textarea in
// tasks.js), as runs `{cls, text}` whose texts, joined, are `text` exactly, the
// neighbours of one class merged. A class names what the detail draws there: "heading"
// a heading's whole line, "code" a fenced block or a code span with its delimiters,
// "strong" and "em" a span with its delimiters, the innermost one's, "marker" an item's
// bullet or number, "quote" a block quote's line whole, "source" any other construct
// the detail draws as typed, and "" plain text and line ends outside a span. Since it is
// read off `tokenize`, it colours no emphasis the detail would not draw, and none past
// NEST_MAX.
export function highlightRuns(text) {
  const runs = [];
  const add = (cls, t) => {
    const last = runs[runs.length - 1];
    if (last && last.cls === cls) last.text += t;
    else runs.push({ cls, text: t });
  };
  for (const block of tokenize(text)) {
    const whole = block.kind === "heading" || block.kind === "code" ? block.kind : null;
    const spans = [];
    for (const { kind, text: t } of block.tokens) {
      if (kind === "eol" && !spans.length) add("", t);
      else if (whole) add(whole, t);
      else if (kind.endsWith("-open")) {
        spans.push(kind.slice(0, -5));
        add(spans[spans.length - 1], t);
      } else if (kind.endsWith("-close")) add(spans.pop(), t);
      else if (spans.length) add(spans[spans.length - 1], t);
      else if (kind === "bullet" || kind === "number") add("marker", t);
      else if (kind === "source") add(QUOTE_LINE.test(t) ? "quote" : "source", t);
      else add("", t);
    }
  }
  return runs;
}
