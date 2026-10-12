// SPDX-License-Identifier: Apache-2.0
/* The Tasks tab: the task registry's work list, read through this UI's relay to the
 * registry's operator listener (`/tasks/api/…`, control-plane-ui/app.py). A backlog
 * nobody waits on, so nothing here alerts the way a hold does: the tab carries a quiet
 * count of the notes agents flagged for the operator, and the lamp ignores it.
 *
 * The registry is optional (`make tasks-up`). The page asks `/tasks/running` first,
 * which this container answers itself, so a registry that is not running is one line
 * on this tab rather than a failed request in the console; and nothing here polls it
 * until the operator opens the tab again.
 *
 * Every string a task carries was written by an agent, or by the operator for agents
 * to read, so it is set as textContent, and a character that renders as nothing or
 * reorders the text around it is spelled out where it sits (`spellHidden`).
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */
import { escapePayload } from "./payload.js";
import { pollStatus, renderListStatus } from "./status.js";
import { fmtStamp, fmtInstant } from "./time.js";

// The registry's states, in the order work moves through them
// (task-registry/DESIGN.md, "States"), and what `GET /api/tasks` takes for all of them
// (operator_api.EVERY_STATE). A test holds both to the registry's.
export const TASK_STATES = ["proposed", "open", "landing", "deferred", "closed"];
export const EVERY_STATE = "all";

// Characters that render as nothing or reorder what is around them: the payload
// view's set (payload.js), less the line break and the tab, since this is prose, and
// with Unicode's default-ignorable code points, which include the variation selectors
// a hidden payload can ride on to an agent. One U+202E in a body draws the rest of its
// line backwards, and an approved body is what every later session reads, so the
// operator must see what is in it rather than what the browser makes of it. An emoji's
// U+FE0F is spelled too: the price of not guessing which selector carries nothing.
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

// The tab's quiet count: notes waiting on the operator, or nothing. Never "0", which
// would still draw the eye to a tab nobody needs to open.
export function flaggedBadge(total) {
  const n = Number(total);
  return Number.isInteger(n) && n > 0 ? String(n) : "";
}

// What a list says about itself, as every polled list here does (status.js). Its own
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

const FLAGGED_STATUS_TEXT = {
  stale: "Could not refresh — these are the last flagged notes loaded and may be out "
         + "of date.",
  cold: "Could not load the flagged notes.",
  empty: "No note waits on you.",
};
export function flaggedStatus(rowCount, failed, loaded) {
  return pollStatus(FLAGGED_STATUS_TEXT, rowCount, failed, loaded);
}

// The relay's answer when the registry did not answer (`_registry_unreachable` in
// control-plane-ui/app.py); the registry never sends it itself.
export const NOT_RUNNING_STATUS = 502;
export const NO_REPOS_TEXT =
  "The registry has no repos yet. A repo is one the operator adds; until then no "
  + "agent can file a task.";
export const NOT_RUNNING_TEXT =
  "The task registry is not running, or this UI cannot reach it. make tasks-up starts "
  + "it; this tab asks again when you open it.";

// ── the DOM ─────────────────────────────────────────────────────────────────

// How long a poll waits between asks while the registry answers. Slower than the
// policy views: nothing here is waiting on the operator's minute.
export const TASKS_POLL_MS = 15000;

let tasksDown, tasksUp, tasksRepo, tasksState, tasksSearch, tasksBody, tasksEmpty,
  tasksOlder, tasksNewest, taskCountEl, taskDetail, flaggedBody, flaggedEmpty,
  flaggedOlder, flaggedNewest, flaggedCountEl, tasksBadge;

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
let flaggedPages = [];
let flaggedSeen = 0;
let flaggedFailed = false;
let flaggedLoaded = false;
let lastFlagged = [];
let lastFlaggedTotal = 0;
let openTask = null;       // the id whose detail is shown

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
  if (!up) tasksBadge.textContent = "";
}

// Ask this container whether the registry answers, then load what the tab shows.
// One ask at a time: a page loaded at #tasks would otherwise ask twice, once as its
// view opens and once at start-up.
export function probeRegistry() {
  if (!probing) probing = probe().finally(() => { probing = null; });
  return probing;
}

async function probe() {
  let up = false;
  try {
    const res = await fetch("/tasks/running");
    up = res.ok && (await res.json()).running === true;
  } catch (e) {
    up = false;
  }
  setRunning(up);
  if (up) {
    await refreshRepos();
    refreshTasks();
    refreshFlagged();
  }
}

async function refreshRepos() {
  try {
    repos = await registryJson(await fetch("/tasks/api/repos"));
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
    const idCell = document.createElement("td");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "group-toggle";
    open.dataset.task = String(t.id);
    open.textContent = `#${t.id}`;
    idCell.appendChild(open);
    tr.appendChild(idCell);
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
      page = await registryJson(await fetch(`/tasks/api/search?${q}`));
    } else {
      const q = new URLSearchParams({ repo: tasksRepo.value, state: tasksState.value });
      if (before !== undefined) q.set("before", String(before));
      page = await registryJson(await fetch(`/tasks/api/tasks?${q}`));
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

export async function refreshFlagged() {
  if (!registryIsRunning()) return;
  const q = new URLSearchParams();
  const before = flaggedPages[flaggedPages.length - 1];
  if (before !== undefined) q.set("before", String(before));
  try {
    const page = await registryJson(await fetch(`/tasks/api/notes/flagged?${q}`));
    lastFlagged = page.notes;
    lastFlaggedTotal = page.total;
    flaggedFailed = false;
    flaggedLoaded = true;
  } catch (e) {
    flaggedFailed = true;
  }
  if (!registryIsRunning()) return;
  flaggedBody.replaceChildren();
  for (const n of lastFlagged) {
    const tr = document.createElement("tr");
    const taskCell = document.createElement("td");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "group-toggle";
    open.dataset.task = String(n.task);
    open.textContent = `#${n.task}`;
    taskCell.appendChild(open);
    tr.appendChild(taskCell);
    authorCell(tr, n.author);
    const at = cell(tr, fmtStamp(n.at), "ts");
    at.title = fmtInstant(n.at);
    textCell(tr, n.text);
    flaggedBody.appendChild(tr);
  }
  flaggedCountEl.textContent = flaggedLoaded ? `· ${lastFlaggedTotal}` : "";
  // The badge counts every flagged note not yet handled, whichever page is shown.
  if (flaggedPages.length === 0 && flaggedLoaded) {
    tasksBadge.textContent = flaggedBadge(lastFlaggedTotal);
  }
  renderListStatus(flaggedEmpty,
                   flaggedStatus(lastFlagged.length, flaggedFailed, flaggedLoaded));
  const older = olderCursor(lastFlagged, "newest-first", flaggedSeen, lastFlaggedTotal);
  flaggedOlder.hidden = older === null;
  flaggedOlder.dataset.before = older === null ? "" : String(older);
  flaggedNewest.hidden = flaggedPages.length === 0;
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

function brief(t) {
  return t ? `#${t.id} ${spellHidden(t.title).text} (${t.state})` : "";
}

// One task in full: what an agent reads of it, then every note with the hidden ones
// marked, and its history on request.
async function showTask(id) {
  openTask = id;
  let task, notes;
  try {
    const path = encodeURIComponent(id);
    task = await registryJson(await fetch(`/tasks/api/tasks/${path}`));
    notes = await registryJson(await fetch(`/tasks/api/tasks/${path}/notes`));
  } catch (e) {
    if (openTask !== id) return;
    const failed = document.createElement("div");
    failed.className = "empty warn";
    failed.textContent = `Could not load task #${id}.`;
    taskDetail.replaceChildren(failed);
    taskDetail.hidden = false;
    return;
  }
  if (openTask !== id) return;
  taskDetail.replaceChildren();
  const head = document.createElement("h3");
  head.textContent = `#${task.id} ${spellHidden(task.title).text}`;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "group-toggle";
  close.dataset.close = "1";
  close.textContent = "close";
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
      taskDetail.appendChild(line("footprint", paths));
    }
    if (task.claim.progress) {
      taskDetail.appendChild(line("progress", proseBlock(task.claim.progress)));
    }
  }
  const body = proseBlock(task.body);
  body.className = "task-body";
  taskDetail.appendChild(body);
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
  const history = document.createElement("div");
  history.className = "history";
  history.appendChild(pagerButton("history", "history", ""));
  taskDetail.appendChild(history);
  taskDetail.hidden = false;
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
      ? await fetch(`/tasks/api/tasks/${path}/notes?${q}`)
      : await fetch(`/tasks/api/tasks/${path}/history?${q}`));
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
  flaggedBody = document.getElementById("flagged");
  flaggedEmpty = document.getElementById("flagged-empty");
  flaggedOlder = document.getElementById("flagged-older");
  flaggedNewest = document.getElementById("flagged-newest");
  flaggedCountEl = document.getElementById("flaggedcount");
  tasksBadge = document.getElementById("badge-tasks");

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
  flaggedOlder.addEventListener("click", () => {
    flaggedSeen += lastFlagged.length;
    flaggedPages.push(Number(flaggedOlder.dataset.before));
    refreshFlagged();
  });
  flaggedNewest.addEventListener("click", () => {
    flaggedPages = [];
    flaggedSeen = 0;
    refreshFlagged();
  });
  const opener = ev => {
    const btn = ev.target.closest("button[data-task]");
    if (btn) showTask(btn.dataset.task);
  };
  tasksBody.addEventListener("click", opener);
  flaggedBody.addEventListener("click", opener);
  taskDetail.addEventListener("click", ev => {
    if (ev.target.closest("button[data-close]")) {
      openTask = null;
      taskDetail.hidden = true;
      taskDetail.replaceChildren();
      return;
    }
    const page = ev.target.closest("button[data-page]");
    if (page) showPage(page);
  });
}
