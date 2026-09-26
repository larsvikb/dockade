// SPDX-License-Identifier: Apache-2.0
/* The agent-authored payload on a tool card, from the JSON text the gateway sent to
 * what the operator reads: the summary line, the indented and coloured pieces, and the
 * invisible-character check that decides whether the escaped form is shown first.
 *
 * Nothing here touches the DOM at import — `renderPayload` fills the element it is
 * handed — so the unit tests can import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

// The summary line above a tool ask's arguments. The payload always starts open: the
// box's max-height (`.payload pre` in index.html) is what keeps a long one from
// pushing the rest of the queue off screen, and a fold on top of it cost a click on
// every review. The byte count says how much there is to scroll through.
export function payloadDisclosure(argsJson) {
  const text = typeof argsJson === "string" ? argsJson : "";
  const bytes = text.length;
  return { bytes, summary: `arguments · ${bytes} bytes` };
}

// The payload one line per field, indented by depth. WHITESPACE ONLY, added outside
// string literals: strip it and the canonical bytes come back exactly. It walks the
// text rather than parsing it, because a JSON.parse/stringify round trip is the
// prettifier control-plane-ui/DESIGN.md forbids — it rounds integers past 2^53,
// moves integer-like keys to the front and decodes `\u` escapes.
//
// With `breaks`, the one exception: a `\n` inside a string becomes `↵` and a line
// break, continuing one level deeper than the string's key so it cannot line up
// with a field. Every other escape stays spelled out.
//
// An array of plain values short enough to read at a glance stays on one line.
//
// Returned as `[kind, text]` pieces so the card can colour them: key, string,
// escape, mark (the `↵`), value (numbers and true/false/null), punct, space. The
// pieces joined are indentPayload's string, so colouring cannot change the text.
export function payloadTokens(argsJson, { breaks = false } = {}) {
  const text = typeof argsJson === "string" ? argsJson : "";
  const tokens = [];
  const push = (kind, piece) => {
    const last = tokens[tokens.length - 1];
    if (last && last[0] === kind) last[1] += piece;
    else tokens.push([kind, piece]);
  };
  let depth = 0, flat = false;
  const newline = () => "\n" + "  ".repeat(depth);
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '"') {
      i = pushString(text, i, depth, breaks, push);
    } else if (ch === "{" && text[i + 1] === "}") {
      push("punct", "{}");
      i++;
    } else if (ch === "[" && isInlineArray(text, i, breaks)) {
      push("punct", ch);
      flat = true;
    } else if (ch === "{" || ch === "[") {
      depth++;
      push("punct", ch);
      push("space", newline());
    } else if (ch === "]" && flat) {
      push("punct", ch);
      flat = false;
    } else if (ch === "}" || ch === "]") {
      depth = Math.max(0, depth - 1);
      push("space", newline());
      push("punct", ch);
    } else if (ch === ",") {
      push("punct", ch);
      push("space", flat ? " " : newline());
    } else if (ch === ":") {
      push("punct", ch);
      push("space", " ");
    } else {
      let j = i;
      while (j + 1 < text.length && !'"{}[],:'.includes(text[j + 1])) j++;
      push("value", text.slice(i, j + 1));
      i = j;
    }
  }
  return tokens;
}

// One string literal, from its opening quote; returns the index of its closing one.
// A string is a key when a `:` follows it — the canonical form has no space between.
function pushString(text, start, depth, breaks, push) {
  let end = start + 1;
  while (end < text.length && text[end] !== '"') end += text[end] === "\\" ? 2 : 1;
  end = Math.min(end, text.length);
  const kind = text[end + 1] === ":" ? "key" : "string";
  push(kind, '"');
  for (let i = start + 1; i < end; i++) {
    if (text[i] !== "\\") {
      push(kind, text[i]);
    } else if (breaks && text[i + 1] === "n") {
      push("mark", "↵");
      push("space", "\n" + "  ".repeat(depth + 1));
      i++;
    } else {
      const escape = text.slice(i, i + (text[i + 1] === "u" ? 6 : 2));
      push("escape", escape);
      i += escape.length - 1;
    }
  }
  if (end < text.length) push(kind, '"');
  return end;
}

export function indentPayload(argsJson, options) {
  return payloadTokens(argsJson, options).map(([, piece]) => piece).join("");
}

// Longest array, as sent, that indentPayload keeps on one line.
export const INLINE_ARRAY_MAX = 60;

// Whether the array opening at `start` holds only plain values, fits in
// INLINE_ARRAY_MAX, and — with breaks on — has no string that would break a line.
function isInlineArray(text, start, breaks) {
  let inString = false;
  for (let j = start + 1; j < text.length && j - start < INLINE_ARRAY_MAX; j++) {
    const ch = text[j];
    if (inString) {
      if (ch === "\\" && breaks && text[j + 1] === "n") return false;
      if (ch === "\\") j++;
      else if (ch === '"') inString = false;
    } else if (ch === '"') {
      inString = true;
    } else if (ch === "{" || ch === "[") {
      return false;
    } else if (ch === "]") {
      return true;
    }
  }
  return false;
}

// Characters that render as nothing, or as something else. `Cf` is the format class —
// the bidi overrides and isolates (U+202E, U+2066..2069), zero-width joiners, the BOM —
// and `Cc`/`Zl`/`Zp` are raw controls and line separators, none of which the canonical
// JSON form ever contains legitimately (json.dumps escapes controls and uses no
// whitespace). Any one of them in a payload means the text on screen is not the text
// that runs: a single U+202E makes the browser draw the rest of the line backwards
// while the bytes stay exactly as they are — the reordering control-plane-ui/DESIGN.md
// warns a view must never do, performed by the renderer rather than by a prettifier.
const INVISIBLE_RE = /[\p{Cf}\p{Cc}\p{Zl}\p{Zp}]/gu;
// The same set for asking about ONE character. A `g` regex carries `lastIndex` across
// calls, so `INVISIBLE_RE.test(ch)` alternates true and false on the same input — a
// bug that would show up as a tally spelling half its overrides out and half of them
// not.
const INVISIBLE_ONE = /[\p{Cf}\p{Cc}\p{Zl}\p{Zp}]/u;
// Everything outside printable ASCII, the invisible set included. Counted rather than
// classified: U+0430 CYRILLIC SMALL LETTER A in an `owner` field is indistinguishable
// from the Latin one on screen, and no confusables table is needed to say "this is not
// the plain text it looks like".
const NON_ASCII_RE = /[^\x20-\x7E]/gu;
// How many DISTINCT characters the note names before it summarises the rest. Real
// payloads carry one or two kinds of dash, not twenty, so this is about keeping a
// pathological payload from writing an essay into the card.
const HAZARD_TALLY_MAX = 4;

// JSON's own escaping applied to a string that already IS JSON: every code point
// outside printable ASCII becomes `\uXXXX`, astral ones as the surrogate pair JSON
// would write. Lossless and byte-for-byte reversible — this is the form
// `json.dumps(ensure_ascii=True)` would have produced — so showing it is the opposite
// of the unescaping the payload rule forbids. Nothing is dropped, summarized or
// reordered; what changes is that an override is spelled out where it sits instead
// of acting on the text around it.
export function escapePayload(text) {
  return text.replace(NON_ASCII_RE, (ch) => {
    const code = ch.codePointAt(0);
    if (code <= 0xFFFF) return "\\u" + code.toString(16).padStart(4, "0");
    const hi = 0xD800 + ((code - 0x10000) >> 10);
    const lo = 0xDC00 + ((code - 0x10000) & 0x3FF);
    return "\\u" + hi.toString(16) + "\\u" + lo.toString(16);
  });
}

// WHICH characters, not just how many. An operator who has to untick the box and hunt
// for what the note means is being asked to do the thing the note exists to save them
// from. Ordered by count so the dominant character leads, then by code point so two
// payloads with the same characters read the same way.
//
// An invisible character is always spelled `\uXXXX` here, including in this note — a
// raw U+202E in the warning would reorder the warning, which is precisely the trick
// being reported.
//
// The counts read `x3` in plain ASCII rather than `×3`, so the only character in the
// parentheses that is not ASCII is the one being REPORTED. A multiplication sign here
// would be indistinguishable from a multiplication sign in the payload — the note
// would be committing the confusion it exists to point out. (`ruff`'s RUF001 makes the
// same objection about the test that asserts this.)
function hazardTally(text) {
  const counts = new Map();
  for (const ch of text.match(NON_ASCII_RE) || []) {
    counts.set(ch, (counts.get(ch) || 0) + 1);
  }
  const ranked = [...counts].sort(
    (a, b) => b[1] - a[1] || a[0].codePointAt(0) - b[0].codePointAt(0));
  const shown = ranked.slice(0, HAZARD_TALLY_MAX).map(
    ([ch, n]) => `${INVISIBLE_ONE.test(ch) ? escapePayload(ch) : ch} x${n}`);
  if (ranked.length > shown.length) {
    shown.push(`+${ranked.length - shown.length} more`);
  }
  return shown;
}

// What the card has to say before the operator reads the arguments, at one of TWO
// levels — and the split is the point.
//
//   `danger`  an invisible or direction-changing character (`Cf`/`Cc`/`Zl`/`Zp`).
//             None of these has a legitimate place in the canonical JSON form, and one
//             of them makes the text on screen differ from the text that runs. The card
//             opens itself and shows the ESCAPED form first, with raw one click away
//             rather than the reverse — a bidi override in the raw form can hide the
//             very thing the note is warning about.
//   `note`    any other non-ASCII. Said, not shouted: the payload is shown exactly as
//             it is, the card does not force itself open, and the escape toggle is
//             there for anyone who wants it.
//
// One level for both was the first shape of this, and it was wrong in the direction
// that costs the most. Ordinary prose in an argument — an em dash in a PR body, an
// arrow in a commit message — flagged every card and rendered every payload as
// `—` soup, which is harder to review, not easier. A warning that fires on
// everything is one an operator learns to click past, and that habit is exactly what a
// real U+202E needs to get through. The quiet tier keeps the reporting while leaving
// the alarm for the characters that earn it.
//
// What the quiet tier gives up is the HOMOGLYPH case — a Cyrillic `а` in `owner` is
// non-ASCII and perfectly visible. Catching that properly means mixed-script detection
// within a token, not a blanket non-ASCII alarm, and it is deliberately a separate
// question (see the note's wording: this tier reports, it does not vouch).
export function payloadHazards(argsJson) {
  const text = typeof argsJson === "string" ? argsJson : "";
  const invisible = (text.match(INVISIBLE_RE) || []).length;
  const nonAscii = (text.match(NON_ASCII_RE) || []).length;
  const level = invisible ? "danger" : nonAscii ? "note" : "none";
  const parts = [];
  if (invisible) parts.push(`${invisible} invisible or direction-changing`);
  if (nonAscii - invisible) {
    parts.push(`${nonAscii - invisible}${invisible ? " other" : ""} non-ASCII`);
  }
  const counted = `${parts.join(" and ")} character${nonAscii === 1 ? "" : "s"} `
    + `in the arguments (${hazardTally(text).join(", ")})`;
  const note = level === "danger"
    ? `${counted} — what you read may not be what runs. Shown escaped; untick to see `
      + "the raw text."
    : level === "note"
    ? `${counted} — nothing is hidden; tick to see them escaped.`
    : "";
  return { level, invisible, nonAscii, note,
           escaped: nonAscii ? escapePayload(text) : text };
}

// The payload's pieces as coloured spans, built with textContent like the rest of
// the card: the text is agent-authored, and a span's class is ours.
export function renderPayload(pre, tokens) {
  pre.replaceChildren(...tokens.map(([kind, piece]) => {
    if (kind === "space") return document.createTextNode(piece);
    const span = document.createElement("span");
    span.className = `j-${kind}`;
    span.textContent = piece;
    return span;
  }));
}
