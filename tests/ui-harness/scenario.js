// SPDX-License-Identifier: Apache-2.0
// What the harness does to the page, one named step at a time; the DOM is recorded
// after each. A step addresses the page by what an operator sees (ids, roles, the
// card's classes) and never by a module's internals, so the same steps run against
// the base tree and the branch.
//
// A PR that moves a surface out of `start()` adds the steps that exercise it here,
// in the PR that moves it.

import { HOLDS } from "./backend.js";

const egressCard = page => page.locator("#pending .card:not(.tool)");
const toolCard = page => page.locator("#pending .card.tool");

export const STEPS = [
  ["load", async ({ load }) => { await load(); }],

  ["audit tab", async ({ page }) => { await page.click("#tab-audit"); }],
  ["policy tab", async ({ page }) => { await page.click("#tab-policy"); }],
  ["tools tab", async ({ page }) => { await page.click("#tab-tools"); }],
  ["approvals tab", async ({ page }) => { await page.click("#tab-approvals"); }],

  ["holds arrive", async ({ push }) => { await push(HOLDS); }],
  // One tick, so the countdowns draw.
  ["a second passes", async ({ clock }) => { await clock(1000); }],

  ["persist confirm opens", async ({ page }) => {
    await egressCard(page).locator('button[data-action="allow_persist"]').click();
  }],
  ["persist confirm, wildcard picked", async ({ page }) => {
    await egressCard(page).locator(".confirm select").selectOption(".example.org");
  }],
  ["persist confirm cancelled", async ({ page }) => {
    await egressCard(page).locator(".confirm").getByRole("button", { name: "Cancel" })
      .click();
  }],

  ["pin confirm opens", async ({ page }) => {
    await toolCard(page).getByRole("button", { name: "Allow + pin…" }).click();
  }],
  ["pin confirm, owner ticked", async ({ page }) => {
    await toolCard(page).locator(".cpins input").first().check();
  }],
  ["pin resolved for good", async ({ page }) => {
    await toolCard(page).locator(".cactions button.confirmgo").nth(1).click();
  }],

  ["persist resolved (the wildcard survives the cancel)", async ({ page }) => {
    await egressCard(page).locator('button[data-action="allow_persist"]').click();
    await egressCard(page).locator(".confirm button.confirmgo").click();
  }],

  // The stream drops both holds, as the backend does once they are decided. The
  // pointer is still over the list from the last click, and a card under it is not
  // swept, so moving it away is what lets the dwell decide.
  ["holds leave", async ({ page, push, clock }) => {
    await push([]);
    await page.mouse.move(0, 0);
    await clock(10000);
  }],
];
