// SPDX-License-Identifier: Apache-2.0
// What the harness does to the tasks page (tasks-ui/), one named step at a time, as
// scenario.js does to the approval page. Addressed by what the operator sees, so the
// same steps run against the base tree and the branch.

import {
  editElsewhere, loseNextAnswer, startRegistry, stopRegistry,
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
];
