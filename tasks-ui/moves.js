// SPDX-License-Identifier: Apache-2.0
/* The tasks page's writes: the ones the relay passes (tasks-ui/app.py), which moves a
 * task or a note offers, and what the page makes of each answer. Nothing here draws:
 * tasks.js draws, and imports this, which imports nothing of it. Why a move names a
 * revision and a note and a move on one none, and why a lost answer is read back rather
 * than sent again, is tasks-ui/DESIGN.md, "A note names no revision" and "A write whose
 * answer was lost".
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_tasks_ui_js.py).
 */

// The registry's states, in the order work moves through them
// (task-registry/DESIGN.md, "States"). A test holds them to the registry's.
export const TASK_STATES = ["proposed", "refined", "open", "landing", "deferred", "closed"];

// The relay's two answers of its own, neither of which the registry sends: it could not
// be reached, so nothing was sent (`_registry_unreachable` in tasks-ui/app.py), and a
// write was sent and its answer never came (`_outcome_unknown`).
export const NOT_RUNNING_STATUS = 502;
export const OUTCOME_UNKNOWN_STATUS = 504;

// What each move is called, from the state it leaves to the one it reaches, where it
// has a name of its own; any other is "move to <state>". Approve and send back are a
// refined task's two answers, kept apart so each can grow its own (task 6's decisions).
export function moveVerb(from, to) {
  if (from === "refined" && to === "open") return "approve";
  if (from === "refined" && to === "proposed") return "send back";
  if (to === "closed") return "close";
  if (to === "deferred") return "defer";
  if (to === "open" && (from === "closed" || from === "deferred")) return "reopen";
  return `move to ${to}`;
}

// The moves the operator API takes from the task's state: every other state, refined
// only with a current plan (`operator_moves.move`), each marked when it ends the claim,
// which a move to another state does. A task's claim is shown only while it is live.
export function movesFor(task) {
  if (!task || typeof task !== "object" || !TASK_STATES.includes(task.state)) return [];
  const current = task.plan && typeof task.plan === "object" && !task.plan.superseded_at;
  return TASK_STATES
    .filter(s => s !== task.state && (s !== "refined" || current))
    .map(s => ({ state: s, verb: moveVerb(task.state, s),
                 endsClaim: Boolean(task.claim) }));
}

// The moves on one note, as the timeline offers them: hide or show any note, mark one
// flagged for the operator handled, or a handled one not. Marking handled opens a box
// for a reply rather than sending at once, so `reply` says the control opens it.
export function noteMovesFor(note) {
  if (!note || typeof note !== "object" || !Number.isInteger(note.id)) return [];
  const moves = [note.hidden
    ? { kind: "hide", value: false, verb: "show" }
    : { kind: "hide", value: true, verb: "hide" }];
  if (note.handled) moves.push({ kind: "handle", value: false, verb: "mark not handled" });
  else if (note.for_operator) moves.push({ kind: "handle", value: true, reply: true,
                                           verb: "mark handled" });
  return moves;
}

// What a bulk handle names: the notes both selected and drawn, in the order drawn, so
// a selection never reaches a note the page no longer shows (task-registry/DESIGN.md,
// "Notes"). Drawn is a page of them, under `operator_moves.BULK_MAX`.
export function bulkHandle(selected, drawn) {
  const chosen = new Set((selected instanceof Set ? [...selected]
    : Array.isArray(selected) ? selected : []).map(Number));
  return (Array.isArray(drawn) ? drawn : [])
    .map(n => (n && typeof n === "object" ? n.id : n))
    .filter(id => Number.isInteger(id) && chosen.has(id));
}

// The states in which a task's flagged notes wait with it, out of the inbox, and come
// back when it is reopened (`operator_moves.inbox`). The flagged list still carries
// them, each with its task's state (`task_state`).
export const PARKED_STATES = ["deferred", "closed"];
export function onParkedTask(note) {
  return Boolean(note) && typeof note === "object"
    && PARKED_STATES.includes(note.task_state);
}

// Notes by id, for comparing a drawn copy with a newer one.
function byId(notes) {
  return new Map((Array.isArray(notes) ? notes : [])
    .filter(n => n && typeof n === "object").map(n => [n.id, n]));
}

// What "select all shown" ticks: every note drawn but one on a parked task, which is
// handled only when ticked on its own, so it is not handled by accident and lost to the
// inbox for good. Parked by the drawn copy or by the newest read (`read`, its notes):
// a task deferred or closed since the rows were drawn counts.
export function selectAll(drawn, read) {
  const now = byId(read);
  return (Array.isArray(drawn) ? drawn : [])
    .filter(n => n && typeof n === "object" && Number.isInteger(n.id)
            && !onParkedTask(n) && !onParkedTask(now.get(n.id)))
    .map(n => n.id);
}

// What a tick records: whether its row is drawn as on a parked task, marked so. Only a
// marked row is an informed choice of a parked note; a poll that has learned of a park
// since moves no row, so it does not count here.
export function tickMark(id, drawn) {
  return onParkedTask(byId(drawn).get(id));
}

// A selection is a map from each ticked id to its `tickMark`. A note whose task was
// parked since its row was drawn is never sent, for a park a read has seen: the rule is
// applied where a bulk is built and counted (`bulkToSend`), and a redraw drops such a
// tick (`keptSelection`). A note ticked on a row marked parked was chosen as it is.
function parkedSince(marks, copy) {
  return onParkedTask(copy) && !marks.get(copy.id);
}

// What a bulk handle sends, from the ticks, the notes drawn and the last good read:
// the ids both ticked and drawn, in the order drawn (`bulkHandle`), less any whose task
// was parked since it was ticked, which are left out with the state that kept them.
export function bulkToSend(ticked, drawn, known) {
  const marks = ticked instanceof Map ? ticked : new Map();
  const now = byId(known), shown = byId(drawn);
  const notes = [], leftOut = [];
  for (const id of bulkHandle([...marks.keys()], drawn)) {
    const copy = now.get(id) || shown.get(id);
    if (parkedSince(marks, copy)) leftOut.push({ id, state: copy.task_state });
    else notes.push(id);
  }
  return { notes, leftOut };
}

// The ticks a redraw keeps: those still drawn, less any whose task was parked since it
// was ticked, so a selection never carries such a note forward unseen.
export function keptSelection(ticked, after) {
  const marks = ticked instanceof Map ? ticked : new Map();
  return new Map((Array.isArray(after) ? after : [])
    .filter(n => n && typeof n === "object" && marks.has(n.id) && !parkedSince(marks, n))
    .map(n => [n.id, marks.get(n.id)]));
}

// Whether a write's outcome redraws the flagged rows: a move on notes, and a task move,
// which can park or reopen the task a drawn note is on; none the registry never got.
export function redrawsFlagged(write, result) {
  return result !== "not-running" && Boolean(write) && typeof write === "object"
    && (isNoteMove(write) || write.kind === "move");
}

// The writes on notes rather than on a task: after one, the page reads the flagged list
// again as well as the task.
const NOTE_MOVES = ["hide", "handle", "handle-many"];
export function isNoteMove(write) {
  return Boolean(write) && typeof write === "object" && NOTE_MOVES.includes(write.kind);
}

const JSON_TYPE = { "content-type": "application/json" };

// Send one write. Each names its path in its own `fetch`, as a POST, where the relay
// test reads both. The registry refuses a field it does not know (`Body` in
// task-registry/operator_api.py), and takes ids as numbers only.
export function sendWrite(write) {
  const path = encodeURIComponent(write.task);
  if (write.kind === "move") {
    return fetch(`/api/tasks/${path}/move`, { method: "POST", headers: JSON_TYPE,
      body: JSON.stringify({ revision: write.revision, state: write.state }) });
  }
  if (write.kind === "break") {
    return fetch(`/api/tasks/${path}/break-claim`, { method: "POST", headers: JSON_TYPE,
      body: JSON.stringify({ revision: write.revision }) });
  }
  if (write.kind === "note") {
    return fetch(`/api/tasks/${path}/notes`, { method: "POST", headers: JSON_TYPE,
      body: JSON.stringify({ text: write.text }) });
  }
  if (write.kind === "hide") {
    return fetch(`/api/notes/hide`, { method: "POST", headers: JSON_TYPE,
      body: JSON.stringify({ notes: [Number(write.note)], hidden: write.hidden }) });
  }
  if (write.kind === "handle") {
    return fetch(`/api/notes/${encodeURIComponent(write.note)}/handle`, {
      method: "POST", headers: JSON_TYPE,
      body: JSON.stringify({ handled: write.handled, reply: write.reply ?? null }) });
  }
  if (write.kind === "handle-many") {
    return fetch(`/api/notes/handle`, { method: "POST", headers: JSON_TYPE,
      body: JSON.stringify({ notes: write.notes.map(Number), handled: true }) });
  }
  throw new Error(`no such write: ${write.kind}`);
}

// An answer as the page judges it: its status, and its body if it parsed. A refusal of
// the guard's is plain text, kept for what it says; an answer cut off has no body.
export async function readAnswer(res) {
  let text;
  try {
    text = await res.text();
  } catch (e) {
    return { status: res.status, body: undefined, text: "" };
  }
  let body;
  try {
    body = JSON.parse(text);
  } catch (e) {
    body = undefined;
  }
  return { status: res.status, body, text };
}

// What an answer says happened, or `null` for a fetch that rejected:
//   landed       2xx with ok;
//   stale        409 with the task as it is now, which the page draws;
//   refused      any other refusal, the guard's 403 among them;
//   not-running  the relay could not reach the registry, so nothing was sent;
//   unknown      no answer to trust: the relay's 504, a rejection, or one cut off
//                (no `ok` to read, or a 409 without the task). `checkWrite` decides.
export function writeOutcome(answer) {
  if (!answer || typeof answer !== "object") return "unknown";
  const { status, body } = answer;
  const parsed = body !== null && typeof body === "object";
  if (status === NOT_RUNNING_STATUS) return "not-running";
  if (status === OUTCOME_UNKNOWN_STATUS) return "unknown";
  if (status >= 200 && status < 300) {
    return parsed && body.ok === true ? "landed" : "unknown";
  }
  if (status === 409) {
    return parsed && body.current !== null && typeof body.current === "object"
      ? "stale" : "unknown";
  }
  return "refused";
}

// Whether a write whose answer was lost landed, from what was read after it, or `null`
// when that read failed. A bulk handle is checked against the flagged notes read from
// just past the highest it named (`{ flagged }`); every other write against its task
// read again (`{ task, held }`), which carries its latest notes and the newest page of
// its timeline:
//   found           it shows it landed: a move's revision moved and its state is the
//                   target; a break's moved and nobody holds it; a note of the
//                   operator's with its text is newer than any the page held; a note
//                   move's own entry, with the value sent, is newer than any the page
//                   held; no note a bulk named is still listed, since all of one lands
//                   or none;
//   not-landed      a move or break whose revision did not move: sending it again
//                   cannot apply it twice, since the revision would refuse it; or a
//                   bulk whose notes are still listed, which sending again only marks;
//   not-seen        a hide, show or handle without a reply whose entry is not there:
//                   it may still land, and sending it again sets the same flag;
//   not-found-yet   a note or a reply that is not there, which may still be committing:
//                   never "safe to send again";
//   could-not-check the read failed, or more landed since than its newest page holds;
//   changed         the task changed, but not as this write would have.
export function checkWrite(write, reread) {
  if (write.kind === "handle-many") {
    const listed = reread && reread.flagged && Array.isArray(reread.flagged.notes)
      ? new Set(reread.flagged.notes.map(n => n && n.id)) : null;
    if (!listed) return "could-not-check";
    return write.notes.some(id => listed.has(Number(id))) ? "not-landed" : "found";
  }
  if (!reread || !reread.task || typeof reread.task !== "object") {
    return "could-not-check";
  }
  const task = reread.task;
  if (write.kind === "hide" || write.kind === "handle") return checkNoteMove(write, reread);
  if (write.kind === "note") {
    const notes = Array.isArray(task.notes) ? task.notes : [];
    for (const n of notes) {
      if (n && n.author === "operator" && n.text === write.text
          && Number(n.id) > Number(write.lastNote)) return "found";
    }
    return "not-found-yet";
  }
  if (task.revision === write.revision) return "not-landed";
  if (write.kind === "move" && task.state === write.state) return "found";
  if (write.kind === "break" && !task.claim) return "found";
  return "changed";
}

// A note move by its own history entry, on the newest page read again, wherever its
// note sits: one newer than any the page held, naming the note with the value sent.
// When even the oldest entry on a full page is newer than those held, entries older
// than the page may be newer than those held too, and its absence says nothing.
function checkNoteMove(write, reread) {
  const timeline = reread.held && reread.held.timeline;
  const history = timeline && Array.isArray(timeline.history) ? timeline.history : null;
  if (!history) return "could-not-check";
  const held = Number(write.lastEntry);
  const action = write.kind === "hide" ? "hide_note" : "handle_note";
  for (const h of history) {
    if (!h || Number(h.id) <= held || h.action !== action) continue;
    const d = h.detail && typeof h.detail === "object" ? h.detail : {};
    if (Number(d.note_id) !== Number(write.note)) continue;
    if (write.kind === "hide" && d.hidden === write.hidden) return "found";
    if (write.kind === "handle" && d.handled === write.handled
        && (d.reply ?? null) === (write.reply ?? null)) return "found";
  }
  const oldest = history.length ? Number(history[0].id) : null;
  if (oldest !== null && oldest > held && Number(timeline.total) > history.length) {
    return "could-not-check";
  }
  return write.reply ? "not-found-yet" : "not-seen";
}

// The id of the newest entry a page of a task's timeline holds, or 0: what a note move's
// check counts from.
export function lastEntryId(timeline) {
  const list = timeline && Array.isArray(timeline.history) ? timeline.history : [];
  return list.reduce((top, h) => (h && Number(h.id) > top ? Number(h.id) : top), 0);
}

// The id of the newest note a read of the task carries, or 0: what a note's check counts
// from.
export function lastNoteId(task) {
  const list = task && Array.isArray(task.notes) ? task.notes : [];
  return list.reduce((top, n) => (n && Number(n.id) > top ? Number(n.id) : top), 0);
}
