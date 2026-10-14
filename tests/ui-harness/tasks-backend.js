// SPDX-License-Identifier: Apache-2.0
// The tasks UI's upstream, faked: its own `/running` and the registry's operator API
// behind its relay (tasks-ui/app.py). As backend.js for the approval page, nothing here
// has to be what the registry would answer, only reach the branch it exists for. Agent
// text carries the characters the page must spell out: U+202E reverses what follows
// it, U+200B is nothing at all, U+FE0F rides after an emoji.

import { NOW_MS } from "./backend.js";

const NOW = NOW_MS / 1000;
const RLO = "\u202E";
const ZWSP = "\u200B";

const REPOS = [
  { name: "dockade", retired: false },
  { name: "old-repo", retired: true },
];

const claim = {
  holder: "claude-3f2a9c1e-0000-4000-8000-000000000001", branch: "tasks-ui",
  expires_at: NOW + 3600, footprint: ["tasks-ui/app.py", `docs${ZWSP}/x.md`],
  progress: "Relay done.\nPage next.",
};

const TASKS = [
  { id: 3, title: `Split the UI ${RLO}gnp.exe`, state: "open", ready: true,
    severity: "medium", kind: "feature", claim, notes_total: 2 },
  { id: 2, title: "Back up the store", state: "open", blocked: true, kind: "chore",
    notes_total: 0 },
  { id: 1, title: "Plain one", state: "open", notes_total: 0 },
];

const PLAN = {
  id: 2, task: 3, task_revision: 3, author: "claude-3f2a9c1e", at: NOW - 7200,
  approach: `Relay first, then the page.\nKeep ${RLO}it small.`,
  footprint: ["tasks-ui/app.py", `tasks-ui/tasks${ZWSP}.js`],
  tests: "The relay table; the page under node.", out_of_scope: "None.",
  risks: "A route the page calls and the relay refuses.", review_rounds: 2,
  review_verdict: "Passes.", superseded_at: null,
};
const OLDER_PLAN = {
  ...PLAN, id: 1, task_revision: 1, at: NOW - 80000, review_rounds: 1,
  approach: "Everything in one change.", superseded_at: NOW - 76000,
};

const TASK = {
  ...TASKS[0], repo: "dockade", revision: 4, created_at: NOW - 86400,
  updated_at: NOW - 600,
  body: `Build the page.\nThen ${RLO}the rest${ZWSP}.\nAn emoji: \u2764\uFE0F`,
  parent: { id: 1, title: "Plain one", state: "open" },
  children: [{ id: 4, title: "Child", state: "closed" },
             { id: 5, title: "Other child", state: "landing" }],
  blockers: [{ id: 2, title: "Back up the store", state: "open" }],
  plan: PLAN,
};

// Task 1 went back to proposed: its plan is the superseded one. Task 2 was opened with
// no plan.
const SENT_BACK = { ...TASKS[2], state: "proposed", ready: false, repo: "dockade",
                    revision: 6, created_at: NOW - 90000,
                    updated_at: NOW - 76000, body: "Plain.", children: [], blockers: [],
                    plan: OLDER_PLAN };
const UNPLANNED = { ...TASKS[1], repo: "dockade", revision: 1, created_at: NOW - 5000,
                    updated_at: NOW - 5000, body: "Back it up.", children: [],
                    blockers: [], plan: null };
const NO_NOTES = { notes: [], total: 0 };

const NOTES = {
  notes: [
    { id: 10, author: "claude-3f2a9c1e", at: NOW - 3000, text: "Started.",
      for_operator: false },
    { id: 11, author: "operator", at: NOW - 2000, text: `Hidden ${RLO}one`,
      hidden: true, reply_to: 10 },
  ],
  total: 2,
};

const HISTORY = {
  history: [
    { ts: NOW - 86400, author: "operator", action: "approve",
      detail: { body: `Approved ${RLO}text`, revision: 1 } },
    { ts: NOW - 600, author: "claude-3f2a9c1e", action: "claim",
      detail: { branch: "tasks-ui" } },
  ],
  total: 2,
};

const FLAGGED = {
  notes: [{ id: 12, task: 3, author: "claude-3f2a9c1e", at: NOW - 100,
            text: "Need a decision on the port.", for_operator: true }],
  total: 1,
};

let registryUp = true;
// A step stops the registry: from then on the relay answers as it does for a registry
// that does not answer, and `/running` says so. Another starts it again.
export function stopRegistry() { registryUp = false; }
export function startRegistry() { registryUp = true; }

const UNREACHABLE = { ok: false, current: null,
                      detail: "the task registry did not answer (ConnectError)" };

export function respond(method, path, body, params) {
  if (method === "GET" && path === "/running") {
    return { status: 200, json: { running: registryUp } };
  }
  if (!path.startsWith("/api/")) return null;
  if (!registryUp) return { status: 502, json: UNREACHABLE };
  if (method !== "GET") return null;
  if (path === "/api/repos") return { status: 200, json: REPOS };
  if (path === "/api/tasks") {
    const rows = params.get("repo") === "dockade" && params.get("state") === "open"
      ? TASKS : [];
    return { status: 200, json: { tasks: rows, total: rows.length } };
  }
  if (path === "/api/search") {
    const rows = TASKS.filter(t => t.title.toLowerCase().includes(
      (params.get("text") || "").toLowerCase()));
    return { status: 200, json: { tasks: rows, total: rows.length } };
  }
  if (path === "/api/notes/flagged") return { status: 200, json: FLAGGED };
  if (path === "/api/tasks/3") return { status: 200, json: TASK };
  if (path === "/api/tasks/3/notes") return { status: 200, json: NOTES };
  if (path === "/api/tasks/3/history") return { status: 200, json: HISTORY };
  if (path === "/api/tasks/3/plans") return { status: 200, json: [PLAN, OLDER_PLAN] };
  if (path === "/api/tasks/1") return { status: 200, json: SENT_BACK };
  if (path === "/api/tasks/2") return { status: 200, json: UNPLANNED };
  if (path === "/api/tasks/1/notes" || path === "/api/tasks/2/notes") {
    return { status: 200, json: NO_NOTES };
  }
  return null;
}
