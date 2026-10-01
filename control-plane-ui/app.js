// SPDX-License-Identifier: Apache-2.0
/* dockade control-plane UI — page behaviour.
 *
 * Split out of index.html rather than left inline for two substantive reasons:
 *   - it lets the Content-Security-Policy this app now sends say `script-src 'self'`
 *     instead of `'unsafe-inline'`, which is the difference between a CSP that
 *     constrains an injection and one that merely decorates the response headers;
 *   - it makes the logic testable. `tests/test_control_plane_ui_js.py` runs the pure
 *     helpers below under node; script inline in an HTML file cannot be reached at all.
 *
 * The structure follows that second point, and mirrors the split the Python side
 * already uses: everything that DECIDES something is a pure function at the top,
 * exported for the tests; everything that TOUCHES THE DOM runs from `start()`,
 * which only a browser calls. Importing this file under node must therefore have
 * no side effects — if DOM work ever migrates to import time, the node test fails
 * at `import`, which is the intended alarm.
 *
 * This is the entry module; a surface with its own file is imported below.
 */

import { mountAudit, refreshAudit } from "./audit.js";
import { esc } from "./dom.js";
import { revokePreview, createPreview, editPreview } from "./egress-rules.js";
import { leaseLabel, leaseRemaining, leaseCountdown, groupLeases } from "./leases.js";
import {
  pinText,
  mountMcp, refreshServers, refreshToolRules, refreshInventory, refreshToolPins,
  updatePinCountdowns,
} from "./mcp.js";
import {
  renderableHolds, diffPending, shouldSweep, DWELL_MS,
  toolRemaining, toolOutcomeMessage, holdRemaining, countdownState, departure,
  COUNTDOWN_URGENT_S, persistPreview, pinPreview, requestsLabel,
  pendingAnnouncement, approvalNotices, shouldNotify, notifyButton,
} from "./holds.js";
import { payloadDisclosure, payloadHazards, payloadTokens, renderPayload }
  from "./payload.js";
import { shortActor } from "./provenance.js";
import { saturationState, ackCount } from "./saturation.js";
import { rulesStatus, leasesStatus, renderListStatus } from "./status.js";
import { fmtTime, fmtStamp } from "./time.js";

// ── pure decision helpers (unit-tested) ─────────────────────────────────────

// Which traffic-light lamp is lit. RED is the stream being down, not a denial:
// being blind is worth more alarm than being busy, because a hold nobody sees
// default-denies when CONTROL_HOLD_TIMEOUT elapses.
function lampState(streamUp, pendingCount) {
  return !streamUp ? "red" : pendingCount ? "amber" : "green";
}

// Reconnect delay for the approvals stream: doubling from 1s, capped at 30s.
// No jitter on purpose — jitter exists to de-synchronise a fleet of clients, and
// this page has exactly one operator, so determinism is worth more than herd
// avoidance (and makes the delay assertable).
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
function backoffDelay(attempt) {
  return Math.min(RECONNECT_MIN_MS * 2 ** Math.max(0, attempt), RECONNECT_MAX_MS);
}

// ── the page ────────────────────────────────────────────────────────────────

function start() {
  // ── state the indicators read ─────────────────────────────────────────────
  let pendingCount = 0;      // approvals waiting on a human
  let streamUp = false;      // is the SSE feed actually delivering?
  let policySig = null;      // signature of the rules, to detect change
  let policyUnseen = false;  // policy changed while another view was showing
  // Seconds a hold waits before it default-denies, from GET /api/config. Stays null
  // until the backend says, and the page works without it — one fewer thing that can
  // stop an approval from reaching a human.
  let holdTimeout = null;
  // Seconds a lease lasts, also from GET /api/config, and null until it answers. Used
  // for the BUTTON LABEL only — never to compute a deadline, which every lease carries
  // as an absolute instant of its own. That split is deliberate: a wrong label is
  // cosmetic, while a locally-computed expiry could show a grant ending at a time it
  // does not, and the second failure is the one that matters.
  let leaseSeconds = null;
  // Seconds a timed pin lasts, from the same GET, for the pin panel's timed button
  // label and nothing else, for the same reason.
  let pinLeaseSeconds = null;

  // ── views ─────────────────────────────────────────────────────────────────
  const VIEWS = ["approvals", "audit", "policy", "tools"];
  const viewFromHash = () =>
    VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "approvals";
  let current = viewFromHash();

  function showView(name) {
    current = VIEWS.includes(name) ? name : "approvals";
    for (const v of VIEWS) {
      document.getElementById("view-" + v).hidden = v !== current;
      const tab = document.getElementById("tab-" + v);
      tab.setAttribute("aria-selected", String(v === current));
      tab.tabIndex = v === current ? 0 : -1;
    }
    // Opening the policy view IS the acknowledgement that its change was seen.
    if (current === "policy") { policyUnseen = false; }
    // The inventory poll only runs while this view is up, so arriving here would
    // otherwise show whatever was last fetched — up to a poll interval old, and after
    // a long absence arbitrarily so. Asking on arrival is the same reasoning as the
    // refresh on `visibilitychange`: the stale moment to avoid is the one where
    // attention has just landed on the data.
    // The pins too: nothing polls them, and a card in the pending view is where one
    // will be written.
    if (current === "tools") { refreshInventory(); refreshToolPins(); }
    updateIndicators();
  }

  for (const v of VIEWS) {
    document.getElementById("tab-" + v).addEventListener("click", () => {
      // Drive through the hash so a reload, a bookmark and the back button all
      // land on the same view; hashchange calls showView.
      if (viewFromHash() === v) showView(v); else location.hash = v;
    });
  }
  window.addEventListener("hashchange", () => showView(viewFromHash()));

  // Arrow-key navigation between tabs, as the tablist role implies.
  document.querySelector("nav.tabs").addEventListener("keydown", e => {
    const i = VIEWS.indexOf(current);
    let next = null;
    if (e.key === "ArrowRight") next = VIEWS[(i + 1) % VIEWS.length];
    if (e.key === "ArrowLeft") next = VIEWS[(i - 1 + VIEWS.length) % VIEWS.length];
    if (e.key === "Home") next = VIEWS[0];
    if (e.key === "End") next = VIEWS[VIEWS.length - 1];
    if (!next) return;
    e.preventDefault();
    location.hash = next;
    document.getElementById("tab-" + next).focus();
  });

  // ── indicators (favicon + title + badges) ─────────────────────────────────
  // A traffic light whose three lamps map onto the three states that matter (see
  // lampState). All three keep their OWN colour at every state and only the
  // brightness moves: with the inactive lamps greyed out the icon is a dark
  // rectangle carrying one small coloured dot and stops reading as a traffic light
  // at 16px, which is the size that actually matters. The cost is honest —
  // dim-to-bright is a weaker peripheral signal than grey-to-colour, so the `(n)`
  // title prefix does most of the work of catching the eye in a background tab.
  const LAMP_DIM = 0.26;
  const LAMPS = [["red", 9, "#d1242f"], ["amber", 16, "#d29922"],
                 ["green", 23, "#2ea043"]];
  const favicon = document.getElementById("favicon");

  function faviconURI(state) {
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">` +
      `<rect x="8" y="1" width="16" height="30" rx="5" fill="#0f1115"` +
      ` stroke="#39404e" stroke-width="1.5"/>` +
      LAMPS.map(([name, cy, colour]) =>
        `<circle cx="16" cy="${cy}" r="4" fill="${colour}"` +
        ` fill-opacity="${state === name ? 1 : LAMP_DIM}"/>`).join("") +
      `</svg>`;
    // encodeURIComponent, not raw: '#' in the colours would otherwise start a
    // fragment and truncate the SVG.
    return "data:image/svg+xml," + encodeURIComponent(svg);
  }

  let faviconState = null;

  function updateIndicators() {
    const state = lampState(streamUp, pendingCount);
    // Only touch the <link> when the state actually CHANGES. This runs on every
    // poll (~15x a minute, forever), and browsers treat each href assignment as a
    // fresh favicon load — which is wasted work at best and a visibly flickering
    // tab icon at worst.
    if (state !== faviconState) {
      faviconState = state;
      favicon.href = faviconURI(state);
    }
    // Title prefix so a BACKGROUND tab shows the count in the tab strip — the
    // case that matters, since nobody watches this page while working.
    document.title = (pendingCount ? `(${pendingCount}) ` : "") +
      "dockade control plane";

    const pa = document.getElementById("badge-approvals");
    pa.textContent = String(pendingCount);
    pa.classList.toggle("hot", pendingCount > 0);

    const pb = document.getElementById("badge-policy");
    pb.classList.toggle("unseen", policyUnseen && current !== "policy");
  }

  // ── desktop notifications ─────────────────────────────────────────────────
  // The one indicator that reaches OUTSIDE the tab. Everything above it needs the
  // page to be looked at; see `approvalNotices` for why this one exists and
  // `shouldNotify` for when it fires.
  const notifyEl = document.getElementById("notify");
  // tag (an approval id) -> the live Notification, so a hold that leaves the queue
  // takes its notice with it.
  const notices = new Map();
  let notifyPrimed = false;
  // Latched by a constructor that throws: some browsers expose `Notification` but
  // allow only the service-worker form, and a page that has no worker cannot get
  // there. One failure is enough to know the rest will fail the same way.
  let notifyBroken = false;

  function notifyState() {
    if (typeof Notification === "undefined" || !window.isSecureContext
        || notifyBroken) {
      return "unavailable";
    }
    return Notification.permission;
  }

  function syncNotifyButton() {
    const b = notifyButton(notifyState());
    notifyEl.hidden = b.hidden;
    notifyEl.disabled = b.disabled;
    notifyEl.textContent = b.text;
    notifyEl.title = b.title;
  }

  notifyEl.addEventListener("click", async () => {
    await Notification.requestPermission();
    syncNotifyButton();
  });

  function notifyArrivals(added, total) {
    if (!shouldNotify(notifyState(), notifyPrimed, visible(), current)) return;
    for (const n of approvalNotices(added, total)) {
      let note;
      try {
        // Deliberately NOT `requireInteraction`. Pinning the toast on screen until it
        // is dealt with reads like the right call for a hold with a ~120s fuse, but
        // Chrome answers a persistent notification with a Close button of its own —
        // unlabelable from here, and a second way to dismiss next to the one the
        // toast already has. An ordinary notification fades after a few seconds into
        // the OS notification centre, which is where an operator who was away from
        // the desk looks anyway; `closeNotice` takes it back out of there when the
        // hold is gone.
        note = new Notification(n.title, { body: n.body, tag: n.tag || undefined });
      } catch (e) {
        notifyBroken = true;
        syncNotifyButton();
        return;
      }
      note.onclick = () => {
        // Bring the console forward AND land on the queue: a notification that
        // raises the window on whatever view was last open has done half its job.
        window.focus();
        location.hash = "approvals";
        note.close();
      };
      // Kept until the HOLD departs rather than until the toast does. Dropping the
      // reference on the notification's own `close` event looks tidier and is wrong:
      // a toast that has merely faded into the notification centre is still there to
      // be read, and whether that fires a close event is the browser's business.
      // `close()` on an already-closed notification does nothing, so holding the
      // reference costs nothing and never misses.
      if (n.tag) notices.set(n.tag, note);
    }
  }

  // A notice is closed by its hold LEAVING the queue, whichever way it went — the
  // operator's own click from this page, a resolve from somewhere else, or the
  // expiry that default-denied it. All three make the question moot, and only the
  // first is one the operator already knows about. This reaches further than the
  // screen: a faded toast is still sitting in the OS notification centre, and that
  // is exactly where a stale one would be read hours later as a live question.
  function closeNotice(id) {
    const note = notices.get(id);
    if (!note) return;
    notices.delete(id);
    note.close();
  }

  // ── pending approvals, keyed by approval id ───────────────────────────────
  const pendingEl = document.getElementById("pending");
  const emptyEl = document.getElementById("pending-empty");
  const pendingLive = document.getElementById("pending-live");
  // id -> entry (see buildCard for the shape)
  //   state: pending → resolving → resolved
  //          pending → confirming → resolving → resolved   (the *_persist actions)
  //          any of the above → stale
  const cards = new Map();

  // The resolve ladder, in increasing order of how far the click reaches. `allow_lease`
  // carries a `null` label because its wording depends on the configured duration —
  // `leaseLabel` derives it, and `relabelLeaseButtons` fills it in again when
  // /api/config answers after a card was already drawn.
  const ACTIONS = [
    ["allow_once", "Allow once", "allow"],
    ["allow_lease", null, "allow"],
    ["allow_persist", "Allow + persist rule", "allow"],
    ["deny_once", "Deny once", "deny"],
    ["deny_persist", "Deny + persist rule", "deny"],
  ];

  // Built with createElement/textContent rather than an HTML string: the host, url
  // and client on an approval are AGENT-CONTROLLED, and a text node needs no
  // escaping to be safe, so the question of whether esc() covers every context
  // does not arise for this list at all. The persist patterns are derived from that
  // same host by the backend, so they get the same treatment.
  //: A tool ask's answers. Deliberately not ACTIONS above: the backend keeps
  //: per-surface action sets and refuses the egress vocabulary here, so a shared list
  //: would render buttons that 400. `allow_pinned` is the one that writes standing
  //: policy, so it opens a confirm panel, whose buttons send it or its timed twin
  //: `allow_pinned_lease`. "Allow this tool forever" is not here: it is promoting the
  //: rule in the MCP tab, not a rung anyone should reach by clicking twice.
  const TOOL_CARD_ACTIONS = [["allow", "Allow", "allow"],
                             ["allow_pinned", "Allow + pin…", "allow"],
                             ["deny", "Deny", "deny"]];

  // A tool ask, which shares the card's shell and almost none of its body. A pin
  // panel where an egress card has its persist panel, no duplicate badge (nothing
  // joins a card by waiting on it), and a payload where an egress card has a URL.
  //
  // Built with createElement/textContent throughout, and here that matters more than
  // it does for a host: the arguments are AGENT-AUTHORED and may contain anything a
  // model can emit. A text node needs no escaping to be safe, so the question of
  // whether an escaper covers every context does not arise.
  function buildToolCard(a) {
    const el = document.createElement("div");
    el.className = "card tool";
    el.dataset.id = a.id;

    const title = document.createElement("div");
    title.className = "host";
    title.textContent = a.tool || "(unnamed tool)";

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [a.server ? `on ${a.server}` : null,
                        `asked ${fmtTime(a.ts)}`,
                        a.client ? `from ${a.client}` : null]
      .filter(Boolean).join(" · ");

    // The payload, verbatim, one click away at worst. This is the thing being
    // approved: the tool name says what KIND of act it is and only the arguments say
    // what it does to which repository.
    // Measured, checked and escaped on the payload as sent, indented only for display:
    // the newlines indenting adds are control characters to payloadHazards.
    const disclosure = payloadDisclosure(a.args_json);
    const hazards = payloadHazards(a.args_json);
    const raw = payloadTokens(a.args_json, { breaks: true });
    const escaped = payloadTokens(hazards.escaped);
    const details = document.createElement("details");
    details.className = "payload";
    details.open = true;
    // A DANGEROUS payload is shown escaped first; a merely non-ASCII one gets the note
    // and nothing else (see payloadHazards for why the two differ).
    const danger = hazards.level === "danger";
    const summary = document.createElement("summary");
    summary.textContent = disclosure.summary;
    const pre = document.createElement("pre");
    renderPayload(pre, danger ? escaped : raw);
    details.append(summary);
    if (hazards.level !== "none") {
      // Escaped FIRST, raw on request — see payloadHazards for why not the reverse.
      const hazard = document.createElement("div");
      hazard.className = danger ? "hazard" : "hazard quiet";
      const note = document.createElement("span");
      note.textContent = hazards.note;
      const toggle = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = danger;
      box.addEventListener("change", () => {
        renderPayload(pre, box.checked ? escaped : raw);
      });
      toggle.append(box, document.createTextNode(" escaped"));
      hazard.append(note, toggle);
      details.append(hazard);
    }
    details.append(pre);

    const cd = document.createElement("div");
    cd.className = "countdown";
    cd.hidden = true;
    const cdText = document.createElement("span");
    cdText.className = "cdtext";
    const bar = document.createElement("span");
    bar.className = "bar";
    const fill = document.createElement("i");
    bar.append(fill);
    cd.append(cdText, bar);

    const actions = document.createElement("div");
    actions.className = "actions";

    const msg = document.createElement("div");
    msg.className = "cardmsg";
    msg.hidden = true;

    const entry = {
      el, actions, msg, cd, cdText, fill,
      kind: "tool",
      state: "pending", staleAt: null, dwell: 0,
      ts: a.ts,
      deadline: Number(a.deadline),
      // The card's own window, so `countdownState` gets a total to take a fraction of
      // without the page knowing what the backend's tool timeout is set to.
      window: Number(a.deadline) - Number(a.ts),
    };

    // No `pin_options` means a backend that predates pins, and nothing is offered.
    const pinOptions = a.pin_options || { fields: [], unpinnable: [],
                                          refused: "this control plane offers no pins" };
    for (const [action, label, kind] of TOOL_CARD_ACTIONS) {
      const b = document.createElement("button");
      b.className = kind;
      b.textContent = label;
      if (action === "allow_pinned" && pinOptions.refused) {
        // Said before the click, as an unclassified egress card says it cannot persist.
        b.disabled = true;
        b.title = `Nothing to pin: ${pinOptions.refused}.`;
        actions.append(b);
        continue;
      }
      b.addEventListener("click", () => action === "allow_pinned"
        ? askPin(a) : resolve(a, action));
      actions.append(b);
    }

    buildPinConfirm(entry, a, pinOptions);
    el.append(title, meta, details, cd, actions, entry.confirm, msg);
    return entry;
  }

  // ── the confirm step for "Allow + pin" ────────────────────────────────────
  // The persist panel's shape, for the persist panel's reason: this click writes
  // standing policy. Below the action row, which is disabled while it is open, and
  // nothing is ticked to begin with — there is no "narrowest" default, because pinning
  // every field answers only this call and pinning none is not a pin.
  function buildPinConfirm(entry, a, options) {
    const box = document.createElement("div");
    box.className = "confirm";
    box.hidden = true;

    const what = document.createElement("div");
    what.className = "cwhat";

    const choices = document.createElement("div");
    choices.className = "cpins";
    const boxes = [];
    for (const f of options.fields) {
      const label = document.createElement("label");
      const tick = document.createElement("input");
      tick.type = "checkbox";
      tick.value = f.field;
      tick.addEventListener("change", () => renderPinPreview(entry, a));
      const code = document.createElement("code");
      // The value as the backend will store it, every non-ASCII character spelled out.
      code.textContent = pinText(f.value).text;
      label.append(tick, document.createTextNode(` ${f.field} = `), code);
      choices.append(label);
      boxes.push(tick);
    }
    for (const u of options.unpinnable) {
      const line = document.createElement("div");
      line.className = "note";
      line.textContent = `${u.field} — not pinnable: ${u.why}`;
      choices.append(line);
    }

    // Two confirms, the shorter grant first, as the egress card orders its lease
    // before its persist.
    const cactions = document.createElement("div");
    cactions.className = "cactions";
    const goTimed = document.createElement("button");
    goTimed.className = "confirmgo allow";
    const go = document.createElement("button");
    go.className = "confirmgo allow";
    const cancel = document.createElement("button");
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => cancelConfirm(entry));
    goTimed.addEventListener("click", () => {
      const p = pinPreview(options, entry.pinned(), a.tool, a.server, pinLeaseSeconds);
      if (p.ok) resolve(a, "allow_pinned_lease", null, p.fields);
    });
    go.addEventListener("click", () => {
      const p = pinPreview(options, entry.pinned(), a.tool, a.server, pinLeaseSeconds);
      if (p.ok) resolve(a, "allow_pinned", null, p.fields);
    });
    cactions.append(goTimed, go, cancel);

    box.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); cancelConfirm(entry); }
    });

    box.append(what, choices, cactions);
    Object.assign(entry, {
      confirm: box, cwhat: what, confirmBtn: go, timedBtn: goTimed, pinBoxes: boxes,
      pinOptions: options,
      pinned: () => boxes.filter(t => t.checked).map(t => t.value),
    });
  }

  function renderPinPreview(entry, a) {
    const p = pinPreview(entry.pinOptions, entry.pinned(), a.tool, a.server,
                         pinLeaseSeconds);
    entry.cwhat.textContent = p.text;
    entry.confirmBtn.disabled = entry.timedBtn.disabled = !p.ok;
    entry.confirmBtn.textContent = p.label;
    entry.timedBtn.textContent = p.timedLabel;
  }

  function askPin(a) {
    const entry = cards.get(a.id);
    // No boxes means nothing to pin. The button is disabled for that, but an error
    // path re-enables the whole action row (`disableActions`).
    if (!entry || entry.state !== "pending" || !entry.pinBoxes.length) return;
    entry.state = "confirming";
    entry.el.classList.add("confirming");
    disableActions(entry, true);
    renderPinPreview(entry, a);
    entry.confirm.hidden = false;
    // The first box, not Confirm: Confirm starts disabled, and focusing a disabled
    // button would drop focus out of the panel that Escape is bound on.
    entry.pinBoxes[0].focus();
  }

  function buildCard(a) {
    if (a.kind === "tool") return buildToolCard(a);
    const el = document.createElement("div");
    el.className = "card";
    el.dataset.id = a.id;

    const host = document.createElement("div");
    host.className = "host";
    host.textContent = a.host + (a.port ? ":" + a.port : "");

    const meta = document.createElement("div");
    meta.className = "meta";
    // The client class rides WITH the address rather than replacing it: the address
    // is what was observed and the class is what a persisted rule would be scoped to,
    // and an operator deciding a card needs both — "from 172.28.0.3 (mcp)" answers
    // both "who asked" and "who would this grant cover".
    meta.textContent = [a.proto || null, `requested ${fmtTime(a.ts)}`,
                        a.client
                          ? `from ${a.client}`
                            + (a.client_class ? ` (${a.client_class})` : "")
                          : null,
                        a.url || null]
      .filter(Boolean).join(" · ");

    // The hold countdown, hidden until /api/config has told us the window — the page
    // does not invent a deadline it cannot know.
    const cd = document.createElement("div");
    cd.className = "countdown";
    cd.hidden = true;
    const cdText = document.createElement("span");
    cdText.className = "cdtext";
    const bar = document.createElement("span");
    bar.className = "bar";
    const fill = document.createElement("i");
    bar.append(fill);
    cd.append(cdText, bar);

    // Immediately above the buttons, not up with the host line: it qualifies what the
    // click does, so it belongs where the eye already is at the moment of clicking.
    const dup = document.createElement("div");
    dup.className = "dup";
    dup.hidden = true;

    const actions = document.createElement("div");
    actions.className = "actions";

    const msg = document.createElement("div");
    msg.className = "cardmsg";
    msg.hidden = true;

    const entry = {
      el, actions, msg, cd, cdText, fill, dup,
      state: "pending", staleAt: null, dwell: 0,
      ts: a.ts,
      // What a persist may write, as the BACKEND will accept it. A backend that has
      // not been restarted into this change sends no options; fall back to the exact
      // host, which is also what resolve() defaults to when no pattern is sent.
      options: (a.persist_options && a.persist_options.length)
        ? a.persist_options
        : [{ pattern: (a.host || "").toLowerCase(), scope: "exact host" }],
      action: null,            // which *_persist action the confirm panel is for
    };

    // Whether a standing rule can be written for this request at all. A rule is
    // scoped to a client class, and a client the backend could not place has none —
    // `resolve` refuses the persist with a 400. Offering the button anyway would put
    // the operator through the confirm panel to reach a refusal, so it is disabled
    // here with the reason on it, the same "say it before the click" the conflict
    // preview does. `!== false` so a backend that predates the field behaves exactly
    // as it did: absent means allowed.
    const persistable = a.persistable !== false;
    // Whether a LEASE can be granted for this request, which fails in exactly the same
    // case and for the same reason (`holds._classified`): a grant outliving the request
    // is scoped to a client class, and an unplaceable client has none. Read from its own
    // field rather than reusing `persistable`, so the page is not quietly relying on the
    // two preconditions staying identical. `!== false` so a backend that predates the
    // field behaves as it did: absent means allowed.
    const leasable = a.leasable !== false;
    for (const [action, label, kind] of ACTIONS) {
      const b = document.createElement("button");
      b.className = kind;
      // On the button so `relabelLeaseButtons` can find it later. Every action carries
      // one rather than only the lease, because a marker present on one button and
      // absent on its neighbours is the kind of asymmetry a future reader has to test
      // for to trust.
      b.dataset.action = action;
      b.textContent = label === null ? leaseLabel(leaseSeconds) : label;
      const grants = action.endsWith("persist") || action.endsWith("lease");
      if (grants && !(action.endsWith("lease") ? leasable : persistable)) {
        b.disabled = true;
        b.title = `No ${action.endsWith("lease") ? "lease" : "standing rule"} can be `
                + "written for an unclassified client — decide this request with a "
                + "once action, or map its network in CONTROL_CLIENT_CLASSES.";
        actions.append(b);
        continue;
      }
      // The two `*_persist` actions write standing policy, so they open the confirm
      // panel; the `*_once` actions decide this request only and stay a single click.
      //
      // `allow_lease` is a single click too, and that is a judgement rather than an
      // oversight. The confirm panel exists to name the PATTERN a persist would write,
      // because the choice is irreversible and nothing in this UI removes a rule; a
      // lease chooses nothing, expires on its own, and can be revoked from the table
      // below — so a second click would be friction with no question behind it.
      b.addEventListener("click", () => action.endsWith("persist")
        ? askPersist(a, action)
        : resolve(a, action));
      actions.append(b);
    }

    buildConfirm(entry, a);
    setRequests(entry, a.requests);
    el.append(host, meta, cd, dup, actions, entry.confirm, msg);
    return entry;
  }

  // The lease button's wording depends on a number that arrives after the first cards
  // do, so it is written twice: once at build time (with whatever is known then) and
  // again here when /api/config answers. Cheaper and less surprising than deferring the
  // whole card — the button is live and correct from the first paint either way, and
  // only its wording sharpens.
  //
  // Skips a card that is no longer pending: a resolved or stale card's buttons are
  // disabled and its message reports what already happened, so relabelling one would
  // rewrite a record of a click that has been made.
  function relabelLeaseButtons() {
    const text = leaseLabel(leaseSeconds);
    for (const entry of cards.values()) {
      if (entry.state !== "pending" && entry.state !== "confirming") continue;
      const b = entry.actions.querySelector('button[data-action="allow_lease"]');
      if (b) b.textContent = text;
    }
  }

  function setRequests(entry, n) {
    const text = requestsLabel(n);
    entry.dup.textContent = text;
    entry.dup.hidden = !text;
  }

  // ── the confirm step for a `+ persist` ────────────────────────────────────
  // Justified by IRREVERSIBILITY rather than by risk. A persisted rule outlives the
  // session, is what makes every future request to that host skip the hold entirely,
  // and nothing in this UI removes it — undoing a mis-click means hand-editing SQLite
  // in a named volume. So the click that writes policy is now two clicks, and the
  // second one names the pattern and lets the operator narrow or widen it.
  //
  // Laid out BELOW the action row, which stays exactly where it was. That is
  // deliberate, and the same concern that drove keyed rendering: if the confirm button
  // appeared where the pointer already is, a double-click on "Allow + persist rule"
  // would sail straight through the confirmation it had just opened.
  function buildConfirm(entry, a) {
    const box = document.createElement("div");
    box.className = "confirm";
    box.hidden = true;

    const what = document.createElement("div");
    what.className = "cwhat";

    const label = document.createElement("label");
    label.className = "cscope";
    label.append(document.createTextNode("rule "));
    const select = document.createElement("select");
    for (const opt of entry.options) {
      const o = document.createElement("option");
      o.value = opt.pattern;
      o.textContent = `${opt.pattern} — ${opt.scope}`;
      select.append(o);
    }
    // Narrowest first from the backend, so the default selection is the safest one.
    select.selectedIndex = 0;
    select.addEventListener("change", () => renderPreview(entry));
    label.append(select);

    const warn = document.createElement("div");
    warn.className = "cwarn";
    warn.hidden = true;

    const cactions = document.createElement("div");
    cactions.className = "cactions";
    const go = document.createElement("button");
    go.className = "confirmgo";
    const cancel = document.createElement("button");
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => cancelConfirm(entry));
    go.addEventListener("click", () => resolve(a, entry.action, select.value));
    cactions.append(go, cancel);

    // Escape backs out, as a dialog-shaped thing should.
    box.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); cancelConfirm(entry); }
    });

    box.append(what, label, warn, cactions);
    Object.assign(entry, { confirm: box, cwhat: what, cwarn: warn,
                           select, confirmBtn: go });
  }

  function renderPreview(entry) {
    const opt = entry.options.find(o => o.pattern === entry.select.value)
      || entry.options[0];
    const p = persistPreview(entry.action, opt);
    // Rebuilt as nodes so the pattern (derived from an agent-chosen host) is text,
    // never markup — same reason as the card body above.
    entry.cwhat.replaceChildren();
    entry.cwhat.append(document.createTextNode(`Writes a standing rule to ${p.verb} `));
    const code = document.createElement("code");
    code.textContent = p.pattern;
    entry.cwhat.append(code, document.createTextNode(` — ${p.scope}.`));
    // A conflict outranks the wildcard warning: the wildcard caution is about a rule
    // that would be too broad, while a conflict means the click cannot write a rule at
    // all. Saying the second is more urgent than saying the first.
    const warn = p.conflict
      ? `⚠ ${p.pattern} is already a standing ${p.existing.toUpperCase()} rule, and ` +
        "nothing here replaces one. This will be refused — decide the request with " +
        "a one-off action, or pick a different pattern."
      : p.redundant
        ? `Already a standing ${p.existing.toUpperCase()} rule — confirming decides ` +
          "this request and leaves policy unchanged."
        : p.wild
          ? `⚠ Wildcard: this covers ${p.pattern.slice(1)} and every subdomain of ` +
            "it, including hosts nothing has requested yet."
          : "";
    entry.cwarn.hidden = !warn;
    entry.cwarn.textContent = warn;
    entry.cwarn.className = "cwarn" + (p.conflict ? " bad" : "");
    // Disabled rather than merely warned about, because the backend will refuse it:
    // letting the click through would spend a round trip to arrive at the same place.
    // The panel still opens, so the operator can read WHY and pick another pattern.
    entry.confirmBtn.disabled = p.conflict;
    entry.confirmBtn.textContent = p.conflict
      ? `Cannot ${p.verb} ${p.pattern} — already a ${p.existing} rule`
      : `Confirm — ${p.verb} ${p.pattern} from now on`;
    entry.confirmBtn.className = "confirmgo " + (p.verb === "allow" ? "allow" : "deny");
  }

  function askPersist(a, action) {
    const entry = cards.get(a.id);
    if (!entry || entry.state !== "pending") return;
    entry.state = "confirming";
    entry.action = action;
    entry.el.classList.add("confirming");
    // The action row is disabled while the panel is open, so the only live buttons are
    // Confirm and Cancel — a stray second click cannot fire a different action.
    disableActions(entry, true);
    renderPreview(entry);
    entry.confirm.hidden = false;
    // Focus lands on the pattern select when Confirm is disabled by a conflict —
    // focusing a disabled button drops focus to the body, which would strand a
    // keyboard operator outside the panel that just opened, with Escape (bound on the
    // panel) no longer reaching anything.
    (entry.confirmBtn.disabled ? entry.select : entry.confirmBtn).focus();
  }

  function cancelConfirm(entry) {
    if (entry.state !== "confirming") return;
    entry.state = "pending";
    entry.action = null;
    entry.confirm.hidden = true;
    entry.el.classList.remove("confirming");
    disableActions(entry, false);
  }

  function setMessage(entry, text, kind) {
    entry.msg.hidden = false;
    entry.msg.textContent = text;
    entry.msg.className = "cardmsg" + (kind ? " " + kind : "");
  }

  function disableActions(entry, off) {
    entry.actions.querySelectorAll("button").forEach(b => { b.disabled = off; });
  }

  // Takes the whole `{text, dwellMs}` from `departure()` rather than the two
  // separately, so the message and how long it stays readable cannot be passed apart —
  // omitting the dwell would silently give an expiry the 5s treatment and undo the fix
  // above, with nothing failing. The 409 caller below builds the same shape by hand.
  function markStale(entry, d) {
    entry.state = "stale";
    entry.staleAt = Date.now();
    entry.dwell = d.dwellMs;
    entry.el.classList.remove("busy", "confirming");
    entry.el.classList.add("stale");
    // A card nobody can act on any more shows neither a deadline nor a half-finished
    // confirmation — the message below replaces both.
    entry.confirm.hidden = true;
    entry.cd.hidden = true;
    disableActions(entry, true);
    setMessage(entry, d.text, "bad");
  }

  // `pattern` is sent only for the `*_persist` actions, and `pins` only for the two
  // pinning actions, and each is only ever what the backend itself offered on this
  // approval (it re-derives and re-validates the set, so the choice is bounded there
  // too, not merely here).
  async function resolve(a, action, pattern, pins) {
    const entry = cards.get(a.id);
    if (!entry || (entry.state !== "pending" && entry.state !== "confirming")) return;
    entry.state = "resolving";
    entry.confirm.hidden = true;
    entry.el.classList.remove("confirming");
    entry.el.classList.add("busy");
    disableActions(entry, true);
    setMessage(entry, "resolving…");
    try {
      const r = await fetch(`/approvals/${encodeURIComponent(a.id)}/resolve`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, ...(pattern ? { pattern } : {}),
                               ...(pins ? { pins } : {}) }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        // Report the outcome HERE instead of waiting for the stream to drop the
        // card. The card disappearing on the next SSE tick used to be the ONLY
        // feedback, so with the feed down — precisely the state the reconnect logic
        // below exists for — a successful approval was indistinguishable from a
        // hung click.
        entry.state = "resolved";
        entry.staleAt = Date.now();
        // Long enough to read the outcome after moving the mouse off the list, which is
        // the natural thing to do right after clicking — before this, the card could go
        // in about a second and take the confirmation with it.
        entry.dwell = DWELL_MS.resolved;
        entry.el.classList.remove("busy");
        entry.cd.hidden = true;
        entry.el.classList.add(d.outcome === "allow" ? "done-allow" : "done-deny");
        if (entry.kind === "tool") {
          // Its own sentence, because the egress one below reports what was PERSISTED
          // and a tool ask persists nothing — the fields it reads are all absent here,
          // so it would settle on "this request only", which is both wrong and
          // reassuring about the wrong thing.
          const m = toolOutcomeMessage(d);
          setMessage(entry, m.text, m.tone);
          refreshAudit();
          // A pin is standing policy in the MCP tab's table, so it appears there now.
          if (d.pin) refreshToolPins();
          return;
        }
        // The pattern comes back from the BACKEND, so this reports what was stored
        // rather than what was clicked — and it is the exact string an operator would
        // have to go and delete.
        //
        // `persisted` now means A ROW WAS WRITTEN, read from the insert's rowcount.
        // The comment that used to sit here reasoned that "standing rule" was true
        // even when nothing was written, "because that only happens when the identical
        // rule was already there". That was the bug: it also happened when the
        // OPPOSITE rule was there, and the card cheerfully confirmed a block that
        // policy had discarded. The conflicting case is refused outright now, and the
        // already-in-place case says so in its own words.
        // The lease clause reads the deadline the BACKEND returned rather than adding
        // the configured duration to this machine's clock, so the time shown is the
        // time the grant actually ends. `fmtTime` for the instant and not a duration:
        // the countdown in the table below is where "how long left" belongs, and this
        // sentence is a record of what was granted.
        setMessage(entry,
          (d.outcome === "allow" ? "✓ allowed" : "✕ denied") +
          (d.persisted ? ` · standing rule written: ${d.pattern || a.host.toLowerCase()}`
            : d.already_present
              ? ` · standing rule already in place: ${d.pattern || a.host.toLowerCase()}`
              : d.leased
                ? ` · leased until ${fmtTime(d.lease_expires_at)}`
                : " · this request only"),
          d.outcome === "allow" ? "ok" : "bad");
      } else if (r.status === 409 && (d.conflict || d.pin_refused)) {
        // A persist that would contradict an existing rule, or a pin whose tool is no
        // longer ruled `ask`. NOT a stale card: the approval is deliberately left
        // pending so the operator can choose again, so the buttons come back — the
        // same treatment as a rejected pattern.
        entry.state = "pending";
        entry.el.classList.remove("busy");
        disableActions(entry, false);
        setMessage(entry, d.detail || "that rule already exists", "bad");
      } else if (r.status === 409) {
        // Backend says it is no longer pending (expired, or resolved elsewhere).
        // Re-enabling the buttons would only invite a second failing click.
        markStale(entry, {
          dwellMs: DWELL_MS.gone,
          text: "no longer pending — the hold expired or was already resolved" });
      } else {
        entry.state = "pending";
        entry.el.classList.remove("busy");
        disableActions(entry, false);
        setMessage(entry, `could not resolve: ${d.detail || "HTTP " + r.status}`, "bad");
      }
    } catch (e) {
      entry.state = "pending";
      entry.el.classList.remove("busy");
      disableActions(entry, false);
      setMessage(entry, `request failed: ${e}`, "bad");
    }
    refreshAudit();
    // A "+ persist" action just changed standing policy. Refresh it at once so the
    // Policy badge marks the change while you are still on the approvals view.
    refreshRules();
    // And a lease just changed what is in force RIGHT NOW, in a table on the view the
    // operator is looking at — so it appears with the click rather than up to a poll
    // interval later, which for a grant with a countdown on it would read as the table
    // having missed it.
    refreshLeases();
  }

  function listBusy() {
    return pendingEl.matches(":hover") ||
      (document.activeElement !== null && pendingEl.contains(document.activeElement));
  }

  function sweep() {
    const busy = listBusy();
    const now = Date.now();
    for (const [id, entry] of cards) {
      if (entry.staleAt === null) continue;
      if (shouldSweep(busy, now - entry.staleAt, entry.dwell)) {
        entry.el.remove();
        cards.delete(id);
      }
    }
    emptyEl.hidden = cards.size > 0;
  }

  // ── saturation banner ─────────────────────────────────────────────────────
  const satEl = document.getElementById("saturation");
  const satText = document.getElementById("sat-text");
  const satDetail = document.getElementById("sat-detail");
  const satDismiss = document.getElementById("sat-dismiss");
  let lastSaturation = null;
  // Optimistic acknowledgement, held only until the stream echoes the backend's own.
  // The POST plus the next push is up to a second, and a banner that lingers after the
  // click reads as a button that did not work.
  let localAck = 0;

  satDismiss.addEventListener("click", async () => {
    // One expression for both the optimistic hide and the POST body, so they cannot
    // disagree. Acknowledging a COUNT rather than sending a "dismiss" is what lets a
    // rejection that landed between the render and the click survive as unread.
    const n = ackCount(lastSaturation);
    localAck = n;
    renderSaturation();
    try {
      const r = await fetch("/api/saturation/ack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: n }),
      });
      if (!r.ok) throw new Error(String(r.status));
      // Take the acknowledgement the backend actually RECORDED, not the one we hoped
      // to send — the same reason a resolved card reports `d.pattern` from the
      // response. If this page ever sends the wrong count, the banner reappears
      // immediately instead of the dismissal quietly failing to persist.
      localAck = Number((await r.json()).acknowledged) || 0;
      renderSaturation();
    } catch (e) {
      // The POST is the whole point — it is what survives a reload. If it failed, put
      // the banner back rather than leaving the operator believing it stuck.
      localAck = 0;
      renderSaturation();
    }
  });

  function renderSaturation() {
    // Whichever acknowledgement is further along: the backend's, or the click we have
    // not had confirmed yet.
    const acked = Math.max(
      localAck, lastSaturation ? Number(lastSaturation.acknowledged) || 0 : 0);
    const s = saturationState(lastSaturation, Date.now(), acked);
    satEl.hidden = !s.show;
    if (!s.show) return;
    satEl.className = "saturation " + s.level;
    satText.textContent = s.text;
    // The time is formatted HERE rather than in the pure helper, which returns the raw
    // stamp: locale formatting is not something a unit test should have to pin down.
    satDetail.textContent = s.lastTs
      ? `${s.detail} at ${fmtTime(s.lastTs)} · counted since ${fmtTime(lastSaturation.since)}`
      : s.detail;
    // Only the acknowledgeable state offers the button; a load warning clears itself
    // when the holds drain, so dismissing it would mean nothing.
    satDismiss.hidden = s.level === "load";
  }

  function renderPending(list) {
    pendingCount = list.length;
    const { add, gone } = diffPending([...cards.keys()], list);
    // Before the DOM work, so the announcement reflects what is arriving rather than
    // racing the cards that carry it.
    const say = pendingAnnouncement(add, list.length);
    if (say) pendingLive.textContent = say;
    // The same arrivals, one surface further out.
    notifyArrivals(add, list.length);
    for (const a of add) {
      const entry = buildCard(a);
      cards.set(a.id, entry);
      pendingEl.append(entry.el);
    }
    // The one field on a SURVIVING card that changes while it waits: how many blocked
    // requests it now speaks for. Updated in place on every push, because a retry can
    // join between the render and the click — a count frozen at first paint would
    // understate what the button does at the moment it is pressed. Left alone once the
    // card is resolving or stale: by then the number is history, and rewriting it
    // under a click that has already been sent would be a lie in the other direction.
    for (const a of list) {
      const entry = cards.get(a.id);
      // Tool cards are skipped rather than defaulted to 1: nothing blocks on an ask,
      // so a joiner adds no waiter and there is no count to keep current. A badge
      // saying "1 request" would be inventing a number the backend never sends.
      if (entry && entry.kind !== "tool"
          && (entry.state === "pending" || entry.state === "confirming")) {
        setRequests(entry, a.requests);
      }
    }
    for (const id of gone) {
      closeNotice(id);
      const entry = cards.get(id);
      if (entry.state === "pending" || entry.state === "confirming"
          || entry.state === "resolving") {
        // Say WHICH way it went. An expiry is a decision the operator failed to make
        // in time and the agent was denied for it; a card leaving with time left is
        // somebody or something else resolving it. Both used to read the same — and
        // the expiry now also stays put long enough to be read (see DWELL_MS).
        // A tool card knows its own window, so it can tell an expiry from a
        // resolved-elsewhere even when `/api/config` never answered — the egress card
        // cannot, and passes null to say so.
        const d = entry.kind === "tool"
          ? departure(toolRemaining(entry.deadline, Date.now()), entry.window)
          : departure(
            holdTimeout === null ? null
              : holdRemaining(entry.ts, holdTimeout, Date.now()),
            holdTimeout);
        markStale(entry, d);
      }
    }
    sweep();
    updateIndicators();
    // Set AFTER the first push has been rendered, never at load: until a list has
    // arrived there is nothing to distinguish "the queue was already this long" from
    // "these just came in", and only the second is worth a notification.
    notifyPrimed = true;
  }

  // Redrawn once a second for every live card. Cheap, and the only thing that makes a
  // deadline legible: the text for a reading, the bar for a glance.
  function updateCountdowns() {
    const now = Date.now();
    for (const entry of cards.values()) {
      if (entry.state === "resolved" || entry.state === "stale") continue;
      // A tool card carries its own absolute deadline, so it counts down whether or
      // not the config fetch ever succeeded. An egress card cannot — its window is
      // configuration — so it stays hidden rather than inventing a deadline.
      const tool = entry.kind === "tool";
      const remaining = tool ? toolRemaining(entry.deadline, now) : null;
      // Same rule on both surfaces: no countdown is shown for a deadline the page
      // cannot know. `countdownState` reads a null remaining as "expiring now" and
      // flags it urgent, so a card with a malformed deadline would sit there crying
      // wolf — the one thing an urgency signal must never do.
      if (tool ? remaining === null : holdTimeout === null) continue;
      const cs = tool
        ? countdownState(remaining, entry.window)
        : countdownState(holdRemaining(entry.ts, holdTimeout, now), holdTimeout);
      entry.cd.hidden = false;
      entry.cdText.textContent = cs.text;
      entry.fill.style.width = `${(cs.frac * 100).toFixed(1)}%`;
      entry.cd.classList.toggle("urgent", cs.urgent);
    }
  }

  // Sweeping is also driven by the operator leaving the list, so a stale card goes
  // as soon as removing it is safe rather than on the next tick.
  pendingEl.addEventListener("mouseleave", sweep);
  pendingEl.addEventListener("focusout", () => setTimeout(sweep, 0));
  // renderSaturation rides this tick too, because its emphasis decays with time
  // rather than with events: a rejection that stops being "recent" must fade on its
  // own, and the stream is silent precisely when nothing is arriving.
  // The lease countdowns ride this tick for the same reason the saturation banner
  // does: what changes is the passage of time, not an event, so nothing is going to
  // push a redraw at the moment one is needed.
  setInterval(() => {
    updateCountdowns(); sweep(); renderSaturation(); updateLeaseCountdowns();
    updatePinCountdowns();
  }, 1000);

  // ── config (the hold window behind the countdown) ─────────────────────────
  async function refreshConfig() {
    try {
      const c = await (await fetch("/api/config")).json();
      const t = Number(c.hold_timeout);
      // Validated rather than trusted: a zero or absent window would divide the
      // progress bar by zero and, worse, imply a deadline that isn't there. Anything
      // unusable leaves holdTimeout null, which just means no countdown.
      holdTimeout = Number.isFinite(t) && t > 0 ? t : null;
      updateCountdowns();
      // Validated the same way and left null when unusable, which `leaseLabel` reads as
      // "say no number". Relabelled rather than only stored, because cards drawn before
      // this first answer are already on screen with the placeholder wording on them.
      const ls = Number(c.lease_seconds);
      leaseSeconds = Number.isFinite(ls) && ls > 0 ? ls : null;
      relabelLeaseButtons();
      // Needs no relabelling: a pin panel reads it each time it renders.
      const ps = Number(c.pin_lease_seconds);
      pinLeaseSeconds = Number.isFinite(ps) && ps > 0 ? ps : null;
      // The classes a rule may be scoped to, from the backend rather than guessed:
      // `create_rule` refuses one it does not know, so a guessed list offers rules that
      // cannot be written. Filtered to strings for the same reason the window above is
      // validated — this feeds a <select> whose value goes straight into a POST.
      clientClasses = (Array.isArray(c.client_classes) ? c.client_classes : [])
        .filter(x => typeof x === "string" && x);
      configState = "ok";
      renderClassOptions();
    } catch (e) {
      // No countdown, and no rule form; the cards are otherwise unaffected.
      configState = "failed";
      renderClassOptions();
    }
  }

  // ── the decisions view: audit.js ──────────────────────────────────────────
  mountAudit();

  // ── the rules and leases tables ───────────────────────────────────────────
  // Whether a load has ever succeeded, and whether the last one failed (`pollStatus`
  // in status.js says what each pair means on screen).
  let rulesLoaded = false;
  let rulesFailed = false;
  let rulesById = new Map();
  // The same two facts for the leases poll, kept separately rather than folded into the
  // rules ones: the two views are filled by two fetches, and one of them failing while
  // the other succeeds is a state the page has to be able to describe.
  let leasesLoaded = false;
  let leasesFailed = false;
  let leasesById = new Map();
  // Which lease groups the operator has opened, by (class, domain) key. Outside the
  // render deliberately — see the toggle handler — and pruned to the live groups on
  // every refresh so a lapsed domain does not keep an expansion nobody asked for.
  const expandedLeaseGroups = new Set();
  // Populated by refreshConfig, not by the rules table: a class with no rules yet is
  // exactly the one an operator most needs to write the first rule for.
  let clientClasses = [];
  // "pending" until the first /api/config answers. Three states rather than an empty
  // list, because an empty list means two different things — not asked yet, and asked
  // and there are none — and only one of them is worth a sentence on screen.
  let configState = "pending";

  const rulesEmpty = document.getElementById("rules-empty");

  function renderRulesStatus(rowCount) {
    renderListStatus(rulesEmpty, rulesStatus(rowCount, rulesFailed, rulesLoaded));
  }

  const leasesEl = document.getElementById("leases");
  const leasesTableEl = document.getElementById("leases-table");
  const leasesEmptyEl = document.getElementById("leases-empty");
  const leasesCountEl = document.getElementById("leasecount");

  function renderLeasesStatus(rowCount) {
    renderListStatus(leasesEmptyEl, leasesStatus(rowCount, leasesFailed, leasesLoaded));
  }

  async function refreshRules() {
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
    if (policySig !== null && sig !== policySig && current !== "policy") {
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
    updateIndicators();
  }

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
      const res = await fetch(`/api/egress/rules/${encodeURIComponent(btn.dataset.rule)}/revoke`,
                              { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // Says what the backend said. The refusals that matter here are 403 (a seed
        // rule, which the UI should not have offered) and 404 (already revoked, or
        // the table on screen is stale) — both are worth reading rather than
        // collapsing into "failed".
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

  // ── the live leases ───────────────────────────────────────────────────────
  // What is being allowed RIGHT NOW on a timer, which until this table existed was
  // the one kind of granted egress with nowhere to see it: a lease writes no rule, so
  // the standing-policy view is silent about it, and by the time an operator went
  // looking in the decisions log the grant might already have lapsed.
  //
  // It also makes the revoke button worth having. A grant nobody can see is a grant
  // nobody closes early, which would leave the configured duration doing the whole job
  // — and it is exactly that revocability that let the default be half an hour rather
  // than a few minutes.
  async function refreshLeases() {
    let rows;
    try {
      const res = await fetch("/api/egress/leases");
      // Checked like the rules poll: a 4xx body reaching .json() either throws
      // somewhere less obvious or parses into something that renders as "nothing is
      // leased", which is the reassuring reading and the wrong one.
      if (!res.ok) throw new Error(String(res.status));
      rows = await res.json();
    } catch (e) {
      // Keeps the rows and says so, rather than blanking a table whose whole subject
      // is what is in force at this moment — an empty one would read as "nothing is
      // granted" when what happened is that we stopped being able to tell.
      leasesFailed = true;
      renderLeasesStatus(document.getElementById("leases").rows.length);
      return;
    }
    leasesFailed = false;
    leasesLoaded = true;
    leasesById = new Map(rows.map(r => [String(r.id), r]));
    // The count is of LEASES, not of rendered rows: it answers "how much is granted
    // right now", and a number that shrank because four hosts folded into one line
    // would be answering a question about the table instead.
    leasesCountEl.textContent = rows.length ? `· ${rows.length} live` : "";
    leasesTableEl.hidden = rows.length === 0;
    const groups = groupLeases(rows);
    // Groups that no longer exist are dropped from the expanded set here rather than
    // left to accumulate: the key is (class, domain), so a group whose last lease
    // lapsed would otherwise keep its expansion and silently re-expand if the same
    // domain were leased again half an hour later.
    const live = new Set(groups.map(g => g.key));
    for (const key of [...expandedLeaseGroups]) {
      if (!live.has(key)) expandedLeaseGroups.delete(key);
    }
    leasesEl.innerHTML = groups.map(g => {
      if (!g.grouped) return leaseRow(g.leases[0], false);
      const open = expandedLeaseGroups.has(g.key);
      const cd = leaseCountdown(leaseRemaining(g.soonest, Date.now()));
      // The summary counts down by the group's SOONEST expiry, which is the next thing
      // about it that will change — and it carries `data-expires` like any other
      // countdown cell, so the one-second tick moves it with no special case.
      //
      // No revoke on this row. A button that ended four grants at once is a different
      // and much sharper action than the per-lease one, and it would need the confirm
      // step this table deliberately does not have; revocation stays where the thing
      // being revoked is named.
      const summary = `<tr class="lease-group">
        <td><button type="button" class="group-toggle"
              data-group="${esc(g.key)}" aria-expanded="${open}"
            >${open ? "▾" : "▸"}</button>
          <code>${esc(g.domain)}</code>
          <span class="ts">${g.count} hosts</span></td>
        <td class="ts">${esc(g.clientClass)}</td>
        <td class="lease-left${cd.urgent ? " urgent" : ""}"
            data-expires="${esc(String(g.soonest))}">${esc(cd.text)}</td>
        <td class="ts">first to lapse</td>
        <td></td></tr>`;
      return summary + g.leases.map(r => leaseRow(r, true, g.key, open)).join("");
    }).join("");
    renderLeasesStatus(rows.length);
  }

  // One lease as a row. `member` rows belong to a drawn group: indented, and hidden
  // while it is collapsed — HIDDEN rather than omitted, so expanding is a class flip on
  // rows already in the DOM and cannot race the four-second poll that would otherwise
  // have to re-render to produce them.
  //
  // `data-expires` carries the absolute deadline so the one-second tick can rewrite the
  // countdown without another fetch. The row is otherwise static, and re-polling once a
  // second to move a clock would be the firehose `/api/egress/leases` avoids by not
  // sending a remaining-seconds field at all.
  function leaseRow(r, member, groupKey, open) {
    const cd = leaseCountdown(leaseRemaining(r.expires_at, Date.now()));
    return `<tr${member ? ` class="lease-member" data-group="${esc(groupKey)}"` : ""}
        ${member && !open ? "hidden" : ""}>
      <td><code>${esc(r.host)}</code></td>
      <td class="ts">${esc(r.client_class || "")}</td>
      <td class="lease-left${cd.urgent ? " urgent" : ""}"
          data-expires="${esc(String(r.expires_at))}">${esc(cd.text)}</td>
      <td class="actor" title="${esc(r.granted_by || "")}"
        >${esc(shortActor(r.granted_by))}</td>
      <td><button type="button" class="revoke"
            data-lease="${esc(String(r.id))}">revoke</button></td></tr>`;
  }

  // Only the countdown cells, and only their text and urgency — never the row. Rebuilding
  // the table every second would drop a click landing on a revoke button at the moment
  // the tick fired, which is precisely when an operator is most likely to be pressing one.
  function updateLeaseCountdowns() {
    const now = Date.now();
    for (const cell of leasesEl.querySelectorAll("td.lease-left")) {
      const cd = leaseCountdown(leaseRemaining(cell.dataset.expires, now));
      cell.textContent = cd.text;
      cell.classList.toggle("urgent", cd.urgent);
    }
  }

  // Delegated for the reason the rules table's handler is: the tbody is replaced
  // wholesale on every poll, so a per-button listener would be re-bound every four
  // seconds and lost in between.
  //
  // No `confirm()`, where revoking a RULE has one. The asymmetry is the point: that
  // dialog guards an action with no undo, and this one has an obvious undo — the host
  // goes back to being held, so the next request raises a card and the operator can
  // grant it again. Making the reversible action as heavy as the irreversible one is
  // how a confirmation stops being read.
  document.getElementById("leases").addEventListener("click", async (ev) => {
    // Expanding a group changes nothing and reaches nothing, so it is handled before
    // the revoke path and takes none of its ceremony. The state lives OUTSIDE the
    // render (`expandedLeaseGroups`) because the table is replaced wholesale every four
    // seconds: held in the DOM alone, an expanded group would collapse itself on the
    // next poll, which is the same class of bug as re-rendering on the one-second tick.
    const toggle = ev.target.closest("button.group-toggle");
    if (toggle) {
      const key = toggle.dataset.group;
      if (expandedLeaseGroups.has(key)) expandedLeaseGroups.delete(key);
      else expandedLeaseGroups.add(key);
      const open = expandedLeaseGroups.has(key);
      // Applied to the rows already on screen rather than by re-rendering, so the click
      // does not depend on a fetch and cannot be undone by one in flight.
      toggle.textContent = open ? "▾" : "▸";
      toggle.setAttribute("aria-expanded", String(open));
      // Matched by comparing `dataset.group` rather than by an attribute SELECTOR built
      // from the key. Half the key is a host the agent chose, so a selector would need
      // escaping to be correct inside a quoted attribute value — `CSS.escape` does in
      // fact produce a string that matches there, but reasoning about why is work this
      // does not need to cost. A comparison has no escaping question to get wrong.
      for (const row of leasesEl.querySelectorAll("tr.lease-member")) {
        if (row.dataset.group === key) row.hidden = !open;
      }
      return;
    }
    const btn = ev.target.closest("button.revoke");
    if (!btn) return;
    const row = leasesById.get(btn.dataset.lease);
    if (!row) return;
    btn.disabled = true;
    try {
      const res = await fetch(
        `/api/egress/leases/${encodeURIComponent(btn.dataset.lease)}/revoke`,
        { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // A 404 here is the ordinary race, not a fault: the lease expired, or another
        // tab revoked it, between this table being drawn and the click. Said plainly
        // rather than as a failure, because the operator's intent — that host is no
        // longer leased — is now satisfied either way.
        window.alert(res.status === 404
          ? "That lease is already gone — it expired or was revoked elsewhere."
          : `Could not revoke: ${body.detail || res.status}`);
        btn.disabled = false;
        refreshLeases();
        return;
      }
    } catch (e) {
      window.alert("Could not revoke: the control plane is unreachable.");
      btn.disabled = false;
      return;
    }
    refreshLeases();
    // The revocation is an audited decision, so it belongs in the decisions view as
    // soon as it happened rather than on that view's own next poll.
    refreshAudit();
  });

  // ── writing a rule with no held request behind it ─────────────────────────
  // The config-first half of policy. Every other rule in the store is downstream of
  // something the agent already did — a seed entry, or a `+ persist` on a card — so
  // until this form existed, pre-authorizing a registry meant letting a build block
  // for the whole hold window first, and writing a block before anything asked for it
  // could not be expressed at all.
  const ruleFormEl = document.getElementById("rule-form");
  const rulePatternEl = document.getElementById("rule-pattern");
  const ruleActionEl = document.getElementById("rule-action");
  const ruleClassEl = document.getElementById("rule-class");
  const ruleAddEl = document.getElementById("rule-add");
  const ruleCancelEl = document.getElementById("rule-cancel");
  const rulePreviewEl = document.getElementById("rule-preview");
  // The id being edited, or null for the create form. Editing REUSES this form rather
  // than adding a second one: the fields are the same fields and the validation is the
  // same validation, so a separate editor would be a second place for the wildcard floor
  // and the conflict check to drift out of. It also means an operator cannot be halfway
  // through both at once.
  let editingRuleId = null;
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
    rulePatternEl.value = "";
    ruleClassEl.disabled = !clientClasses.length;
    ruleCancelEl.hidden = true;
    ruleAddEl.textContent = "add rule";
    renderRulePreview();
  }

  function currentPreview() {
    const rules = [...rulesById.values()];
    if (editingRuleId !== null) {
      // Looked up on every render rather than captured at entry, so a rule revoked or
      // changed under the form is noticed by the preview instead of being written over.
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
          body: JSON.stringify({ pattern: p.pattern, action: p.verb }),
        });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        // 404 is the one worth reading here and it has no analogue on the create path:
        // the rule was revoked while the form was open, so there is nothing to edit and
        // retrying cannot help.
        ruleNotice = { bad: true,
                       text: `Not saved: ${body.detail || `the control plane answered `
                                                        + `${res.status}`}` };
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

  // ── the approvals feed, and the reconnect it used to lack ─────────────────
  const conn = document.getElementById("conn");
  let es = null;
  let attempt = 0;
  let retryTimer = null;

  function setConn(text, cls) { conn.textContent = text; conn.className = cls; }

  function connect() {
    clearTimeout(retryTimer);
    es = new EventSource("/approvals/stream");
    es.onopen = () => {
      attempt = 0;
      streamUp = true;
      setConn("live", "up");
      updateIndicators();
      // Re-read the config on every (re)connect rather than once at load: a card can
      // only exist because this stream delivered it, so this is the earliest useful
      // moment — and a reconnect may be a restarted backend with a different window.
      refreshConfig();
    };
    es.addEventListener("pending", e => {
      const d = JSON.parse(e.data);
      lastSaturation = d.saturation || null;
      renderSaturation();
      renderPending(renderableHolds(d.holds));
    });
    es.onerror = () => {
      // The feed is the only thing that tells us about pending approvals, so losing
      // it means we are blind, not idle — say so in the light rather than sitting on
      // a stale green.
      streamUp = false;
      updateIndicators();
      // The distinction the old code missed: EventSource retries BY ITSELF only from
      // CONNECTING. A non-200 response or a wrong MIME type puts it in CLOSED for
      // good — and that is reachable in ordinary operation, because the relay answers
      // 502 while the backend restarts. The page then sat on "reconnecting…"
      // indefinitely: blind, and claiming otherwise. Reconnect by hand from CLOSED.
      if (es.readyState !== EventSource.CLOSED) {
        setConn("reconnecting…", "down");
        return;
      }
      es.close();
      const delay = backoffDelay(attempt++);
      setConn(`stream closed — retrying in ${Math.round(delay / 1000)}s`, "down");
      retryTimer = setTimeout(connect, delay);
    };
  }

  // ── MCP servers, tool policy and pinned allows: mcp.js ────────────────────
  mountMcp();

  // ── wiring ────────────────────────────────────────────────────────────────
  // `visibilityState` is absent in some non-browser hosts; treat unknown as
  // visible so a missing API degrades to the old always-poll behaviour rather
  // than to a page that silently stops updating.
  const visible = () => document.visibilityState !== "hidden";

  showView(current);
  syncNotifyButton();
  connect();
  refreshAudit();
  refreshRules();
  refreshLeases();
  // Also here, not only on stream open: the rule form needs the client classes, and it
  // has to work while the SSE feed is down — which is exactly when an operator is most
  // likely to be writing policy by hand rather than clicking cards.
  refreshConfig();
  // Once, not polled, and not on the stream's open either: the MCP tab must load while
  // the feed is down, for the reason the line above gives. Server registration changes
  // when an operator changes it, and each action refreshes after itself. Another tab
  // can change it too, which is why a server write fetches it again first
  // (`freshServer`) rather than trusting this copy.
  //
  // Tool rules and pins are the same kind of state and get the same treatment. The
  // INVENTORY is not: it changes because the gateway pushed, which happens without
  // anyone touching this page, so it is the one thing in this view that is polled.
  refreshServers();
  refreshToolRules();
  refreshInventory();
  refreshToolPins();
  // Both keep polling regardless of which VIEW is showing — otherwise the badges
  // could not report a hidden view's state, which is the whole reason they exist.
  //
  // A hidden TAB is a different matter. Nobody is reading either list, the badges
  // are not on screen either, and the cost is real now that /api/audit counts the
  // table on every call: left ungated this is a COUNT(*) every four seconds for as
  // long as the page is open on a machine that never closes it. The SSE stream is
  // deliberately NOT gated — a hold has a ~120s fuse and default-denies, so arrivals
  // must keep landing whether or not the tab is in front, and the title prefix is
  // how they get noticed.
  setInterval(() => { if (visible()) refreshAudit(); }, 4000);
  setInterval(() => { if (visible()) refreshRules(); }, 4000);
  // Gated and paced like the other two. The COUNTDOWNS do not depend on this poll —
  // they tick from each row's own deadline on the one-second interval — so what four
  // seconds bounds is only how long a lease that was revoked elsewhere, or that has
  // just lapsed, stays listed.
  setInterval(() => { if (visible()) refreshLeases(); }, 4000);
  // Gated on the VIEW as well, which the three above deliberately are not. They feed
  // badges that have to report a hidden view's state; this feeds a picker and a column
  // nobody can see from anywhere else, so polling it while another view is up would be
  // work with no reader. Paced to the gateway's own roster tick (GATEWAY_ROSTER_INTERVAL,
  // 10s) rather than to the four seconds the others use: pushes cannot arrive faster
  // than that, so a shorter poll could only re-fetch what it already has.
  setInterval(() => {
    if (visible() && current === "tools") refreshInventory();
  }, 10000);
  // Refresh IMMEDIATELY on return, rather than leaving up to four seconds of
  // stale-but-unlabelled data on screen at the moment attention comes back to it.
  document.addEventListener("visibilitychange", () => {
    if (visible()) {
      refreshAudit();
      refreshRules();
      refreshLeases();
      if (current === "tools") refreshInventory();
    }
  });
}

// Browser: run the page. Node (the unit tests): import the pure helpers and touch
// nothing — see the header comment on why importing this file must be side-effect free.
if (typeof document !== "undefined") { start(); }
export { lampState, backoffDelay, RECONNECT_MIN_MS, RECONNECT_MAX_MS };
