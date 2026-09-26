# Control-plane UI — the approval page and its browser boundary

The frontend that puts the control plane in front of a human: the guards between the
published loopback port and a browser, how the page keeps a failing state visible, and
how it shows a tool ask's payload. Every block here is about code in this directory and
its tests. What the page presents is decided in the backend, and designed in
`control-plane/DESIGN.md`. What stays in the root is the frontend's place in the system,
and the threat no guard in a browser can close: `DESIGN.md` → "Approval UI — the one
surface that can grant egress".

## The browser boundary

**Browser-facing guards on the frontend (and their honest limit).** The frontend
publishes the only API that can GRANT egress — `POST /approvals/{id}/resolve` is
self-approval if reached — on host loopback, with no auth. Loopback binding is not a
defense against **DNS rebinding**: a page the operator visits can point its own name
at `127.0.0.1`, at which point it is *same-origin* with the frontend, so there is no
preflight and responses are readable — it can list the pending approvals (obtaining
the `uuid4` ids a blind attacker cannot guess) and resolve them. That is the vector
that matters most here, but plain CSRF is not self-blocking either, as this once
claimed: a JSON body does not force a preflight (a typeless `Blob` is sent with no
`Content-Type`, which is CORS-safelisted, and FastAPI parses an untyped body as
JSON), and the `resolve` id is not blind for a tool ask, because the agent is handed
it in the pending result. Four structural guards sit in front
(`control-plane-ui/app.py`):

- **Host allowlist** (`CONTROL_UI_ALLOWED_HOSTS`, default `127.0.0.1,localhost,::1`)
  — closes rebinding. Rebinding *requires* the attacker's own name in `Host`, and
  `Host` is a forbidden header JS cannot set, so this removes the attack class
  rather than raising its cost. Compared without the port on purpose: the port is
  irrelevant to the guard, and ignoring it keeps one list valid for both the
  published port (configurable, so pinning it would be wrong twice over) and the
  in-container healthcheck's `:8090`. Import fails closed
  on an empty list (`_assert_host_guard_configured`, mirroring the egress addon).
- **Cross-origin state changes refused** — `Sec-Fetch-Site` when present, else
  `Origin` compared as a full authority (host *and* port) against the request's own
  `Host`: a page on another localhost port is another origin, and a page the agent
  wrote into the workspace is one dev server away from being served on one. Both
  headers absent means no browser is calling, so there is no CSRF to stop and
  refusing would only break scripting. Load-bearing, per the paragraph above.
- **Relay path allowlist** — only the paths the UI actually uses are proxied. The
  backend surface is not uniformly browser-appropriate: reaching `POST /authorize`
  from a page means forged audit rows and consumed hold slots on the governance
  authority. The old catch-all `/{path:path}` relayed it.
- **Refusal to be embedded** — closes **clickjacking**, which is the gap the first
  three guards structurally cannot see, and it was open until now. An attacker page
  that frames the UI's loopback origin produces a request with a perfectly legitimate
  `Host: 127.0.0.1` (guard 1 satisfied) by GET, which is deliberately allowed
  cross-origin (guard 2 does not apply) — and the framed document's own `resolve`
  POST then reads as `Sec-Fetch-Site: same-origin`, because from inside the frame it
  genuinely is. Cross-origin *reads* stay blocked throughout, but a UI-redress attack
  needs no read: overlay a decoy on the invisible frame and the operator's click
  lands on the real "Allow + persist rule" button, granting standing egress. The
  unguessable `uuid4` id that defeats a blind CSRF is no help either — a click does
  not need to know it. Closed by `frame-ancestors 'none'` in the CSP below, plus a
  `Sec-Fetch-Dest: iframe|frame|embed|object` refusal in `_guard`; the second is not
  redundant, it makes the attempt a 403 in *this app's* log rather than a message in
  the operator's browser console, which is the visibility this repo falls back on
  wherever prevention is browser-dependent.

**Security headers on every response, and why the script became a file.** A
`_security_headers` middleware sets a Content-Security-Policy (plus `X-Frame-Options`,
`nosniff`, `no-referrer`) on everything this app serves, refusals included — it is
registered *after* `_guard`, which under Starlette makes it the **outer** middleware,
so a refused framing attempt is itself delivered under the policy. The policy is
`default-src 'none'` naming only what the page uses, and it is applied by assignment
rather than `setdefault` because relayed backend responses pass through it and an
upstream header must not be able to weaken the frontend's policy.

The directive that shaped a file move is `script-src 'self'`. The page's behaviour used
to be inline in `index.html`, which would have forced `'unsafe-inline'` and reduced the
whole policy to decoration — on the one page that renders **agent-controlled** strings
(the requested host, url and client on each approval card). So the script now lives in
`control-plane-ui/app.js`, served from this origin (never a CDN, for the same reason the
favicon is an inline data URI: a governance UI must not fetch its own control logic from
a third party). The payoff is doubled: it also made the frontend testable for the first
time. Because that invariant spans two files and re-inlining would break nothing
*visible*, a test asserts the page carries no inline script or event handlers, and
another asserts every `getElementById` in `app.js` matches an id that exists in
`index.html` — the split's new failure mode is a renamed id, which is a `null`
dereference that would otherwise appear only in a browser.

## The page

**Frontend mechanics — the rationale lives in `control-plane-ui/app.js`.** A cluster of
UI behaviours exists for one reason: the operator must never be *silently* blind, because
an unseen hold default-denies after `CONTROL_HOLD_TIMEOUT`. Each is documented at its
point of use in `app.js`; only the cross-cutting points are kept here.

- **Reconnect.** An `EventSource` retries itself only from `CONNECTING`; a non-200 (the
  relay's 502 during a backend restart) puts it in `CLOSED` for good. So the relay
  returns a clean 502 and `app.js` reconnects by hand with bounded backoff, instead of
  sitting on "reconnecting…" while blind — the exact failure the traffic light exists for.
- **Keyed, non-jumping list + minimum dwell.** Cards are keyed by id and updated in place
  (`diffPending`) so a row of egress-granting buttons never shifts under the pointer; a
  departed card is marked stale in place and swept only after a per-reason dwell
  (`DWELL_MS`), so an expiry — the one departure that reports a governance failure —
  stays on screen long enough to read. Cards are built with `createElement`/`textContent`,
  so the agent-controlled host/url/client render as text with no escaping question.
- **The card reports its own outcome and deadline.** A resolve shows its result inline,
  the pattern echoed from the *backend* so it names what was stored; the hold countdown
  comes from `GET /api/config` (`{"hold_timeout"}`, on the relay allowlist — the one
  cross-component piece) with a two-clock clamp so skew makes it wrong, not absurd. Fails
  soft: no config, no countdown, page otherwise unaffected.

**Presenting a payload for approval.** A schema-driven view gets most of the way —
the tool's JSON Schema gives a field tree with each field's description beside it —
but the rule the approval UI already established governs: the **raw payload is
authoritative and always one click away**, exactly as the persist-confirm shows a
chosen pattern verbatim rather than describing it. A prettifier that truncates,
unescapes or reorders is a place to hide something from the person deciding, so the
card's indentation is whitespace between tokens (`indentPayload`), with one exception.
A `\n` inside a string is drawn as `↵` and a line break, because a PR body as one line
of escapes is the text most worth reading and the hardest to read. The mark is what
keeps a break inside a string from passing for one between fields, so a payload
carrying its own `↵` is named in the note, and shows it in the string's colour rather
than the mark's. Nothing else is unescaped, and the escaped
view shows `\n` as written. The other change to content runs the opposite way: a
payload carrying invisible or non-ASCII characters is shown *escaped* by default, `\u202e` spelled out where it
sits, with the raw text one click away — because a bidi override lets the browser
reorder what the operator reads while the bytes stay as they are, which is the
reordering this rule forbids, performed by the renderer (`payloadHazards` in
`control-plane-ui/payload.js`). The
limit worth stating rather than engineering around: an operator cannot judge an
opaque identifier, and resolving one would mean the control plane making its own MCP
calls — new capability on the crown-jewel container and a fine SSRF surface.

**A pin is shown stricter than a payload.** The same rule applies — the stored form,
never a parsed copy — and one level further: every non-ASCII character in a pin is
spelled out, not only the invisible ones (`pinText` in `control-plane-ui/mcp.js`). A
payload is often prose, where an em dash is ordinary and alarming about it teaches the
operator to click past the warning. A pin value is an identifier that a future call
must equal exactly, so a U+0430 CYRILLIC SMALL LETTER A in a pinned `owner` is a grant
for a different owner than the one on screen, and there is no prose to protect.

**`[hidden]` wins globally, and the test guards the rule rather than a list.** The
script hides things by setting `.hidden`, which relies on the user-agent rule
`[hidden] { display: none }` — and that rule loses to *any* author rule setting
`display` on the same element, because author beats user-agent at equal specificity.
`.saturation` and `.countdown` both set `display: flex`, so on its own the attribute
would leave a dismissed banner and an empty countdown row on screen. The stylesheet
therefore carries a global `[hidden] { display: none !important; }` — the one context
where `!important` is the right tool rather than a smell, since outranking author
`display` declarations on hidden elements is the rule's entire job.
`test_the_hidden_attribute_survives_this_stylesheet` guards **the global rule**, not a
list of elements, and fails on a narrower re-patch: one rule makes the class
impossible, whereas an enumeration is something someone has to remember to extend.

**Timestamps are formatted, not deferred to the viewer's locale.** A correctness call,
not a preference: `toLocaleString()` renders the same audit row as different dates for
different browsers, and a record of *when* something happened cannot mean two things. So
the UI fixes ISO-8601 ordering, local time, 24-hour, with the UTC instant on the row's
`title`, and pins the format in a test. The shaping and the `Number(null)`→`1970-01-01`
guard live in `app.js`; `NOTES.md` has what six locales produce.

**Empty vs. stale, on both polled lists.** "Nothing has happened yet" and "the poll
failed" must not render identically — and the header cannot disambiguate them, because
`conn` reports the SSE *stream* while the decisions and policy tables are filled by a
*separate poll* that can be failing while the stream is healthy. So each list tracks two
facts (has a load ever succeeded; did the last one fail), keeps its rows on a failed
refresh while saying they may be stale, and stays silent before the first response. The
three-state logic is shared (`pollStatus`) with per-view wording a test keeps distinct —
a stale **policy** table is worse than a stale decisions one, since it misstates what is
currently allowed before an operator decides a hold. Details in `app.js`.

**The frontend's own tests.** `tests/test_control_plane_ui_js.py` runs the pure helpers
under `node` (skipped when node is absent, the way `make lint` skips a missing linter)
and asserts in Python, so failures read like the rest of `tests/`. What each helper
must hold is in its test's name; that file is the inventory, not this one. Everything
that touches the DOM runs from `start()`, which only a browser calls — so importing
a module under node must be side-effect free, and the test asserts that too: if DOM
work migrates to import time, the `import` throws and the file cannot quietly become
untestable again.

The page is ES modules, one file per surface, and `app.py` serves them by name from
`UI_MODULES` — a list a human wrote, with the path behind each name built from it at
import; the name off the URL only picks an entry, so the route hands out nothing else
in the directory. The list has two other copies with no
compiler between them, the Dockerfile's `COPY` lines and the modules' own imports, and
a test holds each equal to it; a module left out of the image would 404 in the
container with every unit test green, and stop the whole page at the entry's first
`import`.

Two cross-file couplings have no compiler between their ends, and a test stands in at
each. The first asserts every `getElementById` in `app.js` matches an id in `index.html`. The
second asserts **every path the page `fetch`es is either served here or on the relay
allowlist** — the deliberately narrow allowlist is what keeps `POST /authorize`
unreachable from a browser, and the cost of that narrowness is that adding a call
without adding its route yields a 403 visible only to an operator loading the real page
against a real backend. That is precisely how `/api/config` would have failed; the
guard was verified by removing the route and watching it fail. The guard earned its keep
a second time on `POST /api/saturation/ack`: removing the route from the allowlist fails
the suite rather than the browser.

The **converse** is asserted too — every relayed route must be called by the page
(`test_every_relayed_route_is_actually_called`) — and that direction is about a
different failure. A route with no caller does not break anything, which is exactly
the problem: nothing would reveal it if it were wrong, and a relayed route the page has
stopped using still hands whatever it serves — a superseded form of the pending list
carries every pending host, client and URL — to any caller that gets past the Host and
`Sec-Fetch` guards. The test carries no exception list on purpose: a route that must
stay uncalled should arrive with its reason attached, as an edit someone has to
justify.

A further guard is source-level rather than behavioural: `shouldSweep`'s dwell floor
defaults to `0`, so **dropping the argument at the call site** would restore the
swept-in-a-second bug with every unit test still green, since they exercise the function
directly. The call site is therefore asserted to pass a dwell. Ugly, and the honest
alternative — making omission impossible — is what was done for `markStale` instead;
`shouldSweep` keeps its primitive signature because that is what makes it cheap to
assert at the boundaries.

**`start()` is deliberately unverified, and that is a decision rather than a gap.** Its
DOM path is covered only by the guards above; the card wiring (countdown, confirm panel,
pattern select, dwell) was checked once against a throwaway stub DOM under node and not
kept. The cost is real and measurable: mutation testing was run twice across this work,
and every mutation inside a pure helper was caught while every mutation inside `start()`
survived. So the position needs an argument, not a shrug.

The argument is what a frontend bug can and cannot do here. It **cannot** produce an
out-of-policy rule: `resolve` validates the action against a fixed set and the pattern
against `_persist_candidates` re-derived from the *durable approval row*, never from
anything the page sends, so no breakage in the UI reaches an outcome a hand-crafted curl
could not have asked for. What it **can** do is cause an operator to approve one of the
*legitimate* options they did not intend — sharpest case, a preview reading
`example.com` while the select's value is `.example.com`. That display-versus-value
divergence is the entire residual risk.

Three things already act on exactly that risk, and the load-bearing one is tested. The
card's outcome message reports `d.pattern` **from the backend response**, not from the
click, so a wrong send announces itself in words at the moment of the mistake, while the
operator is still looking (`test_the_response_names_the_pattern_that_was_stored` asserts
the backend echoes what it stored). The audit reason then says whether standing policy
was written, and the Policy view shows the rule with its scope named. That is the same
**detection-where-prevention-is-not-available** pattern this repo already applies to
host-local approval forgery — see "Approval provenance — detection where prevention is
not available" in `DESIGN.md`.

The alternatives were weighed and declined. A hand-rolled stub DOM would catch the
value-flow class but largely asserts its own shape and needs editing on every markup
change. A headless browser is the only thing that catches *layout-level* deception (a
warning that renders invisible, a confirm button that lands under the pointer), but it
does not merely add a dependency — it breaks the property that this suite runs
identically on a host, in CI, **and inside the sandbox image**, where it would skip for
want of egress to install a browser. That is precisely the "silently checks nothing where
the agent actually runs" failure `DOCKADE_REQUIRE_TOOLS` exists to prevent. Revisit if
the UI ever gains a control whose mistake the backend cannot refuse and the response
cannot report.

**Tabbed views + traffic-light favicon, both driven by "don't hide a failing state."**
Approvals / Audit / Policy are tabs (via `location.hash`, with arrow-key nav), and
the hazard tabs introduce — a hidden view drifting unnoticed — is answered by badges
that keep polling even the hidden views: a live pending count, and a policy **unseen**
marker keyed on pattern+action (a flipped rule leaves the count unchanged). The favicon
is an inline SVG data URI (never a CDN — a governance UI must not fetch its own control
logic or icons per page load) whose lit lamp means **red = blind** (SSE down; an unseen
hold default-denies after `CONTROL_HOLD_TIMEOUT`) rather than red = denied, with the
pre-JS fallback amber ("unknown", not "all clear") and a `(n)` title prefix so the
background tab nobody watches still shows the count. Rendering specifics (lamp opacity,
`href`-reassignment throttling) are in `app.js`. Past the tab strip, **an opt-in desktop
notification** is the only indicator that reaches an operator who is looking at
something else — which a ~120s fuse makes the case worth covering, since the tab is not
what they are watching while the hold burns. Opt-in literally: the Notification API
needs a user gesture to ask, so a header button asks and the browser's own permission is
the setting. It notifies per ARRIVAL and only past the first push (a reload delivers the
whole queue as arrivals), stays quiet while the approvals view is on screen, and closes
a notice when its hold leaves the queue however it left. The rules are `approvalNotices`
/ `shouldNotify` in `app.js`. Its one environmental dependency: a secure context, which
`127.0.0.1` satisfies over plain HTTP and another origin would not — so the button says
so rather than going quiet.
