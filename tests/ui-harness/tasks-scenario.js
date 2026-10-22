// SPDX-License-Identifier: Apache-2.0
// What the harness does to the tasks page (tasks-ui/), one named step at a time, as
// scenario.js does to the approval page. Addressed by what the operator sees, so the
// same steps run against the base tree and the branch.

import {
  deferElsewhere, editElsewhere, flagElsewhere, loseNextAnswer, startRegistry,
  stopRegistry,
} from "./tasks-backend.js";

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
    await page.click('#inbox button[data-task="6"]');
  }],
  // The hash names the task: a reload reads it again, and back goes to the one before.
  ["reloaded", async ({ page }) => { await page.reload(); }],
  ["back", async ({ page }) => { await page.goBack(); }],
  ["a landing task", async ({ page }) => {
    await page.click('#inbox button[data-task="5"]');
  }],
  ["all tasks", async ({ page }) => { await page.click("#view-all"); }],
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
    await page.click('#tasks button[data-task="1"]');
  }],
  ["a task with no plan", async ({ page }) => {
    await page.click('#tasks button[data-task="2"]');
  }],
  ["task closed", async ({ page }) => {
    await page.click("#task-detail button[data-close]");
  }],
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
  ["closed", async ({ page }) => {
    await page.click('#task-detail button[data-verb="close"]');
  }],
  // A plan that replaced another opens on what changed, the whole one toggle away.
  ["a re-refined task, on what changed", async ({ page }) => {
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
  ["show it", async ({ page }) => {
    await page.click("#task-detail button[data-show]");
  }],
  // The moves on notes, made on a note in the timeline: each says what it came to above
  // the timeline, and the flagged list above the inbox is drawn again after it. Task 3,
  // closed above, is out of the inbox; its notes are marked so in the flagged list.
  ["a closed task's flagged note opened", async ({ page }) => {
    await page.click('#flagged button[data-task="3"]');
  }],
  // A task move redraws the flagged rows: reopened, its notes are no longer marked.
  ["reopened", async ({ page }) => {
    await page.click('#task-detail button[data-verb="reopen"]');
  }],
  ["a flagged note hidden", async ({ page }) => {
    await page.click('#task-detail button[data-write="hide"][data-note="13"]');
  }],
  ["and shown again", async ({ page }) => {
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
    await page.click('#flagged button[data-task="8"]');
  }],
  ["deferred", async ({ page }) => {
    await page.click('#task-detail button[data-verb="defer"]');
  }],
  ["task 3 again", async ({ page }) => {
    await page.click('#flagged button[data-task="3"]');
  }],
  // "select all shown" leaves out the notes on deferred tasks 7 and 8. The bulk names
  // exactly the notes selected among those drawn, and the open task, which one of them
  // is on, is drawn again with it.
  ["all shown selected, the parked ones left out", async ({ page }) => {
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
];
