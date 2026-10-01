// SPDX-License-Identifier: Apache-2.0
// The control plane the page talks to, faked. Nothing here has to be what the real
// backend would answer: both runs get the same answers, so only a difference in what
// the PAGE does with them shows up in the diff. Each fixture only has to reach the
// rendering branch it exists for.

// Every run sees the same instant (see the clock in harness.js), so a stamp here
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
    { id: 2, ts: NOW - 90, kind: "deny", host: "evil.example", stage: "http",
      client: "172.28.0.5", client_class: "agent", reason: "operator",
      actor: "operator@127.0.0.1" },
    { id: 1, ts: NOW - 300, kind: "tool-ask", server: "mcp-github", tool: "get_me",
      client: "172.28.0.5", reason: "rule ask", fail_closed: false },
  ],
  total: 3, filtered: false, next: null,
};

const RULES = [
  { id: 1, action: "allow", pattern: ".pypi.org", scope: "pypi.org and subdomains",
    client_class: "agent", source: "seed", created_at: NOW - 86400 },
  { id: 2, action: "block", pattern: "evil.example", scope: "exact host",
    client_class: "agent", source: "operator", created_at: NOW - 3600 },
];

const LEASES = [
  { id: 1, host: "files.pythonhosted.org", client_class: "agent",
    expires_at: NOW + 900, granted_by: "operator@127.0.0.1" },
  { id: 2, host: "a.cdn.example", client_class: "agent", expires_at: NOW + 300 },
  { id: 3, host: "b.cdn.example", client_class: "agent", expires_at: NOW + 600 },
];

const SERVERS = [
  { server: "mcp-github", enabled: true, tool_rules: 2,
    auth: { type: "header", header: "Authorization", template: "Bearer {token}" } },
  { server: "mcp-notes", enabled: false, tool_rules: 0, auth: { type: "none" } },
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

const PINS = [
  { id: 4, server: "mcp-github", tool: "create_pull_request", rule: "ask",
    pins: { owner: "acme", repo: "widgets" },
    pins_json: '{"owner": "acme", "repo": "widgets"}', decides: true,
    expires_at: NOW + 1800, created_at: NOW - 600 },
];

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
    ts: NOW - 5, deadline: NOW + 115, client: "172.28.0.5",
    args_json: '{"owner": "acme", "repo": "gadgets", "title": "Fix the build"}',
    pin_options: {
      fields: [{ field: "owner", value: "acme" }, { field: "repo", value: "gadgets" }],
      unpinnable: [{ field: "title", why: "free text" }], refused: null,
    } },
];

const GETS = {
  "/api/config": CONFIG,
  "/api/audit": AUDIT,
  "/api/audit/events": AUDIT,
  "/api/egress/rules": RULES,
  "/api/egress/leases": LEASES,
  "/api/mcp/servers": SERVERS,
  "/api/mcp/rules": TOOL_RULES,
  "/api/mcp/inventory": INVENTORY,
  "/api/mcp/pins": PINS,
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
    answer.pin = { id: 5, created: true, fields: body.pins,
                   expires_at: body.action === "allow_pinned_lease" ? NOW + 3600 : null };
  }
  return answer;
}

// `{status, json}` for a request, or null for one this backend does not know — which
// the harness records, since a path the page asks for and no fixture answers is
// either a fixture to add or a change in what the page asks.
export function respond(method, path, body) {
  if (method === "GET" && path in GETS) return { status: 200, json: GETS[path] };
  const resolve = path.match(/^\/approvals\/([^/]+)\/resolve$/);
  if (method === "POST" && resolve) {
    return { status: 200, json: resolved(decodeURIComponent(resolve[1]), body) };
  }
  return null;
}
