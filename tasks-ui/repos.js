// SPDX-License-Identifier: Apache-2.0
/* The Repos view: the repo list the operator keeps (task-registry/DESIGN.md, "Repos are
 * a list the operator keeps"), and its four writes, which the relay passes
 * (tasks-ui/app.py): add a repo, rename one, set or remove its remote, retire one or
 * bring it back. tasks.js imports this and hands it what it needs of the page
 * (`repoHooks`): the page's one write gate, not running on a 502, and the list's read,
 * which draws this view and All tasks' select both. This imports nothing of tasks.js.
 * Why a lost answer is read back rather than sent again is tasks-ui/DESIGN.md, "A
 * write whose answer was lost".
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_tasks_ui_js.py).
 */
import { readAnswer, writeOutcome } from "./moves.js";

// ── the writes ───────────────────────────────────────────────────────────────
// A write is `{ kind, repo, send, verb }`: `repo` the name it acts on as the view listed
// it, null for an add, whose name rides in its body; `send` the body. A set or removed
// remote carries the remote it replaces (`was`). Each request is null when there is
// nothing to send; what the registry takes of a name or a remote is its own to say, in
// its refusal, so no second copy of its rules is kept here.

// A new repo, from the add form: its name, and its remote or null for none.
export function addRequest(name, remote) {
  if (typeof name !== "string" || !name) return null;
  return { name, remote: typeof remote === "string" && remote ? remote : null };
}

// A repo renamed: null while no name is typed, or the one it has.
export function renameRequest(repo, text) {
  if (!repo || typeof repo !== "object" || typeof text !== "string" || !text
      || text === repo.name) return null;
  return { new_name: text };
}

// A remote set: null while none is typed, or the one it has. Removing one sends null,
// never an absent field, which the registry refuses (`SetRemote`).
export function remoteRequest(repo, text) {
  if (!repo || typeof repo !== "object" || typeof text !== "string" || !text
      || text === repo.remote) return null;
  return { remote: text };
}
export function removeRemoteRequest(repo) {
  if (typeof repo?.remote !== "string") return null;
  return { remote: null };
}

// Retired, or brought back: the other of what the repo is.
export function retireRequest(repo) {
  if (!repo || typeof repo !== "object") return null;
  return { retired: !repo.retired };
}

const JSON_TYPE = { "content-type": "application/json" };

// Send one write. Each names its path in its own `fetch`, as a POST, where the relay
// test reads both; a name listed is the registry's, encoded all the same.
export function sendRepoWrite(write) {
  const path = encodeURIComponent(write.repo);
  if (write.kind === "add") {
    return fetch(`/api/repos`, { method: "POST", headers: JSON_TYPE,
      body: JSON.stringify(write.send) });
  }
  if (write.kind === "rename") {
    return fetch(`/api/repos/${path}/rename`, { method: "POST", headers: JSON_TYPE,
      body: JSON.stringify(write.send) });
  }
  if (write.kind === "remote") {
    return fetch(`/api/repos/${path}/remote`, { method: "POST", headers: JSON_TYPE,
      body: JSON.stringify(write.send) });
  }
  if (write.kind === "retire") {
    return fetch(`/api/repos/${path}/retire`, { method: "POST", headers: JSON_TYPE,
      body: JSON.stringify(write.send) });
  }
  throw new Error(`no such write: ${write.kind}`);
}

// Whether a write whose answer was lost landed, from the repo list read after it, or
// `null` when that read failed. Repos carry no revision, so it is decided by the named
// repo's entry alone, both names for a rename, whatever else the list gained since:
//   found           it reads as sent: the repo added with the remote sent, active; the
//                   old name gone and the new one listed; the remote or the retired
//                   flag the one sent;
//   not-seen        it reads as drawn: no such repo; the old name listed and the new
//                   not; the remote it replaced; the other flag. It may still land, and
//                   sending it again is safe: a landed add or rename refuses a second,
//                   and a remote or a retire sets a value;
//   changed         neither: another remote, both names or neither, a third remote, the
//                   repo gone;
//   could-not-check the list could not be read.
export function checkRepoWrite(write, listed) {
  if (!Array.isArray(listed)) return "could-not-check";
  const named = name => listed.find(r => r && typeof r === "object" && r.name === name)
    || null;
  const send = write.send;
  if (write.kind === "add") {
    const now = named(send.name);
    if (!now) return "not-seen";
    return (now.remote ?? null) === send.remote && !now.retired ? "found" : "changed";
  }
  if (write.kind === "rename") {
    const from = named(write.repo), to = named(send.new_name);
    if (!from && to) return "found";
    return from && !to ? "not-seen" : "changed";
  }
  const now = named(write.repo);
  if (!now) return "changed";
  if (write.kind === "remote") {
    const remote = now.remote ?? null;
    if (remote === send.remote) return "found";
    return remote === write.was ? "not-seen" : "changed";
  }
  if (write.kind === "retire") {
    return Boolean(now.retired) === send.retired ? "found" : "not-seen";
  }
  return "changed";
}

// The repo All tasks' select keeps after a read of the list: the one chosen while it is
// listed; the new name of a rename the page made of it, once the list shows it landed;
// else null, gone, and the list starts again rather than page another repo by its cursor.
export function followedRepo(chosen, renamed, listed) {
  const names = new Set((Array.isArray(listed) ? listed : [])
    .map(r => (r && typeof r === "object" ? r.name : null)));
  if (names.has(chosen)) return chosen;
  if (renamed && renamed.from === chosen && names.has(renamed.to)) return renamed.to;
  return null;
}

// ── what the view says ───────────────────────────────────────────────────────

// What a rename asks of the operator, before it is pressed and once it lands: the
// repo's agents name it from its instructions file (`operator_moves.rename_repo`), which
// lives in that repo, and is `CLAUDE.md` in some and not in others.
export function renameWarning(to) {
  const name = to || "the new name";
  return `The repo's instructions file must name ${name} before its agents can write `
    + "again.";
}

// Each control's label names its outcome, which is the confirmation: no write here asks
// one. A wrong add is renamed or retired, since no repo is ever removed.
export function addLabel(send) {
  if (!send) return "add a repo";
  return `add ${send.name}, ${send.remote === null ? "no remote" : `remote ${send.remote}`}`;
}
export function renameLabel(repo, send) {
  return send ? `rename ${repo.name} to ${send.new_name}` : `rename ${repo.name}`;
}
export function remoteLabel(repo, send) {
  return send ? `set ${repo.name}'s remote to ${send.remote}`
    : `set ${repo.name}'s remote`;
}
export function removeRemoteLabel(repo) {
  return `remove ${repo.name}'s remote ${repo.remote}`;
}
export function retireLabel(repo) {
  return repo.retired ? `bring ${repo.name} back` : `retire ${repo.name}: no new tasks`;
}

// What a write came to, in a line, and whether it warns: `writeOutcome`'s word, or for
// an answer that was lost, `checkRepoWrite`'s. `detail` is the refusal's own reason. A
// rename that landed says what it asks of the operator.
export function repoOutcomeText(write, outcome, detail) {
  const verb = String(write && write.verb);
  const name = verb.charAt(0).toUpperCase() + verb.slice(1);
  const renamed = write && write.kind === "rename" && ["landed", "found"].includes(outcome)
    ? ` ${renameWarning(write.send.new_name)}` : "";
  switch (outcome) {
    case "landed":
      return { text: `${name}: done.${renamed}`, warn: renamed !== "" };
    case "found":
      return { text: `${name}: no answer came, but the repo list shows it landed.`
                     + renamed, warn: renamed !== "" };
    case "refused":
      return { text: `${name}: refused. ${String(detail ?? "")}`.trim(), warn: true };
    case "not-running":
      return { text: `${name}: not sent. The task registry is not running.`, warn: true };
    case "not-seen":
      return { text: `${name}: no answer came, and the repo list does not show it yet. `
                     + "It may still land; sending it again is safe, since "
                     + (["add", "rename"].includes(write && write.kind)
                       ? "a second would be refused." : "it sets the same value."),
               warn: true };
    case "changed":
      return { text: `${name}: no answer came, and the repo list has changed, but not as `
                     + "this would have changed it. This is the list as it is now.",
               warn: true };
    case "could-not-check":
      return { text: `${name}: no answer came, and the repo list could not be read again, `
                     + "so whether it landed is not known. Open Repos again to read it "
                     + "before trying again.", warn: true };
    default:
      return { text: `${name}: no answer came, so whether it landed is not known. Open `
                     + "Repos again to read it before trying again.", warn: true };
  }
}

// ── the DOM ─────────────────────────────────────────────────────────────────

let reposView, reposBody, reposEmpty, repoAddName, repoAddRemote, repoAdd, reposOutcomeEl;

// What tasks.js lends the view (`repoHooks`): `begin(write)`, true when the page's one
// write gate let it through, and `end()` to open it again; `busy()`, whether a write
// is out; `notRunning()`, for the relay's 502; `read(renamed)`, the list read and drawn,
// here and in All tasks' select, which it returns, or null; `after()`, the page's other
// reads after a write; `spell(text)`, a text with its hidden characters spelled.
let hooks = null;
// The text typed in each row, by its repo's name: kept across draws until its write
// lands, so a poll or another write's redraw drops nothing typed.
let drafts = new Map();
let drawnRepos = [];       // the list the rows were drawn from
let repoOutcome = null;    // what the last write came to, above the list

export function repoHooks(given) { hooks = given; }

function busy() {
  return Boolean(hooks) && hooks.busy();
}

function draftOf(name) {
  if (!drafts.has(name)) drafts.set(name, { rename: "", remote: "" });
  return drafts.get(name);
}

function drawnRepo(name) {
  return drawnRepos.find(r => r && typeof r === "object" && r.name === name) || null;
}

// The rows, one per repo as the list has them, each control holding what was typed in it
// and every one off while a write is out.
export function renderRepos(list) {
  if (!reposBody) return;
  drawnRepos = (Array.isArray(list) ? list : []).filter(r => r && typeof r === "object");
  reposBody.replaceChildren();
  for (const repo of drawnRepos) reposBody.appendChild(repoRow(repo));
  reposEmpty.hidden = drawnRepos.length > 0;
  markRepos();
}

function repoCell(tr) {
  const td = document.createElement("td");
  tr.appendChild(td);
  return td;
}

function repoButton(kind, name) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.dataset.repoWrite = kind;
  btn.dataset.repo = name;
  return btn;
}

function repoInput(kind, name, placeholder) {
  const input = document.createElement("input");
  input.type = "text";
  input.dataset[kind] = name;
  input.placeholder = placeholder;
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("aria-label", `${kind === "repoRename" ? "rename" : "remote of"} ${name}`);
  input.value = draftOf(name)[kind === "repoRename" ? "rename" : "remote"];
  return input;
}

function repoRow(repo) {
  const tr = document.createElement("tr");
  tr.dataset.repoRow = repo.name;
  const name = repoCell(tr);
  name.textContent = repo.name;
  if (repo.retired) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = "retired";
    name.append(" ", tag);
  }
  const remote = repoCell(tr);
  remote.className = "remote";
  remote.textContent = typeof repo.remote === "string" ? repo.remote : "no remote";
  const rename = repoCell(tr);
  const renameRow = document.createElement("div");
  renameRow.className = "moves";
  renameRow.append(repoInput("repoRename", repo.name, "new name"),
                   repoButton("rename", repo.name));
  const warn = document.createElement("div");
  warn.className = "empty warn repo-warn";
  warn.dataset.repoWarn = repo.name;
  rename.append(renameRow, warn);
  const setRemote = repoCell(tr);
  const remoteRow = document.createElement("div");
  remoteRow.className = "moves";
  remoteRow.append(repoInput("repoRemote", repo.name, "host/owner/repo"),
                   repoButton("remote", repo.name));
  if (typeof repo.remote === "string") remoteRow.appendChild(repoButton("remove", repo.name));
  setRemote.appendChild(remoteRow);
  repoCell(tr).appendChild(repoButton("retire", repo.name));
  return tr;
}

// The write a control asks for, as of the list drawn and what is typed.
function repoWriteFor(btn) {
  const kind = btn.dataset.repoWrite;
  if (kind === "add") {
    const send = addRequest(repoAddName.value, repoAddRemote.value);
    return send && { kind, repo: null, send, verb: addLabel(send) };
  }
  const repo = drawnRepo(btn.dataset.repo);
  if (!repo) return null;
  const draft = draftOf(repo.name);
  if (kind === "rename") {
    const send = renameRequest(repo, draft.rename);
    return send && { kind, repo: repo.name, send, verb: renameLabel(repo, send) };
  }
  if (kind === "remote") {
    const send = remoteRequest(repo, draft.remote);
    return send && { kind, repo: repo.name, send, was: repo.remote ?? null,
                     verb: remoteLabel(repo, send) };
  }
  if (kind === "remove") {
    const send = removeRemoteRequest(repo);
    return send && { kind: "remote", repo: repo.name, send, was: repo.remote,
                     verb: removeRemoteLabel(repo) };
  }
  if (kind === "retire") {
    const send = retireRequest(repo);
    return send && { kind, repo: repo.name, send, verb: retireLabel(repo) };
  }
  return null;
}

// Every control's label and whether it is on, redrawn in place as the operator types:
// off while any write is out, or while it has nothing to send. Rename says what it asks
// of the operator while one is typed.
export function markRepos() {
  if (!reposView) return;
  const out = busy();
  for (const el of reposView.querySelectorAll("input")) el.disabled = out;
  const add = addRequest(repoAddName.value, repoAddRemote.value);
  repoAdd.textContent = addLabel(add);
  repoAdd.disabled = out || !add;
  for (const btn of reposBody.querySelectorAll("button[data-repo-write]")) {
    const write = repoWriteFor(btn);
    const repo = drawnRepo(btn.dataset.repo);
    const kind = btn.dataset.repoWrite;
    btn.textContent = write ? write.verb
      : kind === "rename" ? renameLabel(repo, null) : remoteLabel(repo, null);
    btn.disabled = out || !write;
  }
  for (const el of reposBody.querySelectorAll("[data-repo-warn]")) {
    const typed = draftOf(el.dataset.repoWarn).rename;
    const shown = renameRequest(drawnRepo(el.dataset.repoWarn), typed) !== null;
    el.textContent = shown ? renameWarning(typed) : "";
    el.hidden = !shown;
  }
  reposOutcomeEl.className = repoOutcome && repoOutcome.warn
    ? "repo-outcome empty warn" : "repo-outcome empty";
  reposOutcomeEl.textContent = repoOutcome ? repoOutcome.text : "";
}

// A landed write's typed text goes: the add form's, a renamed repo's row, a remote set.
function dropTyped(write) {
  if (write.kind === "add") {
    repoAddName.value = "";
    repoAddRemote.value = "";
  } else if (write.kind === "rename") {
    drafts.delete(write.repo);
  } else if (write.kind === "remote" && write.send.remote !== null) {
    draftOf(write.repo).remote = "";
  }
}

// One write, under the page's one gate, then the list as the registry has it after,
// read and drawn while every control is still off. A lost answer is decided from that
// read (`checkRepoWrite`), never sent again; a rename of All tasks' repo that it shows
// landed takes the select with it (`followedRepo`). Returns what it came to, drawn above
// the list; exported for its node test.
export async function runRepoWrite(write) {
  if (!hooks || !hooks.begin(write)) return;
  let answer = null;
  try {
    answer = await readAnswer(await sendRepoWrite(write));
  } catch (e) {
    answer = null;
  }
  let result = writeOutcome(answer);
  if (result === "not-running") hooks.notRunning();
  const renamed = write.kind === "rename" ? { from: write.repo, to: write.send.new_name }
    : null;
  const read = result === "not-running" ? null : await hooks.read(renamed);
  if (result === "unknown") result = checkRepoWrite(write, read);
  if (result === "landed" || result === "found") dropTyped(write);
  const detail = answer && answer.body && typeof answer.body === "object"
    ? answer.body.detail : answer && answer.text;
  // The refusal's reason quotes what was typed, so it is spelled as every other text the
  // page draws from the registry (`spell`, tasks.js's `spellHidden`).
  repoOutcome = repoOutcomeText(write, result, hooks.spell(String(detail ?? "")));
  hooks.end();
  if (result !== "not-running") hooks.after();
  return repoOutcome;
}

export function mountRepos() {
  reposView = document.getElementById("repos-view");
  reposBody = document.getElementById("repos");
  reposEmpty = document.getElementById("repos-empty");
  repoAddName = document.getElementById("repo-add-name");
  repoAddRemote = document.getElementById("repo-add-remote");
  repoAdd = document.getElementById("repo-add");
  reposOutcomeEl = document.getElementById("repos-outcome");

  reposView.addEventListener("input", ev => {
    const el = ev.target;
    if (el.dataset.repoRename !== undefined) draftOf(el.dataset.repoRename).rename = el.value;
    if (el.dataset.repoRemote !== undefined) draftOf(el.dataset.repoRemote).remote = el.value;
    markRepos();
  });
  reposView.addEventListener("click", ev => {
    const btn = ev.target.closest("button[data-repo-write]");
    if (!btn || busy() || ev.detail > 1) return;
    const write = repoWriteFor(btn);
    if (write) runRepoWrite(write);
  });
  markRepos();
}
