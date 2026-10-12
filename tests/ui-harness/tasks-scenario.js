// SPDX-License-Identifier: Apache-2.0
// What the harness does to the tasks page (tasks-ui/), one named step at a time, as
// scenario.js does to the approval page. Addressed by what the operator sees, so the
// same steps run against the base tree and the branch.

import { startRegistry, stopRegistry } from "./tasks-backend.js";

export const STEPS = [
  ["load", async ({ load }) => { await load(); }],
  ["repo picked", async ({ page }) => {
    await page.selectOption("#tasks-repo", "dockade");
  }],
  ["open state", async ({ page }) => {
    await page.selectOption("#tasks-state", "open");
  }],
  ["a task opened", async ({ page }) => {
    await page.click('#tasks button[data-task="3"]');
  }],
  ["its history", async ({ page }) => {
    await page.click('#task-detail button[data-page="history"]');
  }],
  ["task closed", async ({ page }) => {
    await page.click("#task-detail button[data-close]");
  }],
  ["opened from the flagged list", async ({ page }) => {
    await page.click('#flagged button[data-task="3"]');
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
];
