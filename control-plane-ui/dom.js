// SPDX-License-Identifier: Apache-2.0
/* The one way markup reaches the page: `html`, a template tag that escapes every value
 * it is given, and `setHtml`, which puts what `html` built into an element. The parts
 * of the page rendered from an HTML string show values that are often agent- or
 * server-authored, so escaping is what building markup does rather than a call each
 * `${…}` has to remember. A value goes in as markup only if it is itself a fragment
 * `html` made; a string never becomes one.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

const esc = s => (s ?? "").toString().replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Elements whose content the browser reads as text or script, not as markup, so an
// escaped value inside one is not what escaping promises. No template needs one.
const RAW_TEXT = new Set(["script", "style", "textarea", "title", "iframe", "noscript",
                          "noembed", "noframes", "xmp", "plaintext"]);
// The only attributes a value may go in, quoted. Default-deny: escaping keeps a value
// inside the quotes, but in a URL, script or style attribute, or one of the many others
// the browser reads as more than text, being inside the quotes is not enough. A
// constant written in the literal is the template's own and goes anywhere.
const VALUE_ATTR = /^(?:title|class|data-[a-z0-9-]+|aria-[a-z]+)$/;
const SPACE = /^[\t\n\f\r ]$/;

// Where in the markup the tracker is, as the browser's tokenizer would be, reduced to
// what decides what a value may be there:
//   text       between tags
//   name       just after `<` or `</`, and in the tag's name
//   tag        in a tag, between attributes
//   attr       in an attribute's name; `afterattr` past it, before any `=`
//   eq         just after `=`; a quote here opens a value, and only here
//   unq        in an unquoted value
//   dq, sq     in a double- or single-quoted value
// Escaping makes a value safe in text and in an allowed attribute's quoted value
// (`VALUE_ATTR`), and nowhere else: `<tr ${x}>` or `a=${x}` would take a space or `=`
// from `x` as markup.
//
// Default-deny wherever the tokenizer has more than one reading: a `<` that does not
// start a tag name, a comment, a declaration, and an element whose content is not markup
// are refused rather than modelled. Not covered: what a constant literal itself says (a
// literal `href="javascript:…"` is the template's own text), and a hand-built template
// object passed to `html` (see there).
function advance(s, literal) {
  for (let i = 0; i < literal.length; i++) {
    const c = literal[i];
    switch (s.ctx) {
      case "text":
        if (c === "<") {
          s.ctx = "name";
          s.name = "";
          s.slash = false;
        }
        break;
      case "name":
        // A tag starts `<` or `</`, then a letter; the browser reads anything else after
        // `<` (`<!`, `<?`, `</ `, `<1`) as text or a comment, which no template needs.
        if (s.name === "") {
          if (c === "/" && !s.slash) {
            s.slash = true;
          } else if (/^[A-Za-z]$/.test(c)) {
            s.name = c;
          } else {
            throw new Error("html: `<` must start a tag name");
          }
        } else if (SPACE.test(c) || c === "/" || c === ">") {
          if (RAW_TEXT.has(s.name.toLowerCase())) {
            throw new Error(`html: no <${s.name}> in a template; its content is not markup`);
          }
          s.ctx = c === ">" ? "text" : "tag";
        } else {
          s.name += c;
        }
        break;
      case "tag":
      case "afterattr":
        if (c === ">") s.ctx = "text";
        else if (c === "/") s.ctx = "tag";
        else if (c === "=" && s.ctx === "afterattr") s.ctx = "eq";
        else if (!SPACE.test(c)) {
          s.ctx = "attr";
          s.attr = c;
        }
        break;
      case "attr":
        // A quote here is part of the name, as the browser reads it, not a value.
        if (c === "=") s.ctx = "eq";
        else if (c === ">") s.ctx = "text";
        else if (c === "/") s.ctx = "tag";
        else if (SPACE.test(c)) s.ctx = "afterattr";
        else s.attr += c;
        break;
      case "eq":
        if (c === '"') s.ctx = "dq";
        else if (c === "'") s.ctx = "sq";
        else if (c === ">") s.ctx = "text";
        else if (!SPACE.test(c)) s.ctx = "unq";
        break;
      case "unq":
        if (c === ">") s.ctx = "text";
        else if (SPACE.test(c)) s.ctx = "tag";
        break;
      case "dq":
        if (c === '"') s.ctx = "tag";
        break;
      case "sq":
        if (c === "'") s.ctx = "tag";
        break;
    }
  }
}

// The two places a fragment may go, and where it has to end for the markup after it to
// read as it looks: one placed in text ends in text, one placed in a tag ends in that
// tag outside any value. A fragment is re-tracked from where it is placed, so a value
// inside it is judged where it lands, not where the fragment was written.
const placeOf = ctx => (ctx === "text" ? "text"
  : ctx === "name" || ctx === "tag" || ctx === "attr" || ctx === "afterattr" ? "tag"
    : null);

// One value, as markup for the place it sits; moves `s` past it.
function put(value, s) {
  if (Array.isArray(value)) return value.map(v => put(v, s)).join("");
  if (Fragment.isOne(value)) {
    // Not in an attribute's name, which the fragment would extend.
    const from = s.ctx === "attr" ? null : placeOf(s.ctx);
    if (from === null) {
      throw new Error("html: a fragment goes between tags or attributes, nowhere else");
    }
    const markup = Fragment.render(value, s);
    if (placeOf(s.ctx) !== from) {
      throw new Error(`html: a fragment placed in ${from} has to end there`);
    }
    return markup;
  }
  if (s.ctx === "text") return esc(value);
  if (s.ctx === "dq" || s.ctx === "sq") {
    if (!VALUE_ATTR.test(s.attr.toLowerCase())) {
      throw new Error(`html: no value in ${s.attr}=; only title, class, data-* and aria-*`
                      + " take one");
    }
    return esc(value);
  }
  if (value === "") return "";
  throw new Error("html: only a fragment or \"\" may sit inside a tag outside quotes");
}

function render(strings, values, s) {
  advance(s, strings[0]);
  let out = strings[0];
  for (let i = 0; i < values.length; i++) {
    out += put(values[i], s) + strings[i + 1];
    advance(s, strings[i + 1]);
  }
  return out;
}

const fromText = () => ({ ctx: "text", name: "", slash: false, attr: "" });

// Only this module can make a fragment: the class is not exported, its constructor
// refuses a caller without `MINT` (the class itself is reachable from any fragment, as
// `.constructor`), and the parts are private fields, so an object built on its
// prototype has none. Rendering one gives nothing away.
const MINT = Object.freeze({});

class Fragment {
  #strings;
  #values;
  constructor(mint, strings, values) {
    if (mint !== MINT) throw new Error("html: a fragment is made by html`…` only");
    this.#strings = strings;
    this.#values = values;
    Object.freeze(this);
  }
  static isOne(f) {
    return typeof f === "object" && f !== null && #strings in f;
  }
  static render(f, s) {
    return render(f.#strings, f.#values, s);
  }
}

export function html(strings, ...values) {
  // Stops `html` being called by mistake with a string or an array, which would make
  // it markup. It is not airtight: a frozen array with a `raw` beside it passes. What
  // stops a hand-built one is the source guard that `html` is always followed by a
  // backtick (`PageChecks` in tests/test_control_plane_ui_js.py).
  if (!Array.isArray(strings) || !Array.isArray(strings.raw) || !Object.isFrozen(strings)) {
    throw new Error("html: call it as a tag, html`…`, never with a string or an array");
  }
  // Read once now, from text, so a template wrong wherever it goes fails where it is
  // written; it is read again from wherever it is placed.
  render(strings, values, fromText());
  return new Fragment(MINT, strings, values);
}

export function setHtml(el, fragment) {
  if (!Fragment.isOne(fragment)) {
    throw new Error("setHtml: takes a fragment from html`…`, nothing else");
  }
  const s = fromText();
  const markup = Fragment.render(fragment, s);
  if (s.ctx !== "text") throw new Error("setHtml: the markup ends inside a tag");
  el.innerHTML = markup;
}
