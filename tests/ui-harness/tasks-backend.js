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
    { ts: NOW - 600, author: "claude-3f2a9c1e", action: "claim_work",
      detail: { branch: "tasks-ui" } },
  ],
  total: 2,
};

// Task 6 waits for triage with the current plan, and task 8 behind it; task 5 to land,
// its claim ended.
const REFINED = { ...TASK, id: 6, title: "Refined one", state: "refined", ready: false,
                  claim: null, revision: 7, parent: null, children: [], blockers: [] };
const LANDING = { ...REFINED, id: 5, title: `Landing ${RLO}one`, state: "landing",
                  revision: 9 };
const SECOND = { ...REFINED, id: 8, title: "Second refined", revision: 2 };

// Every section, every reason: task 3 has a note for the operator, task 5 waits to
// land, task 6 is refined, task 1's claim ran out and task 2
// went quiet. Drifting shows the oldest few of more.
const INBOX = {
  waiting: {
    tasks: [
      { task: 5, repo: "dockade", title: `Landing ${RLO}one`, state: "landing",
        since: NOW - 9000,
        reasons: [{ why: "landing", since: NOW - 9000, branch: `land${ZWSP}-it` }] },
      { task: 3, repo: "dockade", title: TASKS[0].title, state: "open", since: NOW - 100,
        reasons: [{ why: "flagged", since: NOW - 100, notes: 2 }] },
    ],
    total: 2,
  },
  triage: {
    tasks: [{ task: 6, repo: "old-repo", title: "Refined one", state: "refined",
              since: NOW - 7200,
              reasons: [{ why: "refined", since: NOW - 7200, plan: 2,
                          review_rounds: 2 }] },
            { task: 8, repo: "dockade", title: "Second refined", state: "refined",
              since: NOW - 3600,
              reasons: [{ why: "refined", since: NOW - 3600, plan: 2,
                          review_rounds: 2 }] }],
    total: 2,
  },
  drifting: {
    tasks: [
      { task: 2, repo: "dockade", title: "Back up the store", state: "open",
        since: NOW - 20 * 86400, reasons: [{ why: "quiet", since: NOW - 20 * 86400 }] },
      { task: 1, repo: "dockade", title: "Plain one", state: "proposed",
        since: NOW - 4000, reasons: [{ why: "lapsed", since: NOW - 4000,
                                       holder: `claude-${RLO}x`, branch: null }] },
    ],
    total: 7,
  },
};

let registryUp = true;
// A step stops the registry: from then on the relay answers as it does for a registry
// that does not answer, and `/running` says so. Another starts it again.
export function stopRegistry() { registryUp = false; }
export function startRegistry() { registryUp = true; }

const UNREACHABLE = { ok: false, current: null,
                      detail: "the task registry did not answer (ConnectError)" };

// The tasks the steps write to, and their notes: copies, moved as the operator API
// would move them (task-registry/operator_moves.py), so a read after a write shows it.
const WRITTEN = { 3: { ...TASK }, 6: { ...REFINED }, 8: { ...SECOND } };
const WRITTEN_NOTES = { 3: { notes: [...NOTES.notes], total: NOTES.total },
                        6: { notes: [], total: 0 }, 8: { notes: [], total: 0 } };
let nextNote = 20;

// A step changes a task behind the page's back, as another session would: the page's
// next move on it names a revision that is no longer the task's.
export function editElsewhere(id) { WRITTEN[id].revision += 1; }
// A step loses the next write's answer after the write lands: the relay's 504.
let loseNext = false;
export function loseNextAnswer() { loseNext = true; }

function changed(t) {
  return { status: 409, json: { ok: false, current: t,
    detail: `task ${t.id} changed since it was read: it is at revision ${t.revision}` } };
}

function write(path, body) {
  const m = path.match(/^\/api\/tasks\/(\d+)\/(move|break-claim|notes)$/);
  const t = m && WRITTEN[m[1]];
  if (!t) return null;
  let answer;
  if (m[2] === "notes") {
    const id = nextNote++;
    const notes = WRITTEN_NOTES[m[1]];
    notes.notes.push({ id, task: t.id, author: "operator", text: body.text, at: NOW,
                       for_operator: false, move: null, reply_to: null, handled: false,
                       hidden: false });
    notes.total += 1;
    answer = { status: 200, json: { ok: true, task: t.id, note: id } };
  } else if (body.revision !== t.revision) {
    return changed(t);
  } else if (m[2] === "break-claim") {
    if (!t.claim) {
      return { status: 400, json: { ok: false, current: null,
                                    detail: `task ${t.id} has no live claim to break` } };
    }
    t.claim = null;
    t.revision += 1;
    answer = { status: 200, json: { ok: true, task: t.id, revision: t.revision } };
  } else {
    if (body.state !== t.state) t.claim = null;
    t.state = body.state;
    t.revision += 1;
    answer = { status: 200, json: { ok: true, task: t.id, revision: t.revision } };
  }
  if (loseNext) {
    loseNext = false;
    return { status: 504, json: { ok: false, current: null,
      detail: "the task registry did not answer a write it was sent (ReadTimeout): "
              + "it may have landed" } };
  }
  return answer;
}

export function respond(method, path, body, params) {
  if (method === "GET" && path === "/running") {
    return { status: 200, json: { running: registryUp } };
  }
  if (!path.startsWith("/api/")) return null;
  if (!registryUp) return { status: 502, json: UNREACHABLE };
  if (method === "POST") return write(path, body);
  if (method !== "GET") return null;
  const id = (path.match(/^\/api\/tasks\/(\d+)$/) || [])[1];
  if (id && WRITTEN[id]) return { status: 200, json: WRITTEN[id] };
  const notesOf = (path.match(/^\/api\/tasks\/(\d+)\/notes$/) || [])[1];
  if (notesOf && WRITTEN_NOTES[notesOf]) {
    return { status: 200, json: WRITTEN_NOTES[notesOf] };
  }
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
  if (path === "/api/inbox") return { status: 200, json: INBOX };
  if (path === "/api/tasks/3/history") return { status: 200, json: HISTORY };
  if (path === "/api/tasks/3/plans") return { status: 200, json: [PLAN, OLDER_PLAN] };
  if (path === "/api/tasks/1") return { status: 200, json: SENT_BACK };
  if (path === "/api/tasks/2") return { status: 200, json: UNPLANNED };
  if (path === "/api/tasks/6/plans" || path === "/api/tasks/8/plans") {
    return { status: 200, json: [PLAN] };
  }
  if (path === "/api/tasks/5") return { status: 200, json: LANDING };
  if (["/api/tasks/1/notes", "/api/tasks/2/notes", "/api/tasks/5/notes"].includes(path)) {
    return { status: 200, json: NO_NOTES };
  }
  return null;
}
