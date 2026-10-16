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
// As `operator_moves.PAGE`: how many entries a page of a task's history holds.
const PAGE = 50;

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
// What plan 2 replaced: another approach, one path fewer, the risks spaced otherwise.
const OLDER_PLAN = {
  ...PLAN, id: 1, task_revision: 1, at: NOW - 80000, review_rounds: 1,
  approach: "Everything in one change.", footprint: ["tasks-ui/app.py"],
  risks: "A route the page calls and  the relay refuses.", superseded_at: NOW - 76000,
};

// A task's notes as a read of it carries them: visible ones only, its latest few.
const STARTED = { id: 10, task: 3, author: "claude-3f2a9c1e", at: NOW - 3000,
                  text: "Started.", for_operator: true, handled: true, hidden: false,
                  move: null, reply_to: null };
const HIDDEN = { id: 11, task: 3, author: "operator", at: NOW - 2000,
                 text: `Hidden ${RLO}one`, for_operator: false, handled: false,
                 hidden: true, move: null, reply_to: 10 };
const ANSWER = { id: 12, task: 3, author: "operator", at: NOW - 900, text: "Seen.",
                 for_operator: false, handled: false, hidden: false, move: null,
                 reply_to: 10 };

const TASK = {
  ...TASKS[0], repo: "dockade", revision: 4, created_at: NOW - 86400,
  updated_at: NOW - 600,
  body: `Build the page.\nThen ${RLO}the rest${ZWSP}.\nAn emoji: \u2764\uFE0F`,
  parent: { id: 1, title: "Plain one", state: "open" },
  children: [{ id: 4, title: "Child", state: "closed" },
             { id: 5, title: "Other child", state: "landing" }],
  blockers: [{ id: 2, title: "Back up the store", state: "open" }],
  plan: PLAN, notes: [STARTED, ANSWER],
};

// Task 1 went back to proposed: its plan is the superseded one. Task 2 was opened with
// no plan.
const SENT_BACK = { ...TASKS[2], state: "proposed", ready: false, repo: "dockade",
                    revision: 6, created_at: NOW - 90000,
                    updated_at: NOW - 76000, body: "Plain.", children: [], blockers: [],
                    plan: OLDER_PLAN, notes: [] };
const UNPLANNED = { ...TASKS[1], repo: "dockade", revision: 1, created_at: NOW - 5000,
                    updated_at: NOW - 5000, body: "Back it up.", children: [],
                    blockers: [], plan: null, notes: [] };

// Task 6 waits for triage with a plan that replaced another, and task 8 behind it with
// its first; task 5 to land, its claim ended.
const REFINED = { ...TASK, id: 6, title: "Refined one", state: "refined", ready: false,
                  claim: null, revision: 7, parent: null, children: [], blockers: [],
                  notes: [] };
const LANDING = { ...REFINED, id: 5, title: `Landing ${RLO}one`, state: "landing",
                  revision: 9 };
const SECOND = { ...REFINED, id: 8, title: "Second refined", revision: 2 };

const PLANS = { 1: [OLDER_PLAN], 3: [PLAN, OLDER_PLAN], 5: [PLAN],
                6: [PLAN, OLDER_PLAN], 8: [PLAN] };

// Each task's history, oldest first, as `operator_moves.history` reads it: every write,
// with the note it posted as it is now.
let nextEntry = 1000;
function entry(author, action, ts, detail, note = null) {
  return { id: nextEntry++, ts, author, action, detail, note };
}
const PLAN_DETAIL = { approach: PLAN.approach, footprint: PLAN.footprint,
                      tests: PLAN.tests, review_rounds: PLAN.review_rounds };

// Task 3's runs past a page: a claim's progress checkpoints between its notes, one
// flagged and handled with a reply, and the operator's own, hidden.
function claimedHistory() {
  const rows = [
    entry("operator", "create", NOW - 86400, { title: TASKS[0].title, body: TASK.body,
                                               state: "proposed", actor: null }),
    entry("operator", "move", NOW - 86000, { before: "refined", state: "open",
                                             revision: 3, ended_claim: null, actor: null }),
    entry(claim.holder, "claim_work", NOW - 80000, { branch: "tasks-ui",
                                                     footprint: claim.footprint }),
  ];
  for (let i = 0; i < PAGE; i++) {
    rows.push(entry(claim.holder, "update", NOW - 70000 + i * 1000,
                    { progress: `Step ${i} of the relay.` }));
  }
  rows.push(
    entry(claim.holder, "note", STARTED.at,
          { note: STARTED.text, note_id: 10, for_operator: true }, STARTED),
    entry("operator", "note", HIDDEN.at, { note: HIDDEN.text, note_id: 11, actor: null },
          HIDDEN),
    entry("operator", "hide_note", NOW - 1000, { note_id: 11, hidden: true, actor: null }),
    entry("operator", "handle_note", ANSWER.at,
          { note_id: 10, handled: true, reply: ANSWER.text, reply_id: 12, actor: null },
          ANSWER));
  return rows;
}

function refinedHistory(created) {
  const planned = { id: 30, author: PLAN.author, at: PLAN.at, text: "Planned.",
                    for_operator: false, handled: false, hidden: false,
                    move: "submit_plan", reply_to: null };
  return [
    entry("claude-3f2a9c1e", "propose", created, { title: "Refined one", body: "B" }),
    entry("claude-3f2a9c1e", "submit_plan", PLAN.at,
          { revision: 7, note: planned.text, note_id: planned.id, plan: PLAN.id,
            ...PLAN_DETAIL }, planned),
  ];
}

const HISTORIES = {
  1: [entry("operator", "create", NOW - 90000, { title: "Plain one", state: "proposed" }),
      entry("claude-3f2a9c1e", "send_back", NOW - 76000,
            { revision: 6, note: null, note_id: null })],
  2: [entry("operator", "create", NOW - 5000, { title: "Back up the store",
                                                state: "open" })],
  3: claimedHistory(),
  5: [entry("claude-3f2a9c1e", "submit_work", NOW - 9000,
            { revision: 9, note: null, note_id: null, branch: `land${ZWSP}-it` })],
  6: refinedHistory(NOW - 9000),
  8: refinedHistory(NOW - 4000),
};

function historyPage(id, params) {
  const all = HISTORIES[id];
  const before = Number(params.get("before") || Infinity);
  const older = all.filter(h => h.id < before);
  return { history: older.slice(-PAGE), total: all.length };
}

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

// The tasks the steps write to: copies, moved as the operator API would move them
// (task-registry/operator_moves.py), each write recorded in their history, so a read
// after a write shows it.
const WRITTEN = { 3: { ...TASK, notes: [...TASK.notes] }, 6: { ...REFINED, notes: [] },
                  8: { ...SECOND, notes: [] } };
let nextNote = 20;

// A step changes a task behind the page's back, as another session would: the page's
// next move on it names a revision that is no longer the task's.
export function editElsewhere(id) {
  const t = WRITTEN[id];
  t.revision += 1;
  t.body = `${t.body}\nEdited elsewhere.`;
  HISTORIES[id].push(entry("operator", "edit", NOW, { revision: t.revision,
                                                      body: t.body, actor: null }));
}
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
  const history = HISTORIES[m[1]];
  let answer;
  if (m[2] === "notes") {
    const id = nextNote++;
    const note = { id, task: t.id, author: "operator", text: body.text, at: NOW,
                   for_operator: false, move: null, reply_to: null, handled: false,
                   hidden: false };
    t.notes.push(note);
    t.notes_total += 1;
    history.push(entry("operator", "note", NOW, { note: body.text, note_id: id,
                                                  actor: null }, note));
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
    history.push(entry("operator", "break_claim", NOW, { revision: t.revision,
                                                         actor: null }));
    answer = { status: 200, json: { ok: true, task: t.id, revision: t.revision } };
  } else {
    const before = t.state;
    if (body.state !== t.state) t.claim = null;
    t.state = body.state;
    t.revision += 1;
    history.push(entry("operator", "move", NOW, { before, state: t.state,
                                                  revision: t.revision, actor: null }));
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

const READ = { 1: SENT_BACK, 2: UNPLANNED, 5: LANDING };

export function respond(method, path, body, params) {
  if (method === "GET" && path === "/running") {
    return { status: 200, json: { running: registryUp } };
  }
  if (!path.startsWith("/api/")) return null;
  if (!registryUp) return { status: 502, json: UNREACHABLE };
  if (method === "POST") return write(path, body);
  if (method !== "GET") return null;
  const [, id, what] = path.match(/^\/api\/tasks\/(\d+)(?:\/(history|plans))?$/) || [];
  if (id && !what && (WRITTEN[id] || READ[id])) {
    return { status: 200, json: WRITTEN[id] || READ[id] };
  }
  if (what === "history" && HISTORIES[id]) {
    return { status: 200, json: historyPage(id, params) };
  }
  if (what === "plans" && PLANS[id]) return { status: 200, json: PLANS[id] };
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
  return null;
}
