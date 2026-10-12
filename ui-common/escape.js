// SPDX-License-Identifier: Apache-2.0
/* Text spelled so what is on screen is what it is: every code point outside printable
 * ASCII as `\uXXXX`, the form JSON itself writes.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

// Everything outside printable ASCII. A `g` regex: use it with `match` and `replace`,
// which start from the beginning, never with `.test` or `.exec`, which carry
// `lastIndex` from one call to the next (INVISIBLE_ONE in payload.js says what that did).
export const NON_ASCII_RE = /[^\x20-\x7E]/gu;

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
