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
  review_verdict: "Passes.", decisions: [], superseded_at: null,
};
// Plans with decisions for the operator: a single-choice one and a multi-select one,
// an option of which hides a character. Task 9's and task 10's; plan 7 replaces task
// 10's once it is sent back, and plan 8 that.
function option(label, meaning, assumed) { return { label, meaning, assumed }; }
const HOLD = {
  question: `Hold infra traffic, or ${RLO}deny it?`,
  why: "The design names both and picks neither.", multi_select: false,
  options: [option("Hold", "The operator answers each request.", true),
            option("Deny", "Refused outright, nobody asked.", false)],
};
const LOGS = {
  question: "Which logs keep a rotation?", why: "A retention question.",
  multi_select: true,
  options: [option("Audit", "The audit log.", true),
            option("Access", "The access log.", true),
            option(`Debug${ZWSP}`, "The debug log, which is large.", false)],
};
const decidedPlan = (id, at, decisions) => ({
  ...PLAN, id, task_revision: 1, at, review_rounds: 3, decisions,
  approach: `Rotate the keys in place.\nPlan ${id}.`,
});
const DECIDED_PLANS = { 4: decidedPlan(4, NOW - 5000, [HOLD, LOGS]),
                        5: decidedPlan(5, NOW - 4500, [HOLD, LOGS]),
                        7: decidedPlan(7, NOW, [HOLD]),
                        8: decidedPlan(8, NOW, [LOGS]) };
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
// Notes flagged for the operator and not handled: two on task 3, one each on 6 and 8.
function flaggedNote(id, task, at, text) {
  return { id, task, author: "claude-3f2a9c1e", at, text, for_operator: true,
           handled: false, hidden: false, move: null, reply_to: null };
}
const ASKED = flaggedNote(13, 3, NOW - 500, `Is the relay ${RLO}right?`);
const BLOCKED = flaggedNote(14, 3, NOW - 400, `Blocked on the store${ZWSP}.`);
const ON_REFINED = flaggedNote(15, 6, NOW - 300, "Plan 2 or plan 1?");
const ON_SECOND = flaggedNote(16, 8, NOW - 200, "Ready when you are.");
// And one on task 7, which the operator deferred: it waits with it, out of the inbox.
const ON_PARKED = flaggedNote(17, 7, NOW - 100, "Still wanted later?");

const TASK = {
  ...TASKS[0], repo: "dockade", revision: 4, created_at: NOW - 86400,
  updated_at: NOW - 600,
  body: `Build the page.\nThen ${RLO}the rest${ZWSP}.\nAn emoji: \u2764\uFE0F`,
  parent: { id: 1, title: "Plain one", state: "open" },
  children: [{ id: 4, title: "Child", state: "closed" },
             { id: 5, title: "Other child", state: "landing" }],
  blockers: [{ id: 2, title: "Back up the store", state: "open" }],
  plan: PLAN, answers: [], notes: [STARTED, ANSWER, ASKED, BLOCKED],
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
const PARKED = { ...UNPLANNED, id: 7, title: "Parked one", state: "deferred",
                 ready: false, blocked: false, kind: null, revision: 3,
                 notes: [ON_PARKED] };

// Task 6 waits for triage with a plan that replaced another, and task 8 behind it with
// its first; task 5 to land, its claim ended.
const REFINED = { ...TASK, id: 6, title: "Refined one", state: "refined", ready: false,
                  claim: null, revision: 7, parent: null, children: [], blockers: [],
                  notes: [] };
const LANDING = { ...REFINED, id: 5, title: `Landing ${RLO}one`, state: "landing",
                  revision: 9 };
const SECOND = { ...REFINED, id: 8, title: "Second refined", revision: 2 };
// Tasks 9 and 10 wait for triage with decisions to answer.
const DECIDED = { ...REFINED, id: 9, title: "Decided one", revision: 3,
                  plan: DECIDED_PLANS[4] };
const SECOND_DECIDED = { ...REFINED, id: 10, title: "Second decided", revision: 3,
                         plan: DECIDED_PLANS[5] };

const PLANS = { 1: [OLDER_PLAN], 3: [PLAN, OLDER_PLAN], 5: [PLAN],
                6: [PLAN, OLDER_PLAN], 8: [PLAN], 9: [DECIDED_PLANS[4]],
                10: [DECIDED_PLANS[5]] };

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
          ANSWER),
    noteEntry(ASKED), noteEntry(BLOCKED));
  return rows;
}

// The entry an agent's flagged note was posted with.
function noteEntry(note) {
  return entry(note.author, "note", note.at,
               { note: note.text, note_id: note.id, for_operator: true }, note);
}

function refinedHistory(created, plan = PLAN) {
  const planned = { id: 30, author: plan.author, at: plan.at, text: "Planned.",
                    for_operator: false, handled: false, hidden: false,
                    move: "submit_plan", reply_to: null };
  return [
    entry("claude-3f2a9c1e", "propose", created, { title: "Refined one", body: "B" }),
    entry("claude-3f2a9c1e", "submit_plan", plan.at,
          { revision: 7, note: planned.text, note_id: planned.id, plan: plan.id,
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
  6: [...refinedHistory(NOW - 9000), noteEntry(ON_REFINED)],
  8: [...refinedHistory(NOW - 4000), noteEntry(ON_SECOND)],
  7: [entry("operator", "create", NOW - 6000, { title: "Parked one", state: "deferred" }),
      noteEntry(ON_PARKED)],
  9: refinedHistory(NOW - 6000, DECIDED_PLANS[4]),
  10: refinedHistory(NOW - 5500, DECIDED_PLANS[5]),
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
              reasons: [{ why: "refined", since: NOW - 7200, plan: 2, decisions: 0,
                          review_rounds: 2 }] },
            { task: 9, repo: "dockade", title: "Decided one", state: "refined",
              since: NOW - 5000,
              reasons: [{ why: "refined", since: NOW - 5000, plan: 4, decisions: 2,
                          review_rounds: 3 }] },
            { task: 10, repo: "dockade", title: "Second decided", state: "refined",
              since: NOW - 4500,
              reasons: [{ why: "refined", since: NOW - 4500, plan: 5, decisions: 2,
                          review_rounds: 3 }] },
            { task: 8, repo: "dockade", title: "Second refined", state: "refined",
              since: NOW - 3600,
              reasons: [{ why: "refined", since: NOW - 3600, plan: 2, decisions: 0,
                          review_rounds: 2 }] }],
    total: 4,
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
const WRITTEN = { 3: { ...TASK, notes: [...TASK.notes] },
                  6: { ...REFINED, notes: [ON_REFINED] },
                  8: { ...SECOND, notes: [ON_SECOND] },
                  9: { ...DECIDED, notes: [] },
                  10: { ...SECOND_DECIDED, notes: [] } };
let nextNote = 20;

// Every note a move can name, by id: the ones the timelines carry, so a hide or a
// handle shows wherever the note is drawn.
const NOTES = new Map([STARTED, HIDDEN, ANSWER, ASKED, BLOCKED, ON_REFINED, ON_SECOND,
                       ON_PARKED].map(n => [n.id, n]));

// The flagged list, as `operator_moves.notes_for_operator` reads it: visible notes for
// the operator and not handled, newest first, a page at a time past `before`, each
// with its task's state as it is now.
function flaggedPage(params) {
  const before = Number(params.get("before") || Infinity);
  const all = [...NOTES.values()].filter(n => n.for_operator && !n.handled && !n.hidden)
    .sort((a, b) => b.id - a.id);
  return { notes: all.filter(n => n.id < before).slice(0, PAGE)
             .map(n => ({ ...n, task_state: (WRITTEN[n.task] || READ[n.task]).state })),
           total: all.length };
}

// A step changes the flagged list behind the page's back, as an agent and another tab
// would: a note flagged on task 8, and task 8's other one handled.
export function flagElsewhere() {
  const id = nextNote++;
  const note = flaggedNote(id, 8, NOW, "One more thing.");
  NOTES.set(id, note);
  WRITTEN[8].notes.push(note);
  HISTORIES[8].push(noteEntry(note));
  ON_SECOND.handled = true;
  HISTORIES[8].push(entry("operator", "handle_note", NOW,
                          { note_id: ON_SECOND.id, handled: true, reply: null,
                            reply_id: null, actor: null }));
}

// A step changes a task behind the page's back, as another session would: the page's
// next move on it names a revision that is no longer the task's.
export function editElsewhere(id) {
  const t = WRITTEN[id];
  t.revision += 1;
  t.body = `${t.body}\nEdited elsewhere.`;
  HISTORIES[id].push(entry("operator", "edit", NOW, { revision: t.revision,
                                                      body: t.body, actor: null }));
}
// A step defers a task behind the page's back, as another tab would.
export function deferElsewhere(id) {
  const t = WRITTEN[id];
  const before = t.state;
  t.state = "deferred";
  t.revision += 1;
  HISTORIES[id].push(entry("operator", "move", NOW, { before, state: t.state,
                                                      revision: t.revision, actor: null }));
}
// A step has an agent hand in another plan for a task, as `submit_plan` would: the one
// it had is superseded, and the task is refined at a new revision.
export function replanElsewhere(id, plan) {
  const t = WRITTEN[id];
  const next = DECIDED_PLANS[plan];
  PLANS[id] = [next, ...PLANS[id].map(p => ({ ...p,
                                              superseded_at: p.superseded_at ?? NOW }))];
  t.plan = next;
  t.state = "refined";
  t.revision += 1;
  HISTORIES[id].push(entry("claude-3f2a9c1e", "submit_plan", NOW,
                           { revision: t.revision, plan: next.id, ...PLAN_DETAIL }));
}
// A step loses the next write's answer after the write lands: the relay's 504.
let loseNext = false;
export function loseNextAnswer() { loseNext = true; }

function changed(t) {
  return { status: 409, json: { ok: false, current: t,
    detail: `task ${t.id} changed since it was read: it is at revision ${t.revision}` } };
}

// A move on notes, as `operator_moves` makes it: all of a bulk or none, each recorded
// on its note's task. A handle with a reply posts the reply as the operator's note.
function noteWrite(path, body) {
  const one = path.match(/^\/api\/notes\/(\d+)\/handle$/);
  const ids = one ? [Number(one[1])] : body.notes;
  const notes = ids.map(id => NOTES.get(id));
  if (notes.some(n => !n)) {
    return { status: 404, json: { ok: false, current: null, detail: "no such note" } };
  }
  const hide = path === "/api/notes/hide";
  if (!hide && notes.some(n => !n.for_operator)) {
    return { status: 400, json: { ok: false, current: null,
                                  detail: "a note not marked for the operator" } };
  }
  let reply = null;
  for (const n of notes) {
    const history = HISTORIES[n.task];
    if (hide) {
      n.hidden = body.hidden;
      history.push(entry("operator", "hide_note", NOW,
                         { note_id: n.id, hidden: body.hidden, actor: null }));
      continue;
    }
    n.handled = body.handled;
    if (one && body.reply !== null) {
      reply = { id: nextNote++, task: n.task, author: "operator", text: body.reply,
                at: NOW, for_operator: false, move: null, reply_to: n.id, handled: false,
                hidden: false };
      NOTES.set(reply.id, reply);
      if (WRITTEN[n.task]) WRITTEN[n.task].notes.push(reply);
    }
    history.push(entry("operator", "handle_note", NOW,
                       { note_id: n.id, handled: body.handled, reply: one ? body.reply : null,
                         reply_id: reply && reply.id, actor: null }, reply));
  }
  return { status: 200, json: one
    ? { ok: true, task: notes[0].task, note: notes[0].id, reply: reply && reply.id }
    : { ok: true, notes: ids } };
}

// An approve, as `operator_moves.approve` makes it: answers that are each exactly the
// options the plan assumes open the task; any others send it back, its plan superseded.
// Either way the answers are kept, newest plan first, in the text the operator saw.
function approve(t, body, history) {
  const refuse = detail => ({ status: 400, json: { ok: false, current: null, detail } });
  const plan = t.plan;
  if (t.state !== "refined") return refuse(`task ${t.id} is ${t.state}`);
  if (t.answers.some(a => a.plan === plan.id)) {
    return refuse(`plan ${plan.id} of task ${t.id} was answered already`);
  }
  const decisions = plan.decisions || [];
  if (body.answers.length !== decisions.length) {
    return refuse(`answers is a list of ${decisions.length}`);
  }
  const given = decisions.map((d, i) => {
    const a = body.answers[i];
    return { plan: plan.id, decision: i, question: d.question,
             options: [...a.options].sort((x, y) => x - y)
               .map(j => ({ position: j, ...d.options[j] }))
               .map(({ assumed, ...o }) => o),
             other: a.other ?? null, author: "operator", at: NOW };
  });
  const opens = given.every((a, i) => a.other === null
    && a.options.map(o => o.position).join() === decisions[i].options
      .flatMap((o, j) => (o.assumed ? [j] : [])).join());
  const state = opens ? "open" : "proposed";
  t.answers = [...given, ...t.answers];
  t.state = state;
  t.revision += 1;
  if (!opens) {
    t.plan = { ...plan, superseded_at: NOW };
    PLANS[t.id] = PLANS[t.id].map(p => (p.id === plan.id ? t.plan : p));
  }
  history.push(entry("operator", "approve", NOW,
                     { revision: t.revision, before: "refined", state, plan: plan.id,
                       answers: given.map(({ question, options, other }) =>
                         ({ question, options, other })), actor: null }));
  return { status: 200, json: { ok: true, task: t.id, revision: t.revision, state,
                                plan: plan.id } };
}

function write(path, body) {
  if (path.startsWith("/api/notes/")) return lost(noteWrite(path, body));
  const m = path.match(/^\/api\/tasks\/(\d+)\/(move|approve|break-claim|notes)$/);
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
  } else if (m[2] === "approve") {
    answer = approve(t, body, history);
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
  return lost(answer);
}

// The answer, or the relay's 504 in its place when a step asked to lose it: the write
// has landed either way.
function lost(answer) {
  if (!loseNext || answer.status !== 200) return answer;
  loseNext = false;
  return { status: 504, json: { ok: false, current: null,
    detail: "the task registry did not answer a write it was sent (ReadTimeout): "
            + "it may have landed" } };
}

const READ = { 1: SENT_BACK, 2: UNPLANNED, 5: LANDING, 7: PARKED };

// The inbox as `operator_moves.inbox` has it after a step moved a task: a deferred or
// closed task is nobody's turn, so it is in no section.
function inboxNow() {
  const out = {};
  for (const [key, section] of Object.entries(INBOX)) {
    const kept = section.tasks.filter(t => {
      const now = WRITTEN[t.task] || READ[t.task];
      return !now || !["deferred", "closed"].includes(now.state);
    });
    out[key] = { tasks: kept, total: section.total - (section.tasks.length - kept.length) };
  }
  return out;
}

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
  if (path === "/api/inbox") return { status: 200, json: inboxNow() };
  if (path === "/api/notes/flagged") return { status: 200, json: flaggedPage(params) };
  return null;
}
