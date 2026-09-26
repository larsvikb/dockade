// SPDX-License-Identifier: Apache-2.0
/* The standing egress rules as the operator writes them: the pattern a form value
 * becomes, and what creating, editing or revoking a rule is about to do, said before
 * the click. The backend decides; these only have to agree with it, and a preview
 * that reads differently from what gets written is the mistake this surface can
 * make.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

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
