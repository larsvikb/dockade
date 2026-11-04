// SPDX-License-Identifier: Apache-2.0
/* The standing egress rules as the operator writes them: the pattern a form value
 * becomes, and what creating, editing or revoking a rule is about to do, said before
 * the click. The backend decides; these only have to agree with it, and a preview
 * that reads differently from what gets written is the mistake this surface can
 * make. Below them, the Policy view itself, which `mountRules` wires when `start()`
 * calls it.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

import { esc } from "./dom.js";
import { renderListStatus } from "./list-status.js";
import { rulesStatus } from "./status.js";
import { fmtStamp } from "./time.js";

// What revoking a rule is about to do, for the confirm step — and whether it is the
// safe direction or the dangerous one.
//
// The two are opposites and a single "delete this?" hides that. Revoking an ALLOW
// tightens: the host reverts to unknown and the next request is held for a human.
// Revoking a BLOCK loosens — an explicit operator denial becomes "unknown", which
// can then be approved by someone who never knew it had been deliberately refused.
// Both land on `hold`; only the direction differs, so only wording can carry it.
//
// Says the CONSEQUENCE rather than the pattern alone, the same discipline
// persistPreview uses. "Delete .github.com?" asks about a row. "Requests to
// .github.com will no longer be allowed" asks about the world.
//
// Seed rules are reported as not revocable at all, with the reason. The backend
// refuses them too — this is the explanation, not the control.
export function revokePreview(rule) {
  const pattern = (rule && rule.pattern) || "";
  const action = (rule && rule.action) || "";
  if (rule && rule.source === "seed") {
    return {
      allowed: false,
      pattern,
      danger: false,
      text: `${pattern} comes from the policy seed and cannot be revoked here. `
          + `Edit policies/egress-allowlist.txt and rebuild.`,
    };
  }
  // Unknown action is treated as the dangerous direction: a confirm that
  // under-warns is the failure worth avoiding, and an over-warned allow costs the
  // operator one extra sentence.
  const loosening = action !== "allow";
  return {
    allowed: true,
    pattern,
    danger: loosening,
    text: loosening
      ? `Requests to ${pattern} will no longer be blocked. They will be held for `
        + `approval instead — and can then be allowed.`
      : `Requests to ${pattern} will no longer be allowed. They will be held for `
        + `approval instead.`,
  };
}

// A pattern as the control plane will STORE it (policy._normalize_pattern), mirrored
// here and held equal to it by a test.
//
// Mirrored rather than deferred to the backend because the confirm step has to show
// what will actually be written, not what was typed: `Example.COM.` is stored as
// `example.com`, and a confirm quoting the typed string is confirming a different
// rule from the one that lands. A leading dot survives — it is the wildcard marker.
export function normalizePattern(pattern) {
  const p = String(pattern === null || pattern === undefined ? "" : pattern)
    .trim().toLowerCase();
  return p.startsWith(".")
    ? "." + p.slice(1).replace(/\.+$/, "")
    : p.replace(/\.+$/, "");
}

// A wildcard must keep at least this many labels to be an ALLOW (policy._rule_error,
// held equal by a test). `.com` allowed would end governance for a TLD in one click.
export const WILDCARD_MIN_LABELS = 2;

// What "+ add rule" is about to write, for the live preview and the confirm.
//
// The backend validates; this EXPLAINS. Only three refusals are mirrored here, and the
// choice of which is deliberate rather than laziness:
//
//   - the wildcard floor, because it is the one refusal that would otherwise arrive
//     only after clicking a button labelled with a grant the operator wanted;
//   - a conflicting rule, because the fix is "revoke that one first" and it is on the
//     screen already;
//   - an identical rule, because the call would change nothing and the backend says so
//     with a 200 that is easy to misread as a write.
//
// Everything else a pattern can be wrong about — charset, length, a wildcard over an
// IP — is left to the backend and rendered from its `detail`. That keeps the duplicated
// policy down to one constant instead of a second copy of the grammar, which is the
// copy that would drift.
//
// `rules` is the standing policy already on screen, so the conflict check reads the
// same rows the operator is looking at.
export function createPreview(pattern, action, clientClass, rules) {
  const p = normalizePattern(pattern);
  // Defaults to the SAFER reading when the action is somehow absent, the same way
  // persistPreview does: previewing a block where an allow was meant is caught by the
  // operator, and the reverse is the mistake this step exists to prevent.
  const verb = action === "allow" ? "allow" : "block";
  const wild = p.startsWith(".");
  const scope = wild ? "host + subdomains" : "exact host";
  // `clientClass` rides on the result so the submit posts the class this preview was
  // BUILT from, rather than reading the select a second time. The two reads cannot
  // disagree while `confirm()` blocks the event loop between them — but that is a
  // property of the dialog, not of the code, and it would fall away the moment the
  // confirm became a custom one or anything before the fetch awaited. Preview and
  // request come from one object here, as they already do for `pattern` and `verb`.
  const base = { ok: false, pattern: p, verb, clientClass, wild, scope, danger: false,
                 existing: null, conflict: false, redundant: false, text: "" };
  if (!p) {
    return { ...base, text: "" };
  }
  if (!clientClass) {
    return { ...base, text: "Pick the client class this rule decides for." };
  }
  if (wild && verb === "allow" && p.slice(1).split(".").length < WILDCARD_MIN_LABELS) {
    return { ...base,
             text: `${p} is a wildcard over a single label — as an allow that grants `
                 + `everything under it. A block may be this broad; an allow may not.` };
  }
  const existing = (rules || []).find(
    r => r && r.pattern === p && (r.client_class || "") === clientClass) || null;
  if (existing) {
    const same = existing.action === verb;
    return { ...base, existing: existing.action, conflict: !same, redundant: same,
             text: same
               ? `${p} is already a standing ${verb.toUpperCase()} rule for `
                 + `${clientClass}. Nothing to add.`
               : `${p} is already a standing ${existing.action.toUpperCase()} rule for `
                 + `${clientClass}, and nothing ADDED here replaces a rule. Revoke that `
                 + `one first, edit it in place, or write a different pattern.` };
  }
  // The subtree is spelled out rather than named, because a leading dot is the entire
  // grant and it looks like punctuation — the same reason persistPreview quotes the
  // pattern verbatim beside its scope.
  const subject = wild
    ? `${p} — that host and every subdomain of it, including ones that have never `
      + `been requested —`
    : p;
  return { ...base, ok: true,
           // An allow LOOSENS: it grants egress with no hold and no click, which is the
           // direction worth flagging. A block only ever tightens, and is revocable.
           danger: verb === "allow",
           text: verb === "allow"
             ? `Requests from ${clientClass} to ${subject} will be allowed `
               + `immediately, without being held for approval.`
             : `Requests from ${clientClass} to ${subject} will be denied `
               + `immediately, without being held for approval.` };
}

// What "save changes" is about to do to an existing rule.
//
// The counterpart to createPreview, mirroring the same three refusals — the wildcard
// floor, a conflicting rule, and a no-op — for the same reasons, and leaving everything
// else to the backend's `detail`.
//
// It describes the TRANSITION, not the end state. "api.example.com will be allowed"
// describes a row; "requests to .example.com will no longer be allowed, and requests to
// api.example.com will be" describes what changes about the world. That is the
// distinction revokePreview draws too, and an edit needs it more than either neighbour:
// it is the only operation here that takes something away and gives something back in
// the same click.
//
// `rule` is the row being edited, so the conflict check can EXCLUDE it. A rule always
// holds its own pattern, and counting that as a collision would refuse every action flip
// — the same off-by-one the backend answers with its `id<>?` clause.
export function editPreview(rule, pattern, action, rules) {
  const p = normalizePattern(pattern);
  const verb = action === "allow" ? "allow" : "block";
  const wild = p.startsWith(".");
  const was = (rule && rule.pattern) || "";
  const wasVerb = (rule && rule.action) || "";
  const clientClass = (rule && rule.client_class) || "";
  const base = { ok: false, pattern: p, verb, wild, was, wasVerb, clientClass,
                 danger: false, conflict: false, unchanged: false, text: "" };
  if (!rule) {
    // The row went away under the form — revoked in another tab, or edited to something
    // else. Said out loud rather than left as a silently dead button.
    return { ...base,
             text: "That rule is no longer in the table. It may have been revoked "
                 + "elsewhere; cancel and start again." };
  }
  if (rule.source === "seed") {
    return { ...base,
             text: `${was} comes from the policy seed and cannot be edited here. `
                 + `Edit policies/egress-allowlist.txt and rebuild.` };
  }
  if (!p) return { ...base, text: "" };
  if (wild && verb === "allow" && p.slice(1).split(".").length < WILDCARD_MIN_LABELS) {
    return { ...base,
             text: `${p} is a wildcard over a single label — as an allow that grants `
                 + `everything under it. A block may be this broad; an allow may not.` };
  }
  if (p === was && verb === wasVerb) {
    return { ...base, unchanged: true,
             text: `${p} already ${verb}s for ${clientClass}. Nothing to change.` };
  }
  const clash = (rules || []).find(
    r => r && String(r.id) !== String(rule.id) && r.pattern === p
      && (r.client_class || "") === clientClass) || null;
  if (clash) {
    return { ...base, conflict: true,
             text: `${p} is already a standing ${String(clash.action).toUpperCase()} `
                 + `rule for ${clientClass}, and nothing here merges two rules. Revoke `
                 + `one of them first, or write a different pattern.` };
  }
  const subject = wild
    ? `${p} — that host and every subdomain of it, including ones that have never `
      + `been requested —`
    : p;
  // Loosening in either of two ways, and both are flagged. The rule ENDS as an allow;
  // or it stops being a block, which includes narrowing one — the hosts that fall out
  // from under a shrinking block are no longer denied. So the old action decides this
  // as much as the new one, which is what makes it different from createPreview.
  const danger = verb === "allow" || wasVerb === "block";
  const leaving = p === was
    ? `${was} currently ${wasVerb}s. `
    : (wasVerb === "allow"
        ? `Requests to ${was} will no longer be allowed. `
        : `Requests to ${was} will no longer be blocked. `);
  return { ...base, ok: true, danger,
           text: leaving + (verb === "allow"
             ? `Requests from ${clientClass} to ${subject} will be allowed `
               + `immediately, without being held for approval.`
             : `Requests from ${clientClass} to ${subject} will be denied `
               + `immediately, without being held for approval.`) };
}

// ── the rules table ──────────────────────────────────────────────────────────
// The DOM half of this surface, wired by `mountRules`, which `start()` calls. The
// elements are looked up there rather than at import, so the node tests can still
// import this file with no DOM.
let rulesEmpty, ruleFormEl, rulePatternEl, ruleActionEl, ruleClassEl,
    ruleAddEl, ruleCancelEl, rulePreviewEl;
let policySig = null;      // signature of the rules, to detect change
let policyUnseen = false;  // policy changed while another view was showing
// Whether a load has ever succeeded, and whether the last one failed (`pollStatus`
// in list-status.js says what each pair means on screen).
let rulesLoaded = false;
let rulesFailed = false;
let rulesById = new Map();
// Populated by refreshConfig in app.js, through receiveClientClasses, not by the
// rules table: a class with no rules yet is exactly the one an operator most needs
// to write the first rule for.
let clientClasses = [];
// "pending" until the first /api/config answers. Three states rather than an empty
// list, because an empty list means two different things — not asked yet, and asked
// and there are none — and only one of them is worth a sentence on screen.
let configState = "pending";

function renderRulesStatus(rowCount) {
  renderListStatus(rulesEmpty, rulesStatus(rowCount, rulesFailed, rulesLoaded));
}

export async function refreshRules() {
  let rows;
  try {
    const res = await fetch("/api/egress/rules");
    // `res.ok` checked, not just the parse: a 4xx/5xx body would otherwise flow
    // into .json() and either throw somewhere less obvious or — worse — parse
    // into something that renders as an empty but SUCCESSFUL policy.
    if (!res.ok) throw new Error(String(res.status));
    rows = await res.json();
  } catch (e) {
    // A failed refresh keeps the rows and SAYS SO, rather than swallowing it as
    // "transient; next poll retries" — which it is, right up until it is not.
    // Three things stop here and none of them looked stopped: the table kept
    // showing rules that might no longer be in force, the count froze, and
    // `policySig` stopped advancing, so the change badge silently stopped firing.
    // Leaving `policySig` alone IS correct — we cannot claim a change we did not
    // see — but the operator has to be told the view is not moving.
    rulesFailed = true;
    renderRulesStatus(document.getElementById("rules").rows.length);
    return;
  }
  rulesFailed = false;
  rulesLoaded = true;
  // Signature over pattern+action+class, not just the COUNT: a rule whose action
  // flipped is the change most worth noticing, and it leaves the count alone. The
  // class is in it for the same reason — the same pattern and action in a second
  // class is a real policy change, and without it two such rules sign identically.
  // Keyed by id so the click handler works from the ROW rather than from
  // markup it would otherwise have to parse back out of the DOM.
  rulesById = new Map(rows.map(r => [String(r.id), r]));
  const sig = JSON.stringify(
    rows.map(r => r.action + " " + r.pattern + " " + (r.client_class || "")));
  if (policySig !== null && sig !== policySig && policyHidden()) {
    policyUnseen = true;
  }
  policySig = sig;

  document.getElementById("badge-policy").textContent = String(rows.length);
  document.getElementById("rulecount").textContent =
    rows.length ? `· ${rows.length} rule${rows.length === 1 ? "" : "s"}` : "· none";
  document.getElementById("rules").innerHTML = rows.map(r => {
    const wild = (r.pattern || "").startsWith(".");
    const p = revokePreview(r);
    // A seed rule shows WHY it has no control rather than an empty cell, so
    // nobody has to wonder whether the button failed to render. The id is on the
    // button because revocation keys on it, never on the pattern.
    // Edit before revoke, in the order the operator should reach for them: changing a
    // rule is the recoverable action and taking it away is not, so the destructive one
    // is not the first button under the pointer.
    const control = p.allowed
      ? `<button type="button" class="edit" data-rule="${esc(String(r.id))}"
             >edit</button>
           <button type="button" class="revoke" data-rule="${esc(String(r.id))}"
             >revoke</button>`
      : `<span class="ts" title="${esc(p.text)}">from seed</span>`;
    return `<tr>
        <td><span class="tag ${esc(r.action)}">${esc(r.action)}</span></td>
        <td><code>${esc(r.pattern)}</code></td>
        <td class="${wild ? "wild" : "ts"}">${esc(r.scope)}</td>
        <!-- WHICH client population this rule decides for. Load-bearing rather than
             informational: the same pattern can appear twice with different actions,
             one row per class, and without this column those two rows look like a
             contradiction instead of two scoped rules. -->
        <td class="ts">${esc(r.client_class || "")}</td>
        <td class="ts">${esc(r.source)}</td>
        <td class="ts">${r.created_at ? fmtStamp(r.created_at) : ""}</td>
        <td>${control}</td></tr>`;
  }).join("");
  renderRulesStatus(rows.length);
  // The preview's conflict check reads these rows, so a rule that appeared elsewhere
  // shows up in the form rather than waiting to surface as a 409 on click.
  renderRulePreview();
  renderPolicyBadge();
}

// Whether the Policy view is off screen, read from the section `showView` hides
// rather than from a copy of the current view: one source of truth, and nothing
// more for app.js to export or hand over.
const policyHidden = () => document.getElementById("view-policy").hidden;

// The policy badge's unseen mark. Its count is set by refreshRules beside the table's.
function renderPolicyBadge() {
  const pb = document.getElementById("badge-policy");
  pb.classList.toggle("unseen", policyUnseen && policyHidden());
}

// What `showView` calls when the Policy view comes up.
export function markRulesSeen() {
  policyUnseen = false;
  renderPolicyBadge();
}

// ── writing a rule with no held request behind it ────────────────────────────
// The config-first half of policy. Every other rule in the store is downstream of
// something the agent already did — a seed entry, or a `+ persist` on a card — so
// until this form existed, pre-authorizing a registry meant letting a build block
// for the whole hold window first, and writing a block before anything asked for it
// could not be expressed at all.

// The id being edited, or null for the create form. Editing REUSES this form rather
// than adding a second one: the fields are the same fields and the validation is the
// same validation, so a separate editor would be a second place for the wildcard floor
// and the conflict check to drift out of. It also means an operator cannot be halfway
// through both at once.
let editingRuleId = null;
// The rule as it stood when the operator chose to edit it, sent with the save. The
// preview re-reads `rulesById`, which after a revoke elsewhere can hold a different
// rule under the same id; this cannot, so the backend refuses the save instead.
let editingExpected = null;
// What the last submit came back with. A separate fact from the preview, and it
// OUTRANKS it: the preview describes what a click would do, and this describes what
// the last one actually did — including the refusals this page deliberately does not
// mirror (see createPreview).
let ruleNotice = null;

function renderClassOptions() {
  const chosen = ruleClassEl.value;
  ruleClassEl.innerHTML = clientClasses.map(
    c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  // Keep the operator's choice across a refresh; otherwise a poll landing mid-type
  // silently re-scopes the rule they are composing.
  if (chosen && clientClasses.includes(chosen)) ruleClassEl.value = chosen;
  // With no classes the form cannot produce a rule the backend would accept, so it
  // is disabled and SAYS why, rather than offering an empty picker that 400s.
  const usable = clientClasses.length > 0;
  for (const el of [rulePatternEl, ruleActionEl, ruleClassEl, ruleAddEl]) {
    el.disabled = !usable;
  }
  // A config poll must not hand back a control this form deliberately locked. Without
  // this, re-rendering the class list mid-edit re-enables the picker and the operator
  // can re-scope a rule the backend will not re-scope.
  if (editingRuleId !== null) ruleClassEl.disabled = true;
  renderRulePreview();
}

function enterEditMode(row) {
  editingRuleId = String(row.id);
  editingExpected = { expected_pattern: row.pattern, expected_action: row.action,
                      expected_client_class: row.client_class };
  rulePatternEl.value = row.pattern || "";
  ruleActionEl.value = row.action === "block" ? "block" : "allow";
  // The class is shown but LOCKED. Moving a rule between client classes takes policy
  // from one population and gives it to another, which the backend refuses to call an
  // edit (see RuleEditRequest) — disabled rather than hidden, so the form still says
  // who the rule decides for.
  if (row.client_class) ruleClassEl.value = row.client_class;
  ruleClassEl.disabled = true;
  ruleCancelEl.hidden = false;
  ruleAddEl.textContent = "save changes";
  // Any verdict from a previous submit is about a different rule now.
  ruleNotice = null;
  renderRulePreview();
  rulePatternEl.focus();
}

function leaveEditMode() {
  editingRuleId = null;
  editingExpected = null;
  rulePatternEl.value = "";
  ruleClassEl.disabled = !clientClasses.length;
  ruleCancelEl.hidden = true;
  ruleAddEl.textContent = "add rule";
  renderRulePreview();
}

function currentPreview() {
  const rules = [...rulesById.values()];
  if (editingRuleId !== null) {
    // Looked up on every render rather than captured at entry, so a rule revoked under
    // the form is noticed by the preview. A rule CHANGED under it is described as it
    // now stands; what stops the save writing over it is `editingExpected`.
    return editPreview(rulesById.get(editingRuleId), rulePatternEl.value,
                       ruleActionEl.value, rules);
  }
  return createPreview(rulePatternEl.value, ruleActionEl.value, ruleClassEl.value,
                       rules);
}

function renderRulePreview() {
  if (!clientClasses.length) {
    // Silent while the first /api/config is still in flight — a form that is briefly
    // disabled explains itself a moment later, whereas an error shown before anything
    // has failed is simply wrong.
    rulePreviewEl.hidden = configState === "pending";
    rulePreviewEl.className = "empty";
    rulePreviewEl.textContent = configState === "failed"
      ? "Could not reach the control plane, so no rule can be scoped to a client "
        + "class yet."
      : "No client classes are configured (CONTROL_CLIENT_CLASSES), so a rule "
        + "written here could not decide for anyone.";
    return;
  }
  const p = currentPreview();
  ruleAddEl.disabled = !p.ok;
  const text = ruleNotice ? ruleNotice.text : p.text;
  rulePreviewEl.hidden = !text;
  // The loosening direction is called out the same way the revoke confirm calls out
  // its own: colour is never the only cue, so the wording carries it too.
  rulePreviewEl.className =
    "empty" + ((ruleNotice ? ruleNotice.bad : p.danger) ? " wild" : "");
  rulePreviewEl.textContent = text;
}

// What refreshConfig hands over: the backend's `client_classes` when /api/config
// answered, or `answered` false when it did not, which keeps the classes from the
// last good answer.
export function receiveClientClasses(answered, classes) {
  if (answered) {
    // The classes a rule may be scoped to, from the backend rather than guessed:
    // `create_rule` refuses one it does not know, so a guessed list offers rules that
    // cannot be written. Filtered to strings for the reason `refreshConfig` validates
    // the hold window — this feeds a <select> whose value goes straight into a POST.
    clientClasses = (Array.isArray(classes) ? classes : [])
      .filter(x => typeof x === "string" && x);
    configState = "ok";
  } else {
    configState = "failed";
  }
  renderClassOptions();
}

// The edit half of that submit. Split out rather than branched inline because the two
// differ in more than a URL — the confirm names a transition, the success case leaves
// edit mode, and `changed: false` is a different sentence from `created: false`.
async function submitEdit(p) {
  // The same friction the create path applies, for a stronger reason: this write both
  // grants and takes away, and the confirm is the only place the operator sees both
  // halves stated together.
  if (!window.confirm(`${p.text}\n\nSave this change to ${p.pattern}?`)) return;
  ruleAddEl.disabled = true;
  try {
    const res = await fetch(
      `/api/egress/rules/${encodeURIComponent(editingRuleId)}/edit`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        // The PREVIEWED pattern, not the typed one, for the reason the create path
        // gives: normalization can change what lands, and the confirm has to have been
        // about the rule that does.
        body: JSON.stringify({ pattern: p.pattern, action: p.verb,
                               ...editingExpected }),
      });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.ok) {
      // 404 and 409 are the ones worth reading here: the rule was revoked, or changed
      // into another, while the form was open, so retrying cannot help.
      ruleNotice = { bad: true,
                     text: `Not saved: ${body.detail || `the control plane answered `
                                                      + `${res.status}`}` };
      // The expectation this form holds is the one refused, so every retry from it
      // would be refused too. Leaving edit mode makes the next attempt start from
      // the rule as it now stands; the notice stays up to say why.
      if (res.status === 409 && body.current) leaveEditMode();
    } else if (body.changed === false) {
      ruleNotice = { bad: false,
                     text: `${body.pattern} already ${body.action}s for `
                         + `${body.client_class}; nothing was written.` };
      leaveEditMode();
    } else {
      // Names both states, as the audit row does — "saved" alone would not say which
      // of the two things an edit can change actually moved.
      const prev = body.previous || {};
      ruleNotice = { bad: false,
                     text: `Saved: ${prev.pattern} (${prev.action}) is now `
                         + `${body.pattern} (${body.action}) for `
                         + `${body.client_class}.` };
      leaveEditMode();
    }
  } catch (e) {
    ruleNotice = { bad: true,
                   text: "Not saved: the control plane is unreachable." };
  }
  ruleAddEl.disabled = false;
  renderRulePreview();
  refreshRules();
}

export function mountRules() {
  rulesEmpty = document.getElementById("rules-empty");
  ruleFormEl = document.getElementById("rule-form");
  rulePatternEl = document.getElementById("rule-pattern");
  ruleActionEl = document.getElementById("rule-action");
  ruleClassEl = document.getElementById("rule-class");
  ruleAddEl = document.getElementById("rule-add");
  ruleCancelEl = document.getElementById("rule-cancel");
  rulePreviewEl = document.getElementById("rule-preview");

  // Delegated, because the table is replaced wholesale on every poll — a handler
  // bound per button would be re-bound every four seconds and lost in between.
  //
  // `confirm()` rather than the inline two-step the approval cards use. The cards
  // needed inline confirmation because the button row moves under the pointer as
  // holds expire; this table only changes when policy does, and a modal that steals
  // focus is the right amount of friction for an action with no undo.
  document.getElementById("rules").addEventListener("click", async (ev) => {
    // Editing loads the row into the form above rather than acting here. No confirm on
    // THIS click: it changes nothing yet, and the form's own confirm is the one that
    // guards the write.
    const editBtn = ev.target.closest("button.edit");
    if (editBtn) {
      const editRow = rulesById.get(editBtn.dataset.rule);
      if (editRow) enterEditMode(editRow);
      return;
    }
    const btn = ev.target.closest("button.revoke");
    if (!btn) return;
    const row = rulesById.get(btn.dataset.rule);
    if (!row) return;
    const p = revokePreview(row);
    if (!p.allowed) return;
    if (!window.confirm(`${p.text}\n\nRevoke ${p.pattern}?`)) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/egress/rules/${encodeURIComponent(btn.dataset.rule)}/revoke`, {
        method: "POST", headers: { "content-type": "application/json" },
        // The row this button was rendered with, which the confirm described: a
        // revoke can widen, so one aimed at a rule since changed is refused (409).
        body: JSON.stringify({ expected_pattern: row.pattern, expected_action: row.action,
                               expected_client_class: row.client_class }) });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // Says what the backend said. The refusals that matter here are 403 (a seed
        // rule, which the UI should not have offered), and 404 and 409 (already
        // revoked, or changed: the table on screen is stale) — all worth reading
        // rather than collapsing into "failed".
        window.alert(`Could not revoke: ${body.detail || res.status}`);
        btn.disabled = false;
        return;
      }
    } catch (e) {
      window.alert("Could not revoke: the control plane is unreachable.");
      btn.disabled = false;
      return;
    }
    refreshRules();
  });

  for (const el of [rulePatternEl, ruleActionEl, ruleClassEl]) {
    // Any edit invalidates the last submit's verdict — leaving it up would attach a
    // refusal to a rule that is no longer the one on screen.
    el.addEventListener("input", () => { ruleNotice = null; renderRulePreview(); });
    el.addEventListener("change", () => { ruleNotice = null; renderRulePreview(); });
  }

  // Leaves the rule exactly as it was: nothing has been sent at this point, so there is
  // nothing to undo and no confirm to ask for.
  ruleCancelEl.addEventListener("click", () => { ruleNotice = null; leaveEditMode(); });

  ruleFormEl.addEventListener("submit", async (ev) => {
    // Always: the page's own CSP sends `form-action 'none'`, so a native submit is
    // refused by the browser anyway — this is what makes that a fail-closed backstop
    // rather than a broken form.
    ev.preventDefault();
    const p = currentPreview();
    if (!p.ok) return;
    if (editingRuleId !== null) {
      await submitEdit(p);
      return;
    }
    // `confirm()` for the same reason the revoke path uses one: this table only changes
    // when policy does, so a modal is the right amount of friction for a write that
    // takes effect on the agent's very next request. The pattern quoted is the
    // NORMALIZED one, which is what will actually be stored.
    if (!window.confirm(`${p.text}\n\nAdd this ${p.verb} rule for ${p.pattern}?`)) return;
    ruleAddEl.disabled = true;
    try {
      const res = await fetch("/api/egress/rules", {
        method: "POST",
        headers: { "content-type": "application/json" },
        // What was PREVIEWED and confirmed, not what is in the box: the two differ
        // whenever normalization did anything, and the confirm has to be about the
        // rule that lands. All three fields come from the one preview object for that
        // reason — `client_class` used to re-read its select here, which was a second
        // derivation of a value the operator had already been shown.
        body: JSON.stringify({ pattern: p.pattern, action: p.verb,
                               client_class: p.clientClass }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // The backend's own sentence. The refusals worth reading rather than
        // collapsing into "failed" are 400 (a pattern this page did not mirror a check
        // for) and 409 (a rule appeared under us — the table on screen was stale).
        ruleNotice = { bad: true,
                       text: `Not added: ${body.detail || `the control plane answered `
                                                        + `${res.status}`}` };
      } else if (body.created === false) {
        // A 200 that wrote nothing, because the same rule arrived between the preview
        // and the click. Reported rather than treated as success — the same distinction
        // the approval cards draw between "rule written" and "already in place".
        ruleNotice = { bad: false,
                       text: `${body.pattern} was already a standing ${body.action} `
                           + `rule for ${body.client_class}; nothing was written.` };
      } else {
        ruleNotice = { bad: false,
                       text: `Added: ${body.pattern} now ${body.action}s for `
                           + `${body.client_class}.` };
        rulePatternEl.value = "";
      }
    } catch (e) {
      ruleNotice = { bad: true,
                     text: "Not added: the control plane is unreachable." };
    }
    ruleAddEl.disabled = false;
    renderRulePreview();
    refreshRules();
  });
}
