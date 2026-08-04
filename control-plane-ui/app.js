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
 * exported for the tests; everything that TOUCHES THE DOM lives inside `start()`,
 * which only runs in a browser. Requiring this file under node must therefore have
 * no side effects — if DOM work ever migrates to the top level, the node test fails
 * at `require`, which is the intended alarm.
 */

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

// What changed between the pending list on screen and the one just pushed, keyed by
// approval id so a surviving card is kept and updated IN PLACE.
//
// This is load-bearing, not a rendering nicety. The old code assigned the whole list
// to `innerHTML` on every push (up to once a second), so a card leaving the middle of
// the queue — which happens on its own, since an unresolved hold expires after
// ~120s — shifted every card below it upwards between the operator's eye and their
// click, on a row of buttons that GRANT EGRESS. It also discarded the `disabled`
// state a resolve in flight had just set.
function diffPending(shownIds, list) {
  const incoming = new Set(list.map(a => a.id));
  return {
    add: list.filter(a => !shownIds.includes(a.id)),
    gone: shownIds.filter(id => !incoming.has(id)),
  };
}

// A card that has left the pending queue (its hold expired, or someone else resolved
// it) is marked stale in place rather than yanked out, and swept only once removing
// it cannot move a button under the pointer — i.e. when nothing in the list is
// hovered or focused. The age cap keeps a cursor parked over the list from freezing
// it permanently.
const STALE_MAX_MS = 15000;
function shouldSweep(busy, ageMs) {
  return !busy || ageMs >= STALE_MAX_MS;
}

// ── the page ────────────────────────────────────────────────────────────────

function start() {
  const fmt = ts => new Date(ts * 1000).toLocaleTimeString();
  // Rules persist for weeks, so this one keeps the DATE. (A time-only stamp on
  // long-lived rows reads as out-of-order the moment they span midnight.)
  const fmtDay = ts => new Date(ts * 1000).toLocaleString();
  const esc = s => (s ?? "").toString().replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ── state the indicators read ─────────────────────────────────────────────
  let pendingCount = 0;      // approvals waiting on a human
  let streamUp = false;      // is the SSE feed actually delivering?
  let policySig = null;      // signature of the rules, to detect change
  let policyUnseen = false;  // policy changed while another view was showing

  // ── views ─────────────────────────────────────────────────────────────────
  const VIEWS = ["approvals", "decisions", "policy"];
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

  // ── pending approvals, keyed by approval id ───────────────────────────────
  const pendingEl = document.getElementById("pending");
  const emptyEl = document.getElementById("pending-empty");
  // id -> { el, actions, msg, state, staleAt }
  //   state: pending → resolving → resolved,  or  pending → stale
  const cards = new Map();

  const ACTIONS = [
    ["allow_once", "Allow once", "allow"],
    ["allow_persist", "Allow + persist rule", "allow"],
    ["deny_once", "Deny once", "deny"],
    ["deny_persist", "Deny + persist rule", "deny"],
  ];

  // Built with createElement/textContent rather than an HTML string: the host, url
  // and client on an approval are AGENT-CONTROLLED, and a text node needs no
  // escaping to be safe, so the question of whether esc() covers every context
  // does not arise for this list at all.
  function buildCard(a) {
    const el = document.createElement("div");
    el.className = "card";
    el.dataset.id = a.id;

    const host = document.createElement("div");
    host.className = "host";
    host.textContent = a.host + (a.port ? ":" + a.port : "");

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [a.proto || null, `requested ${fmt(a.ts)}`,
                        a.client ? `from ${a.client}` : null, a.url || null]
      .filter(Boolean).join(" · ");

    const actions = document.createElement("div");
    actions.className = "actions";
    for (const [action, label, kind] of ACTIONS) {
      const b = document.createElement("button");
      b.className = kind;
      b.textContent = label;
      b.addEventListener("click", () => resolve(a, action));
      actions.append(b);
    }

    const msg = document.createElement("div");
    msg.className = "cardmsg";
    msg.hidden = true;

    el.append(host, meta, actions, msg);
    return { el, actions, msg, state: "pending", staleAt: null };
  }

  function setMessage(entry, text, kind) {
    entry.msg.hidden = false;
    entry.msg.textContent = text;
    entry.msg.className = "cardmsg" + (kind ? " " + kind : "");
  }

  function disableActions(entry, off) {
    entry.actions.querySelectorAll("button").forEach(b => { b.disabled = off; });
  }

  function markStale(entry, text) {
    entry.state = "stale";
    entry.staleAt = Date.now();
    entry.el.classList.remove("busy");
    entry.el.classList.add("stale");
    disableActions(entry, true);
    setMessage(entry, text, "bad");
  }

  async function resolve(a, action) {
    const entry = cards.get(a.id);
    if (!entry || entry.state !== "pending") return;
    entry.state = "resolving";
    entry.el.classList.add("busy");
    disableActions(entry, true);
    setMessage(entry, "resolving…");
    try {
      const r = await fetch(`/approvals/${encodeURIComponent(a.id)}/resolve`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
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
        entry.el.classList.remove("busy");
        entry.el.classList.add(d.outcome === "allow" ? "done-allow" : "done-deny");
        // "standing rule for <host>" is true either way: a persist that hit the
        // backend's INSERT OR IGNORE wrote nothing only because a rule for that
        // host already existed.
        setMessage(entry,
          (d.outcome === "allow" ? "✓ allowed" : "✕ denied") +
          (d.persisted ? ` · standing rule for ${a.host.toLowerCase()}`
                       : " · this request only"),
          d.outcome === "allow" ? "ok" : "bad");
      } else if (r.status === 409) {
        // Backend says it is no longer pending (expired, or resolved elsewhere).
        // Re-enabling the buttons would only invite a second failing click.
        markStale(entry, "no longer pending — the hold expired or was already resolved");
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
      if (shouldSweep(busy, now - entry.staleAt)) {
        entry.el.remove();
        cards.delete(id);
      }
    }
    emptyEl.hidden = cards.size > 0;
  }

  function renderPending(list) {
    pendingCount = list.length;
    const { add, gone } = diffPending([...cards.keys()], list);
    for (const a of add) {
      const entry = buildCard(a);
      cards.set(a.id, entry);
      pendingEl.append(entry.el);
    }
    for (const id of gone) {
      const entry = cards.get(id);
      if (entry.state === "pending" || entry.state === "resolving") {
        markStale(entry,
          "no longer pending — the hold expired (default-denied) or was resolved elsewhere");
      }
    }
    sweep();
    updateIndicators();
  }

  // Sweeping is also driven by the operator leaving the list, so a stale card goes
  // as soon as removing it is safe rather than on the next tick.
  pendingEl.addEventListener("mouseleave", sweep);
  pendingEl.addEventListener("focusout", () => setTimeout(sweep, 0));
  setInterval(sweep, 1000);

  // ── audit + rules ─────────────────────────────────────────────────────────
  async function refreshAudit() {
    try {
      const rows = await (await fetch("/api/audit?limit=40")).json();
      document.getElementById("audit").innerHTML = rows.map(r => `
        <tr><td class="ts">${fmt(r.ts)}</td>
          <td><span class="tag ${esc(r.decision)}">${esc(r.decision)}</span></td>
          <td>${esc(r.host)}</td><td>${esc(r.reason)}</td></tr>`).join("");
    } catch (e) { /* transient; next poll retries */ }
  }

  async function refreshRules() {
    try {
      const rows = await (await fetch("/api/rules")).json();
      // Signature over pattern+action, not just the COUNT: a rule whose action
      // flipped is the change most worth noticing, and it leaves the count alone.
      const sig = JSON.stringify(rows.map(r => r.action + " " + r.pattern));
      if (policySig !== null && sig !== policySig && current !== "policy") {
        policyUnseen = true;
      }
      policySig = sig;

      document.getElementById("badge-policy").textContent = String(rows.length);
      document.getElementById("rulecount").textContent =
        rows.length ? `· ${rows.length} rule${rows.length === 1 ? "" : "s"}` : "· none";
      document.getElementById("rules").innerHTML = rows.map(r => {
        const wild = (r.pattern || "").startsWith(".");
        return `<tr>
          <td><span class="tag ${esc(r.action)}">${esc(r.action)}</span></td>
          <td><code>${esc(r.pattern)}</code></td>
          <td class="${wild ? "wild" : "ts"}">${esc(r.scope)}</td>
          <td class="ts">${esc(r.source)}</td>
          <td class="ts">${r.created_at ? fmtDay(r.created_at) : ""}</td></tr>`;
      }).join("");
      updateIndicators();
    } catch (e) { /* transient; next poll retries */ }
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
    };
    es.addEventListener("pending", e => renderPending(JSON.parse(e.data)));
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

  // ── wiring ────────────────────────────────────────────────────────────────
  showView(current);
  connect();
  refreshAudit();
  refreshRules();
  // Both keep polling regardless of which view is showing — otherwise the badges
  // could not report a hidden view's state, which is the whole reason they exist.
  setInterval(refreshAudit, 4000);
  setInterval(refreshRules, 4000);
}

// Browser: run the page. Node (the unit tests): export the pure helpers and touch
// nothing — see the header comment on why requiring this file must be side-effect free.
if (typeof document !== "undefined") { start(); }
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    lampState, backoffDelay, diffPending, shouldSweep,
    RECONNECT_MIN_MS, RECONNECT_MAX_MS, STALE_MAX_MS,
  };
}
