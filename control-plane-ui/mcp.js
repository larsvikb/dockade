// SPDX-License-Identifier: Apache-2.0
/* The MCP surface as the operator configures it — the backend's api_mcp.py, seen
 * from the form: what a server registration will record, the tool policy picker
 * with what a rule written from it will do, and how a pin reads. The backend
 * decides; these shape the previews and have to agree with it.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */
import { escapePayload } from "./payload.js";

// The DNS label a server name has to be. Three spellings of one rule — here, in the
// relay's path pattern, and in `policy._server_name_error` — each load-bearing in a
// different process. The backend stays the one that DECIDES; this copy only shapes
// the preview, so a drift makes the preview wrong rather than the policy wrong.
export const SERVER_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

// A UI preset expanded into the three fields the store actually holds.
//
// This is the ONLY place a concrete header scheme is spelled anywhere in the system.
// The gateway builds its request from whatever descriptor the roster carries and has
// no idea which server it belongs to, which is how "no per-server branch in the code"
// stays true while this form still offers a one-click answer for the common case.
export function serverDescriptor(kind, header, template) {
  if (kind === "header") {
    return { auth_type: "header", auth_header: "Authorization",
             auth_template: "Bearer {secret}" };
  }
  if (kind === "custom") {
    return { auth_type: "header", auth_header: (header || "").trim(),
             auth_template: (template || "").trim() };
  }
  return { auth_type: "none", auth_header: null, auth_template: null };
}

// What registering will do, in the world. A DELIBERATELY partial mirror of the
// backend's validation, like createPreview: it describes, and the backend refuses.
export function serverPreview(name, desc) {
  const n = (name || "").trim();
  if (!n) return { ok: false, text: "" };
  if (!SERVER_NAME_RE.test(n)) {
    return { ok: false,
             text: `${n} is not a DNS label — lowercase letters, digits and '-', not `
                 + `starting or ending with '-'. It is the container name, so it has `
                 + `to match what mcp-servers.yml declares.` };
  }
  if (desc.auth_type === "none") {
    return { ok: true,
             text: `Register ${n}, disabled, with the gateway injecting nothing — the `
                 + `server holds its own credential. Nothing runs until you enable it `
                 + `and write tool rules.` };
  }
  // Named rather than implied: the token's path is DERIVED from the server name, so
  // the operator can see which file they are about to make load-bearing.
  return { ok: Boolean(desc.auth_header && desc.auth_template),
           text: `Register ${n}, disabled. The gateway will send `
               + `${desc.auth_header || "(no header)"}: `
               + `${desc.auth_template || "(no template)"}, reading the token from `
               + `${n}.json in the secrets directory.` };
}

// The body of an enable/disable edit.
//
// The descriptor is ECHOED BACK and that is the whole reason this is a function.
// `ServerEditRequest` takes the TARGET state with `auth_type` defaulting to "none",
// so a body carrying only `enabled` does not mean "leave auth alone" — it means "set
// auth to none". A server toggled off and on again would come back stripped of its
// credential descriptor, the next enumeration would send no header, and the 401 that
// followed would read like an expired token rather than like this.
export function serverEditBody(row, enabled) {
  const auth = row.auth || {};
  return { enabled: enabled,
           auth_type: auth.type || "none",
           auth_header: auth.header || null,
           auth_template: auth.template || null };
}

// ── tool policy: what a server CLAIMS, joined against what a human DECIDED ───
//
// Two sources that must never be confused, which is why they are two endpoints and two
// arguments here rather than one merged payload. `/api/mcp/inventory` is a third
// party's claim about itself, held in memory, authoritative about nothing.
// `/api/mcp/rules` is the operator's standing policy, and it is the only one of the two
// that decides anything.
//
// The JOIN is what this half of the page is for. A tool a server exposes with no rule
// is DENIED, and it is also the one most needing a human — so the picker leads with
// those. Nothing about appearing in the list grants: a server that invents a tool name
// gets it listed, not permitted, and the rule still has to be written.

// The three actions in order of how much they PERMIT. Ranked rather than compared
// against "allow" alone, because deny → ask is a loosening too, and a confirm that
// flagged only `allow` would wave it through.
export const TOOL_ACTION_RANK = { deny: 0, ask: 1, allow: 2 };
const toolRank = a => (a in TOOL_ACTION_RANK ? TOOL_ACTION_RANK[a] : -1);

// "denies", not "denys". Spelled once rather than as `${action}s` at each call site,
// which is right for two of the three actions and wrong for the one an operator reads
// most often. Falls back to the naive form so an action this page has not heard of
// still produces a sentence.
const TOOL_VERB = { allow: "allows", ask: "asks", deny: "denies" };
const toolVerb = a => TOOL_VERB[a] || `${a}s`;
// And the passive form, for saying what a rule will STOP doing. "will no longer ask"
// reads as the tool losing the ability to ask, which is the wrong actor: it is the
// operator who stops being asked.
const TOOL_UNDO = { allow: "be allowed", ask: "be asked about", deny: "be denied" };
const toolUndo = a => TOOL_UNDO[a] || a;

// What a server exposes, split by whether policy has anything to say about it — plus
// one sentence explaining the state the picker is in.
//
// The sentence carries the cases that otherwise render identically as an empty list,
// and they mean opposite things: a server nobody has enumerated (nobody could ask) vs
// one that answered with nothing (it offers nothing). `inventory.py` takes the same
// trouble to keep those apart, and dropping the distinction here would throw away the
// half of it the operator actually reads.
export function toolChoices(server, inventory, rules) {
  const name = (server || "").trim();
  const entry = (inventory || {})[name] || null;
  const mine = (rules || []).filter(r => r && r.server === name);
  const action = new Map(mine.map(r => [r.tool, r.action]));
  const exposed = (entry && entry.tools) || [];
  const readOnly = new Set((entry && entry.read_only) || []);
  const enumerated = Boolean(entry && entry.enumerated);
  const unnameable = Number((entry && entry.unnameable) || 0);
  const status = String((entry && entry.status) || "");

  const unruled = [];
  const ruled = [];
  for (const tool of exposed) {
    const row = { tool, readOnly: readOnly.has(tool), action: action.get(tool) || null };
    (row.action ? ruled : unruled).push(row);
  }
  // Ruled but NOT exposed — the gateway's `ruled_but_absent`, seen from the other end.
  // Claimed only when the server was actually enumerated: before that every rule would
  // look absent, for the sole reason that nobody has been able to ask the server
  // anything yet.
  const absent = enumerated
    ? mine.map(r => r.tool).filter(t => !exposed.includes(t)).sort() : [];

  const parts = [];
  if (!name) {
    parts.push("Pick a server to see what it exposes.");
  } else if (!entry) {
    parts.push(`The gateway has not reported on ${name} yet, so there is nothing to `
             + `pick from. It reports within seconds of a server being enabled.`);
  } else if (!enumerated) {
    parts.push(`${name} is on the gateway's roster but has never been enumerated. This `
             + `is not a claim that it exposes nothing — nobody has been able to ask.`);
  } else if (!exposed.length) {
    parts.push(`${name} answered, and offers no tools.`);
  } else if (unruled.length) {
    parts.push(`${unruled.length} of the ${exposed.length} tools ${name} exposes `
             + `${unruled.length === 1 ? "has" : "have"} no rule, so `
             + `${unruled.length === 1 ? "it is" : "they are"} denied.`);
  } else if (exposed.length === 1) {
    parts.push(`The one tool ${name} exposes has a rule.`);
  } else {
    parts.push(`Every one of the ${exposed.length} tools ${name} exposes has a rule.`);
  }
  // Verbatim from the gateway. "secret missing" and "unreachable" are what an operator
  // most needs here, and they are exactly the states that otherwise arrive as an empty
  // picker with no explanation.
  if (status) parts.push(`The gateway says: ${status}`);
  if (unnameable) {
    // Dropped by `inventory._clean_tools` because no rule could name them — so they
    // are unreachable rather than ungoverned. Counted rather than passed over, so the
    // list can say it is showing fewer tools than the server has.
    parts.push(`${unnameable} name${unnameable === 1 ? "" : "s"} could not be written `
             + `as a rule and ${unnameable === 1 ? "is" : "are"} not listed.`);
  }
  if (absent.length) {
    parts.push(`${absent.length} rule${absent.length === 1 ? "" : "s"} below `
             + `name${absent.length === 1 ? "s" : ""} a tool ${name} did not offer: `
             + `${absent.join(", ")}.`);
  }
  return { server: name, known: Boolean(entry), enumerated, names: exposed.slice(),
           unruled, ruled, absent, unnameable, status,
           seenAt: entry ? Number(entry.seen_at) || null : null,
           note: parts.join(" ") };
}

// What "add rule" is about to write. A deliberately partial mirror of the backend's
// validation, the same division of labour createPreview follows: this EXPLAINS, the
// backend REFUSES. Only the conflict and the no-op are mirrored — both because the fix
// is on screen already — and the tool-name charset is left to the backend's `detail`
// rather than copied into a second grammar that could drift.
//
// It takes no inventory, because the name can only have come FROM the inventory: the
// form offers a picker and nothing else. There is deliberately no check for "a tool
// the server never claimed", since the UI cannot produce one — see the note on the
// form in index.html for why that path was closed rather than warned about.
export function toolRulePreview(server, tool, action, rules) {
  const s = (server || "").trim();
  const t = (tool || "").trim();
  // Defaults to the SAFEST of the three when the action is somehow absent, the same
  // way createPreview defaults to `block`: previewing a deny where an allow was meant
  // is caught by the operator, and the reverse is what this step exists to prevent.
  const verb = toolRank(action) < 0 ? "deny" : action;
  const base = { ok: false, server: s, tool: t, action: verb, danger: false,
                 existing: null, conflict: false, redundant: false, text: "" };
  if (!s) return { ...base, text: "Pick the server whose tool this rule decides for." };
  if (!t) return { ...base, text: "" };
  const existing = (rules || []).find(r => r && r.server === s && r.tool === t) || null;
  if (existing) {
    const same = existing.action === verb;
    return { ...base, existing: existing.action, conflict: !same, redundant: same,
             text: same
               ? `${t} on ${s} already ${toolVerb(verb)}. Nothing to add.`
               : `${t} on ${s} already ${toolVerb(existing.action)}, and nothing ADDED `
                 + `here replaces a rule. Move it in the table below, or revoke it `
                 + `first.` };
  }
  const consequence = verb === "allow"
    ? `The agent may call ${t} on ${s}, and the call runs with no hold and no click.`
    : verb === "ask"
      ? `A call to ${t} on ${s} is put to you before it runs; until you answer, it has `
        + `not run.`
      : `Calls to ${t} on ${s} are refused. An unconfigured tool is refused too — what `
        + `this adds is the record that a human looked and said no.`;
  return { ...base, ok: true,
           // Only `allow` loosens from the default here. `ask` and `deny` both leave a
           // call unable to proceed on its own, so neither grants anything a missing
           // rule did not already withhold.
           danger: verb === "allow",
           text: consequence };
}

// What moving a rule between deny, ask and allow is about to do.
//
// The TRANSITION, not the end state — the same distinction editPreview draws for
// egress, and for the same reason: "allow" describes a row, "denies now, will allow"
// describes what changes about the world.
export function toolEditPreview(rule, action) {
  const verb = toolRank(action) < 0 ? "deny" : action;
  const was = (rule && rule.action) || "";
  const base = { ok: false, action: verb, was, danger: false, unchanged: false,
                 server: (rule && rule.server) || "", tool: (rule && rule.tool) || "",
                 text: "" };
  if (!rule) {
    // The row went away under the button — revoked in another tab. Said out loud
    // rather than left as a click that silently does nothing.
    return { ...base,
             text: "That rule is no longer in the table; it may have been revoked "
                 + "elsewhere." };
  }
  if (was === verb) {
    return { ...base, unchanged: true,
             text: `${base.tool} on ${base.server} already ${toolVerb(verb)}.` };
  }
  return { ...base, ok: true, danger: toolRank(verb) > toolRank(was),
           text: `${base.tool} on ${base.server} ${toolVerb(was)} now; it will `
               + `${verb} instead.` };
}

// What revoking a tool rule does — which is narrow, always, whatever the rule said.
//
// That is the one place this differs from revoking an egress rule, and the difference
// is worth its own function rather than a reworded copy: removing an egress BLOCK
// returns a host to being held, and therefore to being approvable by someone who never
// knew it had been refused. Removing a tool rule returns the tool to unconfigured,
// which DENIES. So the dangerous direction that revokePreview has to warn about does
// not exist here.
export function toolRevokePreview(rule) {
  const tool = (rule && rule.tool) || "";
  const server = (rule && rule.server) || "";
  const was = (rule && rule.action) || "";
  if (was === "deny") {
    // The one case where revoking changes nothing the gateway will do, so the sentence
    // has to be about the RECORD instead — "reviewed and refused" becoming "never
    // looked at" is the whole difference an explicit deny row exists to carry.
    return { danger: false, tool, server, was,
             text: `${tool} on ${server} is denied either way. Removing the rule only `
                 + `removes the record that a human decided it, and it will read as `
                 + `never looked at.` };
  }
  return { danger: false, tool, server, was,
           text: `${tool} on ${server} will no longer ${toolUndo(was)}. With no rule it `
               + `is denied, so this can only take capability away.` };
}

// ── pinned allows: an `ask` answered in advance ──────────────────────────────

// A pin set as the operator reads it: the stored canonical JSON, never a parsed copy,
// with EVERY non-ASCII character spelled out. Stricter than a payload, where only the
// invisible ones are (`payloadHazards`), because a pin value is an identifier and not
// prose. U+0430 CYRILLIC SMALL LETTER A in a pinned `owner` looks exactly like the Latin
// one, and here it means the pin answers calls for a different owner than the one on
// screen.
export function pinText(pinsJson) {
  const raw = typeof pinsJson === "string" ? pinsJson : "";
  const text = escapePayload(raw);
  return { text, escaped: text !== raw };
}

// Whether a pin decides anything now, and if not, which condition fails. `decides` is
// the backend's (api_mcp.api_mcp_pins), so this only NAMES the reason, and the order
// follows `policy._decide_tool`: a row that is not a pin set, then the rule, then the
// server.
export function pinState(pin) {
  const tool = (pin && pin.tool) || "";
  const server = (pin && pin.server) || "";
  if (pin && pin.decides) {
    return { live: true, text: "answers the calls that carry these values" };
  }
  if (!pin || !pin.pins) {
    return { live: false, text: "not a pin set this control plane can read, so it "
                              + "decides nothing" };
  }
  if (!pin.rule) {
    return { live: false, text: `${tool} has no rule, so it is denied and this `
                              + `decides nothing` };
  }
  if (pin.rule !== "ask") {
    return { live: false, text: `the rule ${toolVerb(pin.rule)} ${tool}, and a pin `
                              + `decides only while it asks` };
  }
  return { live: false, text: `${server} is disabled, so this decides nothing` };
}
