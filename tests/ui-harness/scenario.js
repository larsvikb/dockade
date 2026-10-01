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

  // The MCP tab (mcp.js): the servers form both ways, the tool policy form and
  // table, and the pins table. The lists are fixed, so a write changes what the page
  // sends and says, not what it lists next.
  ["tools tab, again", async ({ page }) => { await page.click("#tab-tools"); }],
  ["server form filled in", async ({ page }) => {
    await page.fill("#server-name", "mcp-wiki");
    await page.selectOption("#server-auth", "custom");
    await page.fill("#server-header", "X-Api-Key");
    await page.fill("#server-template", "{secret}");
  }],
  ["server registered", async ({ page }) => { await page.click("#server-add"); }],
  ["server edit opens", async ({ page }) => {
    await page.click('#servers button.edit[data-server="mcp-github"]');
  }],
  ["server edit, port changed", async ({ page }) => {
    await page.fill("#server-port", "9000");
  }],
  ["server edit saved", async ({ page, accept }) => {
    accept();
    await page.click("#server-add");
  }],
  ["server edit opened and cancelled", async ({ page }) => {
    await page.click('#servers button.edit[data-server="mcp-notes"]');
    await page.click("#server-cancel");
  }],
  ["server enabled", async ({ page }) => {
    await page.click('#servers button.toggle[data-server="mcp-notes"]');
  }],
  ["server revoke declined", async ({ page }) => {
    await page.click('#servers button.revoke[data-server="mcp-notes"]');
  }],
  ["tool rule picked", async ({ page }) => {
    await page.selectOption("#toolrule-server", "mcp-github");
    await page.selectOption("#toolrule-tool", "list_commits");
    await page.selectOption("#toolrule-action", "ask");
  }],
  ["tool rule added", async ({ page }) => { await page.click("#toolrule-add"); }],
  // Widening asks first; revoking never does.
  ["tool rule widened", async ({ page, accept }) => {
    accept();
    await page.click('#toolrules button.edit[data-rule="2"][data-action="allow"]');
  }],
  ["tool rule revoked", async ({ page }) => {
    await page.click('#toolrules button.revoke[data-rule="1"]');
  }],
  ["pin revoked", async ({ page }) => {
    await page.click('#toolpins button.revoke[data-pin="4"]');
  }],
];
