// SPDX-License-Identifier: Apache-2.0
/* The MCP surface as the operator configures it — the backend's api_mcp.py, seen
 * from the form: what a server registration will record, the tool policy picker
 * with what a rule written from it will do, and how a pin reads. The backend
 * decides; these shape the previews and have to agree with it. Below them, the MCP
 * tab itself, which `mountMcp` wires when `start()` calls it.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */
import { leaseCountdown, leaseRemaining } from "./leases.js";
import { escapePayload } from "./payload.js";
import { renderListStatus, toolPinsStatus, toolRulesStatus } from "./status.js";
import { fmtStamp, tsSeconds } from "./time.js";

// The DNS label a server name has to be. Three spellings of one rule — here, in the
// relay's path pattern, and in `policy._server_name_error` — each load-bearing in a
// different process. The backend stays the one that DECIDES; this copy only shapes
// the preview, so a drift makes the preview wrong rather than the policy wrong.
export const SERVER_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

const BASIC_TEMPLATE = "Basic {secret}";
// Any spelling of a Basic template, since the scheme name is case-insensitive: the
// encoding note is about what the server will read, not about which preset made it.
const BASIC_RE = /^basic\s+\{secret\}$/i;

// A UI preset expanded into the three fields the store actually holds.
//
// This file is the ONLY place a concrete header scheme is spelled anywhere in the
// system. The gateway builds its request from whatever descriptor the roster carries
// and has no idea which server it belongs to, which is how "no per-server branch in
// the code" stays true while this form still offers a one-click answer for the common
// case.
export function serverDescriptor(kind, header, template) {
  if (kind === "header") {
    return { auth_type: "header", auth_header: "Authorization",
             auth_template: "Bearer {secret}" };
  }
  if (kind === "basic") {
    return { auth_type: "header", auth_header: "Authorization",
             auth_template: BASIC_TEMPLATE };
  }
  if (kind === "custom") {
    return { auth_type: "header", auth_header: (header || "").trim(),
             auth_template: (template || "").trim() };
  }
  return { auth_type: "none", auth_header: null, auth_template: null };
}

// The port and path fields as the API takes them. A port that is not a whole number
// in range is null, which the backend refuses rather than reads as a default.
export function serverEndpoint(port, path) {
  const p = String(port ?? "").trim();
  const n = /^[0-9]{1,5}$/.test(p) ? Number(p) : NaN;
  return { port: n >= 1 && n <= 65535 ? n : null, path: String(path ?? "").trim() };
}

// The preset and custom fields that show a stored descriptor, for the edit form: the
// inverse of serverDescriptor, so a server registered from a preset opens on it.
export function serverPreset(auth) {
  const a = auth || {};
  if (a.type !== "header") return { kind: "none", header: "", template: "" };
  if (a.header === "Authorization" && a.template === "Bearer {secret}") {
    return { kind: "header", header: "", template: "" };
  }
  if (a.header === "Authorization" && a.template === BASIC_TEMPLATE) {
    return { kind: "basic", header: "", template: "" };
  }
  return { kind: "custom", header: a.header || "", template: a.template || "" };
}

// Why an endpoint cannot be sent, or "" if it can — the part of the backend's check
// the form mirrors.
function endpointProblem(endpoint) {
  if (endpoint.port === null) return "The port must be a number from 1 to 65535.";
  if (!endpoint.path.startsWith("/")) return "The path starts with '/', such as /mcp.";
  return "";
}

const authText = d => (d.auth_type === "header"
  ? `${d.auth_header || "(no header)"}: ${d.auth_template || "(no template)"}`
  : "no auth");

// The gateway substitutes the token as it is and encodes nothing, so a Basic token has
// to be stored already encoded. A raw `user:pass` there is a 401.
const basicNote = d => (BASIC_RE.test(d.auth_template || "")
  ? " The token must be the base64 of user:password, already encoded." : "");

// What registering will do, in the world. A DELIBERATELY partial mirror of the
// backend's validation, like createPreview: it describes, and the backend refuses.
export function serverPreview(name, desc, endpoint) {
  const n = (name || "").trim();
  if (!n) return { ok: false, text: "" };
  if (!SERVER_NAME_RE.test(n)) {
    return { ok: false,
             text: `${n} is not a DNS label — lowercase letters, digits and '-', not `
                 + `starting or ending with '-'. It is the container name, so it has `
                 + `to match what mcp-servers.yml declares.` };
  }
  const problem = endpointProblem(endpoint);
  if (problem) return { ok: false, text: problem };
  const where = `Register ${n}, disabled, dialled at `
              + `http://${n}:${endpoint.port}${endpoint.path}.`;
  if (desc.auth_type === "none") {
    return { ok: true,
             text: `${where} The gateway injects nothing — the server holds its own `
                 + `credential. Nothing runs until you enable it and write tool `
                 + `rules.` };
  }
  // Named rather than implied: the token's path is DERIVED from the server name, so
  // the operator can see which file they are about to make load-bearing.
  const text = `${where} The gateway will send ${authText(desc)}, reading the token `
             + `from ${n}.json in the secrets directory.`;
  return { ok: Boolean(desc.auth_header && desc.auth_template),
           text: text + basicNote(desc) };
}

// What saving the edit form will change. The TRANSITION, as toolEditPreview states
// one: each field that moves, from and to, and nothing about the ones that do not.
export function serverEditPreview(row, desc, endpoint) {
  if (!row) {
    return { ok: false, unchanged: false,
             text: "That server is no longer registered; it may have been revoked "
                 + "elsewhere." };
  }
  const problem = endpointProblem(endpoint);
  if (problem) return { ok: false, unchanged: false, text: problem };
  if (desc.auth_type === "header" && !(desc.auth_header && desc.auth_template)) {
    return { ok: false, unchanged: false,
             text: "A custom header needs both a name and a template with {secret}." };
  }
  const n = row.server;
  const was = row.endpoint || {};
  const url = e => `http://${n}:${e.port}${e.path}`;
  const before = serverEditBody(row, row.enabled);
  const changes = [];
  if (was.port !== endpoint.port || was.path !== endpoint.path) {
    changes.push(`endpoint ${url(was)} → ${url(endpoint)}`);
  }
  const authMoves = before.auth_type !== desc.auth_type
    || before.auth_header !== desc.auth_header
    || before.auth_template !== desc.auth_template;
  if (authMoves) changes.push(`auth ${authText(before)} → ${authText(desc)}`);
  if (!changes.length) {
    return { ok: false, unchanged: true,
             text: `This is how ${n} is configured now; nothing to save.` };
  }
  const token = desc.auth_type === "header" && before.auth_type !== "header"
    ? ` The token is read from ${n}.json in the secrets directory.` : "";
  const when = row.enabled
    ? ` ${n} is enabled, so this takes effect at the gateway's next roster poll.`
    : ` ${n} stays disabled.`;
  return { ok: true, unchanged: false,
           text: `${n}: ${changes.join("; ")}.${token}`
               + `${authMoves ? basicNote(desc) : ""}${when}` };
}

// The body of saving the edit form: the server's CURRENT enabled state, so editing
// how it is dialled cannot also switch it.
export function serverSaveBody(row, desc, endpoint) {
  return Object.assign({ enabled: Boolean(row && row.enabled) }, desc, endpoint);
}

// The body of an enable/disable edit.
//
// The descriptor is ECHOED BACK and that is the whole reason this is a function.
// `ServerEditRequest` takes the TARGET state with `auth_type` defaulting to "none",
// so a body carrying only `enabled` does not mean "leave auth alone" — it means "set
// auth to none". A server toggled off and on again would come back stripped of its
// credential descriptor, the next enumeration would send no header, and the 401 that
// followed would read like an expired token rather than like this. The endpoint is
// echoed too; the backend requires it, so a row without one is refused, not reset.
export function serverEditBody(row, enabled) {
  const auth = row.auth || {};
  const endpoint = row.endpoint || {};
  return { enabled: enabled,
           auth_type: auth.type || "none",
           auth_header: auth.header || null,
           auth_template: auth.template || null,
           port: endpoint.port ?? null,
           path: endpoint.path ?? null };
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

// A pin's expiry as its cell in the MCP tab: "never" for a permanent pin, and for a
// timed one the lease countdown, read from the pin's own absolute deadline. `lapsed`
// means the row should be asked about (`pinLapsesDue`).
export function pinExpiry(expiresAt, nowMs) {
  if (tsSeconds(expiresAt) === null) {
    return { text: "never", urgent: false, lapsed: false, timed: false };
  }
  const left = leaseRemaining(expiresAt, nowMs);
  return { ...leaseCountdown(left), lapsed: left === 0, timed: true };
}

// Nothing polls the pins table, so a row that lapses on screen asks for it. Not once
// only: with the browser's clock ahead of the control plane's, the backend still lists
// a pin this page reads as lapsed, and a single refetch would leave the row at "0s"
// until something else redraws. Not every tick either, which would make the tick a
// poll. So a lapsed row asks again this often, for as long as the skew lasts.
export const PIN_LAPSE_RETRY_MS = 5000;

// The keys of the lapsed cells due a refetch now: never asked about, or last asked
// `PIN_LAPSE_RETRY_MS` ago or more. `cells` are `{ key, lapsed }`, the key naming a pin
// and its deadline, so an id reused under a new deadline counts as a new row; `asked`
// maps a key to when it was last asked about.
export function pinLapsesDue(cells, asked, nowMs) {
  return cells.filter(c => c.lapsed).map(c => c.key)
    .filter(key => !asked.has(key) || nowMs - asked.get(key) >= PIN_LAPSE_RETRY_MS);
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

// ── the MCP tab ──────────────────────────────────────────────────────────────
// The DOM half of this surface, wired by `mountMcp`, which `start()` calls. The
// elements are looked up there rather than at import, so the node tests can still
// import this file with no DOM.

export function mountMcp() {
  mountServers();
  mountToolPolicy();
  mountPins();
}

// ── MCP servers: the table and its form ──────────────────────────────────────
// Which of the running servers the gateway may dial. Nothing here starts a
// container and nothing here grants: a registration lands disabled, and an enabled
// server with no tool rules still denies every call.
let serversBody, serversEmpty, serverForm, serverName, serverAuth, serverHeader,
    serverTemplate, serverPort, serverPath, serverPreviewEl, serverAdd, serverCancel;
let serversFailed = false;
const serversByName = new Map();
// The server being edited, or null for the register form. The same form for both,
// for the reason the egress rule form is shared: same fields, same checks.
let editingServer = null;
// True while a save is on the wire, so a re-render cannot turn Save back on.
let serverSaving = false;

const authFields = () =>
  serverDescriptor(serverAuth.value, serverHeader.value, serverTemplate.value);
const endpointFields = () => serverEndpoint(serverPort.value, serverPath.value);

// Looked up on every render rather than captured on entry. The list is not polled,
// so a save fetches it again first (`submitServerEdit`): that is what notices a
// change made in another tab rather than writing over it.
const currentServerPreview = () => (editingServer === null
  ? serverPreview(serverName.value, authFields(), endpointFields())
  : serverEditPreview(serversByName.get(editingServer), authFields(),
                      endpointFields()));

function renderServerPreview() {
  const custom = serverAuth.value === "custom";
  serverHeader.hidden = !custom;
  serverTemplate.hidden = !custom;
  const p = currentServerPreview();
  if (editingServer !== null) serverAdd.disabled = serverSaving || !p.ok;
  serverPreviewEl.textContent = p.text;
}

function enterServerEdit(row) {
  editingServer = row.server;
  // The name is shown but LOCKED: it is the container dialled, the secret's filename
  // and the key every tool rule points at, so changing it is revoke-and-register.
  serverName.value = row.server;
  serverName.readOnly = true;
  const preset = serverPreset(row.auth);
  serverAuth.value = preset.kind;
  serverHeader.value = preset.header;
  serverTemplate.value = preset.template;
  serverPort.value = String((row.endpoint || {}).port ?? "");
  serverPath.value = (row.endpoint || {}).path ?? "";
  serverAdd.textContent = "save changes";
  serverCancel.hidden = false;
  renderServerPreview();
  serverPort.focus();
}

function leaveServerEdit() {
  editingServer = null;
  serverName.readOnly = false;
  serverName.value = "";
  serverAuth.value = "none";
  serverHeader.value = "";
  serverTemplate.value = "";
  serverPort.value = "8082";
  serverPath.value = "/mcp";
  serverAdd.textContent = "register";
  serverAdd.disabled = false;
  serverCancel.hidden = true;
  renderServerPreview();
}

function renderServers(rows) {
  serversByName.clear();
  serversBody.replaceChildren();
  for (const row of rows) {
    serversByName.set(row.server, row);
    const tr = document.createElement("tr");
    const cell = text => {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
      return td;
    };
    cell(row.server);
    cell(row.enabled ? "enabled" : "disabled");
    cell(row.endpoint ? `:${row.endpoint.port}${row.endpoint.path}` : "");
    cell(row.auth && row.auth.type === "header"
           ? `${row.auth.header}: ${row.auth.template}` : "none");
    cell(String(row.tool_rules));
    const actions = document.createElement("td");
    for (const [cls, label] of [["edit", "edit"],
                                ["toggle", row.enabled ? "disable" : "enable"],
                                ["revoke", "revoke"]]) {
      const b = document.createElement("button");
      b.className = cls;
      b.dataset.server = row.server;
      b.textContent = label;
      actions.appendChild(b);
    }
    tr.appendChild(actions);
    serversBody.appendChild(tr);
  }
  document.getElementById("servercount").textContent =
    rows.length ? `${rows.length} registered` : "";
  serversEmpty.hidden = !(serversFailed || rows.length === 0);
  serversEmpty.textContent = serversFailed
    ? "This list did not refresh — the control plane did not answer. What is shown " +
      "may no longer be what the gateway is dialling."
    : (rows.length ? "" :
       "No servers registered, so the gateway dials nothing. Register the container " +
       "name declared in mcp-servers.yml.");
  // The tool-policy picker below reads THIS map for its server list, and the two
  // refreshes race on load — so the picker is rebuilt here rather than left to wait
  // for whichever poll happens to run next. Without it, arriving before the servers
  // did leaves a form that says nothing is registered after everything is.
  renderToolPicker();
  if (editingServer !== null) renderServerPreview();
}

export async function refreshServers() {
  try {
    const res = await fetch("/api/mcp/servers");
    // `res.ok` first, for the reason refreshRules checks it: a 4xx body that parses
    // would render as an empty but SUCCESSFUL list — "no servers registered" is a
    // sentence this page must not say when it simply failed to ask.
    if (!res.ok) throw new Error(String(res.status));
    serversFailed = false;
    renderServers(await res.json());
  } catch (e) {
    serversFailed = true;
    renderServers([...serversByName.values()]);
  }
}

// The current state of one server, fetched rather than remembered: the list is not
// polled, so what this page holds may predate a change made in another tab, and an
// edit is the TARGET state — sending the old copy back would undo that change.
// Null, after an alert, when the list could not be fetched or the server is gone.
async function freshServer(server) {
  await refreshServers();
  if (serversFailed) {
    window.alert("Could not reach the control plane, so nothing was changed.");
    return null;
  }
  const row = serversByName.get(server);
  if (!row) window.alert(`${server} is no longer registered; nothing was changed.`);
  return row || null;
}

// The edit half of that submit: the confirm names the transition against the
// server as it is now, and success leaves edit mode.
async function submitServerEdit() {
  const server = editingServer;
  serverSaving = true;
  serverAdd.disabled = true;
  try {
    const row = await freshServer(server);
    // The form may have moved to another server, or been cancelled, while the fetch
    // was out. Its fields are no longer about `server` then, so nothing is sent.
    if (editingServer !== server) return;
    // The preview is rebuilt from the fresh row; if that changed what saving would
    // do, the operator reads the new preview instead of confirming the old one.
    const p = currentServerPreview();
    if (!row || !p.ok ||
        !window.confirm(`${p.text}\n\nSave this change to ${row.server}?`)) return;
    const res = await fetch(`/api/mcp/servers/${encodeURIComponent(server)}/edit`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(serverSaveBody(row, authFields(), endpointFields())) });
    const answer = await res.json().catch(() => ({}));
    if (!res.ok || !answer.ok) {
      window.alert(`Could not save: ${answer.detail || res.status}`);
      refreshServers();
      return;
    }
    // Only if the form is still on this server: an edit opened on another row while
    // this was in flight is the operator's newer intent.
    if (editingServer === server) leaveServerEdit();
    refreshServers();
  } catch (e) {
    window.alert("Could not save: the control plane is unreachable.");
  } finally {
    serverSaving = false;
    renderServerPreview();
  }
}

function mountServers() {
  serversBody = document.getElementById("servers");
  serversEmpty = document.getElementById("servers-empty");
  serverForm = document.getElementById("server-form");
  serverName = document.getElementById("server-name");
  serverAuth = document.getElementById("server-auth");
  serverHeader = document.getElementById("server-header");
  serverTemplate = document.getElementById("server-template");
  serverPort = document.getElementById("server-port");
  serverPath = document.getElementById("server-path");
  serverPreviewEl = document.getElementById("server-preview");
  serverAdd = document.getElementById("server-add");
  serverCancel = document.getElementById("server-cancel");

  serverAuth.addEventListener("change", renderServerPreview);
  serverName.addEventListener("input", renderServerPreview);
  serverHeader.addEventListener("input", renderServerPreview);
  serverTemplate.addEventListener("input", renderServerPreview);
  serverPort.addEventListener("input", renderServerPreview);
  serverPath.addEventListener("input", renderServerPreview);
  // Nothing has been sent, so there is nothing to undo and no confirm to ask for.
  serverCancel.addEventListener("click", leaveServerEdit);

  serverForm.addEventListener("submit", async ev => {
    ev.preventDefault();
    if (editingServer !== null) {
      await submitServerEdit();
      return;
    }
    const name = serverName.value.trim();
    if (!SERVER_NAME_RE.test(name)) return;
    const body = JSON.stringify(
      Object.assign({ server: name }, authFields(), endpointFields()));
    try {
      const res = await fetch("/api/mcp/servers", {
        method: "POST", headers: { "Content-Type": "application/json" }, body });
      const answer = await res.json().catch(() => ({}));
      if (!res.ok || !answer.ok) {
        // Verbatim. The refusals here are worth reading rather than collapsing: 409
        // says it is already registered and points at edit, and 400 says exactly which
        // half of the descriptor is malformed.
        window.alert(`Could not register: ${answer.detail || res.status}`);
        return;
      }
    } catch (e) {
      window.alert("Could not register: the control plane is unreachable.");
      return;
    }
    serverName.value = "";
    renderServerPreview();
    refreshServers();
  });

  serversBody.addEventListener("click", async ev => {
    const editBtn = ev.target.closest("button.edit");
    if (editBtn) {
      const editRow = serversByName.get(editBtn.dataset.server);
      if (editRow) enterServerEdit(editRow);
      return;
    }
    const btn = ev.target.closest("button.toggle, button.revoke");
    if (!btn) return;
    let row = serversByName.get(btn.dataset.server);
    if (!row) return;
    const name = encodeURIComponent(row.server);
    const revoking = btn.classList.contains("revoke");
    // What the button SAID, which is the operator's intent; the descriptor and endpoint
    // sent with it come from the server as it is now (`freshServer`).
    const enable = !row.enabled;
    if (revoking &&
        !window.confirm(
          `Revoke ${row.server}? The gateway stops dialling it. Its tool rules are ` +
          `not deleted — the backend refuses this while any still name it.`)) {
      return;
    }
    btn.disabled = true;
    if (!revoking) {
      row = await freshServer(row.server);
      if (!row) return;
    }
    try {
      const res = revoking
        ? await fetch(`/api/mcp/servers/${name}/revoke`, { method: "POST" })
        // The descriptor is ECHOED BACK, and it has to be. `ServerEditRequest` takes
        // the TARGET state with `auth_type` defaulting to "none", so an edit carrying
        // only `enabled` would silently strip a server's auth — the next enumeration
        // would send no credential and the 401 would read like a policy problem.
        : await fetch(`/api/mcp/servers/${name}/edit`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(serverEditBody(row, enable)) });
      const answer = await res.json().catch(() => ({}));
      if (!res.ok || !answer.ok) {
        // 409 on revoke is the one an operator most needs to read: it names how many
        // rules still point at the server and tells them to disable instead.
        window.alert(`Could not ${revoking ? "revoke" : "update"}: ` +
                     `${answer.detail || res.status}`);
        btn.disabled = false;
        return;
      }
    } catch (e) {
      window.alert("Could not reach the control plane.");
      btn.disabled = false;
      return;
    }
    refreshServers();
    // A pin on a disabled server decides nothing, so the pins read differently now.
    refreshToolPins();
  });
}

// ── tool policy: the table and its form ──────────────────────────────────────
// The half of this view that actually decides. The servers table above says which
// servers the gateway may dial; this says what may be called on them, and until a
// row exists here an enabled server with a working credential still answers nothing.
let toolRulesBody, toolRulesEmpty, toolRuleForm, toolRuleServer, toolRuleTool,
    toolRuleAction, toolRuleAdd, toolRuleNote, toolRulePreviewEl, toolRuleCountEl;

let toolRules = [];
let toolRulesById = new Map();
let toolRulesFailed = false;
let toolRulesLoaded = false;
let inventory = {};
let inventoryFailed = false;
// What the last submit came back with, outranking the preview while it stands — the
// same split the egress form draws between "what this click would do" and "what the
// last one did", including the refusals this page deliberately does not mirror.
let toolNotice = null;

function renderToolServerOptions() {
  const chosen = toolRuleServer.value;
  const rows = [...serversByName.values()];
  toolRuleServer.replaceChildren();
  for (const row of rows) {
    const opt = document.createElement("option");
    opt.value = row.server;
    // The disabled state rides on the option rather than being left to be read off
    // the table above: a rule written for a server the gateway has stopped dialling
    // decides nothing, and this form is where that is about to happen.
    opt.textContent = row.enabled ? row.server : `${row.server} (disabled)`;
    toolRuleServer.appendChild(opt);
  }
  // Keep the operator's choice across a refresh; otherwise a poll landing mid-edit
  // silently re-points the rule they are composing at another server.
  if (chosen && rows.some(r => r.server === chosen)) toolRuleServer.value = chosen;
}

function renderToolPicker() {
  renderToolServerOptions();
  const choices = toolChoices(toolRuleServer.value, inventory, toolRules);
  const chosen = toolRuleTool.value;
  toolRuleTool.replaceChildren();
  const group = (label, items) => {
    if (!items.length) return;
    const optgroup = document.createElement("optgroup");
    optgroup.label = label;
    for (const item of items) {
      const opt = document.createElement("option");
      opt.value = item.tool;
      // `textContent`, and the whole table below is built the same way: every string
      // here is SERVER-AUTHORED. An escaped template would be correct too, right up
      // until someone edits it — this cannot be got wrong later.
      opt.textContent = item.action
        ? `${item.tool} — ${item.action}${item.readOnly ? ", read-only" : ""}`
        : `${item.tool}${item.readOnly ? " — read-only" : ""}`;
      optgroup.appendChild(opt);
    }
    toolRuleTool.appendChild(optgroup);
  };
  // Unruled FIRST, which is the point of the join rather than a sorting preference:
  // those are the tools denied today, and the only ones a new rule can be written
  // for at all.
  group("no rule yet — denied", choices.unruled);
  group("already ruled", choices.ruled);
  // Keep the operator's choice across a refresh; otherwise an inventory push landing
  // mid-edit silently re-points the rule they are composing at a different tool.
  if (choices.names.includes(chosen)) toolRuleTool.value = chosen;
  // Empty because nothing has been reported, which is a state this form cannot write
  // its way out of — there is no free-text path, deliberately (see index.html). The
  // control is disabled rather than left as an empty box that looks clickable.
  toolRuleTool.disabled = choices.names.length === 0;
  // Both of these are facts about this PAGE rather than about a server, which is why
  // they are appended here instead of inside the pure helper — it would have to be
  // told about registration and about polling to say either.
  //
  // With nothing registered, "pick a server" is advice that cannot be taken, and the
  // backend would refuse the rule anyway: it will not write policy about a server
  // nobody registered. Say what to do instead.
  toolRuleNote.textContent = serversByName.size === 0
    ? "No servers are registered, so there is no tool to write a rule about. "
      + "Register one above first."
    : inventoryFailed
      ? `${choices.note} This list did not refresh — the control plane did not `
        + `answer, so what a server exposes may have changed.`
      : choices.note;
  renderToolRulePreview();
}

function renderToolRulePreview() {
  const p = toolRulePreview(toolRuleServer.value, toolRuleTool.value,
                            toolRuleAction.value, toolRules);
  // With no servers registered the backend refuses every rule this form could
  // produce — it will not write policy about a server nobody registered — so the
  // button is off and the note above says why. The same goes for a server that has
  // reported no tools: there is nothing the picker could have selected.
  toolRuleAdd.disabled = !p.ok || serversByName.size === 0 || toolRuleTool.disabled;
  const text = toolNotice ? toolNotice.text : p.text;
  toolRulePreviewEl.hidden = !text;
  // Colour is never the only cue here either; the wording carries the direction.
  toolRulePreviewEl.className =
    "empty" + ((toolNotice ? toolNotice.bad : p.danger) ? " wild" : "");
  toolRulePreviewEl.textContent = text;
}

function renderToolRules(rows) {
  // Keyed by id so the click handler works from the ROW rather than parsing it back
  // out of the DOM — the same reason the egress table keeps `rulesById`.
  toolRulesById = new Map(rows.map(r => [String(r.id), r]));
  toolRulesBody.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr");
    const cell = (value, cls) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (cls) td.className = cls;
      tr.appendChild(td);
    };
    cell(row.server);
    cell(row.tool);
    const actionCell = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = `tag ${row.action}`;
    tag.textContent = row.action;
    actionCell.appendChild(tag);
    tr.appendChild(actionCell);
    // Whether the server still OFFERS this tool. A rule naming one it does not is
    // inert while reading here exactly like policy in force — the confusion the
    // gateway's `ruled_but_absent` report names, said next to the rule itself.
    // "unknown" and "no" are different facts: the first means nobody has asked.
    const entry = inventory[row.server];
    cell(!entry || !entry.enumerated
           ? "unknown"
           : (entry.tools || []).includes(row.tool) ? "yes" : "no", "ts");
    cell(row.created_at ? fmtStamp(row.created_at) : "", "ts");
    const actions = document.createElement("td");
    // The two actions this rule is NOT, rather than an edit mode: a tool rule's
    // identity is (server, tool) and only the action can change, so promoting one
    // between deny, ask and allow IS the whole edit. A form standing in for the row
    // would be a second place for that to be got wrong.
    for (const target of ["deny", "ask", "allow"]) {
      if (target === row.action) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "edit";
      button.dataset.rule = String(row.id);
      button.dataset.action = target;
      button.textContent = `→ ${target}`;
      actions.appendChild(button);
    }
    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.className = "revoke";
    revoke.dataset.rule = String(row.id);
    revoke.textContent = "revoke";
    actions.appendChild(revoke);
    tr.appendChild(actions);
    toolRulesBody.appendChild(tr);
  }
  toolRuleCountEl.textContent =
    rows.length ? `· ${rows.length} rule${rows.length === 1 ? "" : "s"}` : "· none";
  renderListStatus(toolRulesEmpty,
                   toolRulesStatus(rows.length, toolRulesFailed, toolRulesLoaded));
  // The picker reads these rows for its join and its conflict check, so a rule that
  // appeared elsewhere shows up there rather than waiting to surface as a 409.
  renderToolPicker();
}

export async function refreshToolRules() {
  try {
    const res = await fetch("/api/mcp/rules");
    // `res.ok` first, for the reason refreshRules checks it: a 4xx body that parsed
    // would render as an empty but SUCCESSFUL policy — and an empty tool policy is a
    // sentence meaning "everything is denied", which must not be said on a failure.
    if (!res.ok) throw new Error(String(res.status));
    toolRules = await res.json();
    toolRulesFailed = false;
    toolRulesLoaded = true;
  } catch (e) {
    // Keeps the rows and SAYS SO. A tool-policy table that quietly stops refreshing
    // misstates what the gateway is letting through, which is the one question this
    // view exists to answer.
    toolRulesFailed = true;
  }
  renderToolRules(toolRules);
}

export async function refreshInventory() {
  try {
    const res = await fetch("/api/mcp/inventory");
    if (!res.ok) throw new Error(String(res.status));
    inventory = await res.json();
    inventoryFailed = false;
  } catch (e) {
    // The last picture stands. Unlike the rules above, nothing here decides
    // anything — the inventory only adds context — so a failed poll costs accuracy
    // in the picker and in the "exposed" column. The NOTE is where that is said;
    // the column cannot carry it without a fourth value meaning "stale", which
    // would be a distinction nobody could act on differently from "unknown".
    inventoryFailed = true;
  }
  renderToolRules(toolRules);
}

function mountToolPolicy() {
  toolRulesBody = document.getElementById("toolrules");
  toolRulesEmpty = document.getElementById("toolrules-empty");
  toolRuleForm = document.getElementById("toolrule-form");
  toolRuleServer = document.getElementById("toolrule-server");
  toolRuleTool = document.getElementById("toolrule-tool");
  toolRuleAction = document.getElementById("toolrule-action");
  toolRuleAdd = document.getElementById("toolrule-add");
  toolRuleNote = document.getElementById("toolrule-note");
  toolRulePreviewEl = document.getElementById("toolrule-preview");
  toolRuleCountEl = document.getElementById("toolrulecount");

  for (const el of [toolRuleServer, toolRuleTool, toolRuleAction]) {
    // Any edit invalidates the last submit's verdict — leaving it up would attach a
    // refusal to a rule that is no longer the one on screen.
    el.addEventListener("change", () => { toolNotice = null; renderToolPicker(); });
  }

  toolRuleForm.addEventListener("submit", async ev => {
    // Always, for the reason the egress form gives: `form-action 'none'` in the CSP
    // makes the native submit a fail-closed backstop rather than a broken form.
    ev.preventDefault();
    const p = toolRulePreview(toolRuleServer.value, toolRuleTool.value,
                              toolRuleAction.value, toolRules);
    if (!p.ok) return;
    // NO CONFIRM STEP, and dropping it was a correction rather than a relaxation. The
    // egress form confirms because its pattern is DERIVED — `example.com` and
    // `*.example.com` are different grants and the controls do not say which you picked,
    // so the dialog is the first place the breadth is visible. Nothing here is derived:
    // the server and the tool are chosen from two lists, the action from a third, and
    // `#toolrule-preview` renders this exact sentence live, BEFORE the click. A modal
    // repeating it afterwards asked the operator to read the same text twice and moved
    // the disclosure to the wrong side of the decision.
    //
    // The friction that remains is on the TABLE, where a row action is a single click
    // with no preview beside it, and only when that click widens — see the handler
    // below. Ruling a long tool list is mostly `deny` and `ask`, and neither should cost
    // a dialog.
    toolRuleAdd.disabled = true;
    try {
      const res = await fetch("/api/mcp/rules", {
        method: "POST", headers: { "content-type": "application/json" },
        // Straight off the PREVIEW, never re-read from the controls: what was
        // confirmed has to be what is sent, which is the same single-derivation
        // discipline the egress form follows for its pattern and class.
        body: JSON.stringify({ server: p.server, tool: p.tool, action: p.action }) });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // Verbatim. The refusals worth reading rather than collapsing into "failed"
        // are 400 (a tool name this page did not mirror a check for), and 409 — a rule
        // appeared under us, so the table on screen was stale.
        toolNotice = { bad: true,
                       text: `Not added: ${body.detail
                                           || `the control plane answered ${res.status}`}` };
      } else if (body.created === false) {
        // A 200 that wrote nothing, because the same rule arrived between the preview
        // and the click. Reported rather than read as a write.
        toolNotice = { bad: false,
                       text: `${body.tool} on ${body.server} was already a standing `
                           + `${body.action} rule; nothing was written.` };
      } else {
        toolNotice = { bad: false,
                       text: `Added: ${body.tool} on ${body.server} now `
                           + `${body.action}s.` };
      }
    } catch (e) {
      toolNotice = { bad: true, text: "Not added: the control plane is unreachable." };
    }
    toolRuleAdd.disabled = false;
    await refreshToolRules();
    // The servers table counts rules per server, so it is stale the moment this lands.
    refreshServers();
    refreshToolPins();
  });

  // Delegated, because the table is replaced wholesale on every refresh — a handler
  // bound per button would be lost with the row it was bound to.
  toolRulesBody.addEventListener("click", async ev => {
    const btn = ev.target.closest("button.edit, button.revoke");
    if (!btn) return;
    const row = toolRulesById.get(btn.dataset.rule);
    if (!row) return;
    const revoking = btn.classList.contains("revoke");
    const p = revoking ? toolRevokePreview(row) : toolEditPreview(row, btn.dataset.action);
    if (!revoking && !p.ok) return;
    // CONFIRMED ONLY WHEN THE CLICK WIDENS, which `toolEditPreview` already computes as
    // `danger` — `toolRank(to) > toolRank(from)`, so deny→ask counts as well as anything
    // →allow. The direction is the whole criterion: a row button is one click with no
    // preview beside it, so the dialog is this path's only disclosure, and it is worth
    // spending exactly where capability increases.
    //
    // Revoking never asks, and that is not an omission. Removing a tool rule returns the
    // tool to unconfigured, which DENIES — `toolRevokePreview` exists as its own function
    // because this is the one place the tool surface differs from the egress one, where
    // removing a block returns a host to being HELD and therefore to being approvable.
    // Narrowing does not need a guard rail.
    if (!revoking && p.danger
        && !window.confirm(`${p.text}\n\nSave this rule?`)) {
      return;
    }
    btn.disabled = true;
    const id = encodeURIComponent(String(row.id));
    try {
      // Two whole literal paths rather than one built from a ternary, which is the
      // shape the servers handler above uses and not an accident: the relay allowlist
      // is matched against the literal each `fetch` begins with, and a path assembled
      // by concatenation is invisible to the test that holds the two ends together.
      const res = revoking
        ? await fetch(`/api/mcp/rules/${id}/revoke`, { method: "POST" })
        : await fetch(`/api/mcp/rules/${id}/edit`, {
            method: "POST", headers: { "content-type": "application/json" },
            // The row this button was rendered with, so a table older than the rule
            // now holding this id is refused (409) rather than edits that rule.
            body: JSON.stringify({ action: btn.dataset.action,
                                   expected_server: row.server, expected_tool: row.tool,
                                   expected_action: row.action }) });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // 404 and 409 are the ones worth reading: the rule was revoked or changed in
        // another tab, so the table on screen is stale and retrying cannot help.
        window.alert(`Could not ${revoking ? "revoke" : "update"}: ` +
                     `${body.detail || res.status}`);
        btn.disabled = false;
        return;
      }
    } catch (e) {
      window.alert("Could not reach the control plane.");
      btn.disabled = false;
      return;
    }
    await refreshToolRules();
    refreshServers();
    // A pin decides only while its rule asks, so moving the rule moves the pins.
    refreshToolPins();
  });
}

// ── pinned allows: the table ─────────────────────────────────────────────────
// Beside the rules they sit under, and in a table of their own for the reason the
// live leases are not rows of the standing policy: a pin is a narrower allow on top
// of one rule, and a tool can have several.
let toolPinsBody, toolPinsEmpty, toolPinCountEl;
let toolPins = [];
let toolPinsById = new Map();
let toolPinsFailed = false;
let toolPinsLoaded = false;
function renderToolPins(rows) {
  toolPinsById = new Map(rows.map(r => [String(r.id), r]));
  toolPinsBody.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr");
    const cell = (value, cls) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (cls) td.className = cls;
      tr.appendChild(td);
      return td;
    };
    cell(row.server);
    cell(row.tool);
    // The values came from an agent's payload, so textContent, as on the card.
    const shown = pinText(row.pins_json);
    const pinnedCell = document.createElement("td");
    const code = document.createElement("code");
    code.textContent = shown.text;
    pinnedCell.appendChild(code);
    if (shown.escaped) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "non-ASCII spelled out";
      pinnedCell.appendChild(note);
    }
    tr.appendChild(pinnedCell);
    const state = pinState(row);
    const stateCell = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = `tag ${state.live ? "allow" : "inert"}`;
    tag.textContent = state.live ? "live" : "inert";
    stateCell.append(tag, ` ${state.text}`);
    tr.appendChild(stateCell);
    // `data-expires` lets the one-second tick move it, as in the leases table.
    const left = pinExpiry(row.expires_at, Date.now());
    const leftCell = cell(left.text,
                          left.timed ? `lease-left${left.urgent ? " urgent" : ""}` : "");
    if (left.timed) {
      leftCell.dataset.expires = String(row.expires_at);
      leftCell.dataset.pin = String(row.id);
    }
    cell(row.created_at ? fmtStamp(row.created_at) : "", "ts");
    // Not a revoke-time confirm: taking a pin back only sends its calls to a card.
    const actions = document.createElement("td");
    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.className = "revoke";
    revoke.dataset.pin = String(row.id);
    revoke.textContent = "revoke";
    actions.appendChild(revoke);
    tr.appendChild(actions);
    toolPinsBody.appendChild(tr);
  }
  toolPinCountEl.textContent =
    rows.length ? `· ${rows.length} pin${rows.length === 1 ? "" : "s"}` : "· none";
  renderListStatus(toolPinsEmpty,
                   toolPinsStatus(rows.length, toolPinsFailed, toolPinsLoaded));
}

// The expiry cells, as `updateLeaseCountdowns` moves the leases'. The table is
// redrawn only when a lapsed row is due a refetch (`pinLapsesDue`).
const pinLapsesAsked = new Map();
export function updatePinCountdowns() {
  const now = Date.now();
  const cells = [];
  for (const cell of toolPinsBody.querySelectorAll("td.lease-left")) {
    const left = pinExpiry(cell.dataset.expires, now);
    cell.textContent = left.text;
    cell.classList.toggle("urgent", left.urgent);
    cells.push({ key: `${cell.dataset.pin}@${cell.dataset.expires}`,
                 lapsed: left.lapsed });
  }
  // Only the rows on screen are remembered, so the map is no longer than the table.
  const shown = new Set(cells.map(c => c.key));
  for (const key of pinLapsesAsked.keys()) {
    if (!shown.has(key)) pinLapsesAsked.delete(key);
  }
  const due = pinLapsesDue(cells, pinLapsesAsked, now);
  for (const key of due) pinLapsesAsked.set(key, now);
  if (due.length) refreshToolPins();
}

export async function refreshToolPins() {
  try {
    const res = await fetch("/api/mcp/pins");
    // `res.ok` first, as for the rules: a refused poll rendered as an empty list
    // would say no call is answered without a card, which is the one thing a
    // failure must not say.
    if (!res.ok) throw new Error(String(res.status));
    toolPins = await res.json();
    toolPinsFailed = false;
    toolPinsLoaded = true;
  } catch (e) {
    toolPinsFailed = true;
  }
  renderToolPins(toolPins);
}

function mountPins() {
  toolPinsBody = document.getElementById("toolpins");
  toolPinsEmpty = document.getElementById("toolpins-empty");
  toolPinCountEl = document.getElementById("toolpincount");

  toolPinsBody.addEventListener("click", async ev => {
    const btn = ev.target.closest("button.revoke");
    if (!btn) return;
    const row = toolPinsById.get(btn.dataset.pin);
    if (!row) return;
    btn.disabled = true;
    const id = encodeURIComponent(String(row.id));
    try {
      const res = await fetch(`/api/mcp/pins/${id}/revoke`, { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // A 404 is the ordinary race, as for a lease: the pin was revoked elsewhere,
        // or lapsed and was swept, after this table was drawn.
        window.alert(res.status === 404
          ? "That pin is already gone — it was revoked elsewhere, or expired."
          : `Could not revoke: ${body.detail || res.status}`);
        btn.disabled = false;
        refreshToolPins();
        return;
      }
    } catch (e) {
      window.alert("Could not reach the control plane.");
      btn.disabled = false;
      return;
    }
    refreshToolPins();
  });
}
