// SPDX-License-Identifier: Apache-2.0
// What the harness does to the tasks page (tasks-ui/), one named step at a time, as
// scenario.js does to the approval page. Addressed by what the operator sees, so the
// same steps run against the base tree and the branch.

import {
  deferElsewhere, editElsewhere, flagElsewhere, holdNextCreate, landHeldCreate,
  loseNextAnswer, replanElsewhere, startRegistry, stopRegistry,
} from "./tasks-backend.js";

// The list's own hash, set as a link would set it, once the page has followed it. A
// task open takes the whole page, so a step that acts on the list with one open goes
// back to the list first; on a base that drew the task beside the list, the same hash
// leaves it, so both trees take the step from the same place.
async function toList(page, hash = "") {
  await page.evaluate(h => new Promise(done => {
    if (location.hash === h) return done();
    window.addEventListener("hashchange", () => done(), { once: true });
    location.hash = h;
  }), hash);
}

export const STEPS = [
  ["load", async ({ load }) => { await load(); }],
  ["opened from the inbox", async ({ page }) => {
    await page.click('#inbox button[data-task="3"]');
  }],
  ["its earlier plans", async ({ page }) => {
    await page.click('#task-detail button[data-page="plans"]');
  }],
  ["its earlier timeline", async ({ page }) => {
    await page.click('#task-detail button[data-page="history"]');
  }],
  ["a refined task from triage", async ({ page }) => {
    await toList(page);
    await page.click('#inbox button[data-task="6"]');
  }],
  // The hash names the task: a reload reads it again, and back goes to the one before.
  ["reloaded", async ({ page, navigate }) => { await navigate(() => page.reload()); }],
  ["back", async ({ page, navigate }) => { await navigate(() => page.goBack()); }],
  ["a landing task", async ({ page }) => {
    await toList(page);
    await page.click('#inbox button[data-task="5"]');
  }],
  ["all tasks", async ({ page }) => {
    await toList(page);
    await page.click("#view-all");
  }],
  ["repo picked", async ({ page }) => {
    await page.selectOption("#tasks-repo", "dockade");
  }],
  ["open state", async ({ page }) => {
    await page.selectOption("#tasks-state", "open");
  }],
  ["a task opened", async ({ page }) => {
    await page.click('#tasks button[data-task="3"]');
  }],
  ["a task whose plan was superseded", async ({ page }) => {
    await toList(page, "#view=all");
    await page.click('#tasks button[data-task="1"]');
  }],
  ["a task with no plan", async ({ page }) => {
    await toList(page, "#view=all");
    await page.click('#tasks button[data-task="2"]');
  }],
  ["back to All tasks", async ({ page }) => { await toList(page, "#view=all"); }],
  ["search", async ({ page }) => {
    await page.fill("#tasks-search", "back up");
    await page.press("#tasks-search", "Enter");
  }],
  ["a poll interval passes", async ({ clock }) => { await clock(16000); }],
  // The registry stops on its own: the next poll gets the relay's 502, the page says
  // so in one line, the title loses its count, and the polls after it ask this
  // container's `/running` rather than the registry.
  ["the registry stops, a poll passes", async ({ clock }) => {
    stopRegistry();
    await clock(16000);
  }],
  ["another poll interval", async ({ clock }) => { await clock(16000); }],
  ["the page is come back to", async ({ page }) => {
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  }],
  // Started again, the page picks it up at the next poll, with no one touching it.
  ["the registry starts, a poll passes", async ({ clock }) => {
    startRegistry();
    await clock(16000);
  }],
  ["back to the inbox", async ({ page }) => { await page.click("#view-inbox"); }],
  // The operator's writes, each followed by the detail and the inbox drawn again.
  ["a refined task to triage", async ({ page }) => {
    await page.click('#inbox button[data-task="6"]');
  }],
  ["approved", async ({ page }) => {
    await page.click('#task-detail button[data-verb="approve"]');
  }],
  // Another session moves task 8 after the page read it: approving the revision shown
  // is refused, and the page draws the task the refusal carries, saying so.
  ["another refined task", async ({ page }) => {
    await toList(page);
    await page.click('#inbox button[data-task="8"]');
  }],
  ["approved after it changed elsewhere", async ({ page }) => {
    editElsewhere(8);
    await page.click('#task-detail button[data-verb="approve"]');
  }],
  ["sent back", async ({ page }) => {
    await page.click('#task-detail button[data-verb="send back"]');
  }],
  ["a claimed task", async ({ page }) => {
    await toList(page);
    await page.click('#inbox button[data-task="3"]');
  }],
  ["its claim broken", async ({ page }) => {
    await page.click('#task-detail button[data-write="break"]');
  }],
  ["a note added", async ({ page }) => {
    await page.fill("#task-detail textarea[data-note]", "Looked at this.");
    await page.click('#task-detail button[data-write="note"]');
  }],
  // The note lands and its answer is lost: the page reads the task again and finds it.
  ["a note whose answer is lost", async ({ page }) => {
    loseNextAnswer();
    await page.fill("#task-detail textarea[data-note]", "And this.");
    await page.click('#task-detail button[data-write="note"]');
  }],
  ["the task closed", async ({ page }) => {
    await page.click('#task-detail button[data-verb="close"]');
  }],
  // A plan that replaced another opens on what changed, the whole one toggle away.
  ["a re-refined task, on what changed", async ({ page }) => {
    await toList(page);
    await page.click('#inbox button[data-task="6"]');
  }],
  ["the whole plan", async ({ page }) => {
    await page.click("#task-detail button[data-plan-view]");
  }],
  // Another session changes the task while it is read: the poll on coming back to the
  // page says so atop the detail and redraws nothing, until the operator asks.
  ["it changes elsewhere, and the page is come back to", async ({ page }) => {
    editElsewhere(6);
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  }],
  ["read it again", async ({ page }) => {
    await page.click("#task-detail button[data-show]");
  }],
  // The moves on notes, made on a note in the timeline: each says what it came to above
  // the timeline, and the flagged list above the inbox is drawn again after it. Task 3,
  // closed above, is out of the inbox; its notes are marked so in the flagged list.
  ["a closed task's flagged note opened", async ({ page }) => {
    await toList(page);
    await page.click('#flagged button[data-task="3"]');
  }],
  // A task move redraws the flagged rows: reopened, its notes are no longer marked.
  ["reopened", async ({ page }) => {
    await page.click('#task-detail button[data-verb="reopen"]');
  }],
  ["a flagged note hidden", async ({ page }) => {
    await page.click('#task-detail button[data-write="hide"][data-note="13"]');
  }],
  ["and unhidden", async ({ page }) => {
    await page.click('#task-detail button[data-write="hide"][data-note="13"]');
  }],
  // A flagged note already handled is in no list a hide takes it out of.
  ["a handled flagged note hidden", async ({ page }) => {
    await page.click('#task-detail button[data-write="hide"][data-note="10"]');
  }],
  ["a reply opened and written", async ({ page }) => {
    await page.click('#task-detail button[data-reply-open="14"]');
    await page.fill('#task-detail textarea[data-reply="14"]', "Unblocked: see #2.");
  }],
  ["handled with the reply", async ({ page }) => {
    await page.click('#task-detail button[data-write="handle"][data-value="reply"]'
                     + '[data-note="14"]');
  }],
  // A task deferred from the detail: the flagged rows are drawn again, its note marked.
  ["another task opened from the flagged list", async ({ page }) => {
    await toList(page);
    await page.click('#flagged button[data-task="8"]');
  }],
  ["deferred", async ({ page }) => {
    await page.click('#task-detail button[data-verb="defer"]');
  }],
  // "select all shown" leaves out the notes on deferred tasks 7 and 8. The bulk names
  // exactly the notes selected among those drawn, with no task open: the flagged block
  // is part of the list.
  ["all shown selected, the parked ones left out", async ({ page }) => {
    await toList(page);
    await page.click('#flagged button[data-flagged="all"]');
  }],
  // Task 6 is deferred in another tab, which only a poll sees: its note stays ticked
  // and its row unmarked, but the count drops it, and the bulk leaves it out, saying so.
  ["a ticked note's task deferred elsewhere, a poll passes", async ({ clock }) => {
    deferElsewhere(6);
    await clock(16000);
  }],
  // Unticked and ticked again on a row still drawn open: not an informed choice of a
  // parked note, so it stays left out, and the line beside the handle says why.
  ["the left-out note unticked and ticked again", async ({ page }) => {
    await page.uncheck('#flagged input[data-flagged-note="15"]');
    await page.check('#flagged input[data-flagged-note="15"]');
  }],
  ["handled together", async ({ page }) => {
    await page.click('#flagged button[data-flagged="handle"]');
  }],
  // A note flagged and another handled elsewhere: the poll changes the count line and
  // no row, until the operator asks for them.
  ["the list changes elsewhere, a poll passes", async ({ clock }) => {
    flagElsewhere();
    await clock(16000);
  }],
  ["show them", async ({ page }) => {
    await page.click('#flagged button[data-flagged="show"]');
  }],
  // Only notes on deferred tasks are left, so "select all shown" is off; each can still
  // be ticked on its own.
  ["the parked ones ticked on their own and handled, the last", async ({ page }) => {
    await page.check('#flagged input[data-flagged-note="23"]');
    await page.check('#flagged input[data-flagged-note="17"]');
    await page.check('#flagged input[data-flagged-note="15"]');
    await page.click('#flagged button[data-flagged="handle"]');
  }],
  // Un-handled, then replied to again with the answer lost: the page reads the task
  // again and finds the reply's own entry.
  ["task 3 again, from the inbox", async ({ page }) => {
    await page.click('#inbox button[data-task="3"]');
  }],
  ["a note marked not handled", async ({ page }) => {
    await page.click('#task-detail button[data-write="handle"][data-value="false"]'
                     + '[data-note="14"]');
  }],
  // With a reply typed, "mark handled" is off, so the reply is not dropped by it.
  ["a reply typed", async ({ page }) => {
    await page.click('#task-detail button[data-reply-open="14"]');
    await page.fill('#task-detail textarea[data-reply="14"]', "Still unblocked.");
  }],
  ["its answer lost", async ({ page }) => {
    loseNextAnswer();
    await page.click('#task-detail button[data-write="handle"][data-value="reply"]'
                     + '[data-note="14"]');
  }],
  // A plan's decisions, above it, start on the options it assumes: one click approves
  // it as written, and the label said so before it.
  ["a refined task with decisions", async ({ page }) => {
    await toList(page);
    await page.click('#inbox button[data-task="9"]');
  }],
  ["approved as assumed", async ({ page }) => {
    await page.click('#task-detail button[data-verb="approve"]');
  }],
  // Another option relabels the approve, which then sends the task back, and the send
  // back, which sends no answers.
  ["another task with decisions", async ({ page }) => {
    await toList(page);
    await page.click('#inbox button[data-task="10"]');
  }],
  ["another option picked", async ({ page }) => {
    await page.check('#task-detail input[data-decision="0"][data-option="1"]');
  }],
  // "Other" text left where another option is then picked stays, marked not sent.
  ["an other text typed, then left", async ({ page }) => {
    await page.check('#task-detail input[data-decision="1"][data-option="other"]');
    await page.fill('#task-detail textarea[data-other="1"]', "Weekly\u200b only.");
    await page.uncheck('#task-detail input[data-decision="1"][data-option="other"]');
  }],
  // A multi-select with nothing picked is no answer: no approve is drawn at all.
  ["a multi-select emptied", async ({ page }) => {
    await page.uncheck('#task-detail input[data-decision="1"][data-option="0"]');
    await page.uncheck('#task-detail input[data-decision="1"][data-option="1"]');
  }],
  ["and answered again", async ({ page }) => {
    await page.check('#task-detail input[data-decision="1"][data-option="2"]');
  }],
  // Refused for a revision another session moved: the choices survive the redraw.
  ["approved after it changed elsewhere, the choices kept", async ({ page }) => {
    editElsewhere(10);
    await page.click('#task-detail button[data-verb="approve"]');
  }],
  // Sent back with its answers, and the answer lost: read back, and found by them.
  ["approved with these answers, the answer lost", async ({ page }) => {
    loseNextAnswer();
    await page.click('#task-detail button[data-verb="approve"]');
  }],
  // Another plan, read through the inbox: the decisions say it is another.
  ["a new plan handed in, the task read again", async ({ page }) => {
    replanElsewhere(10, 7);
    await toList(page);
    await page.click('#inbox button[data-task="10"]');
  }],
  // The first choice in it spends the line, which keeps its space.
  ["other picked and written", async ({ page }) => {
    await page.check('#task-detail input[data-decision="0"][data-option="other"]');
    await page.fill('#task-detail textarea[data-other="0"]', "Hold, for the llm ones.");
  }],
  // A plan handed in while the page reads the task: the poll says it changed, and
  // "read it again" draws the new plan with the "other" text it reset.
  ["another plan handed in, a poll passes", async ({ clock }) => {
    replanElsewhere(10, 8);
    await clock(16000);
  }],
  ["read it again, the new plan", async ({ page }) => {
    await page.click("#task-detail button[data-show]");
  }],
  // The plans it replaced, each with its decisions as text: no input on any.
  ["its earlier plans, their decisions as text", async ({ page }) => {
    await page.click('#task-detail button[data-page="plans"]');
  }],
  // The spec edit. Task 11 is refined at its plan's revision, so no lag notice yet; the
  // form opens with nothing to save.
  ["a refined task at its plan's revision", async ({ page }) => {
    await page.evaluate(() => { location.hash = "#task=11"; });
  }],
  ["its spec opened for editing", async ({ page }) => {
    await page.click("#task-detail button[data-spec-open]");
  }],
  // A pasted zero-width space is spelled in what will be stored, and the moves wait on
  // the edit: approve, clicked, sends nothing.
  ["a zero-width space pasted into its body", async ({ page }) => {
    await page.fill('#task-detail textarea[data-spec="body"]',
                    "Rotate the keys\u200b in place.");
  }],
  ["approve clicked while the edit holds it", async ({ page }) => {
    await page.click('#task-detail button[data-verb="approve"]', { force: true });
  }],
  ["the edit discarded", async ({ page }) => {
    await page.click("#task-detail button[data-spec-discard]");
  }],
  // Saved, the plan lags the task, and the plan says so.
  ["the body edited again and saved", async ({ page }) => {
    await page.click("#task-detail button[data-spec-open]");
    await page.fill('#task-detail textarea[data-spec="body"]',
                    "Rotate the keys\u200b in place.");
    await page.click('#task-detail button[data-write="edit"]');
  }],
  // Another session edits the body under a draft that changed the title: the save is
  // refused, and the draft keeps its title and takes the new body.
  ["the title edited while the body changes elsewhere", async ({ page }) => {
    await page.click("#task-detail button[data-spec-open]");
    await page.fill('#task-detail input[data-spec="title"]', "Rotate the keys in place");
    editElsewhere(11);
  }],
  ["saved, and refused for the revision", async ({ page }) => {
    await page.click('#task-detail button[data-write="edit"]');
  }],
  ["saved again: the title alone", async ({ page }) => {
    await page.click('#task-detail button[data-write="edit"]');
  }],
  // A refusal's line, then a poll's newer one over it: discarding the form redraws the
  // read already drawn, which keeps the poll's line rather than bring the refusal back.
  ["the title edited again and refused for the revision", async ({ page }) => {
    await page.click("#task-detail button[data-spec-open]");
    await page.fill('#task-detail input[data-spec="title"]', "Rotate the keys, at last");
    editElsewhere(11);
    await page.click('#task-detail button[data-write="edit"]');
  }],
  ["it changes elsewhere again, a poll passes", async ({ clock }) => {
    editElsewhere(11);
    await clock(16000);
  }],
  ["the edit discarded: the poll's line stays", async ({ page }) => {
    await page.click("#task-detail button[data-spec-discard]");
  }],
  // Task 12's body has CR LF line ends, which its textarea turns to LF: saved after the
  // other fields change, the request carries no body. Its claim stays, as the save says.
  ["a claimed task whose body has CR LF line ends", async ({ page }) => {
    await page.evaluate(() => { location.hash = "#task=12"; });
  }],
  ["its title, severity and blockers changed, one blocker not an id", async ({ page }) => {
    await page.click("#task-detail button[data-spec-open]");
    await page.fill('#task-detail input[data-spec="title"]', "Line ends, kept");
    await page.selectOption('#task-detail select[data-spec="severity"]', "");
    await page.fill('#task-detail input[data-spec="blockers"]', "#2, x3");
  }],
  ["the blocker mended and saved, the body not sent", async ({ page }) => {
    await page.fill('#task-detail input[data-spec="blockers"]', "#2, 3");
    await page.click('#task-detail button[data-write="edit"]');
  }],
  // Task 20's body is thousands of characters long: markdown typed key by key at its
  // end, then approve, clicked while the edit holds it, sends nothing.
  ["a refined task with a long body", async ({ page }) => {
    await page.evaluate(() => { location.hash = "#task=20"; });
  }],
  ["its long spec opened for editing", async ({ page }) => {
    await page.click("#task-detail button[data-spec-open]");
  }],
  ["markdown typed at the long body's end", async ({ page }) => {
    await page.click('#task-detail textarea[data-spec="body"]');
    await page.keyboard.press("Control+End");
    await page.keyboard.type("\n\n13. **Point 13.** With `code`.\n\n> A quote.\n");
  }],
  ["approve clicked while the long edit holds it", async ({ page }) => {
    await page.click('#task-detail button[data-verb="approve"]', { force: true });
  }],
  ["the long edit discarded", async ({ page }) => {
    await page.click("#task-detail button[data-spec-discard]");
  }],
  // Task 18 is written in markdown, drawn as such and then as typed. A tree that draws
  // no switch takes the two steps as doing nothing, so the rest still compares.
  ["a task written in markdown", async ({ page }) => {
    await page.evaluate(() => { location.hash = "#task=18"; });
  }],
  ["its source shown", async ({ page }) => { await toggleSource(page); }],
  ["drawn again", async ({ page }) => { await toggleSource(page); }],
  // A new task, from All tasks. A tree with no create form takes these steps as doing
  // nothing, as it does the markdown switch, so the rest still compares.
  ["a new task's form, a zero-width space in its title", async ({ page }) => {
    await toList(page, "#view=all");
    await ifCreate(page, async () => {
      await page.click("#create-open");
      await page.fill('#create-form input[data-create-field="title"]', "Rotate\u200b keys");
    });
  }],
  ["All tasks left and come back to, the draft kept", async ({ page }) => {
    await page.click("#view-inbox");
    await page.click("#view-all");
  }],
  // Task 19 is in another repo, so the registry refuses it as a parent; the draft stays.
  ["a parent in another repo, refused", async ({ page }) => {
    await ifCreate(page, async () => {
      await page.fill('#create-form textarea[data-create-field="body"]', "In place.");
      await page.fill('#create-form input[data-create-field="parent"]', "#19");
      await page.click('#create-form button[data-write="create"]');
    });
  }],
  // Landed: the page opens it, task 40, as its row would.
  ["with a parent and a blocker, created and opened", async ({ page }) => {
    await ifCreate(page, async () => {
      await page.fill('#create-form input[data-create-field="parent"]', "#3");
      await page.fill('#create-form input[data-create-field="blockers"]', "#2");
      await page.click('#create-form button[data-write="create"]');
    });
  }],
  // It lands and its answer is lost: the repo read back past the bound finds task 41.
  ["a create whose answer is lost, found and opened", async ({ page }) => {
    await toList(page, "#view=all");
    await ifCreate(page, async () => {
      await page.click("#create-open");
      await page.fill('#create-form input[data-create-field="title"]', "Lost one");
      loseNextAnswer();
      await page.click('#create-form button[data-write="create"]');
    });
  }],
  // Lost before it lands: not found, and the create now says it may make a second.
  ["a create not found yet", async ({ page }) => {
    await toList(page, "#view=all");
    await ifCreate(page, async () => {
      await page.click("#create-open");
      await page.fill('#create-form input[data-create-field="title"]', "Held one");
      holdNextCreate();
      await page.click('#create-form button[data-write="create"]');
    });
  }],
  // It lands, and the title is edited after the send: "look again" checks what was
  // sent, finds task 42 and opens it, and the edited draft stays.
  ["it lands, its title is edited, and it is looked for again", async ({ page }) => {
    await ifCreate(page, async () => {
      landHeldCreate();
      await page.fill('#create-form input[data-create-field="title"]', "Held one, edited");
      await page.click('#create-form button[data-create="look-again"]');
    });
  }],
  ["back to All tasks: the edited draft and what it came to", async ({ page }) => {
    await toList(page, "#view=all");
  }],
  // A reparent names the revision shown; none removes the parent.
  ["task 3, by its URL", async ({ page }) => {
    await page.evaluate(() => { location.hash = "#task=3"; });
  }],
  ["its parent set to #2", async ({ page }) => {
    await ifParent(page, async () => {
      await page.fill("#task-detail input[data-parent]", "#2");
      await page.click('#task-detail button[data-write="reparent"]');
    });
  }],
  ["and removed", async ({ page }) => {
    await ifParent(page, async () => {
      await page.fill("#task-detail input[data-parent]", "");
      await page.click('#task-detail button[data-write="reparent"]');
    });
  }],
  // Task 40 is task 3's child, so making it task 3's parent would loop: refused.
  ["a parent below it, refused", async ({ page }) => {
    await ifParent(page, async () => {
      await page.fill("#task-detail input[data-parent]", "#40");
      await page.click('#task-detail button[data-write="reparent"]');
    });
  }],
  // Changed elsewhere since it was read: refused for the revision, and drawn as it is.
  ["a reparent after it changed elsewhere", async ({ page }) => {
    await ifParent(page, async () => {
      editElsewhere(3);
      await page.fill("#task-detail input[data-parent]", "#1");
      await page.click('#task-detail button[data-write="reparent"]');
    });
  }],
  // The Repos view, last: a tree without it takes these steps as doing nothing, as it
  // does the create's, and the steps before still compare. Reloaded on its URL, the
  // page asks /running before it reads the list.
  ["Repos, by its URL, reloaded", async ({ page, navigate }) => {
    await page.evaluate(() => { location.hash = "#view=repos"; });
    await navigate(() => page.reload());
  }],
  ["a repo added", async ({ page }) => {
    await ifRepos(page, async () => {
      await page.fill("#repo-add-name", "widgets");
      await page.click("#repo-add");
    });
  }],
  ["the same name added again, refused", async ({ page }) => {
    await ifRepos(page, async () => {
      await page.fill("#repo-add-name", "widgets");
      await page.click("#repo-add");
    });
  }],
  // The label names the outcome, and the line under it what a rename asks of the
  // operator; once landed, the outcome says it again.
  ["renamed", async ({ page }) => {
    await ifRepos(page, async () => {
      await page.fill('#repos input[data-repo-rename="widgets"]', "gadgets");
      await page.click('#repos button[data-repo-write="rename"][data-repo="widgets"]');
    });
  }],
  ["its remote set", async ({ page }) => {
    await ifRepos(page, async () => {
      await page.fill('#repos input[data-repo-remote="gadgets"]', "github.com/acme/gadgets");
      await page.click('#repos button[data-repo-write="remote"][data-repo="gadgets"]');
    });
  }],
  ["and removed", async ({ page }) => {
    await ifRepos(page, async () => {
      await page.click('#repos button[data-repo-write="remove"][data-repo="gadgets"]');
    });
  }],
  ["retired", async ({ page }) => {
    await ifRepos(page, async () => {
      await page.click('#repos button[data-repo-write="retire"][data-repo="gadgets"]');
    });
  }],
  ["and brought back", async ({ page }) => {
    await ifRepos(page, async () => {
      await page.click('#repos button[data-repo-write="retire"][data-repo="gadgets"]');
    });
  }],
  // It lands and its answer is lost: the list read again shows it, so it is found.
  ["an add whose answer is lost, found", async ({ page }) => {
    await ifRepos(page, async () => {
      await page.fill("#repo-add-name", "sprockets");
      loseNextAnswer();
      await page.click("#repo-add");
    });
  }],
  ["All tasks, its open tasks", async ({ page }) => {
    await page.click("#view-all");
    await page.selectOption("#tasks-state", "open");
  }],
  // The repo All tasks shows, renamed to a name that sorts after the others: the select
  // follows it, rather than falling to the first repo, and its tasks are the same.
  ["that repo renamed in Repos", async ({ page }) => {
    await ifRepos(page, async () => {
      await page.click("#view-repos");
      await page.fill('#repos input[data-repo-rename="dockade"]', "tools");
      await page.click('#repos button[data-repo-write="rename"][data-repo="dockade"]');
    });
  }],
  ["All tasks: the new name chosen, the same tasks", async ({ page }) => {
    await page.click("#view-all");
  }],
];

async function ifRepos(page, act) {
  if (await page.$("#view-repos")) await act();
}
async function ifCreate(page, act) {
  if (await page.$("#create-open")) await act();
}
async function ifParent(page, act) {
  if (await page.$("#task-detail input[data-parent]")) await act();
}

async function toggleSource(page) {
  const toggle = await page.$("#task-detail button[data-source-toggle]");
  if (toggle) await toggle.click();
}
