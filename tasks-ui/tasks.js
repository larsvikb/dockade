// SPDX-License-Identifier: Apache-2.0
/* The tasks page: the task registry's work list, read through this UI's relay to the
 * registry's operator listener (`/api/…`, tasks-ui/app.py). It opens on the inbox,
 * what is the operator's turn; every task is a view away, and an open task takes the
 * whole page until the operator goes back to the list. A backlog nobody waits on, so
 * nothing here alerts the way a hold does: the page's title carries a quiet count of
 * what waits on the operator.
 *
 * The registry can stop on its own. The page asks `/running` first, which this
 * container answers itself, so a registry that is not running is one line on the page
 * rather than a failed request in the console; and nothing here calls it again until
 * `/running` says it answers.
 *
 * Every string a task carries was written by an agent, or by the operator for agents
 * to read, so it is set as textContent, and a character that renders as nothing or
 * reorders the text around it is spelled out where it sits (`spellHidden`). Agent
 * markdown is drawn as elements built here from markdown.js's tokens (`markdownBlock`),
 * never parsed as markup.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_tasks_ui_js.py).
 */
import { lineDiff, wordDiff } from "./diff.js";
import { escapeNonAscii, isHidden, NON_ASCII_RE } from "./escape.js";
import { tokenize } from "./markdown.js";
import { pollStatus, renderListStatus } from "./list-status.js";
import {
  TASK_STATES, NOT_RUNNING_STATUS, movesFor, noteMovesFor, isNoteMove,
  onParkedTask, selectAll, tickMark, bulkToSend, keptSelection, redrawsFlagged,
  sendWrite, readAnswer,
  writeOutcome, checkWrite, lastNoteId, lastEntryId,
  planDecisions, awaitsAnswers, approvable, planAnswered, planApproved,
  assumedChoices, blankText, choiceAnswer,
  unanswered, approveAnswers, choicesDiffer, approveOutcome, currentPlan,
  SPEC_CONTROLS, asControl, controlChanges, specDraft, draftChanges, editRequest,
  changedFields, rebaseDraft, parseBlockers,
  SEVERITIES, KINDS, CREATE_STATES, createRequest, createdTask, parseParent, parentId,
  reparentRequest,
} from "./moves.js";
import { followedRepo, markRepos, mountRepos, renderRepos, repoHooks } from "./repos.js";
import { fmtStamp, fmtInstant } from "./time.js";

// What `GET /api/tasks` takes for every state (operator_api.EVERY_STATE), beside the
// states themselves (TASK_STATES, moves.js). A test holds it to the registry's.
export const EVERY_STATE = "all";

// `text` with each hidden character (`isHidden`, escape.js) spelled `\uXXXX` where it
// sits, and how many there were, less the line break and the tab, since this is prose.
// One U+202E in a body draws the rest of its line backwards, and an approved body is
// what every later session reads, so the operator must see what is in it rather than
// what the browser makes of it. An emoji's U+FE0F is spelled too: the price of not
// guessing which selector carries nothing. Spelled, never dropped: dropping would show
// a cleaned copy of what agents read.
// The task registry's export spells the same set (task-registry/export.py).
export function spellHidden(text) {
  const s = typeof text === "string" ? text : "";
  let hidden = 0;
  const shown = s.replace(NON_ASCII_RE, ch => {
    if (ch === "\n" || ch === "\t" || !isHidden(ch)) return ch;
    hidden++;
    return escapeNonAscii(ch);
  });
  return { text: shown, hidden };
}

// How many hidden characters the texts carry together: what a warning counts, not
// what is drawn.
export function hiddenIn(...texts) {
  return texts.reduce((sum, text) => sum + spellHidden(text).hidden, 0);
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
    return { key, label, text };
  });
}

// "2 review rounds", or nothing when the count is missing.
function reviewRounds(value) {
  const rounds = Number(value);
  return Number.isInteger(rounds) ? `${rounds} review round${rounds === 1 ? "" : "s"}` : "";
}

// What a plan says about itself: which one, whose, when, against which revision of the
// task, after how many reviews, when it was approved, and whether it was superseded.
export function planHeading(plan) {
  if (!plan || typeof plan !== "object") return "";
  const parts = [`plan ${plan.id}`, `by ${spellHidden(plan.author).text}`,
                 fmtStamp(plan.at), `at revision ${plan.task_revision}`,
                 reviewRounds(plan.review_rounds)];
  if (plan.approved_at) parts.push(`approved ${fmtStamp(plan.approved_at)}`);
  if (plan.superseded_at) parts.push(`superseded ${fmtStamp(plan.superseded_at)}`);
  return parts.filter(Boolean).join(" · ");
}

// What the task's own plan, or its lack of one, means for the task: a line and whether
// it warns, or null, as for an earlier plan, drawn with no task. Only a proposed task
// is refined by an agent (`submit_plan` refuses any other), so only there is a missing
// plan the ordinary case; the operator can move a task on with no plan, or past a
// superseded one, and then nobody will hand one in. A refined task whose revision is
// past the one its plan was written against was written to since: a spec edit, or a
// move away and back, which changes no spec but moves the revision all the same.
// Approving approves it as it now is, with that plan, or for a plan approved before,
// which only a reopen opens, reopening opens it so; said only there, since every
// approved task's revision passes its plan's.
export function planNotice(plan, task) {
  if (!task || typeof task !== "object") return null;
  const proposed = task.state === "proposed";
  if (!plan) {
    return proposed
      ? { text: "No plan yet: an agent that claims the proposal hands one in.",
          warn: false }
      : { text: "No plan: this task was moved on without one.", warn: true };
  }
  if (!plan.superseded_at) {
    const lag = task.state === "refined" && Number(task.revision) > Number(plan.task_revision);
    return lag
      ? { text: `The task moved on since plan ${plan.id} was written (revision `
                + `${plan.task_revision}, now ${task.revision}): `
                + (wasApproved(plan, task)
                  ? "reopening opens the task as it now is, with this plan."
                  : "approving approves the task as it now is, with this plan."),
          warn: true }
      : null;
  }
  return { text: proposed
             ? "Superseded: no plan is current until an agent refines the task again."
             : "Superseded: no plan is current.",
           warn: true };
}

// Whether `plan` was approved, as `planAnswered` and `planApproved` tell it of the
// current plan, but of the plan given.
function wasApproved(plan, task) {
  return Boolean(plan.approved_at)
    || (Array.isArray(task.answers) && task.answers.some(a => a && a.plan === plan.id));
}

// Every plan but the one shown above, in the order the registry sent them (newest
// first). Ids compared as numbers: the shown one comes back from a data attribute.
export function earlierPlans(plans, shownId) {
  const shown = Number(shownId);
  return (Array.isArray(plans) ? plans : []).filter(p => p && Number(p.id) !== shown);
}

// The plan the shown one replaced: the next lower id among the task's plans. None when
// the shown one is the first, or is not among them, as when a refusal brought a task
// whose plan is newer than the list the page holds.
export function planPredecessor(plans, shownId) {
  const list = (Array.isArray(plans) ? plans : []).filter(p => p && typeof p === "object");
  const shown = Number(shownId);
  if (!list.some(p => Number(p.id) === shown)) return null;
  return list.filter(p => Number(p.id) < shown)
    .reduce((best, p) => (best === null || Number(p.id) > Number(best.id) ? p : best), null);
}

// What changed from `previous` to `plan`, part by part in PLAN_PARTS' order: each
// unchanged, added, removed or changed, with its runs (diff.js), the footprint path by
// path. A part `previous` lacks is added, so a part the registry adds is one more
// section. A change only whitespace makes is said to be. An unchanged part that carries
// a hidden character keeps its text, so approve's count of them is drawn somewhere.
export function planDiff(plan, previous) {
  const before = new Map(planParts(previous).map(p => [p.key, p.text]));
  return planParts(plan).map(({ key, label, text }) => {
    const was = before.get(key) ?? "";
    if (was === text) {
      return { label, kind: "unchanged",
               runs: spellHidden(text).hidden ? [{ op: "same", text }] : [] };
    }
    if (was === "") return { label, kind: "added", runs: [{ op: "add", text }] };
    if (text === "") return { label, kind: "removed", runs: [{ op: "del", text: was }] };
    const { runs, rewritten } = (key === "footprint" ? lineDiff : wordDiff)(was, text);
    return { label, kind: "changed", runs, rewritten,
             // Not \s, which takes U+FEFF and U+2028, characters spellHidden spells.
             whitespaceOnly: was.replace(/[\t\n\r ]+/g, "") === text.replace(/[\t\n\r ]+/g, "") };
  });
}

// What a changed section says of itself beside its runs, or nothing.
export function changeNote(section) {
  if (!section || section.kind === "unchanged") return "";
  if (section.kind === "added") return "added";
  if (section.kind === "removed") return "removed";
  if (section.rewritten) return "rewritten: too different to compare word by word";
  return section.whitespaceOnly ? "changed in whitespace only" : "";
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

// "2 decisions", "no decisions", or nothing when the count is missing. The registry
// counts every decision the plan carries, answered ones too (`operator_moves.inbox`),
// so this says what the plan has, never what waits on the operator.
function decisionCount(value) {
  const n = Number(value);
  if (value === null || value === undefined || !Number.isInteger(n)) return "";
  return n === 0 ? "no decisions" : `${n} decision${n === 1 ? "" : "s"}`;
}

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
              decisionCount(reason.decisions), reviewRounds(reason.review_rounds)]
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

// The largest task id the registry gives (`ID_MAX` in task-registry/tasks.py), as
// digits: it is past 2^53, so an id never goes through Number. A test holds it to the
// registry's.
export const ID_MAX = "9223372036854775807";

// Whether `digits`, with no leading zero, names an id from 1 to ID_MAX: compared as
// strings, by length first.
function inIdRange(digits) {
  return digits !== "0" && (digits.length < ID_MAX.length
    || (digits.length === ID_MAX.length && digits <= ID_MAX));
}

// The URL's hash names the view and the task open on the page, so a reload or a link
// keeps both, and the browser's back goes to what was read before. A task id is
// digits, as the relay takes one, without leading zeros, as a row names it, from 1 to
// ID_MAX. A task parameter that is anything else names none, and `bad` carries it, so
// the page says the link named no task rather than show the list as if it were asked.
export const VIEWS = ["inbox", "all", "repos"];
export function parseHash(hash) {
  const q = new URLSearchParams(String(hash ?? "").replace(/^#/, ""));
  const named = q.get("view");
  const given = q.get("task");
  const digits = given !== null && /^[0-9]+$/.test(given)
    ? given.replace(/^0+(?=[0-9])/, "") : null;
  const task = digits !== null && inIdRange(digits) ? digits : null;
  return { view: VIEWS.includes(named) ? named : "inbox", task,
           bad: given !== null && task === null ? given : null };
}

// What the list says in place of a task the hash named and the page could not open:
// one no id can be, or one the registry has not got. The hash is anyone's link, so
// it is spelled as agent text is.
export function badTaskText(given) {
  return `The link names no task: "${spellHidden(given).text}" is not a task id.`;
}
export function unknownTaskText(id) {
  return `The registry has no task #${id}.`;
}
export function hashFor(view, task) {
  const q = new URLSearchParams();
  if (view !== "inbox" && VIEWS.includes(view)) q.set("view", view);
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

// ── the controls' words ─────────────────────────────────────────────────────
// Every action control on the task page says one thing the one way
// (tasks-ui/DESIGN.md, "One scheme of labels"): `main`, the action and its outcome in
// the operator's words; `second`, where needed, the mechanism and its cost, smaller
// under it; `caution`, a move that takes away what no move gives back.

// What ending a claim, and superseding a plan, cost: said in a second line.
const ENDS_CLAIM = "ends the claim";
function supersedes(plan) {
  return `supersedes plan ${plan.id}; no move restores it`;
}
function joined(...parts) {
  return parts.filter(Boolean).join(" · ");
}

// A move's label, by the state it reaches. A move to proposed supersedes the current
// plan (`tasks.update`), an approved one too, so with one it is a caution; so is every
// move that ends a live claim, which a move to another state does
// (`operator_moves.move`). A move to open says which plan it opens and whether it was
// approved (`planAnswered`, `planApproved`): opening a plan nobody approved must not
// read as approving it. A send back says it sends no answers once `picked`, the choices
// made, differ from what the plan assumes.
export function moveLabel(move, task, picked = null) {
  if (!move || typeof move !== "object" || !task || typeof task !== "object") return null;
  const plan = currentPlan(task);
  let main, second = "", caution = false;
  if (move.state === "proposed") {
    main = task.state !== "refined" ? "move to proposed"
      : picked && choicesDiffer(planDecisions(task.plan), picked)
        ? "send back without answering" : "send back for a new plan";
    if (plan) {
      second = supersedes(plan);
      caution = true;
    }
  } else if (move.state === "open" && task.state === "landing") {
    main = "move to open";
  } else if (move.state === "open" && !plan) {
    main = "open without a plan";
  } else if (move.state === "open" && (planAnswered(task) || planApproved(task))) {
    main = "reopen";
    second = `plan ${plan.id} as approved · revision ${task.revision}`;
  } else if (move.state === "open") {
    main = `open with plan ${plan.id}`;
    second = `no approve of plan ${plan.id} found: move it to refined to approve it`;
  } else {
    main = { refined: "move to refined", landing: "mark ready to land",
             deferred: "defer", closed: "close the task" }[move.state]
      || `move to ${move.state}`;
  }
  if (move.endsClaim && task.claim) {
    second = joined(second, ENDS_CLAIM);
    caution = true;
  }
  return { main, second, caution };
}

export function breakLabel(claim) {
  return claim ? { main: "break the claim", second: ENDS_CLAIM, caution: true } : null;
}

// Above a claimed task's moves: whose claim it is, and which moves end it. The holder
// is an agent's, so spelled.
export function holderText(claim) {
  return claim && typeof claim === "object"
    ? `${spellHidden(claim.holder).text} holds this task: a state move or a break ends `
      + "the claim; an edit or a note leaves it."
    : "";
}

// The one move drawn accent-filled, or null: approve on a refined task the operator can
// approve, reopen on a refined task whose plan was answered, and closing a landing task.
// Proposed, open, deferred and closed have none: leaving a parked or finished task is a
// choice among several, and a deferred plan nobody approved must draw nothing that
// reads as approving it. "approve", or the state the move reaches.
export function primaryMove(task) {
  if (!task || typeof task !== "object") return null;
  if (approvable(task)) return "approve";
  if (task.state === "refined" && planAnswered(task)) return "open";
  if (task.state === "landing") return "closed";
  return null;
}

// Approving a refined task approves its body and plan as the page shows them
// (task-registry/DESIGN.md, "Plans") and answers its decisions, so the button says
// where the answers take the task (`approveOutcome`), and its second line the revision
// and the plan: it is the confirmation. One that sends the task back supersedes its
// plan, said, but stays primary and unbordered, its answers being the point. A warning
// says when the title, body, plan, decisions or an "other" text to be sent carry a
// character spelled out above it.
export function approveLabel(task, outcome = "open") {
  if (!task || typeof task !== "object" || !task.plan) return null;
  return outcome === "proposed"
    ? { main: "approve with these answers, sending it back",
        second: `revision ${task.revision} · ${supersedes(task.plan)}`, caution: false }
    : { main: "approve and open", second: `revision ${task.revision} · plan ${task.plan.id}`,
        caution: false };
}
export function approveHidden(task, picked = null) {
  if (!task || typeof task !== "object") return "";
  const plan = planParts(task.plan).map(p => p.text);
  const others = (Array.isArray(picked) ? picked : []).map(c => choiceAnswer(c).other);
  const hidden = hiddenIn(task.title, task.body, ...plan, ...others)
    + planDecisions(task.plan).reduce((sum, d) => sum + decisionHidden(d), 0);
  return hidden > 0
    ? `What this approves carries ${hidden} invisible or direction-changing `
      + `character${hidden === 1 ? "" : "s"}, spelled out above: read them first.`
    : "";
}
function decisionHidden(d) {
  const options = Array.isArray(d.options) ? d.options : [];
  return hiddenIn(d.question, d.why, ...options.flatMap(o => [o.label, o.meaning]));
}

// While an answer is incomplete no approve is drawn, only this, naming the decisions
// (counted from 1, as drawn) still to answer.
export function incompleteText(missing) {
  const list = (Array.isArray(missing) ? missing : []).map(i => String(i + 1));
  return `No approve until every decision is answered: ${list.length === 1 ? "decision"
    : "decisions"} ${list.join(", ")} not yet. A picked "other" needs its text.`;
}

// Why no move opens a task whose current plan has decisions not answered, from any
// state but refined, where approve is.
export function openBlockedText(task) {
  return awaitsAnswers(task) && task.state !== "refined"
    ? `No move opens this task: plan ${task.plan.id} has decisions not answered. Move `
      + "it back to refined, then approve it with the answers."
    : "";
}

// The line a draw that brought another plan puts above the decisions, whatever the
// choices were, with the "other" texts it reset (`notePlanDrawn`). The operator's own
// words, spelled as any text is.
export function resetText(reset) {
  if (!reset || typeof reset !== "object") return "";
  const others = (Array.isArray(reset.others) ? reset.others : [])
    .map(text => `"${spellHidden(text).text}"`);
  return `Another plan: this is plan ${reset.to}, and plan ${reset.from} was drawn `
    + "before. Read it again; its choices are back on what it assumes."
    + (others.length ? ` Reset with plan ${reset.from}: ${others.join(", ")}` : "");
}

// ── the spec edit ────────────────────────────────────────────────────────────
// What the form says around its controls (tasks-ui/DESIGN.md, "A spec edit sends what
// changed, as it will be stored"). The values themselves are moves.js's.

// What a field will be stored as, under its control: the text spelled, with its count
// of hidden characters, and, when the edit sends the field, how many characters of the
// stored text its control changed or dropped. A field the edit leaves alone is stored
// as it is, whatever its control shows.
export function specPreview(base, draft, field) {
  const control = SPEC_CONTROLS[field];
  const sent = draftChanges(base, draft).includes(field);
  const stored = base && typeof base === "object" ? base[field] : "";
  const shown = spellHidden(sent ? draft[field] : stored);
  const lost = sent ? controlChanges(stored, control) : 0;
  const what = control === "text" ? "dropped" : "turned into line feeds";
  return { label: sent ? "will be stored" : "stored, and left as it is",
           text: shown.text, hidden: shown.hidden,
           lost: lost ? `${lost} line break${lost === 1 ? "" : "s"} of the stored ${field} `
                        + `${what} by this control.` : "" };
}

// The save's label: the revision it edits, as approve names it, and when the task is
// claimed that the claim stays, as an edit leaves it (`operator_moves.edit`).
export function saveLabel(base) {
  if (!base || typeof base !== "object") return null;
  return { main: "save the edit",
           second: joined(`revision ${base.revision}`, base.claim ? "the claim stays" : ""),
           caution: false };
}

// A note's writes: one added, or a reply, stays on the task, which agents read.
export const NOTE_SECOND = "agents read it; it can be hidden, never removed";
// A note move's label: an unhide says what it gives back.
export function noteMoveLabel(move) {
  if (!move || typeof move !== "object") return null;
  return { main: move.verb, second: move.verb === "unhide" ? "agents read it again" : "",
           caution: false };
}

// What an edit of a refined task does to the plan beside it, or nothing for another.
export function specAmendText(task) {
  const plan = currentPlan(task);
  if (!(task && task.state === "refined" && plan)) return "";
  return `This task is refined: an edit amends the spec plan ${plan.id} was written `
    + `against, at revision ${plan.task_revision}. `
    + (wasApproved(plan, task)
      ? `Plan ${plan.id} was approved before: a reopen after the edit opens the edited `
        + "task with that plan."
      : "Approving after it approves the edited task with that plan.");
}

// A stored severity or kind no select offers, which the select shows as none: said, so
// the operator knows it is there, and kept unless that select is changed.
export function unknownSpecText(base) {
  const stored = base && typeof base === "object" ? base : {};
  return ["severity", "kind"]
    .filter(field => typeof stored[field] === "string"
            && asControl(stored[field], SPEC_CONTROLS[field]) === "")
    .map(field => `The stored ${field} "${spellHidden(stored[field]).text}" is not one `
         + "the form offers: it shows as none, and stays unless you change it.")
    .join(" ");
}

// Why the blockers do not parse: the operator's word, spelled.
export function blockersProblem(bad) {
  return `"${spellHidden(String(bad)).text}" is not a task id: blockers are ids, such `
    + "as #7, 12.";
}

// Why a parent does not parse: the operator's word, spelled.
export function parentProblem(bad) {
  return `"${spellHidden(String(bad)).text}" is not a parent: a parent is one task id, `
    + "such as #7, or nothing for none.";
}

// ── a parent, and a new task ─────────────────────────────────────────────────
// What the reparent and the create form say around their controls. The values are
// moves.js's.

// The reparent's label, for the body `reparentRequest` built: the parent it sets or that
// it removes one, and the revision it names, as the save does; a claim stays.
export function reparentLabel(task, send) {
  if (!task || typeof task !== "object" || !send || typeof send !== "object") return null;
  const main = send.parent === null ? "remove the parent"
    : `make #${send.parent} the parent`;
  return { main,
           second: joined(`revision ${task.revision}`, task.claim ? "the claim stays" : ""),
           caution: false };
}

// The create's label names the repo and state, which is the confirmation: a task can be
// closed, never removed. Sent while an earlier create's answer is lost and not found,
// it says that it may make a second task.
export const CREATE_SECOND = "a task can be closed, never removed";
export function createLabel(draft, lost) {
  if (!draft || typeof draft !== "object") return null;
  const where = `in ${spellHidden(String(draft.repo)).text} as ${draft.state}`;
  return lost
    ? { main: "send anyway — may make a second task",
        second: `${where} · ${CREATE_SECOND}`, caution: true }
    : { main: `create the task ${where}`, second: CREATE_SECOND, caution: false };
}

// What a new task's title or body will be stored as: the text spelled, with its count
// of hidden characters.
export function createPreview(text) {
  const shown = spellHidden(text);
  return { label: "will be stored", text: shown.text, hidden: shown.hidden };
}

// What a create came to, in a line, and whether it warns: `writeOutcome`'s word, or for
// an answer that was lost, `checkWrite`'s, or "no-bound" when the repo could not be
// read before it, so nothing was sent. `created` is the task's id, landed or found;
// `edited`, that the form's draft was edited since the send and is kept.
export function createOutcomeText(write, outcome, detail, created, edited = false) {
  const asSent = edited ? " The task was created as sent; your edits since are kept "
    + "in the form." : "";
  const repo = spellHidden(String(write && write.send ? write.send.repo : "")).text;
  const bound = Number(write && write.bound);
  const past = bound > 0 ? ` past task #${bound}` : "";
  switch (outcome) {
    case "landed":
      return { text: `Created task #${created}.${asSent}`, warn: false };
    case "found":
      return { text: `No answer came, but ${repo} has task #${created}${past} with the `
                     + `title and body sent, so it landed.${asSent}`, warn: false };
    case "no-bound":
      return { text: "Could not read the repo; nothing sent.", warn: true };
    case "refused":
      return { text: (`Create the task: refused. ${spellHidden(String(detail ?? "")).text}`)
                 .trim(), warn: true };
    case "not-running":
      return { text: "Create the task: not sent. The task registry is not running.",
               warn: true };
    case "not-found-yet":
      return { text: `No answer came, and no task in ${repo}${past} has the title `
                     + "and body sent yet. It may still land: look again before sending "
                     + "it again, which may make a second task.", warn: true };
    case "could-not-check":
      return { text: `No answer came, and ${repo} could not be read back, or more tasks `
                     + "landed in it since than its newest page holds, so whether it "
                     + "landed is not known. Look again before sending it again, which "
                     + "may make a second task.", warn: true };
    default:
      return { text: "No answer came, so whether it landed is not known. Look again "
                     + "before sending it again, which may make a second task.",
               warn: true };
  }
}

// What a draw past the draft's base says: the revisions, the spec fields that moved,
// those of them the operator's draft keeps, which saving writes over, and, when the
// draft had changes before and has none now, that nothing is left to save.
export function rebaseText(from, to, moved, kept, emptied) {
  const taken = moved.filter(field => !kept.includes(field));
  let text = `The task moved from revision ${from} to ${to} while you edited it.`;
  if (!moved.length) text += " Its spec did not change.";
  if (taken.length) text += ` Your edit takes the new ${taken.join(", ")}.`;
  if (kept.length) {
    text += ` It keeps your ${kept.join(", ")} over the new one${kept.length === 1 ? ""
      : "s"}: saving writes over ${kept.length === 1 ? "that change" : "those changes"}.`;
  }
  if (emptied) text += " Nothing is left to save: the task reads as your edit did.";
  return text;
}

// Beside the moves a draft with changes holds: approve, send back, every state move and
// breaking the claim. A note, the moves on notes and a reparent stay free.
export const HELD_TEXT = "Save or discard the edit first: a move now would act on a "
  + "spec other than the one on screen.";

// What a write came to, in a line, and whether it warns: `writeOutcome`'s word, or for
// an answer that was lost, `checkWrite`'s. `detail` is the refusal's own reason. A bulk
// is all or none, so a refused one says none was handled; a hide or an unhide says what
// it changed for the note as it was (`noteLanded`); an approve, the state it `reached`.
export function outcomeText(write, outcome, detail, reached) {
  const said = outcomeLine(write, outcome, detail);
  const approved = approvedText(write, outcome, reached);
  return { text: said.text + approved.text + leftOutText(write),
           warn: said.warn || approved.warn };
}

// The state a landed approve reached, from its reply or the task read again, warning
// when it is not the one its label said: another write may have moved the task since.
export function approvedText(write, outcome, reached) {
  if (!write || write.kind !== "approve" || !["landed", "found"].includes(outcome)
      || typeof reached !== "string") return { text: "", warn: false };
  const now = reached === "open" ? " The task is open."
    : reached === "proposed"
      ? " The task is back in proposed, its plan superseded, for a plan written from the "
        + "answers."
      : ` The task is ${spellHidden(reached).text}.`;
  if (reached === write.predicted) return { text: now, warn: false };
  return { text: `${now} That is not what this page said: it said the answers `
                 + `${write.predicted === "open" ? "open it" : "send it back"}.`,
           warn: true };
}

// The notes a bulk left out because their task was parked since the list was drawn,
// each with its task's state, after whatever the bulk came to.
export function leftOutText(write) {
  const out = write && Array.isArray(write.leftOut) ? write.leftOut : [];
  return out.map(o => ` Note #${o.id} left out: its task was `
    + `${spellHidden(String(o.state)).text} since the list was drawn.`).join("");
}

// Beside the handle, the ticked notes it would leave out, so a box ticked and not
// counted is explained: in a span of fixed width, so nothing moves when it changes.
export function leftOutLine(leftOut) {
  return (Array.isArray(leftOut) ? leftOut : [])
    .map(o => `#${o.id} left out: its task was ${spellHidden(String(o.state)).text}`)
    .join("; ");
}

function outcomeLine(write, outcome, detail) {
  const what = write && write.kind === "note" ? "The note" : `${write && write.verb}`;
  const name = what.charAt(0).toUpperCase() + what.slice(1);
  const kind = write && write.kind;
  const bulk = kind === "handle-many";
  switch (outcome) {
    case "landed":
      return { text: `${name}: done.${kind === "hide" ? noteLanded(write) : ""}`,
               warn: false };
    case "stale":
      return { text: `${name}: not done. The task changed since this page read it; `
                     + "this is the task as it is now.", warn: true };
    case "refused":
      return { text: (`${name}: refused${bulk ? ", so none of them was handled" : ""}. `
                      + spellHidden(String(detail ?? "")).text).trim(), warn: true };
    case "not-running":
      return { text: `${name}: not sent. The task registry is not running.`, warn: true };
    case "found":
      return { text: bulk
                 ? `${name}: no answer came, but none of them is flagged any more, so it `
                   + "landed."
                 : `${name}: no answer came, but the task shows it landed.`,
               warn: false };
    case "not-landed":
      return { text: bulk
                 ? `${name}: no answer came, and they are still flagged, so it has not `
                   + "landed yet. Sending it again is harmless: it only marks them."
                 : `${name}: no answer came, and the task is unchanged, so it has not `
                   + "landed yet. Sending it again cannot apply it twice.", warn: true };
    case "not-seen":
      return { text: `${name}: no answer came, and the task's timeline does not show it `
                     + "yet. It may still land; sending it again is harmless, since it "
                     + "sets the same flag.", warn: true };
    case "not-found-yet":
      return { text: `${name}: no answer came, and the ${kind === "handle" ? "reply" : "note"}`
                     + " is not on the task yet. It may still land: look at the timeline "
                     + "before sending it again.", warn: true };
    case "could-not-check":
      return { text: `${name}: no answer came, and `
                     + (bulk ? "the flagged notes could not be read again"
                       : isNoteMove(write) ? "the task's timeline could not be checked for it"
                         : "the task could not be read again")
                     + ", so whether it landed is not known. "
                     + (bulk ? "Read them" : "Read the task") + " before trying again.",
               warn: true };
    case "changed":
      return { text: `${name}: no answer came, and the task has changed, but not as `
                     + "this would have changed it. This is the task as it is now.",
               warn: true };
    default:
      return { text: `${name}: no answer came, so whether it landed is not known. Read `
                     + "the task before trying again.", warn: true };
  }
}

// What a hide or an unhide changed, as far as it is true for this note: agents' reads
// always; the flagged list only for a note flagged and not handled; the inbox's count
// only for such a note whose task is not deferred or closed, since that one waits with
// its task out of the inbox either way.
function noteLanded(write) {
  const counted = write.forYou && !onParkedTask({ task_state: write.taskState });
  if (write.hidden) {
    return " Agents no longer read it; it stays on its task, marked hidden."
      + (counted ? " It leaves the flagged list and the inbox's count."
        : write.forYou ? " It leaves the flagged list." : "");
  }
  return " Agents read it again."
    + (counted ? " It is back in the flagged list and the inbox's count."
      : write.forYou ? " It is back in the flagged list." : "");
}

// What marks a note: whom it is for, whether it was handled or hidden, the move it came
// with, and the note it answers. Hidden is said as what it does: the operator still
// reads the note, agents do not.
export function noteMarks(n) {
  if (!n || typeof n !== "object") return [];
  return [`#${n.id}`, n.for_operator ? "for you" : "", n.handled ? "handled" : "",
          n.hidden ? "hidden from agents" : "", n.move || "",
          n.reply_to ? `reply to #${n.reply_to}` : ""].filter(Boolean);
}

// What one entry of the timeline shows besides its time, author and action
// (`operator_moves.history`): the note its write posted, once, with its marks, and what
// else the write recorded, folded. The note's text and id are in the detail too, a
// handle's reply as `reply` and `reply_id`, any other's as `note` and `note_id`, so
// those go. The note a hide or a handle acts on stays in the open, to match it with the
// entry that posted it.
export function timelineEntry(h) {
  if (!h || typeof h !== "object") return null;
  const detail = { ...(h.detail && typeof h.detail === "object" ? h.detail : {}) };
  const note = h.note && typeof h.note === "object" ? h.note : null;
  const handle = h.action === "handle_note";
  if (note) {
    delete detail[handle ? "reply" : "note"];
    delete detail[handle ? "reply_id" : "note_id"];
  }
  let named = "";
  if ((handle || h.action === "hide_note") && detail.note_id != null) {
    named = `#${detail.note_id}`;
    delete detail.note_id;
  }
  return { note, marks: noteMarks(note), named, folded: Object.entries(detail) };
}

// What a poll's read of the open task says against the one drawn: changed, at a newer
// revision; new notes, when only a note came, since a note moves no revision
// (tasks-ui/DESIGN.md, "A task that changed while it is read"); else null, as for
// another task, or a read older than the one drawn.
export function taskUpdate(shown, polled) {
  if (!shown || !polled || typeof shown !== "object" || typeof polled !== "object"
      || String(shown.id) !== String(polled.id)) return null;
  const was = Number(shown.revision), now = Number(polled.revision);
  if (now > was) {
    return { kind: "changed",
             text: `This task changed since it was drawn: it is at revision ${now}, `
                   + `this shows ${was}.` };
  }
  if (now === was && lastNoteId(polled) > lastNoteId(shown)) {
    return { kind: "notes", text: "New notes on this task since it was drawn." };
  }
  return null;
}

// The flagged list's count line: how many flagged notes are not handled now, and how
// the newest read differs from the rows drawn, which only a draw changes
// (tasks-ui/DESIGN.md, "The flagged notes are listed above the inbox"). `drawn` is the
// notes drawn, or their ids, `read` the newest read of the list, or null when it
// failed. A full page holds only the newest, so a drawn note older than all of it is
// not counted gone. A drawn note whose task was parked or reopened since is counted
// too, since its row's mark is the draw's. `show` offers to draw the read.
export function flaggedLine(drawn, read) {
  if (!read || typeof read !== "object" || !Array.isArray(read.notes)) {
    return { text: "Could not refresh the notes flagged for you: these are as last read.",
             warn: true, show: false };
  }
  const drawnNotes = (Array.isArray(drawn) ? drawn : [])
    .map(n => (n && typeof n === "object" ? n : { id: n }));
  const shown = new Set(drawnNotes.map(n => n.id));
  const readNotes = new Map(read.notes.filter(n => n && typeof n === "object")
    .map(n => [n.id, n]));
  const moved = drawnNotes.filter(n => "task_state" in n && readNotes.has(n.id)
    && onParkedTask(n) !== onParkedTask(readNotes.get(n.id))).length;
  const now = read.notes.map(n => (n && typeof n === "object" ? n.id : n));
  const listed = new Set(now);
  const oldest = now.length >= PAGE_MAX ? Math.min(...now) : null;
  const newer = now.filter(id => !shown.has(id)).length;
  const gone = [...shown]
    .filter(id => !listed.has(id) && (oldest === null || id > oldest)).length;
  const total = Number(read.total);
  const parts = [total > 0 ? `${total} not handled` : "none left"];
  // Those wait out of the inbox with their task, so this total is more than its count.
  const parked = read.notes.filter(onParkedTask).length;
  if (parked) {
    parts.push(`${parked} on a deferred or closed task`
               + (read.notes.length < total ? ` among the newest ${read.notes.length}` : ""));
  }
  if (newer) parts.push(`${newer} not shown`);
  if (gone) parts.push(`${gone} gone since`);
  if (moved) parts.push(`${moved} whose task was deferred, closed or reopened since`);
  return { text: parts.join(" · "), warn: false, show: newer + gone + moved > 0 };
}

export const NO_REPOS_TEXT =
  "The registry has no repos yet. Add one under Repos; until then no agent can file a "
  + "task.";
export const NOT_RUNNING_TEXT =
  "The task registry is not running, or this page cannot reach it. make tasks-up "
  + "starts it; this page picks it up again when it answers.";

// ── the DOM ─────────────────────────────────────────────────────────────────

// How long a poll waits between asks while the registry answers. Slower than the
// policy views: nothing here is waiting on the operator's minute.
export const TASKS_POLL_MS = 15000;

let tasksDown, tasksUp, tasksRepo, tasksState, tasksSearch, tasksBody, tasksEmpty,
  tasksOlder, tasksNewest, taskCountEl, taskDetail, listView, taskView, taskBack,
  listNotice, inboxEl, inboxEmpty,
  inboxCountEl, viewInbox, viewAll, viewRepos, inboxView, allView, reposSection,
  flaggedEl, flaggedLineEl,
  flaggedBody, flaggedEmpty, flaggedAll, flaggedHandle, flaggedLeftOut, flaggedOutcomeEl,
  createOpen, createForm, createOutcomeEl;

let running = null;        // null: not asked yet
let probing = null;        // the ask in flight, if one is
let repos = [];
let reposAsks = 0;         // reads of the repo list asked
let reposDrawn = 0;        // the last of them drawn
let following = null;      // a rename the page made, while its write's read is out
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
let lastRead = null;       // the id last drawn, whose row stays marked back on the list
let listScroll = 0;        // the list's scroll offset when a task was opened from it
let appliedIndex = 0;      // the index stamped on the history entry the page last followed
let shownTask = null;      // the task drawn on the page
let shownHeld = null;      // and what the page holds with it: its timeline and plans
let draws = 0;             // how many times a task was drawn: what a poll's answer checks
let noticeSlot = null;     // the line atop the detail that says the task changed
let writing = null;        // the write in flight: one at a time
let outcome = null;        // what the last write came to, while its task is open
let noteDraft = { task: null, text: "" };   // kept until it lands
let replyDrafts = new Map();  // a reply's text by its note's id: its box is open while kept
// The spec edit open on a task: its id, the read it is a draft of (`base`), the fields
// as its controls hold them, and what its last rebase said. Kept across the task's
// draws, each past its base rebasing it (`noteSpecDrawn`); dropped only when it lands,
// is discarded, or the task is left, for the list or another task. Leaving a draft
// with changes asks first (`mayLeave`): the confirm guards unsent text, not a write.
let specEdit = null;
// The choices made on a plan to approve, by task and plan (`choicesFor`), so a redraw
// keeps them, a stale write's too; dropped when its approve lands or another plan is
// drawn for the task (`notePlanDrawn`), which a line says until the first choice in
// the new plan or a landed approve.
let choices = new Map();
let drawnPlans = new Map();   // by task: the plan it was last drawn with
let planResets = new Map();   // by task: the line saying a draw brought another plan
// The flagged list. Rows are drawn only from a read the operator asked for, or a write's
// (`readFlagged`); every other read rewrites the count line.
let flaggedDrawn = null;   // the notes drawn as rows; null: no draw since it last answered
let flaggedRead = null;    // the newest read, or null when it failed
let flaggedAsks = 0;       // reads asked
let flaggedUsed = 0;       // the last of them whose answer was used
let flaggedWanted = 0;     // the read a draw was asked with, until an answer makes it
let flaggedSelected = new Map();   // ticked ids, each to whether its task was parked then
let flaggedKnown = [];     // the last good read's notes, which a failed read keeps
let flaggedOutcome = null; // what the last bulk came to, until the next or another view
// A new task. Its draft is the form's controls, kept across views and draws while the
// form is open; only discard, or a create it still equals landing or found, drops it
// (`finishCreate`). A create whose answer was lost and not found is kept as sent, with
// its bound, for "look again" to check: never the draft, which may be edited since and
// would miss the task that landed. A reload loses both, as it loses a note's draft.
let createDraft = null;
let lostCreate = null;
let createOutcome = null;  // what the last create came to, under the form
// A reparent's text by task, kept until one lands or is found: the input shows the
// task's parent until it is typed in.
let parentDrafts = new Map();

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
  if (!up) flaggedDrawn = null;
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

// The one read of the repo list, which draws both the Repos view and All tasks' select,
// and returns what it read, or null when it failed. No poll reads it while the registry
// answers, so nothing in the view moves under the pointer: on the page's start and when a
// poll finds the registry back (`probe`), on entering Repos, and after a repo write or a
// refused create. `renamed`, a rename the page made (`{ from, to }`), takes the select,
// and a new task's draft, with it once the list shows it landed; a repo gone otherwise
// starts the list again (`followedRepo`).
async function refreshRepos(renamed = null) {
  if (!registryIsRunning()) return null;
  const asked = ++reposAsks;
  // Followed by whichever read draws first from now: one asked on entering Repos while
  // the write's own read is out may answer, and draw, before it.
  if (renamed) following = renamed;
  let list = null;
  try {
    list = await registryJson(await fetch("/api/repos"));
  } catch (e) {
    list = null;
  }
  // A read answered after a newer one was drawn draws nothing, since its list is older,
  // but still returns it: a write is settled from its own read.
  if (list !== null && asked > reposDrawn) {
    reposDrawn = asked;
    drawRepos(list, following);
  }
  if (renamed) following = null;
  return list;
}

// The repo list as read, in the Repos view and All tasks' select, following `renamed`.
function drawRepos(list, renamed) {
  repos = list;
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
  const kept = followedRepo(chosen, renamed, repos);
  if (kept !== null) tasksRepo.value = kept;
  else if (chosen) resetList();
  renderRepos(repos);
  // A repo retired since is offered no more.
  if (createDraft !== null) {
    const repo = followedRepo(createDraft.repo, renamed, repos);
    if (repo !== null) createDraft = { ...createDraft, repo };
    renderCreate();
  }
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
  await readFlagged(false);
}

// The newest page of the notes flagged for the operator and not handled. Its rows are
// drawn only when `draw` asks, or when none were drawn since the registry last
// answered; any other read rewrites the count line alone, so a poll moves no row. An
// answer a later one overtook is dropped, so a slow poll cannot put back an older
// count; a draw asked for is made by the first answer used to a read asked with it or
// after, never by one asked before it, which may predate the write that asked.
async function readFlagged(draw) {
  if (!registryIsRunning()) return;
  const asked = ++flaggedAsks;
  if (draw) flaggedWanted = asked;
  let read = null;
  try {
    read = await registryJson(await fetch("/api/notes/flagged"));
  } catch (e) {
    read = null;
  }
  if (!registryIsRunning() || asked < flaggedUsed) return;
  flaggedUsed = asked;
  flaggedRead = read && Array.isArray(read.notes) ? read : null;
  if (flaggedRead) flaggedKnown = flaggedRead.notes;
  const wanted = flaggedWanted !== 0 && asked >= flaggedWanted;
  if (wanted) flaggedWanted = 0;
  const drawNow = wanted || flaggedDrawn === null;
  if (flaggedRead && drawNow) {
    flaggedSelected = keptSelection(flaggedSelected, flaggedRead.notes);
    flaggedDrawn = flaggedRead.notes;
  }
  if (drawNow) renderFlagged();
  else renderFlaggedLine();
}

// The last good read's notes, which can know a task parked since the rows were drawn.
// A failed read leaves them, so a park once learned is not forgotten.
function newestFlagged() {
  return flaggedKnown;
}

// What a lost bulk is checked against: the flagged list from just past the highest note
// it named, so notes flagged since cannot push the named ones off the page.
async function readFlaggedPast(notes) {
  const q = new URLSearchParams({ before: String(Math.max(...notes) + 1) });
  return registryJson(await fetch(`/api/notes/flagged?${q}`));
}

// The flagged block's rows, as last drawn, with its count line and the last bulk's
// outcome. Only `readFlagged` calls this, on a draw.
function renderFlagged() {
  flaggedBody.replaceChildren();
  for (const n of flaggedDrawn || []) flaggedBody.appendChild(flaggedRow(n));
  flaggedEmpty.textContent = flaggedDrawn && !flaggedDrawn.length
    ? "No note is flagged for you." : "";
  flaggedOutcomeEl.className = flaggedOutcome && flaggedOutcome.warn
    ? "flagged-outcome empty warn" : "flagged-outcome empty";
  flaggedOutcomeEl.textContent = flaggedOutcome ? flaggedOutcome.text : "";
  renderFlaggedLine();
  markOpen();
}

// The count line, whose space is kept whatever it says: "show them" first, so a count
// that changes moves no button.
function renderFlaggedLine() {
  flaggedLineEl.replaceChildren();
  if (flaggedDrawn === null && flaggedRead === null && flaggedUsed === 0) return;
  const line = flaggedLine(flaggedDrawn || [], flaggedRead);
  if (line.show) {
    const show = document.createElement("button");
    show.type = "button";
    show.className = "group-toggle";
    show.dataset.flagged = "show";
    show.textContent = "show them";
    flaggedLineEl.appendChild(show);
  }
  const text = document.createElement("span");
  text.textContent = line.text;
  flaggedLineEl.appendChild(text);
  flaggedLineEl.className = line.warn ? "flagged-line warn" : "flagged-line";
  // A read can park a ticked note's task: the controls and the handle's count follow it,
  // and no row moves.
  markFlagged();
}

// One flagged note: its box to select it, its task to open, who wrote it and when, and
// what it says, spelled.
function flaggedRow(n) {
  const tr = document.createElement("tr");
  const pick = document.createElement("td");
  pick.className = "pick";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.dataset.flaggedNote = String(n.id);
  box.checked = flaggedSelected.has(n.id);
  box.setAttribute("aria-label", `select note #${n.id}`);
  pick.appendChild(box);
  tr.append(pick, openCell(n.task));
  authorCell(tr, n.author);
  const at = cell(tr, fmtStamp(n.at), "ts");
  at.title = fmtInstant(n.at);
  const text = document.createElement("td");
  if (onParkedTask(n)) {
    // Waits with its task, out of the inbox: said in words, and left out of "select
    // all shown".
    const parked = document.createElement("div");
    parked.className = "why";
    parked.textContent = `on a ${spellHidden(String(n.task_state)).text} task: it waits `
      + "with it, out of the inbox, until it is reopened";
    text.appendChild(parked);
    tr.className = "parked";
  }
  text.appendChild(proseBlock(n.text));
  tr.appendChild(text);
  return tr;
}

// The flagged block's controls, off while any write is out, whatever task is open; the
// handle names how many it would send, by the rule it is sent by.
function markFlagged() {
  const { notes: named, leftOut } = bulkToSend(flaggedSelected, flaggedDrawn,
                                               newestFlagged());
  for (const el of flaggedEl.querySelectorAll("input[data-flagged-note]")) {
    el.disabled = writing !== null;
  }
  flaggedAll.disabled = writing !== null || !selectAll(flaggedDrawn, newestFlagged()).length;
  flaggedHandle.disabled = writing !== null || !named.length;
  flaggedHandle.textContent = `handle selected (${named.length})`;
  flaggedLeftOut.textContent = leftOutLine(leftOut);
  flaggedLeftOut.title = flaggedLeftOut.textContent;
}

// The bulk handle, naming exactly the notes selected among those drawn: never a
// filter, so a note flagged since is not swept up unseen, and never one whose task was
// parked since it was ticked, which its outcome names. It is on no task: the flagged
// block is part of the list, never on screen with a task open.
function bulkWrite() {
  const { notes, leftOut } = bulkToSend(flaggedSelected, flaggedDrawn, newestFlagged());
  if (!notes.length) return null;
  return { kind: "handle-many", notes, leftOut, task: null,
           verb: `handle ${notes.length} note${notes.length === 1 ? "" : "s"}` };
}

// ── a new task ──────────────────────────────────────────────────────────────
// The second write outside the detail, from All tasks: a form above the list, which no
// task's detail is on screen with. Its outcome line is the form's own, since `runWrite`
// draws an outcome only for the task open.

// The repos a task can be created in: the active ones (`reads.repos` marks the rest).
function activeRepos() {
  return repos.filter(r => r && !r.retired).map(r => r.name);
}

// Where a draft starts: on the list's repo when it is active, proposed, nothing else.
function newCreateDraft() {
  const active = activeRepos();
  return { repo: active.includes(tasksRepo.value) ? tasksRepo.value : (active[0] ?? ""),
           state: "proposed", title: "", body: "", severity: "", kind: "", parent: "",
           blockers: "" };
}

// Each field's control, by its draft's key: a select is the list of its options' values.
function createControls() {
  return { repo: activeRepos(), state: CREATE_STATES, title: "text", body: "textarea",
           severity: ["", ...SEVERITIES], kind: ["", ...KINDS], parent: "text",
           blockers: "text" };
}

// The form, drawn from the draft, its values set as properties; or nothing while no
// draft is open. Under the title and body what will be stored, spelled, and under the
// parent and blockers why they do not parse (`markCreate` fills both as it is typed).
function renderCreate() {
  createOpen.hidden = createDraft !== null;
  createForm.replaceChildren();
  createForm.hidden = createDraft === null;
  if (createDraft !== null) {
    const controls = createControls();
    const picks = document.createElement("div");
    picks.className = "moves";
    for (const field of ["repo", "state"]) {
      picks.appendChild(line(field, createControl(field, controls[field])));
    }
    createForm.appendChild(picks);
    for (const field of ["title", "body"]) {
      createForm.appendChild(line(field, createControl(field, controls[field])));
      const preview = document.createElement("div");
      preview.className = field === "body" ? "spec-preview task-body" : "spec-preview";
      preview.dataset.createPreview = field;
      createForm.appendChild(preview);
    }
    const kinds = document.createElement("div");
    kinds.className = "moves";
    for (const field of ["severity", "kind"]) {
      kinds.appendChild(line(field, createControl(field, controls[field])));
    }
    createForm.appendChild(kinds);
    for (const field of ["parent", "blockers"]) {
      createForm.appendChild(line(field === "blockers" ? "blocked by" : field,
                                  createControl(field, controls[field])));
      const problem = document.createElement("div");
      problem.dataset.createProblem = field;
      createForm.appendChild(problem);
    }
    const row = document.createElement("div");
    row.className = "moves";
    const send = writeButton(createLabel(createDraft, lostCreate), "create", "");
    const again = document.createElement("button");
    again.type = "button";
    again.dataset.create = "look-again";
    again.textContent = "look again";
    again.hidden = lostCreate === null;
    const discard = document.createElement("button");
    discard.type = "button";
    discard.dataset.create = "discard";
    discard.textContent = "discard";
    row.append(send, again, discard);
    createForm.appendChild(row);
  }
  createOutcomeEl.className = createOutcome && createOutcome.warn
    ? "create-outcome empty warn" : "create-outcome empty";
  createOutcomeEl.textContent = createOutcome ? createOutcome.text : "";
  markCreate();
}

// One field's control, holding the draft's value.
function createControl(field, control) {
  let el;
  if (Array.isArray(control)) {
    el = document.createElement("select");
    for (const value of control) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = value || "none";
      el.appendChild(opt);
    }
  } else if (control === "textarea") {
    el = document.createElement("textarea");
    el.rows = 8;
  } else {
    el = document.createElement("input");
    el.type = "text";
    if (field === "parent") el.placeholder = "#7, or nothing for none";
    if (field === "blockers") el.placeholder = "#7, 12";
  }
  el.dataset.createField = field;
  el.setAttribute("aria-label", `the new task's ${field}`);
  el.value = createDraft[field];
  // A value its select no longer offers, a repo retired since, is none, as shown.
  if (Array.isArray(control)) createDraft[field] = el.value;
  return el;
}

// What follows the draft, redrawn in place as the operator types: what title and body
// will be stored, why the parent or blockers do not parse, and the create, its label
// naming the repo and state, off while it has nothing to send. Every control is off
// while any write is out.
function markCreate() {
  if (!createForm || createDraft === null) {
    if (createOpen) createOpen.disabled = writing !== null;
    return;
  }
  for (const el of createForm.querySelectorAll("[data-create-field], button")) {
    el.disabled = writing !== null;
  }
  for (const el of createForm.querySelectorAll("[data-create-preview]")) {
    const preview = createPreview(createDraft[el.dataset.createPreview]);
    const text = document.createElement("div");
    text.className = "prose";
    text.textContent = preview.text;
    el.replaceChildren(line(preview.label, text));
    if (preview.hidden) {
      el.appendChild(noticeLine({ text: hiddenNote(preview.hidden), warn: true }));
    }
  }
  const bad = { parent: parseParent(createDraft.parent).bad,
                blockers: parseBlockers(createDraft.blockers).bad };
  for (const el of createForm.querySelectorAll("[data-create-problem]")) {
    const field = el.dataset.createProblem;
    el.replaceChildren();
    if (bad[field] !== null) {
      el.appendChild(noticeLine({ text: field === "parent" ? parentProblem(bad[field])
        : blockersProblem(bad[field]), warn: true }));
    }
  }
  const send = createForm.querySelector('button[data-write="create"]');
  if (send) {
    setLabel(send, createLabel(createDraft, lostCreate));
    if (!createRequest(createDraft)) send.disabled = true;
  }
}

// The create as the draft makes it; its bound is read by `runWrite`, inside the gate.
function createWrite() {
  const send = createRequest(createDraft);
  return send && { kind: "create", task: null, send, bound: null,
                   verb: "create the task" };
}

// The newest id in a repo, every state, or 0 for an empty one: what a lost create is
// looked for past. Ids are never reused and a task never changes repo.
async function readBound(repo) {
  const q = new URLSearchParams({ repo, state: EVERY_STATE });
  const page = await registryJson(await fetch(`/api/tasks?${q}`));
  return (Array.isArray(page.tasks) ? page.tasks : [])
    .reduce((top, t) => (t && Number(t.id) > top ? Number(t.id) : top), 0);
}

// What a lost create is checked against (`checkWrite`): its repo's newest page, every
// state, and in full each task on it past the bound with the title sent, since a list
// row carries no body. A failed read is null in its place, never thrown: a candidate
// that could not be read may be the one, so none read stands for all of them. Exported
// for its node test; it draws nothing unless the registry answers 502.
export async function readCreated(write) {
  let list;
  try {
    const q = new URLSearchParams({ repo: write.send.repo, state: EVERY_STATE });
    list = await registryJson(await fetch(`/api/tasks?${q}`));
  } catch (e) {
    return { list: null, candidates: null };
  }
  const named = (Array.isArray(list.tasks) ? list.tasks : [])
    .filter(row => row && Number(row.id) > Number(write.bound)
            && row.title === write.send.title);
  const candidates = [];
  try {
    for (const row of named) {
      const path = encodeURIComponent(row.id);
      candidates.push(await registryJson(await fetch(`/api/tasks/${path}`)));
    }
  } catch (e) {
    return { list, candidates: null };
  }
  return { list, candidates };
}

// What a create came to, kept for the form: landed or found, the kept lost write goes,
// and the draft too while it still makes what was sent; edited since, it stays and the
// line says the task was created as sent. Lost and not found, the write is kept, as
// sent, for "look again", replacing any earlier one: a refused or unsent "send anyway"
// keeps the one before, which may still have landed. A refusal reads the repos again,
// since a repo retired since is one. Returns the task to open, or null.
function finishCreate(write, result, detail, created) {
  const landed = (result === "landed" || result === "found") && created !== null;
  const draft = createDraft === null ? null : createRequest(createDraft);
  const asSent = JSON.stringify(draft) === JSON.stringify(write.send);
  if (landed) {
    lostCreate = null;
    if (asSent) createDraft = null;
  } else if (result === "not-found-yet" || result === "could-not-check") {
    lostCreate = write;
  }
  createOutcome = createOutcomeText(write, result, detail, created,
                                    landed && createDraft !== null);
  if (result === "refused") refreshRepos();
  renderCreate();
  return landed ? created : null;
}

// "look again": the kept lost create checked again, as sent, never the draft. It sends
// nothing, and holds the gate while it reads, so "send anyway" is off meanwhile.
async function lookAgain() {
  if (writing || lostCreate === null || !registryIsRunning()) return;
  const write = lostCreate;
  writing = write;
  markWriting();
  const reread = await readCreated(write);
  const result = checkWrite(write, reread);
  writing = null;
  const opens = finishCreate(write, result, null, createdTask(write, reread));
  markWriting();
  if (opens !== null) goTo(view, String(opens));
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

// The cell that opens a task, by naming it in the hash: the keyboard's way in, and what
// a click elsewhere on its row opens (`rowOpens`).
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

// The row of the task open, or back on the list the one last read, in every list
// marked as the one. The list's rows only: a task named in the detail has no row.
function markOpen() {
  const marked = openTask ?? lastRead;
  for (const btn of listView.querySelectorAll("tr button[data-task]")) {
    const row = btn.closest("tr");
    if (btn.dataset.task === marked) row.setAttribute("aria-current", "true");
    else row.removeAttribute("aria-current");
  }
}

// The task a click on a list row opens: its row's, unless the click was on a control
// of its own, in the flagged row's pick cell, or ended a drag that selected text.
function rowOpens(ev) {
  const row = ev.target.closest("tr");
  if (!row || ev.target.closest("button, input, select, textarea, a, label, td.pick")) {
    return null;
  }
  const selected = window.getSelection();
  if (selected && !selected.isCollapsed) return null;
  const open = row.querySelector("button[data-task]");
  return open ? open.dataset.task : null;
}

// An author, cut to the cell as `td.actor` cuts it, whole on hover.
function authorCell(tr, author) {
  const td = cell(tr, spellHidden(author).text, "actor");
  td.title = td.textContent;
  return td;
}

// Agent prose: pre-wrapped, hidden characters spelled out and noted.
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

// The tokens markdownBlock drops, all ASCII: a heading's opening hashes and the space or
// tab after them, a bullet and the one after it, the emphasis and backtick delimiters,
// and a fence's backticks or tildes. Every other token is drawn and spelled.
const DROPPED = new Set(["heading-marker", "bullet", "em-open", "em-close", "strong-open",
                         "strong-close", "code-open", "code-close", "fence"]);
// The element a block is drawn as, and as `inline`, as an option's label is drawn: a
// kind not here, or past "paragraph" inline, is drawn as typed.
const BLOCK_TAGS = { paragraph: "p", code: "pre", blank: "div", source: "div" };
const INLINE_TAGS = { paragraph: "span", blank: "span" };

// Agent markdown (markdown.js): the subset drawn as elements built here, never as
// markup, each run spelled; beside it the text as proseBlock draws it, and the detail's
// class shows one (`toggleSource`). The note counts the hidden characters of the text,
// which both forms spell where they sit: the tokens dropped are ASCII.
function markdownBlock(text, { inline = false } = {}) {
  const wrap = proseBlock(text);
  wrap.classList.add("md-field");
  const source = wrap.firstChild;
  source.classList.add("md-source");
  const rendered = document.createElement("div");
  rendered.className = "md";
  let list = null;
  for (const block of tokenize(typeof text === "string" ? text : "")) {
    if (block.kind === "item" && !inline) {
      const tag = block.ordered ? "ol" : "ul";
      if (!list || list.localName !== tag) {
        list = document.createElement(tag);
        rendered.appendChild(list);
      }
      list.appendChild(markdownRuns(document.createElement("li"), block.tokens));
      continue;
    }
    list = null;
    const tags = inline ? INLINE_TAGS : BLOCK_TAGS;
    const tag = block.kind === "heading" && !inline ? `h${block.level}` : tags[block.kind];
    if (tag) {
      rendered.appendChild(markdownRuns(document.createElement(tag), block.tokens));
    } else {
      const typed = document.createElement(inline ? "span" : "div");
      typed.textContent = spellHidden(block.tokens.map(t => t.text).join("")).text;
      rendered.appendChild(typed);
    }
  }
  wrap.insertBefore(rendered, source);
  return wrap;
}

// A block's tokens drawn into `el`: each kept run spelled and set as text, emphasis and
// code spans as elements nested as the tokens open and close them, a bullet as a mark
// the stylesheet draws, and a fence's info string dimmed.
const OPENS = { "em-open": "em", "strong-open": "strong", "code-open": "code" };
const CLOSES = new Set(["em-close", "strong-close", "code-close"]);
function markdownRuns(el, tokens) {
  const open = [el];
  for (const token of tokens) {
    const top = open[open.length - 1];
    if (OPENS[token.kind]) {
      open.push(top.appendChild(document.createElement(OPENS[token.kind])));
    } else if (CLOSES.has(token.kind)) {
      open.pop();
    } else if (token.kind === "bullet") {
      const mark = document.createElement("span");
      mark.className = "md-bullet";
      top.appendChild(mark);
    } else if (token.kind === "info") {
      const info = document.createElement("span");
      info.className = "md-info";
      info.textContent = spellHidden(token.text).text;
      top.appendChild(info);
    } else if (!DROPPED.has(token.kind)) {
      top.appendChild(document.createTextNode(spellHidden(token.text).text));
    }
  }
  return el;
}

// The detail's fields as drawn, or as their source: one switch for them all, kept until
// the page reloads. It hides one form of each by a class on the detail, which outlives
// every draw, so it redraws nothing: typed text, the plan's view and loaded pages stay.
let showSource = false;
function sourceToggle() {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "group-toggle source-toggle";
  btn.dataset.sourceToggle = "1";
  btn.textContent = showSource ? "show rendered" : "show source";
  return btn;
}
function toggleSource(btn) {
  showSource = !showSource;
  taskDetail.classList.toggle("source-shown", showSource);
  btn.textContent = showSource ? "show rendered" : "show source";
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
// its task (`planNotice`). Among the earlier plans `task` is null: a newer plan is the
// task's, and the heading's "superseded" is enough. With the plan it replaced,
// `previous`, it is drawn as what changed from it, the whole a toggle away.
function planBlock(plan, task, previous = null) {
  const block = document.createElement("div");
  block.className = "plan";
  const head = document.createElement("div");
  head.className = "qual";
  head.textContent = planHeading(plan);
  block.appendChild(head);
  const notice = planNotice(plan, task);
  if (notice) block.appendChild(noticeLine(notice));
  const whole = document.createElement("div");
  whole.className = "plan-whole";
  // The footprint is paths, one a line, never markdown.
  for (const part of planParts(plan)) {
    whole.appendChild(line(part.label, part.key === "footprint" ? proseBlock(part.text)
      : markdownBlock(part.text)));
  }
  // An earlier plan's decisions, as text: the task's own are drawn above its plan.
  if (task === null) {
    planDecisions(plan).forEach((d, i) => whole.appendChild(decisionGroup(d, i, null)));
  }
  if (!previous) {
    block.appendChild(whole);
    return block;
  }
  const changes = document.createElement("div");
  changes.className = "plan-changes";
  for (const section of planDiff(plan, previous)) changes.appendChild(diffLine(section));
  whole.hidden = true;
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "group-toggle";
  toggle.dataset.planView = "whole";
  toggle.textContent = "show the whole plan";
  const about = document.createElement("div");
  about.append(`What changed from plan ${previous.id} `, toggle);
  block.append(about, changes, whole);
  return block;
}

// Above the plan, since they matter more to the operator than it: a line when this
// draw brought another plan than the last, the decisions of the plan drawn, with
// choices while it can be approved and as text otherwise, then every answer the task
// has had, newest plan first.
function decisionsBlock(task) {
  const block = document.createElement("div");
  block.className = "decisions";
  const reset = planResets.get(String(task.id));
  if (reset) {
    const said = noticeLine({ text: resetText(reset), warn: true });
    said.dataset.planReset = "1";
    block.appendChild(said);
  }
  const decisions = planDecisions(task.plan);
  if (decisions.length) {
    const head = document.createElement("h3");
    head.textContent = `Decisions of plan ${task.plan.id} · ${decisions.length}`;
    block.appendChild(head);
    const picked = approvable(task) ? choicesFor(task) : null;
    decisions.forEach((d, i) => block.appendChild(decisionGroup(d, i, picked && picked[i])));
  }
  const answers = Array.isArray(task.answers) ? task.answers : [];
  if (answers.length) {
    const head = document.createElement("h3");
    head.textContent = `Answers · ${answers.length}`;
    block.appendChild(head);
    for (const answer of answers) block.appendChild(answerBlock(answer));
  }
  return block;
}

// One decision: its question, why its refiner could not settle it, and each option
// whole, the assumed ones said in words. With a `choice`, each option is an input that
// carries its positions, never its text, and "other" a box for the operator's words.
function decisionGroup(decision, i, choice) {
  const group = document.createElement("fieldset");
  group.className = "decision";
  const legend = document.createElement("legend");
  legend.textContent = `decision ${i + 1}: `
    + (decision.multi_select ? "any of these" : "one of these")
    + (choice ? ", or other" : "");
  group.append(legend, markdownBlock(decision.question),
               line("why", markdownBlock(decision.why)));
  const multi = Boolean(decision.multi_select);
  (Array.isArray(decision.options) ? decision.options : []).forEach((option, j) => {
    const row = document.createElement("div");
    row.className = "option";
    if (choice) {
      const input = choiceInput(i, String(j), multi);
      input.checked = choice.options.includes(j);
      row.appendChild(input);
    }
    const text = document.createElement("div");
    const label = markdownBlock(option.label, { inline: true });
    label.classList.add("option-label");
    text.appendChild(label);
    if (option.assumed) {
      const mark = document.createElement("div");
      mark.className = "qual";
      mark.textContent = "the plan as written assumes this";
      text.appendChild(mark);
    }
    text.appendChild(markdownBlock(option.meaning));
    row.appendChild(text);
    group.appendChild(row);
  });
  if (choice) group.appendChild(otherRow(i, choice, multi));
  return group;
}

function choiceInput(i, option, multi) {
  const input = document.createElement("input");
  input.type = multi ? "checkbox" : "radio";
  input.name = `decision-${i}`;
  input.dataset.decision = String(i);
  input.dataset.option = option;
  input.setAttribute("aria-label", option === "other" ? `decision ${i + 1}: other`
    : `decision ${i + 1}: option ${Number(option) + 1}`);
  return input;
}

// "Other": its input, and a box for the text, open once it is picked or holds text
// (`markOther`).
function otherRow(i, choice, multi) {
  const row = document.createElement("div");
  row.className = "option";
  const input = choiceInput(i, "other", multi);
  input.checked = choice.other;
  const box = document.createElement("div");
  box.className = "other";
  box.dataset.otherBox = String(i);
  const said = document.createElement("div");
  said.textContent = "other: in your own words, which sends the task back";
  const text = document.createElement("textarea");
  text.rows = 2;
  text.dataset.other = String(i);
  text.setAttribute("aria-label", `decision ${i + 1}: other, in your own words`);
  text.value = choice.text;
  text.hidden = !choice.other && !choice.text;
  const note = document.createElement("div");
  note.dataset.otherNote = String(i);
  box.append(said, text, note);
  row.append(input, box);
  return row;
}

// An "other" box as its choice is: open once picked, and kept open; under it, text
// left there while another option is picked marked as not sent, and the text spelled
// when it carries a hidden character.
function markOther(box, choice) {
  const text = box.querySelector("textarea[data-other]");
  text.hidden = text.hidden && !choice.other && !choice.text;
  const note = box.querySelector("[data-other-note]");
  note.replaceChildren();
  if (!choice.other && !blankText(choice.text)) {
    note.appendChild(noticeLine({ text: "Not sent: \"other\" is not picked.", warn: true }));
  }
  if (spellHidden(choice.text).hidden) note.appendChild(proseBlock(choice.text));
}

// One answer the operator gave: its plan and decision, the question, and the options
// chosen and "other" text as the registry keeps them, in the text the operator saw.
function answerBlock(answer) {
  const block = document.createElement("div");
  block.className = "answer";
  const head = document.createElement("div");
  head.className = "qual";
  head.textContent = [`plan ${answer.plan}`, `decision ${Number(answer.decision) + 1}`,
                      `by ${spellHidden(answer.author).text}`, fmtStamp(answer.at)]
    .filter(Boolean).join(" · ");
  block.append(head, markdownBlock(answer.question));
  for (const option of Array.isArray(answer.options) ? answer.options : []) {
    block.appendChild(line("chose", markdownBlock(option.label, { inline: true })));
  }
  if (typeof answer.other === "string") {
    block.appendChild(line("other", proseBlock(answer.other)));
  }
  return block;
}

// The choices kept for the task's plan, started on the options it assumes.
function choicesFor(task) {
  const key = `${task.id}:${task.plan.id}`;
  if (!choices.has(key)) choices.set(key, assumedChoices(planDecisions(task.plan)));
  return choices.get(key);
}

// A draw that brings another plan than the one this task was last drawn with says so,
// whatever the choices were, and drops the old plan's, its "other" texts kept for the
// line (`resetText`).
function notePlanDrawn(task) {
  const id = String(task.id);
  const plan = task.plan ? task.plan.id : null;
  const was = drawnPlans.get(id);
  drawnPlans.set(id, plan);
  if (was === undefined || was === null || plan === null || was === plan) return;
  const key = `${id}:${was}`;
  const others = (choices.get(key) || []).map(c => c.text).filter(text => !blankText(text));
  choices.delete(key);
  planResets.set(id, { from: was, to: plan, others });
}

// One part of a plan as what changed in it: a line when nothing did, else its runs,
// each spelled, added and removed ones marked by their element as well as their colour.
function diffLine(section) {
  if (section.kind === "unchanged" && !section.runs.length) {
    return line(section.label, "unchanged");
  }
  const wrap = document.createElement("div");
  const said = section.kind === "unchanged" ? "unchanged" : changeNote(section);
  if (said) wrap.appendChild(noticeLine({ text: said, warn: false }));
  const runs = document.createElement("div");
  runs.className = "prose";
  let hidden = 0;
  for (const run of section.runs) {
    const shown = spellHidden(run.text);
    hidden += shown.hidden;
    const el = document.createElement(run.op === "add" ? "ins"
      : run.op === "del" ? "del" : "span");
    el.textContent = shown.text;
    runs.appendChild(el);
  }
  wrap.appendChild(runs);
  if (hidden) wrap.appendChild(noticeLine({ text: hiddenNote(hidden), warn: true }));
  return line(section.label, wrap);
}

// The plan block's toggle: what changed, or the whole plan.
function togglePlanView(btn) {
  const block = btn.closest(".plan");
  const whole = btn.dataset.planView === "whole";
  block.querySelector(".plan-changes").hidden = whole;
  block.querySelector(".plan-whole").hidden = !whole;
  btn.dataset.planView = whole ? "changes" : "whole";
  btn.textContent = whole ? "show what changed" : "show the whole plan";
}

// Every plan the task had but the one shown, newest first, under the button that
// asked: what a plan replaced, to read beside it, from the plans the page holds.
function showPlans(btn) {
  const holder = document.createElement("div");
  const earlier = earlierPlans(shownHeld && shownHeld.plans, btn.dataset.shown);
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

// A task the open one names, its parent, a child or a blocker: its `#id` opens it
// through the hash, as a list row's does (`openCell`), so the browser's back returns
// here; its title and state follow, as text.
function relatedRow(t) {
  const span = document.createElement("span");
  const open = document.createElement("button");
  open.type = "button";
  open.className = "group-toggle";
  open.dataset.task = String(t.id);
  open.textContent = `#${t.id}`;
  const title = document.createElement("span");
  title.textContent = ` ${spellHidden(t.title).text} `;
  const state = document.createElement("span");
  state.className = "qual";
  state.textContent = `(${t.state})`;
  span.append(open, title, state);
  return span;
}

// The tasks `list` names, one per row under `head`, the label or the summary atop them.
function relatedRows(holder, head, list) {
  head.className = "qual";
  holder.appendChild(head);
  for (const t of list) {
    const row = document.createElement("div");
    row.className = "related";
    row.appendChild(relatedRow(t));
    holder.appendChild(row);
  }
  return holder;
}

// Whether a task's children are shown or only counted: the operator's last choice on
// any task, until a reload expands them again.
let childrenOpen = true;

// A parent's children, one per row, in a list that collapses to how many are left. The
// choice is kept from the list's own `toggle`, which does not bubble, so the detail's
// delegation never sees the folded history's.
function childrenList(children) {
  const list = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `children ${childrenLeft(children).text}`;
  relatedRows(list, summary, children);
  list.open = childrenOpen;
  list.addEventListener("toggle", () => { childrenOpen = list.open; });
  return list;
}

// A task, the newest page of its timeline, and for a task with a plan every plan it
// had: what the detail is drawn from.
async function readTask(id) {
  const path = encodeURIComponent(id);
  const task = await registryJson(await fetch(`/api/tasks/${path}`));
  const timeline = await registryJson(await fetch(`/api/tasks/${path}/history`));
  const plans = task.plan
    ? await registryJson(await fetch(`/api/tasks/${path}/plans`)) : [];
  return { task, held: { timeline, plans } };
}

// One task in full, read and drawn on the whole page. Another task than the one open
// starts at the top; the one open read again keeps its scroll.
async function showTask(id) {
  if (specEdit && specEdit.task !== id) specEdit = null;
  const another = id !== openTask;
  openTask = id;
  outcome = null;
  sayOnList(null);
  if (another) enterTask();
  markOpen();
  // The detail still drawn is another task's until this one is read: its controls go
  // off now, so no click on it is sent while the page loads another.
  markWriting();
  let task, held;
  try {
    ({ task, held } = await readTask(id));
  } catch (e) {
    if (openTask !== id) return;
    // A task the registry has not got: back to the list, which says so, under the
    // list's URL in place of this one, so back and a reload pass over it.
    if (e.message === "404") {
      window.history.replaceState(window.history.state, "", urlFor(hashFor(view, null)));
      closeTask();
      sayOnList(unknownTaskText(id));
      return;
    }
    // A registry that stopped is read again when it answers: applyHash reads a task
    // only when it is not the one open.
    if (!registryIsRunning()) openTask = null;
    const failed = document.createElement("div");
    failed.className = "empty warn";
    failed.textContent = `Could not load task #${id}.`;
    taskDetail.replaceChildren(failed);
    return;
  }
  if (openTask !== id) return;
  lastRead = id;
  drawTask(task, held);
}

// Under a line kept for what changed since, what an agent reads of a task, its plan,
// the operator's moves on it, then its timeline, every note with the hidden ones marked
// among the other writes, and a note to add.
function drawTask(task, held) {
  // A redraw of the read already drawn, as opening or discarding an edit makes, keeps
  // what the line atop it says: the task is no newer than when it was filled.
  const kept = task === shownTask ? noticeSlot : null;
  shownTask = task;
  shownHeld = held;
  draws += 1;
  notePlanDrawn(task);
  noteSpecDrawn(task);
  const editing = specEditOn(task);
  taskDetail.replaceChildren();
  // Its space is kept whether or not it says anything, so filling it moves nothing.
  if (!kept) {
    noticeSlot = document.createElement("div");
    noticeSlot.className = "notice-slot";
  }
  taskDetail.appendChild(noticeSlot);
  // Refilled only when rebuilt: a kept line may say something newer since, a poll's.
  if (!kept && outcome && outcome.task === String(task.id) && outcome.result === "stale") {
    fillSlot(outcome);
  }
  const head = document.createElement("h3");
  head.textContent = `#${task.id} ${spellHidden(task.title).text}`;
  // After what is always drawn and before what comes and goes, at a width of its own,
  // so neither its own clicks nor opening an edit moves it.
  head.append(" ", sourceToggle());
  if (!editing) {
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "group-toggle";
    edit.dataset.specOpen = "1";
    edit.textContent = "edit spec";
    head.append(" ", edit);
  }
  taskDetail.appendChild(head);
  taskDetail.appendChild(line("state", `${task.state}${task.ready ? ", ready" : ""}`
    + `${task.blocked ? ", blocked" : ""} · revision ${task.revision} · ${task.repo}`));
  const sk = [task.severity, task.kind].filter(Boolean).join(" · ");
  if (sk && !editing) taskDetail.appendChild(line("severity · kind", sk));
  taskDetail.appendChild(line("written", `${fmtStamp(task.created_at)}, last changed `
    + fmtStamp(task.updated_at)));
  if (task.parent) taskDetail.appendChild(line("parent", relatedRow(task.parent)));
  if (childrenLeft(task.children).total) {
    taskDetail.appendChild(childrenList(task.children));
  }
  if (task.blockers && task.blockers.length && !editing) {
    const head = document.createElement("div");
    head.textContent = "blocked by";
    taskDetail.appendChild(relatedRows(document.createElement("div"), head,
                                       task.blockers));
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
  if (editing) {
    taskDetail.appendChild(specForm(task));
  } else {
    const body = markdownBlock(task.body);
    body.classList.add("task-body");
    taskDetail.appendChild(body);
  }
  taskDetail.appendChild(decisionsBlock(task));
  // The plan beside the body: approving a refined task approves the two together
  // (task-registry/DESIGN.md, "Plans").
  const planHead = document.createElement("h3");
  planHead.textContent = "Plan";
  taskDetail.appendChild(planHead);
  if (task.plan) {
    taskDetail.appendChild(planBlock(task.plan, task,
                                     planPredecessor(held.plans, task.plan.id)));
    const more = pagerButton("earlier plans", "plans", "");
    more.dataset.shown = String(task.plan.id);
    taskDetail.appendChild(more);
  } else {
    taskDetail.appendChild(noticeLine(planNotice(null, task)));
  }
  // Below the plan, so what a move approves is read before it.
  taskDetail.appendChild(movesBlock(task));
  // A move on a note says what it came to above the timeline it was made in.
  if (outcome && outcome.task === String(task.id) && outcome.onNote) {
    taskDetail.appendChild(noticeLine(outcome));
  }
  const timeline = held.timeline;
  const timelineHead = document.createElement("h3");
  timelineHead.textContent = `Timeline · ${timeline.total}`;
  taskDetail.appendChild(timelineHead);
  // Earlier pages go above the one shown, so the timeline reads oldest first throughout.
  const earlier = olderCursor(timeline.history, "oldest-first", 0, timeline.total);
  if (earlier !== null) {
    const btn = pagerButton("earlier", "history", earlier);
    btn.dataset.seen = String(timeline.history.length);
    taskDetail.appendChild(btn);
  }
  const timelineTable = document.createElement("table");
  const timelineBody = document.createElement("tbody");
  renderTimeline(timelineBody, timeline.history);
  timelineTable.appendChild(timelineBody);
  taskDetail.appendChild(timelineTable);
  taskDetail.appendChild(noteForm(task));
  markAnswers();
  markWriting();
}

// The line atop the detail, filled: what changed since the task was drawn, and the
// way to read it again.
function fillSlot(notice) {
  const show = document.createElement("button");
  show.type = "button";
  show.className = "group-toggle";
  show.dataset.show = "1";
  show.textContent = "read it again";
  const text = document.createElement("span");
  text.textContent = notice.text;
  text.title = notice.text;
  noticeSlot.className = notice.warn ? "notice-slot warn" : "notice-slot";
  noticeSlot.dataset.kind = notice.kind || notice.result;
  noticeSlot.replaceChildren(show, text);
}

// The open task read again by the poll, to say in the slot when it changed since it
// was drawn (`taskUpdate`), and to draw nothing else: the reader asks for the newer one.
// Only while the registry answers, no write is out and the task drawn is the one named;
// an answer a draw overtook is dropped, since it was asked against a task no longer
// shown. It fills the slot or replaces what fills it, and never empties it: only a draw
// does. New notes do not displace a refused write's notice; a newer change does.
export async function refreshOpenTask() {
  if (!registryIsRunning() || writing || !drawnIsOpen()) return;
  const asked = draws;
  let polled;
  try {
    polled = await registryJson(await fetch(`/api/tasks/${encodeURIComponent(openTask)}`));
  } catch (e) {
    return;
  }
  if (draws !== asked || writing || !drawnIsOpen()) return;
  const update = taskUpdate(shownTask, polled);
  if (!update || (update.kind === "notes" && noticeSlot.dataset.kind === "stale")) return;
  fillSlot({ ...update, warn: update.kind === "changed" });
}

// The moves the operator API takes from the task's state, and what the last one came
// to, but a refusal for a task that changed, which the slot atop the detail says, and a
// move on a note, said above the timeline.
// Approve and send back are each a control of their own, as a refined task's two
// answers. Approve has a row of its own, above the rest and two lines high, which
// `markAnswers` fills as choices are made: neither its label nor the send back's
// moves the other. A claimed task's moves open with whose claim it is. The primary,
// when the task has one, is marked here, or on approve by `markAnswers`.
function movesBlock(task) {
  const block = document.createElement("div");
  const head = document.createElement("h3");
  head.textContent = "Moves";
  block.appendChild(head);
  if (task.claim) {
    const holder = document.createElement("div");
    holder.className = "holder-line";
    holder.dataset.holder = "1";
    holder.textContent = holderText(task.claim);
    block.appendChild(holder);
  }
  if (outcome && outcome.task === String(task.id) && outcome.result !== "stale"
      && !outcome.onNote) {
    block.appendChild(noticeLine(outcome));
  }
  const blocked = openBlockedText(task);
  if (blocked) block.appendChild(noticeLine({ text: blocked, warn: false }));
  // While the edit is open, a line one high that says when it holds the moves below.
  if (specEditOn(task)) {
    const held = document.createElement("div");
    held.className = "held-line";
    held.dataset.specHeld = "1";
    block.appendChild(held);
  }
  if (approvable(task)) {
    const approve = document.createElement("div");
    approve.className = "approve-row";
    approve.dataset.approveRow = "1";
    block.appendChild(approve);
  }
  const row = document.createElement("div");
  row.className = "moves";
  const primary = primaryMove(task);
  for (const move of movesFor(task)) {
    const btn = writeButton(moveLabel(move, task), "move", move.state);
    btn.dataset.verb = move.verb;
    if (move.state === primary) btn.classList.add("primary");
    row.appendChild(btn);
  }
  if (task.claim) row.appendChild(writeButton(breakLabel(task.claim), "break", ""));
  block.appendChild(row);
  const hidden = noticeLine({ text: "", warn: true });
  hidden.dataset.approveHidden = "1";
  hidden.hidden = true;
  block.appendChild(hidden);
  // The parent, typed as an id, and after it what `markParent` draws as it is typed: the
  // reparent, while the text builds one, or why the text does not.
  const parent = document.createElement("div");
  parent.className = "moves parent-row";
  const input = document.createElement("input");
  input.type = "text";
  input.dataset.parent = String(task.id);
  input.placeholder = "#7, or nothing for none";
  input.setAttribute("aria-label", "the task's parent");
  input.value = parentText(task);
  const slot = document.createElement("div");
  slot.dataset.parentSlot = "1";
  parent.append(line("parent", input), slot);
  block.appendChild(parent);
  return block;
}

// What the parent's input holds: the text typed on this task, kept until a reparent of
// it lands, or the task's parent as read.
function parentText(task) {
  const id = parentId(task);
  return parentDrafts.get(String(task.id)) ?? (id === null ? "" : `#${id}`);
}

// The reparent the text makes of the task drawn, or why it makes none: a button only
// while `reparentRequest` builds a body, so none is offered for the parent it has.
function markParent() {
  const slot = taskDetail.querySelector("[data-parent-slot]");
  if (!slot || !drawnIsOpen()) return;
  const text = parentText(shownTask);
  const send = reparentRequest(shownTask, text);
  const { bad } = parseParent(text);
  slot.replaceChildren();
  if (send) {
    const btn = writeButton(reparentLabel(shownTask, send), "reparent", "");
    btn.dataset.verb = "reparent";
    slot.appendChild(btn);
  } else if (bad !== null) {
    slot.appendChild(noticeLine({ text: parentProblem(bad), warn: true }));
  }
}

// What follows the choices, redrawn in place as the operator makes them, and once by
// each draw: the approve, or while an answer is incomplete the line saying which,
// since no approve is built then; the send back's label; each "other" box; and the
// count of hidden characters in what the approve sends.
function markAnswers() {
  if (!drawnIsOpen()) return;
  const task = shownTask;
  const picked = approvable(task) ? choicesFor(task) : null;
  const decisions = planDecisions(task.plan);
  const row = taskDetail.querySelector("[data-approve-row]");
  if (row && picked) {
    const goes = approveOutcome(decisions, picked);
    if (goes === "incomplete") {
      const said = document.createElement("span");
      said.className = "empty warn";
      said.textContent = incompleteText(unanswered(decisions, picked));
      row.replaceChildren(said);
    } else {
      const btn = writeButton(approveLabel(task, goes), "approve", "");
      btn.dataset.verb = "approve";
      if (primaryMove(task) === "approve") btn.classList.add("primary");
      row.replaceChildren(btn);
    }
  }
  const back = movesFor(task).find(m => m.verb === "send back");
  const backBtn = taskDetail.querySelector('button[data-write="move"][data-verb="send back"]');
  if (back && backBtn) setLabel(backBtn, moveLabel(back, task, picked));
  for (const box of taskDetail.querySelectorAll("[data-other-box]")) {
    if (picked) markOther(box, picked[Number(box.dataset.otherBox)]);
  }
  const hidden = taskDetail.querySelector("[data-approve-hidden]");
  if (hidden) {
    hidden.textContent = picked ? approveHidden(task, picked) : "";
    hidden.hidden = !hidden.textContent;
  }
}

// A choice made in a decision's group, kept by task and plan. The first one in a plan
// that replaced another spends the line saying so, which keeps its space until the
// next draw.
function choose(input) {
  if (!drawnIsOpen() || !approvable(shownTask)) return;
  const i = Number(input.dataset.decision);
  const choice = choicesFor(shownTask)[i];
  const multi = Boolean(planDecisions(shownTask.plan)[i].multi_select);
  if (input.dataset.option === "other") {
    choice.other = multi ? input.checked : true;
    if (!multi) choice.options = [];
  } else {
    const j = Number(input.dataset.option);
    if (!multi) {
      choice.options = [j];
      choice.other = false;
    } else {
      choice.options = choice.options.filter(k => k !== j).concat(input.checked ? [j] : []);
    }
  }
  if (planResets.delete(String(shownTask.id))) {
    for (const said of taskDetail.querySelectorAll("[data-plan-reset]")) {
      said.classList.add("spent");
    }
  }
  markAnswers();
  markWriting();
}

// A write's control, its label a string or the scheme's lines (`moveLabel`).
function writeButton(label, kind, value) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.dataset.write = kind;
  btn.dataset.value = value;
  setLabel(btn, label);
  return btn;
}

// A control's words: the main line, the second under it, smaller, and the caution
// border. `data-name` keeps the main line, which the outcome names the write by.
function setLabel(btn, label) {
  const said = typeof label === "string" ? { main: label, second: "", caution: false }
    : label || { main: "", second: "", caution: false };
  const main = document.createElement("span");
  main.className = "main";
  main.textContent = said.main;
  btn.replaceChildren(main);
  if (said.second) {
    const second = document.createElement("span");
    second.className = "second";
    second.textContent = said.second;
    btn.appendChild(second);
  }
  btn.classList.toggle("caution", Boolean(said.caution));
  btn.dataset.name = said.main;
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
  form.append(text, writeButton({ main: "add the note", second: NOTE_SECOND,
                                   caution: false }, "note", ""));
  return form;
}

// Whether the spec edit is open on `task`.
function specEditOn(task) {
  return specEdit !== null && Boolean(task) && specEdit.task === String(task.id);
}

// A draw of the task being edited past the read its draft was made from, whatever
// brought it (a refusal's `current`, the read after a write, "read it again"), moves the
// draft onto it (`rebaseDraft`) and says what moved, and when a draft that had changes
// has none left.
function noteSpecDrawn(task) {
  if (!specEditOn(task) || !(Number(task.revision) > Number(specEdit.base.revision))) return;
  const { base, draft } = specEdit;
  const mine = draftChanges(base, draft);
  const moved = changedFields(base, task);
  const rebased = rebaseDraft(base, draft, task);
  specEdit = { task: specEdit.task, base: task, draft: rebased,
               rebased: rebaseText(base.revision, task.revision, moved,
                                   mine.filter(field => moved.includes(field)),
                                   mine.length > 0 && !draftChanges(task, rebased).length) };
}

// The spec as a form, in place of the lines that show it: each field's control, set
// as a property from the draft, and under the title and body what will be stored,
// spelled (`markEdit` fills them as the operator types). A refined task says what an
// edit does to its plan.
function specForm(task) {
  const form = document.createElement("div");
  form.className = "spec-form";
  form.dataset.specForm = "1";
  const amend = specAmendText(task);
  if (amend) form.appendChild(noticeLine({ text: amend, warn: true }));
  if (specEdit.rebased) form.appendChild(noticeLine({ text: specEdit.rebased, warn: true }));
  for (const field of ["title", "body"]) {
    form.appendChild(line(field, specControl(field)));
    const preview = document.createElement("div");
    preview.className = field === "body" ? "spec-preview task-body" : "spec-preview";
    preview.dataset.specPreview = field;
    form.appendChild(preview);
  }
  const picks = document.createElement("div");
  picks.className = "moves";
  picks.append(line("severity", specControl("severity")), line("kind", specControl("kind")));
  form.appendChild(picks);
  const unknown = unknownSpecText(specEdit.base);
  if (unknown) form.appendChild(noticeLine({ text: unknown, warn: true }));
  form.appendChild(line("blocked by", specControl("blockers")));
  const problem = document.createElement("div");
  problem.dataset.specProblem = "1";
  form.appendChild(problem);
  const row = document.createElement("div");
  row.className = "moves";
  const save = writeButton(saveLabel(specEdit.base), "edit", "");
  save.dataset.verb = "save the edit";
  const discard = document.createElement("button");
  discard.type = "button";
  discard.dataset.specDiscard = "1";
  discard.textContent = "discard the edit";
  row.append(save, discard);
  form.appendChild(row);
  return form;
}

// One field's control, holding the draft's value. A select offers none and each value
// the registry takes.
function specControl(field) {
  const control = SPEC_CONTROLS[field];
  let el;
  if (Array.isArray(control)) {
    el = document.createElement("select");
    for (const value of control) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = value || "none";
      el.appendChild(opt);
    }
  } else if (control === "textarea") {
    el = document.createElement("textarea");
    el.rows = 10;
  } else {
    el = document.createElement("input");
    el.type = "text";
    if (field === "blockers") el.placeholder = "#7, 12";
  }
  el.dataset.spec = field;
  el.setAttribute("aria-label", `the task's ${field}`);
  el.value = specEdit.draft[field];
  return el;
}

// The writes a draft with changes holds: each that moves the task or ends its claim.
// A note, the moves on notes and a reparent stay free: a reparent changes no spec field,
// so a draft only rebases onto the read after it.
const HELD_KINDS = ["move", "approve", "break"];
const HELD_MOVES = HELD_KINDS.map(kind => `button[data-write="${kind}"]`).join(", ");

// Whether the edit holds the task's moves: open on the task drawn, with changes.
function editHolds() {
  return drawnIsOpen() && specEditOn(shownTask)
    && draftChanges(specEdit.base, specEdit.draft).length > 0;
}

// What follows the draft, redrawn in place as the operator types, and once by each
// draw: what title and body will be stored, why the blockers do not parse, the save,
// off while it has nothing to send, and the moves a draft with changes holds.
function markEdit() {
  const held = editHolds();
  for (const btn of taskDetail.querySelectorAll(HELD_MOVES)) {
    if (held) btn.disabled = true;
  }
  const said = taskDetail.querySelector("[data-spec-held]");
  if (said) {
    said.textContent = held ? HELD_TEXT : "";
    said.className = held ? "held-line warn" : "held-line";
  }
  if (!drawnIsOpen() || !specEditOn(shownTask)) return;
  const { base, draft } = specEdit;
  for (const el of taskDetail.querySelectorAll("[data-spec-preview]")) {
    const preview = specPreview(base, draft, el.dataset.specPreview);
    const text = document.createElement("div");
    text.className = "prose";
    text.textContent = preview.text;
    el.replaceChildren(line(preview.label, text));
    if (preview.hidden) el.appendChild(noticeLine({ text: hiddenNote(preview.hidden), warn: true }));
    if (preview.lost) el.appendChild(noticeLine({ text: preview.lost, warn: true }));
  }
  const { bad } = parseBlockers(draft.blockers);
  const problem = taskDetail.querySelector("[data-spec-problem]");
  if (problem) {
    problem.replaceChildren();
    if (bad !== null) problem.appendChild(noticeLine({ text: blockersProblem(bad), warn: true }));
  }
  const save = taskDetail.querySelector('button[data-write="edit"]');
  if (save && !editRequest(base, draft)) save.disabled = true;
}

// The edit as the draft makes it, on the read it is a draft of.
function editWrite(base) {
  if (!specEditOn(shownTask)) return null;
  const send = editRequest(specEdit.base, specEdit.draft);
  return send && { ...base, kind: "edit", revision: send.revision, send,
                   verb: "save the edit" };
}

// The edit opened on the task drawn, or discarded: the detail drawn again from what the
// page holds.
function toggleSpecEdit(open) {
  if (writing || !drawnIsOpen()) return;
  specEdit = open ? { task: String(shownTask.id), base: shownTask,
                      draft: specDraft(shownTask), rebased: null } : null;
  drawTask(shownTask, shownHeld);
}

// Every write control in the detail, on only while no write is out and what is drawn
// is the task the page names; and the flagged block's and the create form's, on while
// no write is out. The reparent is drawn first, so its button comes in gated.
function drawnIsOpen() {
  return shownTask !== null && String(shownTask.id) === openTask;
}
function markWriting() {
  markParent();
  for (const el of taskDetail.querySelectorAll(
    "[data-write], textarea[data-note], textarea[data-reply], input[data-decision], "
    + "textarea[data-other], [data-spec], button[data-spec-open], "
    + "button[data-spec-discard], input[data-parent]")) {
    el.disabled = writing !== null || !drawnIsOpen();
  }
  for (const box of taskDetail.querySelectorAll("[data-reply-box]")) markReply(box);
  markEdit();
  markFlagged();
  markCreate();
  markRepos();
}

// "mark handled" in a reply box is off while the box holds a reply, so a typed reply is
// not dropped by the wrong click: "reply and mark handled" sends it.
function markReply(box) {
  const plain = box.querySelector('button[data-write="handle"][data-value="true"]');
  if (plain && (replyDrafts.get(box.dataset.replyBox) ?? "").trim()) plain.disabled = true;
}

// A repo write (repos.js) goes through the gate every write here does: none while
// another is out or the registry is not known to answer, and every control off until
// its outcome is drawn.
function beginRepoWrite(write) {
  if (writing || !registryIsRunning()) return false;
  writing = write;
  markWriting();
  return true;
}
function endRepoWrite() {
  writing = null;
  markWriting();
}

// One write, then the page as the registry has it after: the task read again, or for
// a stale one drawn from the task the refusal carries, which is not read again
// (task-registry/DESIGN.md, "The operator API"); the inbox and the list refreshed. A
// write whose answer was lost is decided from a read (`checkWrite`), never sent again.
// A write in the detail is on the task drawn; a bulk handle, from the flagged block on
// the list, and a create, from the form there, on none (`task` null). A create first
// reads its bound, inside the gate, so a second click cannot send while it is out: one
// that fails sends nothing.
async function runWrite(write) {
  if (writing || !registryIsRunning()) return;
  writing = write;
  markWriting();
  if (write.kind === "create") {
    write.bound = await readBound(write.send.repo).catch(() => null);
  }
  const unsent = write.kind === "create" && write.bound === null;
  let answer = null;
  try {
    if (!unsent) answer = await readAnswer(await sendWrite(write));
  } catch (e) {
    answer = null;
  }
  let result = unsent ? "no-bound" : writeOutcome(answer);
  let drawn = null;
  if (result === "not-running") {
    setRunning(false);
  } else if (result === "stale") {
    drawn = { task: answer.body.current, held: shownHeld };
  } else if (write.task !== null) {
    drawn = await readTask(write.task).catch(() => null);
  }
  let created = result === "landed" && write.kind === "create" ? answer.body.task : null;
  if (result === "unknown") {
    const reread = write.kind === "handle-many"
      ? { flagged: await readFlaggedPast(write.notes).catch(() => null) }
      : write.kind === "create" ? await readCreated(write) : drawn;
    result = checkWrite(write, reread);
    if (write.kind === "create") created = createdTask(write, reread);
  }
  const landed = result === "landed" || result === "found";
  if (write.kind === "note" && landed && noteDraft.task === write.task
      && noteDraft.text === write.text) {
    noteDraft = { task: null, text: "" };
  }
  if (write.kind === "handle" && write.handled && landed) {
    replyDrafts.delete(String(write.note));
  }
  if (write.kind === "approve" && landed) {
    choices.delete(`${write.task}:${write.plan}`);
    planResets.delete(write.task);
  }
  // Only a landed edit drops its draft: any other outcome keeps it for the next draw,
  // which rebases it when the task moved.
  if (write.kind === "edit" && landed && specEdit && specEdit.task === write.task) {
    specEdit = null;
  }
  if (write.kind === "reparent" && landed) parentDrafts.delete(write.task);
  const detail = answer && answer.body && typeof answer.body === "object"
    ? answer.body.detail : answer && answer.text;
  // The state an approve reached: as its answer names it, or as the task read again.
  const reached = result === "landed" ? answer.body.state
    : drawn && drawn.task && drawn.task.state;
  // Not `kind`: the slot reads an outcome's `kind` before its `result` (`fillSlot`).
  const said = { task: write.task, result, onNote: isNoteMove(write),
                 ...outcomeText(write, result, detail, reached) };
  if (write.kind === "handle-many") flaggedOutcome = said;
  // The flagged rows as the registry now has them, after a move on notes or on a task,
  // which can park or reopen a drawn note's task: drawn while every control is still
  // off, so none comes back on over a row that moved.
  if (redrawsFlagged(write, result)) await readFlagged(true);
  writing = null;
  const opens = write.kind === "create" ? finishCreate(write, result, detail, created)
    : null;
  if (write.task !== null && openTask === write.task) {
    outcome = said;
    if (drawn) drawTask(drawn.task, drawn.held);
    else if (shownTask) drawTask(shownTask, shownHeld);
  } else {
    markWriting();
  }
  if (result !== "not-running") {
    refreshInbox();
    refreshTasks();
  }
  // A create that landed, or was found, opens its task, as a click on its row would.
  if (opens !== null) goTo(view, String(opens));
}

// The write a control in the detail asks for, as of the task the page shows.
// Built from what is drawn, id and revision both, and only while that is the task the
// page names: while another loads, the drawn one is not what a click is for.
function writeFor(btn) {
  if (!drawnIsOpen()) return null;
  const base = { task: String(shownTask.id), revision: shownTask.revision,
                 lastNote: lastNoteId(shownTask) };
  if (HELD_KINDS.includes(btn.dataset.write) && editHolds()) return null;
  if (btn.dataset.write === "edit") return editWrite(base);
  if (btn.dataset.write === "move") {
    return { ...base, kind: "move", state: btn.dataset.value, verb: btn.dataset.name };
  }
  if (btn.dataset.write === "approve") return approveWrite(base);
  if (btn.dataset.write === "break") return { ...base, kind: "break", verb: "break the claim" };
  if (btn.dataset.write === "reparent") return reparentWrite(base);
  if (btn.dataset.write === "hide" || btn.dataset.write === "handle") {
    return noteWrite(btn, base);
  }
  const text = noteDraft.task === base.task ? noteDraft.text : "";
  return text.trim() ? { ...base, kind: "note", text, verb: "add the note" } : null;
}

// A reparent of the task drawn, as its parent's text makes it, naming the revision drawn.
function reparentWrite(base) {
  const send = reparentRequest(shownTask, parentText(shownTask));
  return send && { ...base, kind: "reparent", revision: send.revision, send,
                   verb: reparentLabel(shownTask, send).main };
}

// An approve of the plan drawn, with an answer per decision from the choices made on
// it, and the state they take the task to, which the outcome checks; none while an
// answer is incomplete.
function approveWrite(base) {
  if (!approvable(shownTask)) return null;
  const decisions = planDecisions(shownTask.plan);
  const picked = choicesFor(shownTask);
  const answers = approveAnswers(decisions, picked);
  const predicted = approveOutcome(decisions, picked);
  return answers && { ...base, kind: "approve", plan: shownTask.plan.id, answers,
                      predicted, verb: approveLabel(shownTask, predicted).main };
}

// A move on a note in the timeline, as of the newest timeline entry the page holds:
// what its check counts from if the answer is lost. A reply goes only with text in it.
function noteWrite(btn, base) {
  const note = btn.dataset.note;
  const at = { ...base, note: Number(note), lastEntry: lastEntryId(shownHeld.timeline) };
  if (btn.dataset.write === "hide") {
    const hidden = btn.dataset.value === "true";
    return { ...at, kind: "hide", hidden, forYou: btn.dataset.forYou === "1",
             taskState: shownTask.state, verb: `${hidden ? "hide" : "unhide"} note #${note}` };
  }
  if (btn.dataset.value === "reply") {
    const reply = replyDrafts.get(note) ?? "";
    return reply.trim() ? { ...at, kind: "handle", handled: true, reply,
                            verb: `reply to note #${note} and mark it handled` } : null;
  }
  const handled = btn.dataset.value === "true";
  return { ...at, kind: "handle", handled, reply: null,
           verb: `mark note #${note} ${handled ? "handled" : "not handled"}` };
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

// Timeline entries as rows: when, who, the action with the marks of the note it posted,
// then that note, the note a hide or a handle acts on, and the rest of the write folded.
function renderTimeline(tbody, history) {
  for (const h of history) {
    const entry = timelineEntry(h);
    if (!entry) continue;
    const tr = document.createElement("tr");
    const at = cell(tr, fmtStamp(h.ts), "ts");
    at.title = fmtInstant(h.ts);
    authorCell(tr, h.author);
    cell(tr, [h.action, ...entry.marks].join(" · "), "qual");
    const td = document.createElement("td");
    if (entry.note) {
      td.appendChild(markdownBlock(entry.note.text));
    }
    if (entry.named) td.appendChild(line("note", entry.named));
    if (entry.folded.length) {
      const folded = document.createElement("details");
      const summary = document.createElement("summary");
      summary.className = "qual";
      summary.textContent = entry.folded.map(([key]) => key).join(", ");
      folded.appendChild(summary);
      for (const [key, value] of entry.folded) {
        folded.appendChild(line(key, typeof value === "string" ? proseBlock(value)
          : spellHidden(JSON.stringify(value)).text));
      }
      td.appendChild(folded);
    }
    if (entry.note) td.appendChild(noteControls(entry.note));
    tr.appendChild(td);
    if (entry.note && entry.note.hidden) tr.className = "hidden-note";
    tbody.appendChild(tr);
  }
}

// The moves on a note, under it on whichever page of the timeline it is drawn
// (`noteMovesFor`). Marking one handled opens a box for a reply, open while its note
// has a draft, an empty one too, so a redraw keeps it open; "mark handled" sends no
// reply, "reply and mark handled" the text.
function noteControls(note) {
  const id = String(note.id);
  const wrap = document.createElement("div");
  wrap.className = "note-moves";
  const row = document.createElement("div");
  row.className = "moves";
  wrap.appendChild(row);
  for (const move of noteMovesFor(note)) {
    if (move.reply) {
      const open = document.createElement("button");
      open.type = "button";
      open.dataset.replyOpen = id;
      open.textContent = "mark handled…";
      open.hidden = replyDrafts.has(id);
      row.appendChild(open);
      wrap.appendChild(replyBox(id));
      continue;
    }
    const btn = writeButton(noteMoveLabel(move), move.kind, String(move.value));
    btn.dataset.note = id;
    if (note.for_operator && !note.handled) btn.dataset.forYou = "1";
    row.appendChild(btn);
  }
  return wrap;
}

function replyBox(id) {
  const box = document.createElement("div");
  box.className = "note-form";
  box.dataset.replyBox = id;
  box.hidden = !replyDrafts.has(id);
  const text = document.createElement("textarea");
  text.rows = 2;
  text.setAttribute("aria-label", `A reply to note #${id}`);
  text.placeholder = "a reply, which agents read with the note it answers (optional)";
  text.value = replyDrafts.get(id) ?? "";
  text.dataset.reply = id;
  const row = document.createElement("div");
  row.className = "moves";
  const plain = writeButton("mark handled", "handle", "true");
  plain.dataset.note = id;
  const reply = writeButton({ main: "reply and mark handled", second: NOTE_SECOND,
                              caution: false }, "handle", "reply");
  reply.dataset.note = id;
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.dataset.replyCancel = id;
  cancel.textContent = "cancel";
  row.append(plain, reply, cancel);
  box.append(text, row);
  return box;
}

// The reply box of a note opened or closed where it is drawn, its draft kept or dropped.
function toggleReply(id, open) {
  if (open) replyDrafts.set(id, replyDrafts.get(id) ?? "");
  else replyDrafts.delete(id);
  for (const btn of taskDetail.querySelectorAll("button[data-reply-open]")) {
    if (btn.dataset.replyOpen === id) btn.hidden = open;
  }
  for (const box of taskDetail.querySelectorAll("[data-reply-box]")) {
    if (box.dataset.replyBox === id) box.hidden = !open;
  }
  markWriting();
}

// An older page of the timeline, under the button that asked.
async function showPage(btn) {
  const id = openTask;
  const q = new URLSearchParams();
  if (btn.dataset.before) q.set("before", btn.dataset.before);
  let page;
  try {
    page = await registryJson(
      await fetch(`/api/tasks/${encodeURIComponent(id)}/history?${q}`));
  } catch (e) {
    btn.textContent = "could not load the earlier entries";
    return;
  }
  if (openTask !== id) return;
  const table = document.createElement("table");
  const tbody = document.createElement("tbody");
  renderTimeline(tbody, page.history);
  table.appendChild(tbody);
  const seen = Number(btn.dataset.seen || 0) + page.history.length;
  const more = olderCursor(page.history, "oldest-first", seen, page.total);
  // Above this page, as in drawTask, so the timeline reads oldest first.
  const holder = document.createElement("div");
  if (more !== null) {
    const next = pagerButton("earlier", "history", more);
    next.dataset.seen = String(seen);
    holder.appendChild(next);
  }
  holder.appendChild(table);
  btn.replaceWith(holder);
  // Its note moves are write controls like any other.
  markWriting();
}

function closeTask() {
  specEdit = null;
  openTask = null;
  shownTask = null;
  shownHeld = null;
  outcome = null;
  taskDetail.replaceChildren();
  enterList();
  markOpen();
}

// The list or one task, never both. The list stays in the DOM while hidden, so its
// filters, search and page are as they were; the page places every scroll itself
// (`history.scrollRestoration` is "manual"): a task opened starts at the top, and the
// list comes back at the offset it had when it was left. Left for a task only: going
// from one task to another keeps the list's.
function enterTask() {
  if (!listView.hidden) listScroll = window.scrollY;
  listView.hidden = true;
  taskView.hidden = false;
  window.scrollTo(0, 0);
}
function enterList() {
  if (taskView.hidden) return;
  taskView.hidden = true;
  listView.hidden = false;
  window.scrollTo(0, listScroll);
}

// The line atop the list that says the hash named a task the page could not open,
// kept until the next open or change of view; null empties it.
function sayOnList(text) {
  listNotice.textContent = text ?? "";
  listNotice.hidden = text === null;
}

// The page's URL with `hash` in place of its own, for history.replaceState, which
// changes the URL without a hashchange: no read, and no entry of its own.
function urlFor(hash) {
  return location.pathname + location.search + hash;
}

export const LEAVE_TEXT = "Leave this task? The spec edit's changes have not been saved, "
  + "and leaving discards them.";

// Whether a spec edit with changes is open on the task open: what leaving would lose.
function draftChanged() {
  return specEdit !== null && specEdit.task === openTask
    && draftChanges(specEdit.base, specEdit.draft).length > 0;
}

// Whether the page may leave the open task, for the list or another: asked once, only
// while a draft with changes would be lost. Accepted, the draft goes here, so the hash
// change that follows asks nothing more. A draft with no changes drops unasked, with
// the task. No write is behind this: it guards unsent text, so no move's guard changes.
function mayLeave() {
  if (!draftChanged()) return true;
  if (!window.confirm(LEAVE_TEXT)) return false;
  specEdit = null;
  return true;
}

// Every history entry the page follows carries its place in history.state, so a
// declined leave can tell a back or forward to an entry it knows from a new entry, and
// undo either without rewriting one.
function entryIndex() {
  const state = window.history.state;
  return state && Number.isInteger(state.tasksPage) ? state.tasksPage : null;
}
function stampEntry(index) {
  window.history.replaceState({ tasksPage: index }, "", location.href);
}

// A declined leave, undone: a back or forward by going as far the other way, a new
// entry, a link's or a typed hash, by going back from it, which leaves it ahead, as a
// back always does. The hashchange the undo fires names the task still open, so
// `applyHash` finds nothing to leave and nothing to read: no flag has to skip it, and
// none can be left set by an undo whose hashchange never comes.
function undoLeave(index) {
  if (index !== null) window.history.go(appliedIndex - index);
  else window.history.back();
}

// The three views, each on the list: the Inbox, All tasks and Repos. Back from either
// of the other two, the flagged rows are drawn from a fresh read; a bulk's outcome lasts
// until another view is opened. Entering Repos reads the repo list, once, never on a
// hash change that keeps the view. The back control names the view it goes back to.
const BACK_TEXT = { inbox: "← Inbox", all: "← All tasks", repos: "← Repos" };
function showView(name) {
  const back = view !== "inbox" && name === "inbox";
  const entering = view !== "repos" && name === "repos";
  if (name !== view) sayOnList(null);
  view = name;
  inboxView.hidden = name !== "inbox";
  allView.hidden = name !== "all";
  reposSection.hidden = name !== "repos";
  viewInbox.setAttribute("aria-pressed", String(name === "inbox"));
  viewAll.setAttribute("aria-pressed", String(name === "all"));
  viewRepos.setAttribute("aria-pressed", String(name === "repos"));
  taskBack.textContent = BACK_TEXT[name];
  if (name !== "inbox") flaggedOutcome = null;
  if (back) readFlagged(true);
  if (entering) refreshRepos();
}

// The page as the hash names it: the view at once, the task once the registry answers
// (probe calls this again when it does). Leaving the open task by any change of the
// hash, the back control's, Esc's, the browser's back or forward, a link's, asks first
// when an edit would be lost (`mayLeave`). Declined, the move is undone in history,
// never rewritten over (`undoLeave`), and nothing is read. A task parameter no id can
// be is not read: the list says so, under the list's URL, so back and a reload pass
// over it.
function applyHash() {
  const named = parseHash(location.hash);
  const index = entryIndex();
  if (openTask !== null && named.task !== openTask && !mayLeave()) {
    undoLeave(index);
    return;
  }
  // An entry the page has not stamped is a new one, pushed past the one it followed.
  appliedIndex = index ?? appliedIndex + 1;
  if (index === null) stampEntry(appliedIndex);
  showView(named.view);
  if (named.bad !== null) {
    window.history.replaceState(window.history.state, "",
                                urlFor(hashFor(named.view, null)));
    if (openTask !== null) closeTask();
    sayOnList(badTaskText(named.bad));
  }
  if (!registryIsRunning()) return;
  if (named.task === null) {
    // The task view can be up with no task open: a read the stopped registry failed.
    if (openTask !== null || !taskView.hidden) closeTask();
  } else if (named.task !== openTask) {
    showTask(named.task);
  }
}

// Every way to open, close or switch goes through the hash, so back and a reload agree
// with what was clicked. A click naming what the hash already names reads the task
// again, or closes it.
function goTo(nextView, task) {
  if (openTask !== null && task !== openTask && !mayLeave()) return;
  const next = hashFor(nextView, task);
  if (next !== location.hash) {
    location.hash = next;
  } else if (task === null) {
    closeTask();
  } else if (registryIsRunning()) {
    showTask(task);
  }
}

// Back to the list, by the control atop the task, or by Esc outside a text field: from
// whatever the task view shows, a read that failed included.
function backToList() {
  if (!taskView.hidden) goTo(view, null);
}
function escGoesBack(ev) {
  if (ev.key !== "Escape" || ev.defaultPrevented || taskView.hidden) return false;
  const el = ev.target;
  return !(el && typeof el.closest === "function"
           && el.closest("input, textarea, select, [contenteditable]"));
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
  listView = document.getElementById("list-view");
  taskView = document.getElementById("task-view");
  taskBack = document.getElementById("task-back");
  listNotice = document.getElementById("list-notice");
  inboxEl = document.getElementById("inbox");
  inboxEmpty = document.getElementById("inbox-empty");
  inboxCountEl = document.getElementById("inboxcount");
  viewInbox = document.getElementById("view-inbox");
  viewAll = document.getElementById("view-all");
  viewRepos = document.getElementById("view-repos");
  inboxView = document.getElementById("inbox-view");
  allView = document.getElementById("all-view");
  reposSection = document.getElementById("repos-view");
  flaggedEl = document.getElementById("flagged");
  flaggedLineEl = document.getElementById("flagged-line");
  flaggedBody = document.getElementById("flagged-notes");
  flaggedEmpty = document.getElementById("flagged-empty");
  flaggedAll = document.getElementById("flagged-all");
  flaggedHandle = document.getElementById("flagged-handle");
  flaggedLeftOut = document.getElementById("flagged-left-out");
  flaggedOutcomeEl = document.getElementById("flagged-outcome");
  createOpen = document.getElementById("create-open");
  createForm = document.getElementById("create-form");
  createOutcomeEl = document.getElementById("create-outcome");

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
  // A new task, from All tasks: the form opens on a fresh draft, or the one kept.
  createOpen.addEventListener("click", () => {
    if (writing || createDraft !== null) return;
    createDraft = newCreateDraft();
    renderCreate();
  });
  createForm.addEventListener("input", ev => {
    const field = ev.target.dataset.createField;
    if (!field || createDraft === null) return;
    createDraft = { ...createDraft, [field]: ev.target.value };
    markCreate();
  });
  createForm.addEventListener("click", ev => {
    const btn = ev.target.closest("button");
    if (!btn || writing) return;
    if (btn.dataset.write === "create") {
      const write = createWrite();
      if (write) runWrite(write);
    } else if (btn.dataset.create === "look-again") {
      lookAgain();
    } else if (btn.dataset.create === "discard") {
      createDraft = null;
      lostCreate = null;
      createOutcome = null;
      renderCreate();
    }
  });
  renderCreate();
  // The views are on the list, never on screen with a task open, so they name none.
  viewInbox.addEventListener("click", () => goTo("inbox", null));
  viewAll.addEventListener("click", () => goTo("all", null));
  viewRepos.addEventListener("click", () => goTo("repos", null));
  // The Repos view, its writes under this page's one gate.
  mountRepos();
  repoHooks({ begin: beginRepoWrite, end: endRepoWrite, busy: () => writing !== null,
              notRunning: () => setRunning(false), read: refreshRepos,
              after: () => { refreshInbox(); refreshTasks(); },
              spell: text => spellHidden(text).text });
  // A task opens from its #id button, the keyboard's way, or a click elsewhere on its row.
  listView.addEventListener("click", ev => {
    const btn = ev.target.closest("button[data-task]");
    const task = btn ? btn.dataset.task : rowOpens(ev);
    if (task !== null) goTo(view, task);
  });
  taskBack.addEventListener("click", backToList);
  document.addEventListener("keydown", ev => {
    if (escGoesBack(ev)) backToList();
  });
  // Closing or reloading the page with an edit's changes unsaved asks, as leaving does.
  window.addEventListener("beforeunload", ev => {
    if (!draftChanged()) return;
    ev.preventDefault();
    ev.returnValue = "";
  });
  taskDetail.addEventListener("click", ev => {
    // A task the detail names opens through the hash, as one from the list does, so
    // the browser's back returns to the task it was opened from. A double-click's second
    // click lands on the task the first opened, so it opens nothing.
    const named = ev.target.closest("button[data-task]");
    if (named) {
      if (ev.detail <= 1) goTo(view, named.dataset.task);
      return;
    }
    // The task read again, through the hash as every open is.
    if (ev.target.closest("button[data-show]")) {
      goTo(view, openTask);
      return;
    }
    const source = ev.target.closest("button[data-source-toggle]");
    if (source) {
      toggleSource(source);
      return;
    }
    if (ev.target.closest("button[data-spec-open]")) {
      toggleSpecEdit(true);
      return;
    }
    if (ev.target.closest("button[data-spec-discard]")) {
      toggleSpecEdit(false);
      return;
    }
    const replyOpen = ev.target.closest("button[data-reply-open]");
    if (replyOpen) toggleReply(replyOpen.dataset.replyOpen, true);
    const replyCancel = ev.target.closest("button[data-reply-cancel]");
    if (replyCancel) toggleReply(replyCancel.dataset.replyCancel, false);
    const planView = ev.target.closest("button[data-plan-view]");
    if (planView) togglePlanView(planView);
    const page = ev.target.closest("button[data-page]");
    if (page) (page.dataset.page === "plans" ? showPlans : showPage)(page);
    const btn = ev.target.closest("button[data-write]");
    // A double-click's second click is no move: one that opened a task, by its parent,
    // a child or a blocker, would otherwise land on whatever move the task drew there.
    if (!btn || ev.detail > 1) return;
    const write = writeFor(btn);
    if (write) runWrite(write);
  });
  taskDetail.addEventListener("input", ev => {
    if (ev.target.matches("textarea[data-note]") && shownTask) {
      noteDraft = { task: String(shownTask.id), text: ev.target.value };
    }
    if (ev.target.matches("textarea[data-reply]")) {
      replyDrafts.set(ev.target.dataset.reply, ev.target.value);
      markWriting();
    }
    if (ev.target.matches("input[data-decision]")) choose(ev.target);
    if (ev.target.matches("input[data-parent]") && drawnIsOpen()) {
      parentDrafts.set(String(shownTask.id), ev.target.value);
      markWriting();
    }
    if (ev.target.matches("[data-spec]") && drawnIsOpen() && specEditOn(shownTask)) {
      specEdit.draft = { ...specEdit.draft, [ev.target.dataset.spec]: ev.target.value };
      markWriting();
    }
    if (ev.target.matches("textarea[data-other]") && drawnIsOpen()
        && approvable(shownTask)) {
      choicesFor(shownTask)[Number(ev.target.dataset.other)].text = ev.target.value;
      markAnswers();
      markWriting();
    }
  });
  flaggedEl.addEventListener("click", ev => {
    const btn = ev.target.closest("button[data-flagged]");
    if (!btn) return;
    if (btn.dataset.flagged === "show") {
      readFlagged(true);
    } else if (btn.dataset.flagged === "all") {
      for (const id of selectAll(flaggedDrawn, newestFlagged())) flaggedSelected.set(id, false);
      for (const box of flaggedEl.querySelectorAll("input[data-flagged-note]")) {
        box.checked = flaggedSelected.has(Number(box.dataset.flaggedNote));
      }
      markFlagged();
    } else if (btn.dataset.flagged === "handle") {
      const write = bulkWrite();
      if (write) runWrite(write);
    }
  });
  flaggedEl.addEventListener("change", ev => {
    const box = ev.target.closest("input[data-flagged-note]");
    if (!box) return;
    const id = Number(box.dataset.flaggedNote);
    if (box.checked) flaggedSelected.set(id, tickMark(id, flaggedDrawn));
    else flaggedSelected.delete(id);
    markFlagged();
  });
  // The page places every scroll itself (`enterTask`): a browser restoring one on back
  // would apply the list's offset to a task, a declined leave's included, before
  // hashchange fires, and clamp it on a task too short to hold it.
  window.history.scrollRestoration = "manual";
  // The first applyHash stamps the entry loaded on; a reload's keeps the index it had.
  window.addEventListener("hashchange", applyHash);
  applyHash();
}
