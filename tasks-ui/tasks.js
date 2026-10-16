// SPDX-License-Identifier: Apache-2.0
/* The tasks page: the task registry's work list, read through this UI's relay to the
 * registry's operator listener (`/api/…`, tasks-ui/app.py). It opens on the inbox,
 * what is the operator's turn, with the task read beside it; every task is a view
 * away. A backlog nobody waits on, so nothing here alerts the way a hold does: the
 * page's title carries a quiet count of what waits on the operator.
 *
 * The registry can stop on its own. The page asks `/running` first, which this
 * container answers itself, so a registry that is not running is one line on the page
 * rather than a failed request in the console; and nothing here calls it again until
 * `/running` says it answers.
 *
 * Every string a task carries was written by an agent, or by the operator for agents
 * to read, so it is set as textContent, and a character that renders as nothing or
 * reorders the text around it is spelled out where it sits (`spellHidden`).
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_tasks_ui_js.py).
 */
import { escapePayload } from "./escape.js";
import { pollStatus, renderListStatus } from "./list-status.js";
import {
  TASK_STATES, NOT_RUNNING_STATUS, movesFor, sendWrite, readAnswer, writeOutcome,
  checkWrite, lastNoteId,
} from "./moves.js";
import { fmtStamp, fmtInstant } from "./time.js";

// What `GET /api/tasks` takes for every state (operator_api.EVERY_STATE), beside the
// states themselves (TASK_STATES, moves.js). A test holds it to the registry's.
export const EVERY_STATE = "all";

// Characters that render as nothing or reorder what is around them: the payload
// view's set (control-plane-ui/payload.js), less the line break and the tab, since this
// is prose, and with Unicode's default-ignorable code points, which include the
// variation selectors a hidden payload can ride on to an agent. One U+202E in a body
// draws the rest of its line backwards, and an approved body is what every later
// session reads, so the operator must see what is in it rather than what the browser
// makes of it. An emoji's U+FE0F is spelled too: the price of not guessing which
// selector carries nothing.
// The task registry's export spells the same set (task-registry/export.py).
const HIDDEN_RE =
  /(?![\n\t])[\p{Cf}\p{Cc}\p{Zl}\p{Zp}\p{Default_Ignorable_Code_Point}]/gu;

// `text` with each hidden character spelled `\uXXXX` where it sits, and how many there
// were. Spelled, never dropped: dropping would show a cleaned copy of what agents read.
export function spellHidden(text) {
  const s = typeof text === "string" ? text : "";
  let hidden = 0;
  const shown = s.replace(HIDDEN_RE, ch => { hidden++; return escapePayload(ch); });
  return { text: shown, hidden };
}

// The note under a text that carried any.
export function hiddenNote(hidden) {
  return hidden > 0
    ? `${hidden} invisible or direction-changing character${hidden === 1 ? "" : "s"} `
      + "spelled out where they sit."
    : "";
}

// How many items a page of any paged registry read holds (`reads.LIST_MAX`,
// `operator_moves.PAGE`; a test holds them equal).
export const PAGE_MAX = 50;

// Where the next older page starts, or null when nothing is older. The registry pages
// two ways (task-registry/operator_api.py): a list of tasks or of flagged notes comes
// newest first, so the oldest on a page is its last; a task's notes and its history
// come oldest first, so it is the first. A page short of full is the last, whatever
// the total says: the total also counts what arrived since the first page, so on its
// own it would offer an older page that is empty. `before` is how many came before
// this page in the same direction.
export function olderCursor(items, order, before, total) {
  if (!Array.isArray(items) || items.length < PAGE_MAX) return null;
  if (before + items.length >= Number(total)) return null;
  const edge = order === "oldest-first" ? items[0] : items[items.length - 1];
  return edge && Number.isInteger(edge.id) ? edge.id : null;
}

// A parent's children still to be done: every state but closed, since landing is not
// landed (task-registry/DESIGN.md, "Parent and blockers").
export function childrenLeft(children) {
  const all = Array.isArray(children) ? children : [];
  const left = all.filter(c => c && c.state !== "closed").length;
  return { left, total: all.length,
           text: all.length ? `${left} of ${all.length} not closed` : "" };
}

// Who holds a task, on which branch, and until when; empty when nobody does. The
// holder and branch are an agent's, so spelled as any other text of theirs.
export function claimText(claim) {
  if (!claim || typeof claim !== "object") return "";
  let text = `held by ${spellHidden(claim.holder).text}`;
  if (claim.branch) text += ` on ${spellHidden(claim.branch).text}`;
  const until = fmtStamp(claim.expires_at);
  if (until) text += `, until ${until}`;
  return text;
}

// A plan's parts, in the order `submit_plan` asks for them (task-registry/agent_tools.py),
// each with its label: what the operator reads beside the body before approving the
// two. The footprint one path to a line. The text is the agent's, spelled where it is
// drawn.
export const PLAN_PARTS = [["approach", "approach"], ["footprint", "footprint"],
                           ["tests", "tests"], ["out_of_scope", "out of scope"],
                           ["risks", "risks"], ["review_verdict", "review verdict"]];
export function planParts(plan) {
  if (!plan || typeof plan !== "object") return [];
  return PLAN_PARTS.map(([key, label]) => {
    const value = plan[key];
    const text = Array.isArray(value) ? value.join("\n") : String(value ?? "");
    return { label, text };
  });
}

// "2 review rounds", or nothing when the count is missing.
function reviewRounds(value) {
  const rounds = Number(value);
  return Number.isInteger(rounds) ? `${rounds} review round${rounds === 1 ? "" : "s"}` : "";
}

// What a plan says about itself: which one, whose, when, against which revision of the
// task, after how many reviews, and whether it was superseded.
export function planHeading(plan) {
  if (!plan || typeof plan !== "object") return "";
  const parts = [`plan ${plan.id}`, `by ${spellHidden(plan.author).text}`,
                 fmtStamp(plan.at), `at revision ${plan.task_revision}`,
                 reviewRounds(plan.review_rounds)];
  if (plan.superseded_at) parts.push(`superseded ${fmtStamp(plan.superseded_at)}`);
  return parts.filter(Boolean).join(" · ");
}

// What the task's own plan, or its lack of one, means in the task's state: a line and
// whether it warns, or null. Only a proposed task is refined by an agent
// (`submit_plan` refuses any other), so only there is a missing plan the ordinary
// case; the operator can move a task on with no plan, or past a superseded one, and
// then nobody will hand one in.
export function planNotice(plan, state) {
  const proposed = state === "proposed";
  if (!plan) {
    return proposed
      ? { text: "No plan yet: an agent that claims the proposal hands one in.",
          warn: false }
      : { text: "No plan: this task was moved on without one.", warn: true };
  }
  if (!plan.superseded_at) return null;
  return { text: proposed
             ? "Superseded: no plan is current until an agent refines the task again."
             : "Superseded: no plan is current.",
           warn: true };
}

// Every plan but the one shown above, in the order the registry sent them (newest
// first). Ids compared as numbers: the shown one comes back from a data attribute.
export function earlierPlans(plans, shownId) {
  const shown = Number(shownId);
  return (Array.isArray(plans) ? plans : []).filter(p => p && Number(p.id) !== shown);
}

// The inbox's sections, highest first, as the registry names them
// (`operator_moves.SECTIONS`; a test holds them equal), each with its heading and what
// puts a task there.
export const INBOX_SECTIONS = [
  ["waiting", "Waiting on you",
   "A note an agent flagged for you, or work handed in for you to land."],
  ["triage", "To triage",
   "A plan an agent had reviewed and handed in, for you to approve or send back."],
  ["drifting", "Drifting",
   "A claim that ran out and nobody swept, or a task nobody holds that went quiet."],
];

// One reason a task is in the inbox, and since when. A holder and a branch are an
// agent's, so spelled.
export function reasonText(reason) {
  if (!reason || typeof reason !== "object") return "";
  const since = fmtStamp(reason.since);
  switch (reason.why) {
    case "flagged": {
      const n = Number(reason.notes);
      return `${n} note${n === 1 ? "" : "s"} for you since ${since}`;
    }
    case "landing":
      return `landing since ${since}`
        + (reason.branch ? ` on ${spellHidden(reason.branch).text}` : "");
    case "refined":
      return [`refined since ${since}`, `plan ${reason.plan}`,
              reviewRounds(reason.review_rounds)]
        .filter(Boolean).join(", ");
    case "lapsed":
      return `claim by ${spellHidden(reason.holder).text}`
        + (reason.branch ? ` on ${spellHidden(reason.branch).text}` : "")
        + ` ran out ${since}`;
    case "quiet":
      return `nothing written since ${since}`;
    default:
      return spellHidden(String(reason.why)).text;
  }
}

// A section's count beside its heading: the total, and how many are shown when a page
// does not hold them all.
export function sectionCount(shown, total) {
  return shown < total ? `· ${shown} of ${total}, oldest first` : `· ${total}`;
}

// The quiet count: what waits on the operator, or nothing. Never "0", which would
// still draw the eye to a page nobody needs to open.
export function countBadge(total) {
  const n = Number(total);
  return Number.isInteger(n) && n > 0 ? String(n) : "";
}

// The page's title, with that count ahead of it as the approval page puts its pending
// count, so a background tab shows it in the tab strip.
export const PAGE_TITLE = "dockade tasks";
export function pageTitle(total) {
  const count = countBadge(total);
  return (count ? `(${count}) ` : "") + PAGE_TITLE;
}

// The URL's hash names the view and the task read beside it, so a reload or a link
// keeps both, and the browser's back goes to the task read before. A task id is
// digits, as the relay takes one, without leading zeros, as a row names it; anything
// else names none. Not through Number: an id may pass 2^53.
export function parseHash(hash) {
  const q = new URLSearchParams(String(hash ?? "").replace(/^#/, ""));
  const task = q.get("task");
  return { view: q.get("view") === "all" ? "all" : "inbox",
           task: task !== null && /^[0-9]{1,19}$/.test(task)
             ? task.replace(/^0+(?=[0-9])/, "") : null };
}
export function hashFor(view, task) {
  const q = new URLSearchParams();
  if (view === "all") q.set("view", "all");
  if (task !== null && task !== undefined && task !== "") q.set("task", String(task));
  const s = q.toString();
  return s ? `#${s}` : "";
}

// What a list says about itself, as every polled list does (list-status.js). Its own
// sentence when a search found nothing, since that is a question's answer and not an
// empty backlog.
const TASKS_STATUS_TEXT = {
  stale: "Could not refresh — these are the last tasks loaded and may be out of date.",
  cold: "Could not load the tasks.",
  empty: "No tasks here.",
};
export function tasksStatus(rowCount, failed, loaded, searching) {
  const s = pollStatus(TASKS_STATUS_TEXT, rowCount, failed, loaded);
  if (searching && !failed && loaded && rowCount === 0) {
    return { ...s, text: "No task matches that search." };
  }
  return s;
}

const INBOX_STATUS_TEXT = {
  stale: "Could not refresh — this is the inbox as last loaded and may be out of date.",
  cold: "Could not load the inbox.",
  empty: "Nothing is your turn: no note for you, nothing to land, triage or look into.",
};
export function inboxStatus(rowCount, failed, loaded) {
  return pollStatus(INBOX_STATUS_TEXT, rowCount, failed, loaded);
}

// What a move's button says: its verb, approve's naming what it approves, and whose
// claim it ends, since a move to another state ends the claim (`operator_moves.move`).
// The holder is an agent's, so spelled.
export function moveLabel(move, task) {
  if (!move || typeof move !== "object" || !task || typeof task !== "object") return "";
  const verb = move.verb === "approve" ? approveLabel(task) : move.verb;
  return move.endsClaim && task.claim
    ? `${verb} (ends ${spellHidden(task.claim.holder).text}'s claim)` : verb;
}

export function breakLabel(claim) {
  return claim ? `break ${spellHidden(claim.holder).text}'s claim` : "";
}

// Approving a refined task approves its body and plan as the page shows them
// (task-registry/DESIGN.md, "Plans"), so the button names the revision and the plan,
// and says when the title, body or plan carry a character spelled out above it.
export function approveLabel(task) {
  if (!task || typeof task !== "object" || !task.plan) return "";
  return `approve revision ${task.revision} and plan ${task.plan.id}`;
}
export function approveHidden(task) {
  if (!task || typeof task !== "object") return "";
  const plan = planParts(task.plan).map(p => p.text);
  const hidden = [task.title, task.body, ...plan].map(text => spellHidden(text).hidden)
    .reduce((sum, n) => sum + n, 0);
  return hidden > 0
    ? `What this approves carries ${hidden} invisible or direction-changing `
      + `character${hidden === 1 ? "" : "s"}, spelled out above: read them first.`
    : "";
}

// What a write came to, in a line, and whether it warns: `writeOutcome`'s word, or for
// an answer that was lost, `checkWrite`'s. `detail` is the refusal's own reason.
export function outcomeText(write, outcome, detail) {
  const what = write && write.kind === "note" ? "The note" : `${write && write.verb}`;
  const name = what.charAt(0).toUpperCase() + what.slice(1);
  switch (outcome) {
    case "landed":
      return { text: `${name}: done.`, warn: false };
    case "stale":
      return { text: `${name}: not done. The task changed since this page read it; `
                     + "this is the task as it is now.", warn: true };
    case "refused":
      return { text: `${name}: refused. ${spellHidden(String(detail ?? "")).text}`.trim(),
               warn: true };
    case "not-running":
      return { text: `${name}: not sent. The task registry is not running.`, warn: true };
    case "found":
      return { text: `${name}: no answer came, but the task shows it landed.`,
               warn: false };
    case "not-landed":
      return { text: `${name}: no answer came, and the task is unchanged, so it has not `
                     + "landed yet. Sending it again cannot apply it twice.", warn: true };
    case "not-found-yet":
      return { text: `${name}: no answer came, and the note is not on the task yet. It `
                     + "may still land: look at the notes before sending it again.",
               warn: true };
    case "could-not-check":
      return { text: `${name}: no answer came, and the task could not be read again, `
                     + "so whether it landed is not known. Read the task before "
                     + "trying again.", warn: true };
    case "changed":
      return { text: `${name}: no answer came, and the task has changed, but not as `
                     + "this would have changed it. This is the task as it is now.",
               warn: true };
    default:
      return { text: `${name}: no answer came, so whether it landed is not known. Read `
                     + "the task before trying again.", warn: true };
  }
}

export const NO_REPOS_TEXT =
  "The registry has no repos yet. A repo is one the operator adds; until then no "
  + "agent can file a task.";
export const NOT_RUNNING_TEXT =
  "The task registry is not running, or this page cannot reach it. make tasks-up "
  + "starts it; this page picks it up again when it answers.";

// ── the DOM ─────────────────────────────────────────────────────────────────

// How long a poll waits between asks while the registry answers. Slower than the
// policy views: nothing here is waiting on the operator's minute.
export const TASKS_POLL_MS = 15000;

let tasksDown, tasksUp, tasksRepo, tasksState, tasksSearch, tasksBody, tasksEmpty,
  tasksOlder, tasksNewest, taskCountEl, taskDetail, panes, inboxEl, inboxEmpty,
  inboxCountEl, viewInbox, viewAll, inboxView, allView;

let running = null;        // null: not asked yet
let probing = null;        // the ask in flight, if one is
let repos = [];
let listPages = [];        // the `before` cursor of each page shown, newest first
let listSeen = 0;          // tasks on the pages before this one
let listFailed = false;
let listLoaded = false;
let searching = "";
let lastTasks = [];
let lastTotal = 0;
let inboxFailed = false;
let inboxLoaded = false;
let lastInbox = null;
let view = "inbox";
let openTask = null;       // the id whose detail is shown, as the hash names it
let shownTask = null;      // the task drawn beside the list, and the notes the page holds
let shownNotes = null;
let writing = null;        // the write in flight: one at a time
let outcome = null;        // what the last write came to, while its task is open
let noteDraft = { task: null, text: "" };   // kept until it lands

export function registryIsRunning() { return running === true; }

// A relayed answer's body. A 502 is the registry gone: say so, stop polling, and let
// whoever asked keep what they had. Each call site names its path in its own `fetch`,
// where the relay-allowlist test reads it.
async function registryJson(res) {
  if (res.status === NOT_RUNNING_STATUS) {
    setRunning(false);
    throw new Error("not running");
  }
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

function setRunning(up) {
  running = up;
  tasksDown.hidden = up;
  tasksUp.hidden = !up;
  if (!up) document.title = PAGE_TITLE;
}

// Ask this container whether the registry answers, then load what the page shows.
// One ask at a time: a return to the page while the first ask is out would otherwise
// make a second.
export function probeRegistry() {
  if (!probing) probing = probe().finally(() => { probing = null; });
  return probing;
}

async function probe() {
  let up = false;
  try {
    const res = await fetch("/running");
    up = res.ok && (await res.json()).running === true;
  } catch (e) {
    up = false;
  }
  setRunning(up);
  if (up) {
    await refreshRepos();
    refreshInbox();
    refreshTasks();
    applyHash();
  }
}

async function refreshRepos() {
  try {
    repos = await registryJson(await fetch("/api/repos"));
  } catch (e) {
    return;
  }
  if (!repos.length) {
    renderListStatus(tasksEmpty, { show: true, level: "none", text: NO_REPOS_TEXT });
  }
  const chosen = tasksRepo.value;
  tasksRepo.replaceChildren();
  for (const r of repos) {
    const opt = document.createElement("option");
    opt.value = r.name;
    opt.textContent = r.retired ? `${r.name} (retired)` : r.name;
    tasksRepo.appendChild(opt);
  }
  if (repos.some(r => r.name === chosen)) tasksRepo.value = chosen;
}

function cell(tr, value, cls) {
  const td = document.createElement("td");
  td.textContent = value;
  if (cls) td.className = cls;
  tr.appendChild(td);
  return td;
}

function renderTasks(rows, total) {
  tasksBody.replaceChildren();
  for (const t of rows) {
    const tr = document.createElement("tr");
    tr.appendChild(openCell(t.id));
    cell(tr, spellHidden(t.title).text);
    const stateCell = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = t.state;
    stateCell.appendChild(tag);
    if (t.ready) stateCell.append(" ready");
    else if (t.blocked) stateCell.append(" blocked");
    tr.appendChild(stateCell);
    cell(tr, [t.severity, t.kind].filter(Boolean).join(" · "));
    const claimCell = cell(tr, t.claim ? spellHidden(t.claim.holder).text : "", "actor");
    if (t.claim) claimCell.title = claimText(t.claim);
    cell(tr, t.notes_total ? String(t.notes_total) : "");
    tasksBody.appendChild(tr);
  }
  taskCountEl.textContent = !listLoaded ? ""
    : searching && rows.length < total ? `· ${rows.length} of ${total} found, newest first`
    : `· ${total} ${searching ? "found" : "in all"}`;
  markOpen();
  renderListStatus(tasksEmpty,
                   tasksStatus(rows.length, listFailed, listLoaded, Boolean(searching)));
}

export async function refreshTasks() {
  if (!registryIsRunning() || (!tasksRepo.value && !searching)) return;
  const before = listPages[listPages.length - 1];
  try {
    let page;
    if (searching) {
      const q = new URLSearchParams({ text: searching });
      if (tasksRepo.value) q.set("repo", tasksRepo.value);
      page = await registryJson(await fetch(`/api/search?${q}`));
    } else {
      const q = new URLSearchParams({ repo: tasksRepo.value, state: tasksState.value });
      if (before !== undefined) q.set("before", String(before));
      page = await registryJson(await fetch(`/api/tasks?${q}`));
    }
    lastTasks = page.tasks;
    lastTotal = page.total;
    listFailed = false;
    listLoaded = true;
  } catch (e) {
    listFailed = true;
  }
  renderTasks(lastTasks, lastTotal);
  const older = searching ? null
    : olderCursor(lastTasks, "newest-first", listSeen, lastTotal);
  tasksOlder.hidden = older === null;
  tasksOlder.dataset.before = older === null ? "" : String(older);
  tasksNewest.hidden = listPages.length === 0;
}

function resetList() {
  listPages = [];
  listSeen = 0;
  listLoaded = false;
}

export async function refreshInbox() {
  if (!registryIsRunning()) return;
  try {
    lastInbox = await registryJson(await fetch("/api/inbox"));
    inboxFailed = false;
    inboxLoaded = true;
  } catch (e) {
    inboxFailed = true;
  }
  // Checked again: a 502 clears the count, and drawing the old inbox would set it back.
  if (!registryIsRunning()) return;
  renderInbox(lastInbox);
}

// Each section that has a task in it, highest first, one row per task with every
// reason it is there.
function renderInbox(inbox) {
  inboxEl.replaceChildren();
  let rows = 0;
  for (const [key, heading, about] of INBOX_SECTIONS) {
    const section = inbox && inbox[key];
    const shown = section && Array.isArray(section.tasks) ? section.tasks : [];
    if (!shown.length) continue;
    rows += shown.length;
    const head = document.createElement("h2");
    const count = document.createElement("span");
    count.className = "note";
    count.textContent = sectionCount(shown.length, section.total);
    head.append(`${heading} `, count);
    const line = document.createElement("div");
    line.className = "empty";
    line.textContent = about;
    const table = document.createElement("table");
    const tbody = document.createElement("tbody");
    for (const t of shown) tbody.appendChild(inboxRow(t));
    table.appendChild(tbody);
    inboxEl.append(head, line, table);
  }
  const waiting = inbox && inbox.waiting ? inbox.waiting.total : 0;
  inboxCountEl.textContent = countBadge(waiting);
  if (inboxLoaded) document.title = pageTitle(waiting);
  renderListStatus(inboxEmpty, inboxStatus(rows, inboxFailed, inboxLoaded));
  markOpen();
}

function inboxRow(t) {
  const tr = document.createElement("tr");
  tr.appendChild(openCell(t.task));
  const td = document.createElement("td");
  const title = document.createElement("div");
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = t.state;
  title.append(spellHidden(t.title).text, " ", tag);
  const why = document.createElement("div");
  why.className = "why";
  why.textContent = [t.repo, ...(Array.isArray(t.reasons) ? t.reasons : [])
    .map(reasonText)].filter(Boolean).join(" · ");
  td.append(title, why);
  tr.appendChild(td);
  return tr;
}

// The cell that opens a task beside the list, by naming it in the hash.
function openCell(id) {
  const td = document.createElement("td");
  const open = document.createElement("button");
  open.type = "button";
  open.className = "group-toggle";
  open.dataset.task = String(id);
  open.textContent = `#${id}`;
  td.appendChild(open);
  return td;
}

// The row of the task read beside the list, in either view, marked as the one.
function markOpen() {
  for (const btn of panes.querySelectorAll("button[data-task]")) {
    const row = btn.closest("tr");
    if (btn.dataset.task === openTask) row.setAttribute("aria-current", "true");
    else row.removeAttribute("aria-current");
  }
}

// An author, cut to the cell as `td.actor` cuts it, whole on hover.
function authorCell(tr, author) {
  const td = cell(tr, spellHidden(author).text, "actor");
  td.title = td.textContent;
  return td;
}

// A text cell for agent prose: pre-wrapped, hidden characters spelled out and noted.
function textCell(tr, text) {
  const td = document.createElement("td");
  td.appendChild(proseBlock(text));
  tr.appendChild(td);
  return td;
}

function proseBlock(text) {
  const wrap = document.createElement("div");
  const pre = document.createElement("div");
  pre.className = "prose";
  const shown = spellHidden(text);
  pre.textContent = shown.text;
  wrap.appendChild(pre);
  if (shown.hidden) {
    const note = document.createElement("div");
    note.className = "empty warn";
    note.textContent = hiddenNote(shown.hidden);
    wrap.appendChild(note);
  }
  return wrap;
}

function line(label, value) {
  const div = document.createElement("div");
  const b = document.createElement("span");
  b.className = "qual";
  b.textContent = `${label} `;
  div.append(b, value);
  return div;
}

// One plan, its parts as the agent wrote them, and under its heading what it means for
// a task in `state` (`planNotice`). Among the earlier plans `state` is null: a newer
// plan is the task's, and the heading's "superseded" is enough.
function planBlock(plan, state) {
  const block = document.createElement("div");
  block.className = "plan";
  const head = document.createElement("div");
  head.className = "qual";
  head.textContent = planHeading(plan);
  block.appendChild(head);
  const notice = state === null ? null : planNotice(plan, state);
  if (notice) block.appendChild(noticeLine(notice));
  for (const part of planParts(plan)) {
    block.appendChild(line(part.label, proseBlock(part.text)));
  }
  return block;
}

// Every plan the task had but the one shown, newest first, under the button that
// asked: what a plan replaced, to read beside it.
async function showPlans(btn) {
  const id = openTask;
  let plans;
  try {
    plans = await registryJson(await fetch(`/api/tasks/${encodeURIComponent(id)}/plans`));
  } catch (e) {
    btn.textContent = "could not load the earlier plans";
    return;
  }
  if (openTask !== id) return;
  const holder = document.createElement("div");
  const earlier = earlierPlans(plans, btn.dataset.shown);
  if (!earlier.length) {
    const none = document.createElement("div");
    none.className = "empty";
    none.textContent = "No earlier plan.";
    holder.appendChild(none);
  }
  for (const plan of earlier) holder.appendChild(planBlock(plan, null));
  btn.replaceWith(holder);
}

function noticeLine(notice) {
  const div = document.createElement("div");
  div.className = notice.warn ? "empty warn" : "empty";
  div.textContent = notice.text;
  return div;
}

function brief(t) {
  return t ? `#${t.id} ${spellHidden(t.title).text} (${t.state})` : "";
}

// A task and every note on it, hidden ones marked: what the detail is drawn from.
async function readTask(id) {
  const path = encodeURIComponent(id);
  const task = await registryJson(await fetch(`/api/tasks/${path}`));
  const notes = await registryJson(await fetch(`/api/tasks/${path}/notes`));
  return { task, notes };
}

// One task in full, read and drawn beside the list.
async function showTask(id) {
  openTask = id;
  outcome = null;
  panes.classList.add("reading");
  markOpen();
  // The detail still drawn is another task's until this one is read: its controls go
  // off now, so no click on it is sent while the page loads another.
  markWriting();
  let task, notes;
  try {
    ({ task, notes } = await readTask(id));
  } catch (e) {
    if (openTask !== id) return;
    // A registry that stopped is read again when it answers: applyHash reads a task
    // only when it is not the one open.
    if (!registryIsRunning()) openTask = null;
    const failed = document.createElement("div");
    failed.className = "empty warn";
    failed.textContent = `Could not load task #${id}.`;
    taskDetail.replaceChildren(failed);
    taskDetail.hidden = false;
    return;
  }
  if (openTask !== id) return;
  drawTask(task, notes);
}

// What an agent reads of a task, its plan, the operator's moves on it, then every note
// with the hidden ones marked and a note to add, and its history on request.
function drawTask(task, notes) {
  shownTask = task;
  shownNotes = notes;
  taskDetail.replaceChildren();
  const head = document.createElement("h3");
  head.textContent = `#${task.id} ${spellHidden(task.title).text}`;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "group-toggle";
  close.dataset.close = "1";
  close.textContent = "close";
  close.title = "close this task (the list stays)";
  head.append(" ", close);
  taskDetail.appendChild(head);
  taskDetail.appendChild(line("state", `${task.state}${task.ready ? ", ready" : ""}`
    + `${task.blocked ? ", blocked" : ""} · revision ${task.revision} · ${task.repo}`));
  const sk = [task.severity, task.kind].filter(Boolean).join(" · ");
  if (sk) taskDetail.appendChild(line("severity · kind", sk));
  taskDetail.appendChild(line("written", `${fmtStamp(task.created_at)}, last changed `
    + fmtStamp(task.updated_at)));
  if (task.parent) taskDetail.appendChild(line("parent", brief(task.parent)));
  const kids = childrenLeft(task.children);
  if (kids.total) {
    taskDetail.appendChild(line("children", `${kids.text}: `
      + task.children.map(brief).join("; ")));
  }
  if (task.blockers && task.blockers.length) {
    taskDetail.appendChild(line("blocked by", task.blockers.map(brief).join("; ")));
  }
  if (task.claim) {
    taskDetail.appendChild(line("claim", claimText(task.claim)));
    if (Array.isArray(task.claim.footprint)) {
      const paths = task.claim.footprint.map(p => spellHidden(p).text).join(", ");
      taskDetail.appendChild(line("claim's footprint", paths));
    }
    if (task.claim.progress) {
      taskDetail.appendChild(line("progress", proseBlock(task.claim.progress)));
    }
  }
  const body = proseBlock(task.body);
  body.className = "task-body";
  taskDetail.appendChild(body);
  // The plan beside the body: approving a refined task approves the two together
  // (task-registry/DESIGN.md, "Plans").
  const planHead = document.createElement("h3");
  planHead.textContent = "Plan";
  taskDetail.appendChild(planHead);
  if (task.plan) {
    taskDetail.appendChild(planBlock(task.plan, task.state));
    const more = pagerButton("earlier plans", "plans", "");
    more.dataset.shown = String(task.plan.id);
    taskDetail.appendChild(more);
  } else {
    taskDetail.appendChild(noticeLine(planNotice(null, task.state)));
  }
  // Below the plan, so what a move approves is read before it.
  taskDetail.appendChild(movesBlock(task));
  const notesHead = document.createElement("h3");
  notesHead.textContent = `Notes · ${notes.total}`;
  taskDetail.appendChild(notesHead);
  // Earlier pages go above the one shown, so the notes read oldest first throughout.
  const earlier = olderCursor(notes.notes, "oldest-first", 0, notes.total);
  if (earlier !== null) {
    const btn = pagerButton("earlier notes", "notes", earlier);
    btn.dataset.seen = String(notes.notes.length);
    taskDetail.appendChild(btn);
  }
  const notesTable = document.createElement("table");
  const notesBody = document.createElement("tbody");
  renderNotes(notesBody, notes.notes);
  notesTable.appendChild(notesBody);
  taskDetail.appendChild(notesTable);
  taskDetail.appendChild(noteForm(task));
  const history = document.createElement("div");
  history.className = "history";
  history.appendChild(pagerButton("history", "history", ""));
  taskDetail.appendChild(history);
  taskDetail.hidden = false;
  markWriting();
}

// The moves the operator API takes from the task's state, and what the last one came
// to. Approve and send back are each a control of their own, as a refined task's two
// answers; approve names what it approves.
function movesBlock(task) {
  const block = document.createElement("div");
  const head = document.createElement("h3");
  head.textContent = "Moves";
  block.appendChild(head);
  if (outcome && outcome.task === String(task.id)) block.appendChild(noticeLine(outcome));
  const row = document.createElement("div");
  row.className = "moves";
  for (const move of movesFor(task)) {
    const btn = writeButton(moveLabel(move, task), "move", move.state);
    btn.dataset.verb = move.verb;
    row.appendChild(btn);
  }
  if (task.claim) row.appendChild(writeButton(breakLabel(task.claim), "break", ""));
  block.appendChild(row);
  const hidden = task.state === "refined" ? approveHidden(task) : "";
  if (hidden) block.appendChild(noticeLine({ text: hidden, warn: true }));
  return block;
}

function writeButton(label, kind, value) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.dataset.write = kind;
  btn.dataset.value = value;
  btn.textContent = label;
  return btn;
}

// A note to the task, as the operator: agents read it. Its text stays until it lands.
function noteForm(task) {
  const form = document.createElement("div");
  form.className = "note-form";
  const text = document.createElement("textarea");
  text.rows = 3;
  text.setAttribute("aria-label", "A note to this task");
  text.placeholder = "a note to this task, which agents read";
  text.value = noteDraft.task === String(task.id) ? noteDraft.text : "";
  text.dataset.note = "1";
  form.append(text, writeButton("add the note", "note", ""));
  return form;
}

// Every write control in the detail, on only while no write is out and what is drawn
// is the task the page names.
function drawnIsOpen() {
  return shownTask !== null && String(shownTask.id) === openTask;
}
function markWriting() {
  for (const el of taskDetail.querySelectorAll("[data-write], textarea[data-note]")) {
    el.disabled = writing !== null || !drawnIsOpen();
  }
}

// One write, then the page as the registry has it after: the task read again, or for
// a stale one drawn from the task the refusal carries, which is not read again
// (task-registry/DESIGN.md, "The operator API"); the inbox and the list refreshed. A
// write whose answer was lost is decided from the read (`checkWrite`), never sent again.
async function runWrite(write) {
  if (writing || !registryIsRunning()) return;
  writing = write;
  markWriting();
  let answer = null;
  try {
    answer = await readAnswer(await sendWrite(write));
  } catch (e) {
    answer = null;
  }
  let result = writeOutcome(answer);
  let drawn = null;
  if (result === "not-running") {
    setRunning(false);
  } else if (result === "stale") {
    drawn = { task: answer.body.current, notes: shownNotes };
  } else {
    drawn = await readTask(write.task).catch(() => null);
    if (result === "unknown") result = checkWrite(write, drawn);
  }
  const landed = result === "landed" || result === "found";
  if (write.kind === "note" && landed && noteDraft.task === write.task
      && noteDraft.text === write.text) {
    noteDraft = { task: null, text: "" };
  }
  writing = null;
  if (openTask === write.task) {
    const detail = answer && answer.body && typeof answer.body === "object"
      ? answer.body.detail : answer && answer.text;
    outcome = { task: write.task, ...outcomeText(write, result, detail) };
    if (drawn) drawTask(drawn.task, drawn.notes);
    else if (shownTask) drawTask(shownTask, shownNotes);
  } else {
    markWriting();
  }
  if (result !== "not-running") {
    refreshInbox();
    refreshTasks();
  }
}

// The write a control in the detail asks for, as of the task the page shows.
// Built from what is drawn, id and revision both, and only while that is the task the
// page names: while another loads, the drawn one is not what a click is for.
function writeFor(btn) {
  if (!drawnIsOpen()) return null;
  const base = { task: String(shownTask.id), revision: shownTask.revision,
                 lastNote: lastNoteId(shownNotes) };
  if (btn.dataset.write === "move") {
    return { ...base, kind: "move", state: btn.dataset.value, verb: btn.dataset.verb };
  }
  if (btn.dataset.write === "break") return { ...base, kind: "break", verb: "break the claim" };
  const text = noteDraft.task === base.task ? noteDraft.text : "";
  return text.trim() ? { ...base, kind: "note", text, verb: "add the note" } : null;
}

function pagerButton(label, kind, before) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "group-toggle";
  btn.dataset.page = kind;
  btn.dataset.before = String(before);
  btn.textContent = label;
  return btn;
}

function renderNotes(tbody, notes) {
  for (const n of notes) {
    const tr = document.createElement("tr");
    const marks = [n.for_operator ? "for you" : "", n.handled ? "handled" : "",
                   n.hidden ? "hidden" : "", n.move || "",
                   n.reply_to ? `reply to #${n.reply_to}` : ""].filter(Boolean);
    cell(tr, `#${n.id}`, "ts");
    authorCell(tr, n.author);
    const at = cell(tr, fmtStamp(n.at), "ts");
    at.title = fmtInstant(n.at);
    cell(tr, marks.join(" · "), "qual");
    textCell(tr, n.text);
    if (n.hidden) tr.className = "hidden-note";
    tbody.appendChild(tr);
  }
}

// An older page of a task's notes or its history, shown under the button that asked.
async function showPage(btn) {
  const id = openTask;
  const kind = btn.dataset.page;
  const q = new URLSearchParams();
  if (btn.dataset.before) q.set("before", btn.dataset.before);
  const path = encodeURIComponent(id);
  let page;
  try {
    page = await registryJson(kind === "notes"
      ? await fetch(`/api/tasks/${path}/notes?${q}`)
      : await fetch(`/api/tasks/${path}/history?${q}`));
  } catch (e) {
    btn.textContent = `could not load the ${kind}`;
    return;
  }
  if (openTask !== id) return;
  const table = document.createElement("table");
  const tbody = document.createElement("tbody");
  const items = kind === "notes" ? page.notes : page.history;
  if (kind === "notes") {
    renderNotes(tbody, items);
  } else {
    for (const h of items) {
      const tr = document.createElement("tr");
      const at = cell(tr, fmtStamp(h.ts), "ts");
      at.title = fmtInstant(h.ts);
      authorCell(tr, h.author);
      cell(tr, h.action, "qual");
      const td = document.createElement("td");
      for (const [key, value] of Object.entries(h.detail || {})) {
        td.appendChild(line(key, typeof value === "string" ? proseBlock(value)
          : spellHidden(JSON.stringify(value)).text));
      }
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }
  table.appendChild(tbody);
  const seen = Number(btn.dataset.seen || 0) + items.length;
  const more = olderCursor(items, "oldest-first", seen, page.total);
  // Above this page, as in showTask, so the record reads oldest first.
  const holder = document.createElement("div");
  if (more !== null) {
    const next = pagerButton(`earlier ${kind}`, kind, more);
    next.dataset.seen = String(seen);
    holder.appendChild(next);
  }
  holder.appendChild(table);
  btn.replaceWith(holder);
}

function closeTask() {
  openTask = null;
  shownTask = null;
  shownNotes = null;
  outcome = null;
  panes.classList.remove("reading");
  taskDetail.hidden = true;
  taskDetail.replaceChildren();
  markOpen();
}

function showView(name) {
  view = name;
  inboxView.hidden = name !== "inbox";
  allView.hidden = name !== "all";
  viewInbox.setAttribute("aria-pressed", String(name === "inbox"));
  viewAll.setAttribute("aria-pressed", String(name === "all"));
}

// The page as the hash names it: the view at once, the task once the registry answers
// (probe calls this again when it does).
function applyHash() {
  const named = parseHash(location.hash);
  showView(named.view);
  if (!registryIsRunning()) return;
  if (named.task === null) {
    if (openTask !== null) closeTask();
  } else if (named.task !== openTask) {
    showTask(named.task);
  }
}

// Every way to open, close or switch goes through the hash, so back and a reload agree
// with what was clicked. A click naming what the hash already names reads the task
// again, or closes it.
function goTo(nextView, task) {
  const next = hashFor(nextView, task);
  if (next !== location.hash) {
    location.hash = next;
  } else if (task === null) {
    closeTask();
  } else if (registryIsRunning()) {
    showTask(task);
  }
}

export function mountTasks() {
  tasksDown = document.getElementById("tasks-down");
  tasksUp = document.getElementById("tasks-up");
  tasksRepo = document.getElementById("tasks-repo");
  tasksState = document.getElementById("tasks-state");
  tasksSearch = document.getElementById("tasks-search");
  tasksBody = document.getElementById("tasks");
  tasksEmpty = document.getElementById("tasks-empty");
  tasksOlder = document.getElementById("tasks-older");
  tasksNewest = document.getElementById("tasks-newest");
  taskCountEl = document.getElementById("taskcount");
  taskDetail = document.getElementById("task-detail");
  panes = document.getElementById("panes");
  inboxEl = document.getElementById("inbox");
  inboxEmpty = document.getElementById("inbox-empty");
  inboxCountEl = document.getElementById("inboxcount");
  viewInbox = document.getElementById("view-inbox");
  viewAll = document.getElementById("view-all");
  inboxView = document.getElementById("inbox-view");
  allView = document.getElementById("all-view");

  tasksDown.textContent = NOT_RUNNING_TEXT;
  for (const s of [...TASK_STATES, EVERY_STATE]) {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s === EVERY_STATE ? "every state" : s;
    tasksState.appendChild(opt);
  }
  tasksState.value = "proposed";

  tasksRepo.addEventListener("change", () => { resetList(); refreshTasks(); });
  tasksState.addEventListener("change", () => { resetList(); refreshTasks(); });
  tasksSearch.addEventListener("change", () => {
    searching = tasksSearch.value.trim();
    resetList();
    refreshTasks();
  });
  tasksOlder.addEventListener("click", () => {
    listSeen += lastTasks.length;
    listPages.push(Number(tasksOlder.dataset.before));
    refreshTasks();
  });
  tasksNewest.addEventListener("click", () => { resetList(); refreshTasks(); });
  viewInbox.addEventListener("click", () => goTo("inbox", openTask));
  viewAll.addEventListener("click", () => goTo("all", openTask));
  panes.addEventListener("click", ev => {
    const btn = ev.target.closest("button[data-task]");
    if (btn) goTo(view, btn.dataset.task);
  });
  taskDetail.addEventListener("click", ev => {
    if (ev.target.closest("button[data-close]")) {
      goTo(view, null);
      return;
    }
    const page = ev.target.closest("button[data-page]");
    if (page) (page.dataset.page === "plans" ? showPlans : showPage)(page);
    const btn = ev.target.closest("button[data-write]");
    const write = btn ? writeFor(btn) : null;
    if (write) runWrite(write);
  });
  taskDetail.addEventListener("input", ev => {
    if (ev.target.matches("textarea[data-note]") && shownTask) {
      noteDraft = { task: String(shownTask.id), text: ev.target.value };
    }
  });
  window.addEventListener("hashchange", applyHash);
  applyHash();
}
