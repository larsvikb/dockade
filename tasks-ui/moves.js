// SPDX-License-Identifier: Apache-2.0
/* The tasks page's writes: the three the relay passes (tasks-ui/app.py), which moves a
 * task offers, and what the page makes of each answer. Nothing here draws: tasks.js
 * draws, and imports this, which imports nothing of it. Why a move names a revision and
 * a note none, and why a lost answer is read back rather than sent again, is
 * tasks-ui/DESIGN.md, "A note names no revision" and "A write whose answer was lost".
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

const JSON_TYPE = { "content-type": "application/json" };

// Send one write. Each names its path in its own `fetch`, as a POST, where the relay
// test reads both.
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
  return fetch(`/api/tasks/${path}/notes`, { method: "POST", headers: JSON_TYPE,
    body: JSON.stringify({ text: write.text }) });
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

// Whether a write whose answer was lost landed, from the task and notes read after it,
// or `null` when that read failed:
//   found           the task shows it landed: a move's revision moved and its state is
//                   the target; a break's moved and nobody holds it; a note of the
//                   operator's with its text is newer than any the page held;
//   not-landed      a move or break whose revision did not move: sending it again
//                   cannot apply it twice, since the revision would refuse it;
//   not-found-yet   a note that is not there, which may still be committing: never
//                   "safe to send again";
//   could-not-check the read failed;
//   changed         the task changed, but not as this write would have.
export function checkWrite(write, reread) {
  if (!reread || !reread.task || typeof reread.task !== "object") {
    return "could-not-check";
  }
  const task = reread.task;
  if (write.kind === "note") {
    const notes = reread.notes && Array.isArray(reread.notes.notes) ? reread.notes.notes : [];
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

// The id of the newest note the page holds, or 0: what a note's check counts from.
export function lastNoteId(notes) {
  const list = notes && Array.isArray(notes.notes) ? notes.notes : [];
  return list.reduce((top, n) => (n && Number(n.id) > top ? Number(n.id) : top), 0);
}
