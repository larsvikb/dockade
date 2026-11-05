// SPDX-License-Identifier: Apache-2.0
// What the harness does to the page, one named step at a time; the DOM is recorded
// after each. A step addresses the page by what an operator sees (ids, roles, the
// card's classes) and never by a module's internals, so the same steps run against
// the base tree and the branch.
//
// A PR that moves a surface out of `start()` adds the steps that exercise it here,
// in the PR that moves it.

import { HOLDS, NOW_MS, RULES, STALE_HOLD, abortConfig, failRules, setRules } from "./backend.js";

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

  // A card whose call met pins made under other arguments of the tool: the panel
  // names them, refuses the rebind the backend would, and says when one rebinds.
  ["stale-pin hold arrives", async ({ push }) => { await push([STALE_HOLD]); }],
  ["stale pin confirm opens", async ({ page }) => {
    await toolCard(page).getByRole("button", { name: "Allow + pin…" }).click();
  }],
  ["stale pin confirm, owner ticked (refused rebind)", async ({ page }) => {
    await toolCard(page).locator(".cpins input").first().check();
  }],
  ["stale pin confirm, owner and repo ticked (rebind)", async ({ page }) => {
    await toolCard(page).locator(".cpins input").nth(1).check();
  }],
  ["stale pin rebound for good", async ({ page }) => {
    await toolCard(page).locator(".cactions button.confirmgo").nth(1).click();
  }],
  ["stale-pin hold leaves", async ({ page, push, clock }) => {
    await push([]);
    await page.mouse.move(0, 0);
    await clock(10000);
  }],

  // The agent withdraws an ask: the stream drops its card and names it in
  // `withdrawn`, so the card says so rather than hedging.
  ["a tool ask is withdrawn", async ({ page, push }) => {
    await push([{ ...HOLDS[1], id: "tool-w" }]);
    await page.evaluate(() => window.__harnessPush({ holds: [], withdrawn: ["tool-w"] }));
  }],
  // A deny that lost the race to a withdrawal: the backend's 409 names it.
  // No clock advance: the snapshot must catch the card's message before its dwell
  // ends. The next step sweeps it.
  ["a deny meets a withdrawn ask", async ({ page, push }) => {
    await push([{ ...HOLDS[1], id: "tool-409" }]);
    // By id: the withdrawn card from the step above is still on screen, stale.
    await page.locator('#pending .card.tool[data-id="tool-409"]')
      .getByRole("button", { name: "Deny", exact: true }).click();
    await page.mouse.move(0, 0);
  }],
  // Both stale cards go, so no later step carries them.
  ["the departed asks are swept", async ({ clock }) => { await clock(10000); }],

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

  // A standing rule's edit and revoke: each sends the rule the page showed, and
  // the edit the one it opened rather than the one the table holds at save.
  ["policy tab, to edit", async ({ page }) => { await page.click("#tab-policy"); }],
  ["rule edit opens", async ({ page }) => {
    await page.click('#rules button.edit[data-rule="2"]');
  }],
  ["rule edit, narrowed", async ({ page }) => {
    await page.fill("#rule-pattern", "api.evil.example");
  }],
  ["rule edit saved", async ({ page, accept }) => {
    accept();
    await page.click("#rule-add");
  }],
  ["rule revoked", async ({ page, accept }) => {
    accept();
    await page.click('#rules button.revoke[data-rule="2"]');
  }],
  ["rule edit opened and cancelled", async ({ page }) => {
    await page.click('#rules button.edit[data-rule="2"]');
    await page.click("#rule-cancel");
  }],
  // A create, each of the three ways the backend answers one (backend.js).
  ["rule created", async ({ page, accept }) => {
    await page.fill("#rule-pattern", "files.example.com");
    accept();
    await page.click("#rule-add");
  }],
  ["rule created, already there", async ({ page, accept }) => {
    await page.fill("#rule-pattern", "already.example");
    accept();
    await page.click("#rule-add");
  }],
  ["rule create refused", async ({ page, accept }) => {
    await page.fill("#rule-pattern", "refused.example");
    await page.selectOption("#rule-action", "allow");
    accept();
    await page.click("#rule-add");
  }],

  // The decisions view (audit.js): the filters, the switch to the record, its pager,
  // and a filter the backend refuses. A filter waits for its timer, and the page's
  // timers are frozen, so each step moves the clock past it.
  ["audit tab, again", async ({ page }) => { await page.click("#tab-audit"); }],
  ["audit searched", async ({ page, clock }) => {
    await page.fill("#audit-q", "pypi");
    await clock(250);
  }],
  ["audit kind picked", async ({ page, clock }) => {
    await page.selectOption("#audit-kind", "deny");
    await clock(1);
  }],
  ["audit window picked", async ({ page, clock }) => {
    await page.selectOption("#audit-window", "1h");
    await clock(1);
  }],
  ["every event", async ({ page, clock }) => {
    await page.check("#audit-every");
    await clock(1);
  }],
  ["older page", async ({ page }) => { await page.click("#audit-older"); }],
  ["newer page", async ({ page }) => { await page.click("#audit-newer"); }],
  ["audit search refused", async ({ page, clock }) => {
    await page.fill("#audit-q", "refuse me");
    await clock(250);
  }],
  ["audit filters cleared", async ({ page, clock }) => {
    await page.click("#audit-clear");
    await clock(1);
  }],

  // The live leases (leases.js): a folded group opened, the countdowns moving on the
  // one-second tick, a revoke, one that lost the race with the lease's expiry, and the
  // group closed again. Revoking re-fetches the table, so the group staying open
  // through it is part of what the run records.
  ["approvals tab, again", async ({ page }) => { await page.click("#tab-approvals"); }],
  ["lease group opened", async ({ page }) => {
    await page.click("#leases button.group-toggle");
  }],
  ["lease countdowns tick", async ({ clock }) => { await clock(1000); }],
  ["lease revoked", async ({ page }) => {
    await page.click('#leases button.revoke[data-lease="1"]');
  }],
  ["lease already gone", async ({ page }) => {
    await page.click('#leases button.revoke[data-lease="2"]');
  }],
  ["lease group closed", async ({ page }) => {
    await page.click("#leases button.group-toggle");
  }],

  // The Policy badge's unseen mark (egress-rules.js), and the rules and config reads
  // failing. What these change in backend.js stays changed, so they run last. The
  // rules read changes behind a card's persist, whose refresh is what notices it.
  ["rules change behind a persist", async ({ page, push }) => {
    setRules([...RULES, {
      id: 3, action: "allow", pattern: "registry.example.org", scope: "exact host",
      client_class: "agent", source: "operator", created_at: NOW_MS / 1000 }]);
    await push([{ ...HOLDS[0], id: "egress-2" }]);
    await egressCard(page).locator('button[data-action="allow_persist"]').click();
    await egressCard(page).locator(".confirm button.confirmgo").click();
  }],
  ["audit tab, the mark stays", async ({ page }) => { await page.click("#tab-audit"); }],
  ["policy tab, the mark clears", async ({ page }) => {
    await page.click("#tab-policy");
  }],
  // A change seen while the Policy tab is up is already seen: no mark after leaving.
  ["rules change on the policy tab", async ({ clock }) => {
    setRules([...RULES, {
      id: 4, action: "block", pattern: "tracker.example", scope: "exact host",
      client_class: "agent", source: "operator", created_at: NOW_MS / 1000 }]);
    await clock(4000);
  }],
  ["audit tab, no mark", async ({ page }) => { await page.click("#tab-audit"); }],
  ["policy tab, for the failures", async ({ page }) => { await page.click("#tab-policy"); }],
  ["the rules poll fails", async ({ clock }) => {
    failRules();
    await clock(4000);
  }],
  // A config read that throws, after one that answered: the form keeps its classes.
  ["the stream drops, and the config read aborts on reconnect", async ({ drop, clock }) => {
    abortConfig();
    await drop();
    await clock(1000);
  }],
  ["the form is still usable", async ({ page }) => {
    await page.fill("#rule-pattern", "after.example");
  }],
  ["reload, the config and rules reads failing", async ({ load }) => { await load(); }],

];
