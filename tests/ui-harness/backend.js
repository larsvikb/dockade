// SPDX-License-Identifier: Apache-2.0
// The control plane the page talks to, faked. Nothing here has to be what the real
// backend would answer: both of ui-diff's runs get the same answers, so only a
// difference in what the PAGE does with them shows up in the diff, and
// approval.test.js and failing-state.test.js work their expected values out from the
// values here. Each fixture only has to reach the rendering branch it exists for.

// Every run sees the same instant (see the clock in page.js), so a stamp here
// renders the same text in both runs.
export const NOW_MS = Date.UTC(2026, 8, 15, 10, 0, 0);
const NOW = NOW_MS / 1000;

const CONFIG = {
  hold_timeout: 120, lease_seconds: 1800, pin_lease_seconds: 3600,
  client_classes: ["agent", "mcp"],
};

const AUDIT = {
  rows: [
    { id: 3, ts: NOW - 30, kind: "allow", host: "pypi.org", port: 443,
      client: "172.28.0.5", client_class: "agent", reason: "rule .pypi.org", n: 4,
      first_ts: NOW - 600 },
    // Markup in a field the agent can influence, so the run shows it rendered as text.
    { id: 2, ts: NOW - 90, kind: "deny", host: "evil.example", stage: "http",
      client: "172.28.0.5", client_class: "agent",
      reason: `operator: <img src=x onerror="alert('x')"> & more`,
      actor: "operator@127.0.0.1" },
    { id: 1, ts: NOW - 300, kind: "tool-ask", server: "mcp-github", tool: "get_me",
      client: "172.28.0.5", reason: "rule ask", fail_closed: false },
  ],
  total: 3, filtered: false, next: null,
};

export const RULES = [
  { id: 1, action: "allow", pattern: ".pypi.org", scope: "pypi.org and subdomains",
    client_class: "agent", source: "seed", created_at: NOW - 86400 },
  { id: 2, action: "block", pattern: "evil.example", scope: "exact host",
    client_class: "agent", source: "operator", created_at: NOW - 3600 },
];

const LEASES = [
  { id: 1, host: "files.pythonhosted.org", client_class: "agent",
    expires_at: NOW + 900, granted_by: "operator@127.0.0.1" },
  { id: 2, host: "a.cdn.example", client_class: "agent", expires_at: NOW + 300 },
  // Markup in the host, which the agent chooses, so the run shows it rendered as text.
  { id: 3, host: `b<img src=x onerror="alert('x')">.cdn.example`, client_class: "agent",
    expires_at: NOW + 600 },
];

const SERVERS = [
  { server: "mcp-github", enabled: true, tool_rules: 2,
    endpoint: { port: 8082, path: "/mcp" },
    auth: { type: "header", header: "Authorization", template: "Bearer {secret}" } },
  { server: "mcp-notes", enabled: false, tool_rules: 0,
    endpoint: { port: 8082, path: "/mcp" }, auth: { type: "none" } },
];

const TOOL_RULES = [
  { id: 1, server: "mcp-github", tool: "get_me", action: "allow",
    created_at: NOW - 7200 },
  { id: 2, server: "mcp-github", tool: "create_pull_request", action: "ask",
    created_at: NOW - 7200 },
];

const INVENTORY = {
  "mcp-github": { enumerated: true, status: "ok", seen_at: NOW - 20,
                  tools: ["get_me", "create_pull_request", "list_commits"],
                  read_only: ["get_me", "list_commits"], unnameable: 0 },
};

// The sandbox every tool card here came from, as the prefix it printed at boot.
const SANDBOX = "a1b2c3d4";

// The first pin is timed, so it answers only the sandbox its card came from. The second
// was made under arguments the inventory has since seen move.
const PINS = [
  { id: 4, server: "mcp-github", tool: "create_pull_request", rule: "ask",
    pins: { owner: "acme", repo: "widgets" },
    pins_json: '{"owner": "acme", "repo": "widgets"}', decides: true,
    bound: true, schema_moved: false, expires_at: NOW + 1800, created_at: NOW - 600,
    sandbox_prefix: SANDBOX },
  { id: 6, server: "mcp-github", tool: "create_pull_request", rule: "ask",
    pins: { owner: "acme" }, pins_json: '{"owner": "acme"}', decides: true,
    bound: true, schema_moved: true, expires_at: null, created_at: NOW - 900,
    sandbox_prefix: null },
];

// The policy export, as `api_mcp._export_bytes` writes it: answered as raw bytes, not
// JSON, since the page must save exactly these. The whole-second stamps carry Python's
// `.0`, which `JSON.stringify` drops, so a page that parsed and re-serialized the body
// would save other bytes.
export const POLICY_EXPORT = `{
  "format": "dockade-policy",
  "format_version": 1,
  "mcp_servers": [
    {
      "server": "mcp-github",
      "enabled": true,
      "auth": {
        "type": "header",
        "header": "Authorization",
        "header_withheld": false,
        "template": "Bearer {secret}",
        "template_withheld": false
      },
      "endpoint": {
        "port": 8082,
        "path": "/mcp",
        "path_withheld": false
      },
      "created_at": ${NOW - 86400}.0
    }
  ],
  "tool_rules": [
    {
      "server": "mcp-github",
      "tool": "get_me",
      "action": "allow",
      "source": "operator",
      "created_at": ${NOW - 7200}.0
    }
  ]
}
`;

// One hold of each kind the page draws a card for, both with something to confirm:
// the egress hold two persist patterns, the tool ask two pinnable fields.
export const HOLDS = [
  { id: "egress-1", kind: "egress", host: "registry.example.org", port: 443,
    proto: "https", ts: NOW - 10, client: "172.28.0.5", client_class: "agent",
    requests: 2,
    persist_options: [
      { pattern: "registry.example.org", scope: "exact host" },
      { pattern: ".example.org", scope: "example.org and subdomains" },
    ] },
  { id: "tool-1", kind: "tool", server: "mcp-github", tool: "create_pull_request",
    ts: NOW - 5, deadline: NOW + 115, client: "172.28.0.5", sandbox_prefix: SANDBOX,
    args_json: '{"owner": "acme", "repo": "gadgets", "title": "Fix the build"}',
    pin_options: {
      fields: [{ field: "owner", value: "acme" }, { field: "repo", value: "gadgets" }],
      unpinnable: [{ field: "title", why: "free text" }], refused: null,
      stale: [], timed_refused: null,
    } },
];

// The saturation banner's two readings, for failing-state.test.js to push on the
// stream, shaped as holds._saturation sends them; scenario.js pushes neither. A record
// of three rejections none acknowledged, the last stamped 55 s before NOW at the per-
// client card cap, the count kept since an hour before NOW; and a load with no
// rejections, 12 of 16 requests held on 3 of 8 cards, the requests at the 75% that
// warns and the cards under it.
export const SATURATION = {
  in_flight: 0, cards: 0, max_waiters: 16, max_pending: 8,
  rejections: 3, acknowledged: 0, last_ts: NOW - 55,
  last_scope: "client 172.28.0.5 cards", last_host: "registry.example.org",
  since: NOW - 3600,
};
export const LOAD = {
  in_flight: 12, cards: 3, max_waiters: 16, max_pending: 8,
  rejections: 0, acknowledged: 0, last_ts: null, last_scope: null, last_host: null,
  since: NOW - 3600,
};

// A tool ask whose call met two pins made under other arguments of the tool: a
// permanent one a later card has rebound, which this card may neither rebind back nor
// revoke, and its own sandbox's timed one, which it may rebind.
export const STALE_HOLD = {
  id: "tool-2", kind: "tool", server: "mcp-github", tool: "create_pull_request",
  ts: NOW + 20, deadline: NOW + 135, client: "172.28.0.5", sandbox_prefix: SANDBOX,
  args_json: '{"owner": "acme", "repo": "widgets", "title": "Bump"}',
  pin_options: {
    fields: [{ field: "owner", value: "acme" }, { field: "repo", value: "widgets" }],
    unpinnable: [{ field: "title", why: "free text" }], refused: null,
    timed_refused: null,
    stale: [{ id: 6, fields: ["owner"], sandbox_prefix: null,
              refused: "pin 6 was pinned again from a card raised after this one, so "
                     + "this card cannot bind it back to the arguments it was made "
                     + "under; allow this call without a pin instead",
              revoke_refused: "pin 6 was pinned again from a card raised after this "
                            + "one, so this card cannot revoke it as made under older "
                            + "arguments; allow this call without a pin instead" },
            { id: 4, fields: ["owner", "repo"], sandbox_prefix: SANDBOX, refused: null,
              revoke_refused: null }],
  } };

// The record view, two pages of it: the first says there is an older one, and the
// second, asked for with that cursor, says there is not.
const EVENTS = {
  rows: [
    { id: 12, ts: NOW - 40, kind: "deny", host: "plain.example", port: 80, proto: "http",
      stage: "http", method: "GET", url: "http://plain.example/setup.sh",
      client: "172.28.0.5", client_class: "agent", reason: "operator" },
    { id: 11, ts: NOW - 60, kind: "allow", host: "pypi.org", port: 443,
      client: "172.28.0.5", client_class: "agent", reason: "rule .pypi.org" },
  ],
  total: 102, filtered: false, next: "cursor-11",
};
const EVENTS_OLDER = {
  rows: [{ id: 2, ts: NOW - 90, kind: "deny", host: "evil.example", stage: "http",
           client: "172.28.0.5", client_class: "agent", reason: "operator" }],
  total: 102, filtered: false, next: null,
};

const GETS = {
  "/api/config": CONFIG,
  "/api/audit": AUDIT,
  "/api/audit/events": EVENTS,
  "/api/egress/rules": RULES,
  "/api/egress/leases": LEASES,
  "/api/mcp/servers": SERVERS,
  "/api/mcp/rules": TOOL_RULES,
  "/api/mcp/inventory": INVENTORY,
  "/api/mcp/pins": PINS,
  // Where the header's link points, which this UI answers itself (control-plane-ui/app.py).
  "/tasks-link": { url: "http://127.0.0.1:28091/" },
};

function resolved(id, body) {
  const allow = body.action.startsWith("allow");
  const answer = { ok: true, id, outcome: allow ? "allow" : "deny" };
  if (body.action.endsWith("_persist")) {
    Object.assign(answer, { persisted: true, pattern: body.pattern });
  }
  if (body.action.endsWith("_lease")) {
    Object.assign(answer, { leased: true, lease_expires_at: NOW + 1800 });
  }
  if (body.pins) {
    // The stale card's pins rebind pin 4, its sandbox's timed pin, which a pin for
    // good also makes permanent; any other card's make pin 5.
    const rebound = id === STALE_HOLD.id;
    const timed = body.action === "allow_pinned_lease";
    answer.pin = { id: rebound ? 4 : 5, created: !rebound, rebound, fields: body.pins,
                   expires_at: timed ? NOW + 3600 : null,
                   sandbox_prefix: timed ? SANDBOX : null,
                   promoted: rebound && !timed, revoked: null };
  }
  return answer;
}

// The MCP tab's writes. Each answers as the backend does when the write lands; the
// lists it would change stay as they were, since every read above is fixed.
const MCP_WRITES = [
  [/^\/api\/mcp\/servers$/, () => ({ ok: true })],
  [/^\/api\/mcp\/servers\/[^/]+\/(edit|revoke)$/, () => ({ ok: true })],
  [/^\/api\/mcp\/rules$/, body => ({ ok: true, created: true, ...body })],
  [/^\/api\/mcp\/rules\/[^/]+\/(edit|revoke)$/, () => ({ ok: true })],
  [/^\/api\/mcp\/pins\/[^/]+\/revoke$/, () => ({ ok: true })],
];

// A standing rule's create, edit and revoke, answered as ones that landed on the
// fixture's rule. A create's answer follows its pattern: one already there
// (`created: false`), one refused because a rule appeared under the form (409), and any
// other written.
const RULE_WRITES = [
  [/^\/api\/egress\/rules$/, body => (body.pattern === "refused.example"
    ? { status: 409, json: { ok: false, detail: "a rule for refused.example already "
                                                + "exists for agent" } }
    : { ok: true, created: body.pattern !== "already.example", ...body })],
  [/^\/api\/egress\/rules\/[^/]+\/revoke$/, () => ({ ok: true })],
  [/^\/api\/egress\/rules\/([^/]+)\/edit$/, (body, [, id]) => {
    const rule = RULES.find(r => String(r.id) === decodeURIComponent(id));
    return { ok: true, changed: true, pattern: body.pattern, action: body.action,
             client_class: rule.client_class,
             previous: { pattern: rule.pattern, action: rule.action } };
  }],
];

// What the scenario's last steps change: the rules read answering a new list or
// failing, the config read dropped by the network, and the tool rules read answered
// after every other. Each stays in force for the rest of the run once a step sets it,
// which is why those steps come last.
let rulesFailing = false;
let configAborted = false;
let toolRulesLast = false;
export function setRules(rows) { GETS["/api/egress/rules"] = rows; }
export function failRules() { rulesFailing = true; }
export function abortConfig() { configAborted = true; }
export function answerToolRulesLast() { toolRulesLast = true; }
// How the policy export answers: "ok" with POLICY_EXPORT, "fail" with a 500, "drop" as
// a request the network drops. The tests' only.
let exportAnswer = "ok";
export function answerExport(how) { exportAnswer = how; }

// The tests', never the scenario's: each test in approval.test.js and
// failing-state.test.js starts from `reset()`. `overrideResolve` changes what the
// backend says it resolved, so the test can tell a card that reports the response from
// one that reports the click; `overrideAck` replaces the answer to a saturation
// acknowledgement, `{status, json}`, so the test can tell a banner that takes the count
// the backend recorded from one that keeps its own. ui-diff never calls any of them, so
// the switches above still carry from step to step there.
let resolveOverride = null;
let ackOverride = null;
export function overrideResolve(change) { resolveOverride = change; }
export function overrideAck(answer) { ackOverride = answer; }
export function reset() {
  GETS["/api/egress/rules"] = RULES;
  rulesFailing = configAborted = toolRulesLast = false;
  exportAnswer = "ok";
  answered.length = 0;
  resolveOverride = null;
  ackOverride = null;
}

// Which requests the harness answers after every other one it holds (answerQueue). A
// GET only: a write to the same path keeps its place.
export function answeredLast(method, path) {
  return toolRulesLast && method === "GET" && path === "/api/mcp/rules";
}
// The order requests were answered in once that is in force, so the step can check
// the deferral happened: both sides of ui-diff would otherwise read alike without it.
const answered = [];
export function answerOrder() { return answered.slice(); }

// `{status, json}` for a request, `{status, body, contentType}` for one answered with
// raw bytes, `{abort: true}` for one the network drops, or null for one this backend
// does not know — which the harness records, since a path the page
// asks for and no fixture answers is either a fixture to add or a change in what the
// page asks.
export function respond(method, path, body, query) {
  if (toolRulesLast) answered.push(`${method} ${path}`);
  if (path === "/api/config" && configAborted) return { abort: true };
  if (method === "GET" && path === "/api/egress/rules" && rulesFailing) {
    return { status: 503, json: { detail: "upstream unavailable" } };
  }
  // A filter the backend refuses, said in its own words, as `_bad_filter` does.
  if (path.startsWith("/api/audit") && query.get("q") === "refuse me") {
    return { status: 400, json: { detail: "q: the search text is longer than allowed" } };
  }
  if (path === "/api/audit/events" && query.get("before") === "cursor-11") {
    return { status: 200, json: EVENTS_OLDER };
  }
  // Lease 2 lapsed between the table being drawn and the click, as the backend's 404
  // says; any other revoke lands. The list stays fixed, like the MCP tab's.
  const lease = path.match(/^\/api\/egress\/leases\/([^/]+)\/revoke$/);
  if (method === "POST" && lease) {
    return lease[1] === "2"
      ? { status: 404, json: { ok: false, detail: "unknown lease" } }
      : { status: 200, json: { ok: true } };
  }
  if (method === "GET" && path === "/api/mcp/export") {
    if (exportAnswer === "drop") return { abort: true };
    return exportAnswer === "fail"
      ? { status: 500, json: { detail: "internal error" } }
      : { status: 200, body: POLICY_EXPORT, contentType: "application/json" };
  }
  if (method === "GET" && path in GETS) return { status: 200, json: GETS[path] };
  const resolve = path.match(/^\/approvals\/([^/]+)\/resolve$/);
  // The agent withdrew this ask before the click landed.
  if (method === "POST" && resolve && resolve[1] === "tool-409") {
    return { status: 409, json: { ok: false, status: "withdrawn" } };
  }
  if (method === "POST" && resolve) {
    const answer = resolved(decodeURIComponent(resolve[1]), body);
    return { status: 200, json: resolveOverride ? resolveOverride(answer) : answer };
  }
  // Stands in for the real endpoint's answer, echoing the count sent as both what it
  // recorded and the total there has been. It does not model the real endpoint's
  // clamp to that total or its mark that never lowers (api_views.py,
  // api_saturation_ack); a test wanting another answer sets `overrideAck`. The page
  // reads only `acknowledged`.
  if (method === "POST" && path === "/api/saturation/ack") {
    return ackOverride ?? { status: 200, json: { ok: true, acknowledged: body.count,
                                                 rejections: body.count } };
  }
  const write = method === "POST"
    && [...MCP_WRITES, ...RULE_WRITES].find(([re]) => re.test(path));
  if (write) {
    const answer = write[1](body, path.match(write[0]));
    return "status" in answer ? answer : { status: 200, json: answer };
  }
  return null;
}
