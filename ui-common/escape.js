// SPDX-License-Identifier: Apache-2.0
/* Text spelled so what is on screen is what it is: every code point outside printable
 * ASCII as `\uXXXX`, the form JSON itself writes, and the set of characters that hide
 * what is around them, which both pages spell wherever they find one.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

// Everything outside printable ASCII, every hidden character (`isHidden`) included. A
// `g` regex: use it with `match` and `replace`, which start from the beginning, never
// with `.test` or `.exec`, which carry `lastIndex` from one call to the next.
export const NON_ASCII_RE = /[^\x20-\x7E]/gu;

// Characters that render as nothing, or as something else. `Cf` is the format class —
// the bidi overrides and isolates (U+202E, U+2066..2069), zero-width joiners, the BOM —
// and `Cc`/`Zl`/`Zp` are raw controls and line separators. Unicode's default-ignorable
// code points add what a renderer draws as nothing whatever its class: the variation
// selectors (U+FE00..FE0F, U+E0100..E01EF), one per byte being the published way to
// smuggle data past a reader, the combining grapheme joiner and the Hangul fillers. A
// single U+202E makes the browser draw the rest of the line backwards while the bytes
// stay exactly as they are.
//
// Without `g`, deliberately: a `g` regex carries `lastIndex` across calls, so `.test`
// on it alternates true and false on the same input — a bug that would show up as a
// tally spelling half its overrides out and half of them not.
const HIDDEN_ONE =
  /^[\p{Cf}\p{Cc}\p{Zl}\p{Zp}\p{Default_Ignorable_Code_Point}]$/u;

// Whether one character (a code point, as a string iterator yields it) is hidden.
export function isHidden(ch) {
  return HIDDEN_ONE.test(ch);
}

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
