# Control plane — policy, holds, approvals and the audit views

The control plane's decisions as an operator meets them: how a card grants, how holds
behave under load, the audit views, the verbs over standing policy, and why the tool
surface gets tables of its own but shares the queue. Each sits beside the code that
decides it, in this directory, including where the approval page (`control-plane-ui/`)
presents it. What the control plane shares with the rest of the system stays in the
root, and this file does not repeat it:

- the control nets and the enforcers' bridges, `DESIGN.md` → "Control plane — policy,
  audit, hold-for-approval";
- what a client class is, `DESIGN.md` → "Policy is scoped to a client class";
- the hold-for-approval flow across the proxy, this backend and the page,
  `DESIGN.md` → "Hold-for-approval";
- why the frontend is a container of its own, `DESIGN.md` → "Approval UI — the one
  surface that can grant egress", and its browser boundary and the page, in
  `control-plane-ui/DESIGN.md`;
- the tool surface, `DESIGN.md` → "MCP gateway — governed tool capability".

## Client classes, where the control plane applies them

**Unplaceable fails safe, and cannot become a scope.** A client in no configured
range is `unclassified`: it matches no rule and is held, the same default-deny an
unknown host gets. `resolve` refuses to *persist* for one, because a rule scoped to
"whoever we could not identify" would grant to every future unidentified client —
the erosion this dimension exists to stop, reintroduced through the approval flow.
`allow_once` still works, so an operator is never stuck; the card says so before the
click rather than after.

**Existing rules were backfilled to the agent's class, and that is what they meant.**
They were approved while the proxy had one client population. Widening them to every
client would have granted the whole accumulated allowlist to `mcp-net`; narrowing
them differently would have revoked policy a human decided. Uniqueness became the
*pair* — the same pattern can be allowed for one class and blocked for another — so
this needed a table rebuild rather than an added column, and the store now has a real
`_migrate` (see the NOTE under `store._init_db`, and the SQLite DDL note in
`NOTES.md`).

## Granting from a card: persist and lease

**A `+ persist` says what it will write, lets you choose it, and asks twice.** This one
closes a sharp edge, not just a UX gap. `resolve` used to store the **requested host
verbatim** as the rule pattern, and two facts made that worse than it looks: a leading
dot is a *subdomain wildcard* in `_match`, and the host on an approval is chosen by the
**agent** — so a request for `.example.com` turned one click into a standing rule over
every subdomain, chosen by the agent rather than the operator and outliving the session
(revoking it is a separate, deliberate step — see "Taking a rule back").

The fix puts the pattern under the operator's control and out of the requester's.
`_persist_candidates(host)` derives a **bounded set** in the backend, beside the
`_match` it must agree with: the exact host, `.host`, and `.<last two labels>` (the
registrable domain, which is what one usually wants when a service spreads over many
hostnames) — narrowest first, so the default is the safest. Leading dots, trailing FQDN
dots and case are normalized away, IP literals get no wildcard at all (`.1.2.3.4` is
nonsense), and a one-label wildcard is never offered because `.com` as a standing allow
rule would end governance for an entire TLD in one click. `ResolveRequest` gained an
optional `pattern`, **validated against that set server-side** and refused with a `400`
*before* the conditional UPDATE — so a rejected pattern neither resolves the hold nor
consumes it, and the operator can choose again rather than losing the approval to a
race they didn't cause. The candidate list travels with each pending approval, so the
UI offers exactly what the backend will accept instead of reimplementing the derivation
in JavaScript and drifting into offering a pattern that then 400s.

On the card, both `*_persist` buttons now open a confirm panel — justified by
**irreversibility rather than risk**: undoing a mis-click means hand-editing SQLite in
a named volume. It names the pattern verbatim in a `<code>` (not described — exact-host
and whole-subtree differ by one dot, and that dot *is* the grant), offers the
candidates in a select, shouts specifically about a wildcard ("covers … and every
subdomain of it, including hosts nothing has requested yet"), and labels its own button
`Confirm — allow .example.com from now on`. It sits **below** the action row, which
stays exactly where it was: if the confirm button appeared where the pointer already
is, a double-click on "Allow + persist rule" would sail straight through the
confirmation it had just opened — the same concern that drove keyed rendering. The
action row is disabled while the panel is open, so the only live buttons are Confirm
and Cancel, and Escape backs out.

Known limitation, stated rather than hidden: with no public-suffix list the two-label
suffix of `example.co.uk` is `.co.uk`, which grants far more than it appears to. That
is exactly why the operator picks and why the pattern is shown verbatim in a step where
it is still reversible. The audit trail also learned to say whether policy changed at
all — the reason now reads `human approval (standing rule written) [peer=…]` vs
`(this request only)`, from the durable `mode` column. Naming the *pattern* there would
need a new column on `approvals`, which is a migration step and not a DDL edit (see the
NOTE above `_seed_if_empty`); the rule itself is recorded with its pattern and
`source='operator'`
in the rules table, and every later use of it is audited as `allowed by rule (…)`.

**A lease is the third grant duration.** The resolve vocabulary had two rungs — this
request, or a standing rule forever — and nothing between them, so the answer for a
host an agent is about to hit twenty times was either twenty clicks or permanent
policy. `allow_lease` is the middle rung: it allows one exact host, for one client
class, until `CONTROL_LEASE_SECONDS` elapses.

It gets its **own table** rather than an `expires_at` column on `rules`, by the same
test that gave tool policy its own (see "Tool policy gets its own table" below): the
rows are a different *kind*, not a differently keyed one. A lease belongs to the card it
was granted from, self-destructs, and must not appear in the answer to "what have I
permanently allowed?". The consequence that settled it is that every existing reader of
`rules` is a reader of standing policy — the rules view, the conflict check in
`resolve`, `revoke_rule`, the edit path and `_decide` — so a nullable expiry would have
taught each of them "except the expired ones", which is five places to drift instead of
one new pass.

Four properties are decisions rather than defaults, and each is one someone would
otherwise add later as an obvious improvement:

- **A lease loses to a block.** `_decide` runs blocks, then standing allows, then live
  leases, and the ordering is what guarantees it: a timed grant must never be a way
  around policy an operator wrote down. Leases come *last* among the two allows because
  when both cover a host the standing rule is the more informative audit reason —
  which of two allows answers a request cannot change the answer, only the record.
- **No breadth choice.** `_persist_candidates` exists because a permanent rule needs an
  operator decision about how wide it is; a lease answers that question by expiring
  instead, so it is always the exact host and the card stays one click. The visible
  cost is real and accepted: one page load often touches several hosts, so a lease for
  `example.com` can be followed immediately by a card for `cdn.example.com` — and the
  live-lease table then carries a row for each. It **folds** those under their
  registrable domain rather than truncating to a `+N more`, because a live grant hidden
  behind a click is a grant nobody revokes, which was the whole reason for showing them.
  The fold is keyed `(client class, domain)` and not domain alone, the same way
  `api_rules` groups standing policy by class first: two populations under one heading
  would read as one grant covering both tenants. Folding is a display fix for a cost the
  exact-host decision creates; offering `.example.com` as a lease pattern would remove
  it at the source, and the expiry makes that far safer than it is for persist — which
  is the shape of the next decision here if the folding stops being enough.
- **No `deny_lease`.** An unmatched host is *held*, not denied, so a timed deny would
  mean "stop raising this card for a while" — silencing a looping agent, which is a
  different feature wearing this one's name.
- **An unclassified client cannot be leased to**, exactly as it cannot be persisted for.
  Expiry bounds how *long* a grant lasts and says nothing about *who* it covers, and a
  grant scoped to "whoever we could not identify" covers a population rather than a
  client.

Revocation is why `CONTROL_LEASE_SECONDS` defaults long (the value is beside it in
`control-plane/policy.py`) rather than to the few minutes a safety floor would want.
`POST /api/egress/leases/{id}/revoke` closes a grant early, so
the duration stopped being a safety floor and became an ergonomics number — long enough
that an agent finishes what it was doing without a second card. The action is named
`allow_lease` and not `allow_30m` for the same reason: the number is configuration, and
a button that spelled it into the page would keep saying it on a store set to something
else. `/api/config` serves it so the button labels itself.

**A lease does not release a card already raised for a sibling request.** A lease is
keyed `(host, client_class)`; a hold's group key is `(client, host, port, proto)`
(`holds._group_key`). Those do not line up, so granting a lease from one card leaves
another card for the *same host on a different port*, or from a different client in the
same class, still pending — its waiter blocking until `CONTROL_HOLD_TIMEOUT` elapses
and it default-denies, even though leased policy now allows it. The agent's retry then
sails through on the live lease.

Accepted rather than fixed, and the alternative is why. Re-deciding every pending card
when a lease is granted would make a card resolvable by something other than a human
clicking it, which means a third writer to the `approvals` row — and "exactly one of
the resolve and the timeout flips it out of `pending`, via a conditional UPDATE SQLite
serializes" is the invariant that keeps a resolve landing as a hold expires from
telling the agent *deny* while recording *allowed* and writing a grant. Trading that
for one avoided card is the wrong trade. The common case is collapsed anyway: one agent
retrying one host is already one card (see "Duplicate holds share one card — and that
changes what a click grants"), so the uncollapsed shapes are the rarer two-port and
two-client-one-class ones.

**A persist cannot overwrite, so one that would is refused.** `rules.pattern` is
`UNIQUE`, and the insert was `INSERT OR IGNORE` — so persisting a pattern that already
carried the **opposite** action wrote nothing, while the endpoint returned
`persisted: true` and the card confirmed a standing rule. Deny-over-allow was the
dangerous direction: the operator believed a subtree was permanently blocked, and every
later request to it was allowed without so much as raising a hold.

Reachability is the part worth recording, because it looks unreachable at first. A
conflicting rule cannot already exist when the hold is raised — every persist candidate
is derived from the held host and matches it, so a pre-existing rule would have *decided*
the request instead of holding it. The conflict can therefore only be created **while the
hold is pending**, which gives one shape: two concurrent holds for sibling hosts, resolved
with the same broadened pattern in opposite directions. That is what a burst of holds
across one domain looks like, and it reproduces in a few lines.

Refused before the `UPDATE`, matching the rejected-pattern branch beside it, so the
approval stays pending and decidable rather than half-applying with the decision recorded
and the rule not — which is exactly the state that branch's comment already called "the
worst of both". Overwriting was the alternative and was declined: `ON CONFLICT DO UPDATE`
would make a click the operator was never shown silently flip standing policy, and it
would pre-empt the rule-mutation design rather than settle it. That design is now built
and named (see "Changing a rule is one operation, not two"), which is what keeps this
refusal honest rather than merely restrictive — replacing a rule is available, as an act
the operator asks for by name and that records what it displaced.

Two supporting changes make the refusal legible rather than surprising. `persisted` now
reports **whether a row was written**, read from the insert's `rowcount` instead of from
what was asked for, with `already_present` carrying the harmless same-action case — so a
card stops claiming a write it did not make. And each offered pattern travels with any
rule already holding it, so the confirm panel says so *before* the click. Both halves are
required: the panel is prevention, and the backend check covers the race that is the only
way the conflict arises at all.

## Holds under load: the banner, grouping and the caps

**The banner for requests that never became cards.** Over any of the four hold caps
(see "Four hold caps: two nouns, two scopes"), `/authorize` fails closed **without creating an
approval row** — correct, since default-deny is the right failure and a held request
pins a threadpool worker this control plane shares across every sandbox. But it meant
the agent was being refused while the operator's queue looked exactly like a quiet
afternoon. The refusal existed only as a reason string in the audit table.

Two things about the shape of that problem drove the design, and the second overturned
the obvious fix:

- **The count cannot be taken from the visible cards.** The cap is measured against the
  in-memory hold set; the card list comes from `status='pending'` rows. They normally
  agree and diverge exactly when it matters — after a restart the table can carry
  pending rows with no live hold behind them — so a card-derived gauge would be
  confidently wrong in the one situation it exists to report. `_saturation()` counts
  the in-memory waiter set, and reports the card count beside it (see duplicate
  grouping below, which gave the two numbers a second way to diverge).
- **Saturation is a burst, so a gauge is nearly useless.** Holds drain in seconds; by
  the time anyone looks, the level is healthy again. What was filed here was an
  `n/16 holds` counter, and that would have reported almost nothing. The **rejection
  record** is the load-bearing part — a count plus the last one's time, scope and host,
  kept in memory — and the gauge is context beside it, shown only from 75% of the cap.

In-memory is not a weakening: the deny is already audited with its reason, so this is a
display index over a durable record. It does mean a restart resets the count, which is
why the payload carries `since` and the banner says *3 since 14:02* rather than a bare
3 — and why it renders nothing at all at zero, since an in-memory counter must never be
able to present itself as a positive all-clear.

Two decisions worth keeping. The banner **does not auto-clear**: the failure it reports
is precisely that nobody was looking, so expiring the notice after a minute would
re-create the bug for the only population it serves. Emphasis decays (`recent` → `past`
at 60s) and dismissal is explicit. And it says nothing in its headline about *which*
cap fired: the operator's response is the same either way, and per-client detail would
be the first thing in this UI to expose client identity, so it sits in the detail line.

**Dismissal is server-side, and it is a high-water mark.** It began as page state,
which meant a reload restored the banner — worse than offering no button, because the
operator believes they cleared something and the page disagrees on refresh. It now
POSTs to `/api/saturation/ack`, and three properties of that endpoint are load-bearing:

- **A count, not a "dismiss".** Acknowledging *the two I have read* leaves a third that
  landed while the click was in flight still unread. Zeroing a counter would swallow
  it, and rejections arrive in bursts — exactly when that window is open.
- **Monotonic and clamped to what actually happened.** A lower count never
  un-acknowledges (a stale tab, a replay), and a count above the current total is
  clamped, because otherwise an unvalidated client number silences *future* rejections
  until they catch up. Same reasoning as validating `pattern` in `resolve`.
- **The window moves with it.** `since` becomes the acknowledgement time, so the banner
  reports what has happened *since you dismissed* and the number always describes the
  span the stamp names. The count shown is therefore the **unread** delta, not the
  lifetime total — which the audit table holds durably anyway.

Not audited, deliberately: the rejections are already in the audit table with their
reasons, and acknowledging one changes what the banner displays while touching no
evidence. A row here would put a non-decision in the decisions log.

The dismiss path also reaches into `start()`, which is unverified by choice — so it is
built to make its own mistakes loud. `ackCount()` is a pure function purely so the
optimistic hide and the POST body are *one expression* and cannot disagree, and the
handler adopts the acknowledgement the backend **echoes back** rather than assuming its
own number stuck, so a wrong count re-raises the banner immediately instead of failing
silently. Those two lines are each other's safety net — drop the echo and the detector
for the first mistake goes with it — which is why a source-level guard asserts both.

It rides the existing SSE payload rather than a new endpoint — `/approvals` and the
stream now return `{holds, saturation}` — so a rejection reaches the banner within a
second through the push the page already listens to, with no new relay-allowlist entry
and no poll to lag behind the burst. That carries one constraint into the backend: the
stream emits on *serialized payload change*, so every field must be stable while nothing
happens. An `elapsed_seconds` convenience would defeat the change detection, silence the
heartbeat, and make an idle page a 1 Hz firehose — hence absolute timestamps only, with
the client doing the arithmetic exactly as the countdown already does. A test pins the
field set for that reason rather than for tidiness.

**Duplicate holds share one card — and that changes what a click grants.** A retrying
agent asks the same question repeatedly, and each attempt used to become its own card
and its own slot. With `CONTROL_MAX_PENDING_PER_CLIENT` at its default, as many
retries as it allows filled the client's whole budget with copies of one question and
the next was refused with no card at all — the failure the banner above exists to
report, reached by the most ordinary
behaviour an agent has. Identical holds now attach to the existing card, keyed on
`(client, host, port, proto)`: `client` is in the key because approving one sandbox's
request must not release another's, and `method`/`url` are out because they are exactly
what varies between retries, so keying on them would defeat grouping in the case that
motivates it.

What made this more than a UI tidy-up is that **the two caps were counting the same set
while protecting different things**, and nothing made that visible until duplicates
stopped being distinct. The global cap bounds *blocked workers* — the availability of
governance for every other sandbox. The per-client cap bounds *cards on the operator's
screen* — attention. Grouping forced them apart. Skip that split and the feature is
cosmetic; the fifth retry is still refused at four, just after showing one card instead
of four. Forcing them apart is also what left a gap on the other diagonal — see "Four
hold caps: two nouns, two scopes" below.

The governance cost is real and is paid explicitly. **"Allow once" now releases every
request on the card**, which widens what a single click grants — the precise class of
surprise the hold mechanism exists to prevent. What keeps it honest is a count on the
card, and the count is *live*: a retry can join between the render and the click, so a
number frozen at first paint would understate what the button is about to do. Two
invariants sitting in different files back that up. The joining window is closed in the
critical section right after the decision commits — a duplicate can still slip into the
narrow gap between the commit and that close, but it is identical by group key
(client/host/port/proto), so it rides the same grant just made for that key and a deny
is fail-safe regardless; and
a joiner inherits the card's **existing deadline** rather than starting a fresh window,
or an agent retrying on a loop would push the deadline out indefinitely and the
countdown on the card would be a lie.

Grouping is a concept of the screen and the worker pool, and deliberately **not** of the
record: every joined request still writes its own audit lines with its own `method` and
`url`, and the joiner's hold reason names the card it attached to — so the log explains
why four requests produced one approval without the reader needing to know grouping
exists. One consequence worth stating, because several waiters now wake together on one
row: only one of them wins the conditional expiry `UPDATE`, so the outcome each reports
is read from the row's *status* rather than from "did I win". Branching on the latter —
which is what the single-waiter code did — told every loser that a human had rejected
their request.

**Four hold caps: two nouns, two scopes.** Grouping split *cards* from *waiters*, and
the caps were left describing three of the four cells: global waiters, per-client cards,
and nothing bounding one client's share of the workers. That missing cell was a real
defect rather than a tidiness gap. Duplicates cost the card caps nothing by design, so
one agent retrying one host filled the entire global pool **from a single card**, and
every other sandbox was refused until those holds drained. The per-client cap could not
see it at any setting, because it was counting the wrong noun.

So the grid is completed and the names say which cell they are:
`CONTROL_MAX_PENDING` / `CONTROL_MAX_PENDING_PER_CLIENT` count **cards** and protect
attention; `CONTROL_MAX_WAITERS` / `CONTROL_MAX_WAITERS_PER_CLIENT` count **waiters**
and protect the threadpool. Three consequences worth recording, because none is
recoverable from the constants:

- **`CONTROL_MAX_PENDING` changed meaning**, from waiters to cards. It is the same name
  for a different noun, which is the one kind of change a config file cannot announce,
  so `_bootstrap` logs all four with their units at every boot.
- **The ordering rule is the actual fix**: every cap a request will draw on is checked
  *before* the early return that consumes it. The bug was positional — the join
  returned above the per-client check — and `_reserve_hold` now runs global waiters,
  per-client waiters, join-and-return, global cards, per-client cards. A cap check
  after an early return is the shape to look for. The tool surface follows the RULE and
  lands on the opposite ORDER: its join sits below both caps, because a tool joiner
  costs no row, no card and no attention, where an egress joiner still pins a worker.
  Copying the sequence rather than the reasoning would have denied a call whose question
  is already on the operator's screen.
- **The defaults are chosen so every cap can fire first.** Cards are always ≤ waiters,
  so a card cap set equal to its waiter cap is dead code that no test would notice.
  The defaults in `control-plane/holds.py` keep all four live — each card cap strictly
  below its waiter cap — and a test asserts the relationship rather than the numbers.

Zero means different things by scope, deliberately: on a global cap it refuses
everything (fail-closed, and a plausible way to say "stop holding anything"), on a
per-client cap it disables the cap — the fail-closed reading would make every client's
first hold impossible, which cannot be what setting it meant.

**Hold bounds are fail-closed, so their values stay env vars.** The obvious home for the
four caps and `CONTROL_HOLD_TIMEOUT` would be the store, on the reasoning that governs
everything else here: policy belongs in the crown-jewel volume, and every change to it
is audited. That reasoning does not reach these values, and the distinction decides
where the next governed service's numbers live too.

Every one of these knobs can only produce a **deny**. Over a cap, `/authorize` refuses
fail-closed; with no decision inside the window, the hold default-denies; an allow
requires a human resolution, recorded with its actor. A wrong value therefore makes the
system more restrictive, never more permissive — which is what separates these from
rules. "Everything consequential is audited" exists to record capability *granted*, and a
number that cannot grant capability is not what it governs. The record already carries
the part an investigation needs, too: every deny names **which** bound fired, in its
reason string. The magnitude of that bound is readable off the running config, which
`_bootstrap` prints with its units at every boot.

For values changed this rarely — compose sets none of them — a compose diff is also
the better trail than a row in a named volume, carrying a message, an author, a date and
a review. It is the argument `CLAUDE.md` already makes for commit messages, applied to
configuration.

So per-surface hold config is a **naming** problem rather than a storage one, and the MCP
gateway's hold window is a second constant with its own name, beside the tool policy that
does get its own table (see "Tool policy gets its own table" below). What survives of
2c-2 is rule **editing**, where the audit argument does apply, because a rule grants.

What none of this excuses is that both cross-value invariants here went unenforced at
runtime. The four caps must each be able to fire, and the test asserting it covers only
the defaults — an override could silence a cap with nothing to notice. And the egress
proxy's authorize timeout must outlast the hold window, or the proxy abandons a request
whose card is still on the operator's screen and the click decides nothing; those two
live in different images and neither can see the other. Both are now guarded — see
"A dead cap warns, it does not refuse to boot" for why the first warns rather than
exiting, and `ProxyOutlastsTheHoldWindowTests` in `tests/test_topology.py` for the
second, which compares the two images' defaults because compose overrides neither.

**A dead cap warns, it does not refuse to boot.** `_assert_listeners_separated` exits on
a wildcard management bind, and copying that shape here would be wrong. A cap that cannot
fire is not a containment failure: cards are bounded by waiters regardless, so the system
stays bounded and only the operator's belief about *which* limit binds is wrong. Refusing
to start would trade that for an outage of the governance authority, which denies every
sandbox's egress — a strictly worse failure than the one being prevented. The zero cases
settle it: a global cap of zero legitimately means "refuse everything" and a per-client
zero legitimately disables that cap, so a naive ordering assertion would refuse to boot
on two documented settings. `_warn_on_dead_caps` therefore exempts zeros and prints
beside the caps line that `_bootstrap` already emits.

## The audit views

**A decision row says whose request it was.** `/api/audit` carries `client`, the peer
address the proxy records on every decision. On a control plane **shared across every
sandbox** that is the difference between a record and half a record: "egress to
pypi.org was allowed" does not answer the question an audit trail exists for once two
agents are running.

**`client` asked; `actor` acted.** `client` is only ever the sandbox whose request the
row concerns, and `client_class` is derived from it alone, so a row no sandbox asked
for — a rule written, a server registered, an inventory push — carries neither, and an
egress rule's class goes in its reason. Whoever performed the act, when that is not
the client, is `actor`: the operator behind a click, or the gateway behind a push. A
row with both is a human answering a sandbox's request. Kept apart because a search
for one sandbox or one class must return what that population asked for, not the
configuration written about it. `ActorColumnTests` holds every writer to this. On the
page the two share one `who` cell, the actor on a labelled line of its own (`auditRow`
in `control-plane-ui/app.js`): apart in the store, adjacent where a reader asks "who".

This is not a reversal of keeping client identity out of the saturation banner's
headline. That banner is a glanceable alert where a bare IP is noise; this table is the
forensic view where *who* is the entire question. The two decisions point the same way —
put the address where someone is reading carefully, not where they are only glancing.

Both proxy hooks — `http_connect` and the plaintext `request` — derive `client` the
same way, and `test_a_connect_and_a_request_report_the_client_the_same_way` holds
them to it, because one derivation in two places is exactly the shape that drifts
silently: an HTTP decision stored against no client at all stays invisible for as long
as nothing renders the column.

What stays out of the response matters as much: `url` is agent-controlled and unbounded,
and `method`/`port`/`proto` are noise in a forty-row glance. All four remain in the
table and in `make logs-cp`. This endpoint is a legible summary, not the record — and
the record is what the invariant is about. (2c-1 gave the record its own endpoint,
which does serve those four; see "Browsing it" below for why that is a scoped
reversal rather than a change of mind.)

**The list groups; the record does not.** `/api/audit` folds rows whose *displayed*
fields are identical into one, with a count and the span's start. It exists because a
client retrying a permanently-refused host on a timer — a background exporter or
updater denied by a standing rule, once a minute — writes 1440 identical rows a day,
and a forty-row list of them covers under an hour. Everything else, including the
fronting refusal that "Ingesting the decisions the proxy makes alone" in `DESIGN.md` was
built to surface, falls off the bottom before
anyone looks.

Three choices in it are less obvious than the feature:

- **Group by key over a window, not by consecutive runs.** Runs were the first idea and
  the live log disproved it: two periodic sources (the refused retry loop, the lifeline
  allows) interleave and chop each other's runs into singletons, so run-collapsing folds
  almost nothing on exactly the data that motivated it.
- **The key is precisely the set of displayed fields.** That is what guarantees no two
  rows in the list can look identical — rows that would look the same *are* the same
  group — and it settles the edge cases by itself: `client` is in the key because one host
  refused for two sandboxes is two facts, while `port`/`proto` are out because keying on
  what is not shown splits a group into rows a reader cannot tell apart.
- **The scan is bounded by event count, not by a time window.** Cost then stays fixed as
  the table grows, and coverage adapts on its own — about a day when something is
  retrying every minute, months when nothing is. A time bound would go empty on a quiet
  system, which is the one thing a decisions list must not do.

It also introduced one honest limitation, which is filed here rather than fixed: a
grouped `client` is an **address**, not a sandbox. Docker reassigns `172.30.0.2` to
whichever container starts first, so a group spanning days covers every sandbox that
held that address. Concurrently the column still does its job — two live sandboxes are
two rows — but folding a fortnight into one line makes an address look like an identity,
which an ungrouped row (one instant) never did. A real fix needs stable per-sandbox
identity, and the only sources are the Docker socket (which the egress proxy must never
hold) or a launcher-to-control-plane path that does not exist; neither is worth
inventing for a label, so `first_ts` stands as the cue that a long span is involved.

**Browsing it: two views, not one view with a page parameter (2c-1).** The trail was
the artifact the whole design exists to keep trustworthy and the only way to read it
was a forty-row live summary — so the interface could not answer a question about a
*specific* request, and "browse the audit log" meant `docker compose exec` and SQL
against the crown-jewel volume. It is now two endpoints over one table
(`control-plane/audit.py`), and the split is the design decision rather than an
implementation detail:

- The **glance** (`/api/audit`) folds, bounds its scan by event count, and cannot
  page — a group is defined *relative to its window*, so paging it would either split
  one group across two pages with partial counts on each, or need a second definition
  of what a group is. Filtering it has no such problem.
- The **record** (`/api/audit/events`) is one row per decision, keyset-paged on
  `(ts, id)`, and serves the columns the glance drops. An offset would have been
  simpler and wrong: this table takes an insert per governed request, so
  `LIMIT/OFFSET` drops rows between pages exactly while something interesting is
  happening, and a `ts`-only cursor mis-pages whenever two rows share a timestamp,
  which nothing rules out (`time.time()` need not advance between two writes).

Three consequences worth stating because each was a choice against the obvious one.
**A view searches exactly what it displays** — the same discipline the group key
follows, which is why `url` is searchable in the record and not in the glance rather
than being either everywhere or nowhere; a result whose visible content does not
contain what was typed is the worst kind of list. **`total` follows the filter**, or a
complete filtered view reports itself as truncated on every query, and the response
says whether it filtered at all so the frontend can say "matching" rather than implying
the store is that size. And **the record serving `url`/`method`/`port` is a scoped
reversal** of what the glance deliberately omits: unboundedness is handled where it
belongs — capped on write, page-bounded on read, escaped under a CSP that gives an
injected string nowhere to go — and a forensic view without the field that identifies
the request cannot do its job.

## Standing policy and its three verbs

**Standing policy is visible in the UI (`GET /api/egress/rules`).** The UI showed
pending approvals and recent decisions but never the **rules** — the thing that
actually decides every request. So answering "what have I permanently allowed?" meant
`docker compose exec` plus SQL against the volume, and in practice rules accumulated
across weeks unseen. Policy that is invisible drifts, and every `*_persist` approval
writes to it, so the read-only view is the small half of the rule-management item and
removes most of its risk. Three deliberate choices: **unpaginated**, because a
silently truncated view of policy is worse than none (the opposite of `/api/audit`,
which is capped precisely because it grows without bound); **blocks listed first**, so
the listing reads in the same precedence order `_decide` applies rather than
alphabetically; and the **match scope named in words** (`host + subdomains` vs
`exact host`, derived by `_pattern_scope` beside the `_match` that implements it, so
the frontend cannot drift from the real semantics) — because a leading-dot wildcard
otherwise looks exactly like an ordinary hostname in a list. Read-only on purpose:
this makes policy reviewable, it does not add mutation. `_pattern_scope` earns its
keep twice over now — the same words label the candidates in the persist confirm step,
so what an approval promises to write and what the policy view later shows it wrote are
described identically.

**Taking a rule back (`POST /api/egress/rules/{id}/revoke`).** A governance plane that can
grant but never revoke is half a plane, and until this landed a mistaken `+ persist`
was permanent short of hand-editing SQLite in the volume. Four decisions worth
recording, because none of them is the obvious one:

- **The two directions are opposites, and the confirm carries the difference.**
  Revoking an *allow* tightens: the host reverts to unknown and the next request is
  held. Revoking a *block* loosens — an explicit operator denial becomes a request
  that can then be approved, quite possibly by someone who never knew it had been
  deliberately refused. Both land on `hold`, so nothing structural distinguishes
  them; only wording can. `revokePreview` states the consequence rather than the row
  ("requests to X will no longer be blocked… and can then be allowed"), the same
  discipline the persist confirm uses, and an unrecognised action warns as the
  dangerous direction rather than the safe one.
- **Seed rules cannot be revoked, and the refusal is in the BACKEND.** Their source
  of truth is `policies/egress-allowlist.txt`, a reviewed file under version control,
  and a click that left the file disagreeing with the store would make the file a
  lie. It also closes a trap for free: `_seed_if_empty` re-reads that file whenever
  the rules table is empty, so a store whose every rule could be revoked would
  resurrect the entire seed allowlist on the next restart. With seed rules
  undeletable that state is unreachable — which is why the property is asserted as
  behaviour rather than left as a consequence. The cost is that retiring a
  *transitional* seed entry (npm, PyPI, GitHub) is a migration shipped beside the
  code that replaces it, not an operator action. That is the right shape for a
  versioned change to a declared policy.
- **Keyed on `id`, not pattern.** A pattern-keyed delete would have to exact-match the
  stored `rules.pattern` string, inheriting every normalization subtlety (case, a
  leading wildcard dot) and possibly missing the row the operator is looking at; an
  integer `id` is unambiguous. The relay bounds that segment to digits, and the bound is
  load-bearing rather than tidy: it lands in a URL path, so a looser class admits
  dot-segments that httpx resolves upstream into a different path than the allowlist
  approved.
- **Deletion, not a tombstone, with the audit row as the history.** Dead rows in the
  rules table would have to be filtered by every reader of it — including `_decide`,
  the one place a mistake is unrecoverable. Provenance is recorded exactly as
  `resolve` records it: editing standing policy is more consequential than any single
  egress decision, and nothing recorded that it had happened at all.

**Writing a rule with no request behind it (`POST /api/egress/rules`).** Every other
rule in the store is *downstream of something the agent already did*: a seed entry is a
declared allowlist, and a `*_persist` approval can only write about a host that was
requested. So the operator could answer questions and never state a position —
pre-authorizing a registry meant letting a build block for the whole hold window first,
and writing a **block** before anything asked for it was not expressible at all, because
there was no card to click. This is the config-first half, and the reasoning that spans
files is what does *not* carry over from the persist path:

- **The pattern is caller-supplied, so it must be validated rather than constrained.**
  A persist is safe because `_persist_candidates` derives a bounded ladder from an
  observed host and `resolve` re-checks the choice against it. Here there is no observed
  host, so that guarantee is unavailable and `policy._rule_error` replaces it. Both live
  in `policy.py`, beside the `_match` that defines what a pattern means, so the grammar
  has one home rather than a copy per entrance — and `_normalize_pattern` is shared for
  the sharper reason that `_decide` strips a trailing FQDN dot from the *host*: a
  pattern that keeps one matches nothing while reading, in the rules view, as policy in
  force. An **inert rule is worse than a refused one**, which is why malformed input is
  a 400 and not a stored row.
- **The wildcard floor applies to `allow` only.** `.com` as an allow ends governance for
  a TLD in one call and nothing afterwards raises a hold to notice it by; as a block it
  only tightens, announces itself the first time anything is denied, and is revocable.
  Refusing both would make the broadest blocks — the ones most worth writing — the ones
  this endpoint cannot express.
- **`source` is server-set to `operator` and is not a field on the request model.**
  `seed` is the value `revoke_rule` refuses to delete, so a caller that could set it
  could write an *unrevocable* rule — and one that would also stop `_seed_if_empty` from
  ever re-reading the file. The two guards are in different modules and only compose
  because neither trusts the caller for this one string.
- **The class is checked against `CLIENT_CLASSES`, and the UI is told the list.** An
  unlisted class inserts cleanly, lists cleanly and decides nothing, so a typo is the
  quiet failure this endpoint is most exposed to. `GET /api/config` carries the names
  for the form to offer; deriving them from the *rules* instead would be worse than
  guessing, because a class with no rules yet is exactly the one an operator needs to
  write the first rule for. `UNCLASSIFIED` is absent by construction rather than by
  exclusion — `_parse_client_classes` refuses it as a name.
- **A create still never replaces a rule.** An existing rule with the opposite action is
  a 409 with the conflict described; the same action is a 200 that reports it wrote
  nothing. Same refusal and same reasoning as the persist path (see "A persist cannot
  overwrite, so one that would is refused"). Replacing one is a named operation of its
  own (see "Changing a rule is one operation, not two") — the distinction being that an
  edit records what it displaced, where a create that silently overwrote would be the
  same act with nothing in the record saying so.
- **The page mirrors three refusals and no more.** `createPreview` in `app.js` checks
  the wildcard floor, a conflicting rule and an identical one — the floor because it is
  the refusal that would otherwise arrive only *after* clicking a button labelled with a
  grant the operator wanted, the other two because their fix is on screen already.
  Everything else a pattern can be wrong about is left to the backend and rendered from
  its `detail`. That keeps the duplicated policy down to one constant instead of a
  second copy of the grammar, and the constant plus the normalizer are held equal across
  the two languages by tests rather than by intent.

This is also a **third way to grant egress**, and the first that does not begin with a
request. It sits on the management listener with `resolve` for that reason, and the
audit vocabulary (`audit.DECISIONS`) gains `create` beside `revoke` so a rule that
appeared without a card is distinguishable in the record from one a human approved at
one — which, once it is sitting in the table, it otherwise is not.

**Changing a rule is one operation, not two (`POST /api/egress/rules/{id}/edit`).**
Create and revoke could already express every end state; what they could not express is
a *transition*. Narrowing `.example.com` to `api.example.com` meant revoking and
re-creating, which cost two things. One is the record: two rows that each describe half
of an intent, with nothing tying them together and no order guaranteed between them in a
busy log. The other is a window in which the subtree was unknown and every request under
it was held, one card at a time, for as long as the second step took. That window failed
to `hold` rather than to allow, which is why this was tolerable rather than a hole — but
tolerable is not atomic, and the operator paying for it was the one tightening policy
under load. A single `UPDATE` in one transaction closes it.

Four things follow, and each is a consequence of *which* endpoint this resembles:

- **It carries create's exposure, so it gets create's validation.** There is no
  `_persist_candidates` bounded set behind an edit any more than behind a create, so
  `policy._rule_error` is the whole of what stands between this and a rule matching more
  than the operator meant. The wildcard floor matters more here than anywhere: an edit is
  the one operation that can walk a narrow allow outward a label at a time.
- **The seed refusal is the same one `revoke_rule` makes, for a stronger reason.** A
  revoked seed rule at least *leaves*, and `store._seed_if_empty` re-reads the file on the
  next empty-table start. An edited one stays, indexed and deciding, while
  `policies/egress-allowlist.txt` says something else about the same host.
- **The class is not editable.** Moving a rule between client classes takes policy from
  one population and gives it to another, which is two changes wearing one audit row —
  the exact defect this endpoint exists to remove, reintroduced from the other side. The
  request model has no such field and the UI locks the picker; revoke-then-create stays
  the honest shape for a re-scope.
- **The conflict check must exclude the rule being edited.** A rule always holds its own
  pattern, so a naive uniqueness check refuses every action flip. The backend answers it
  with an `id<>?` clause and `editPreview` in `app.js` mirrors it; both are tested,
  because a page that previews a collision with itself makes the operation look broken
  rather than refused.

`audit.DECISIONS` gains `edit`, and the row carries **both** states. That is the whole
difference from what it replaces: the record now says what a rule was, not only what it
became. The UI's confirm says the same thing in the same shape, which is why
`editPreview` flags *both* loosening directions where `createPreview` only has a new
action to judge — narrowing a block loosens too, since the hosts falling out from under
it stop being denied.

## The tool surface: its own tables, the one queue

**Tool policy gets its own table, not a new scope on `rules`.** The three states are
the same three (`allow` / `deny` / `ask` against `allow` / `block` / `hold`), and the
two-column key looks like a near-fit — a tool rule wants `(tool, server)` where an
egress rule has `(pattern, client_class)`. Both are false friends. `action` is the
only column that carries over.

`client_class` is not a label anyone writes: `policy._client_class` derives it from
the peer address, and it names a *network*. The server on a tool call is the name the
gateway dialled — the other of the two identities "Per-server identity has two
different answers" in `DESIGN.md` keeps apart. Sharing the column puts both meanings in
one table, sorting `mcp` (a network whose egress is being decided) beside `mcp-github`
(a server whose tools are) in a view that groups by that column precisely so unrelated
rules are never adjacent (`api_rules` in `control-plane/api_egress.py`). `pattern` fares no
better: its leading-dot wildcard and the breadth ladder built over it
(`policy._match`, `policy._persist_candidates`) describe a host namespace, and a tool
name has no hierarchy to widen along.

Deeper than the key, and the reason this is a different *kind* of row rather than a
differently keyed one: an egress rule decides a whole request, because the host is
the unit of decision, while a tool name is only a prefix of one — the payload carries
the rest. `ask` not decaying (see "`ask` does not decay" in `DESIGN.md`) is a
consequence of that same fact, and it makes pinning an argument a predicate over a
payload rather than a string in a column. The write paths also run opposite ways: egress
policy accumulates from approvals, with editing retrofitted onto it; tool policy is
configuration first, and the one thing that grows by use, a pin, grows only beneath an
`ask` an operator configured.

Cost breaks the same direction, which settles the choice rather than makes the case
for it. A new table is a `CREATE TABLE IF NOT EXISTS` over no existing rows; a shared
one needs a discriminator inside `UNIQUE(pattern, client_class)`, and SQLite cannot
add a uniqueness constraint by `ALTER`, so that is the drop-copy-rename rebuild
`_migrate` in `control-plane/store.py` already had to write once. What the two
surfaces share is a *pattern* and not code — the backend derives a bounded candidate
set, the operator picks from it, the chosen value is shown verbatim — and the ladders
themselves have no common implementation: one is host-breadth, the other
argument-shaped.

**Pins get a table of their own, and a rule with pins cannot be revoked.** A rule is
one row per (server, tool); a pin set is a predicate over the payload, and a tool can
have several, so `tool_pins` sits beside `tool_rules` rather than in it.
`UNIQUE(server, tool, pins_json)` means what it says because a pin set is stored in
one canonical form (`policy._canonical_pins`). Revoking a rule that still has pins is
refused rather than cascading, as revoking a server that still has rules is. The
reason is specific: a pin outliving its rule decides nothing until a rule for the
same tool returns as `ask`, and then the pin returns with it, with nobody having
asked for it. Editing the rule's action keeps its pins, and `/api/mcp/pins` reports
them as not deciding while the rule is `allow` or `deny`.

**`approvals` splits the same way; the operator's queue does not.** The approvals
table is egress-shaped exactly as `rules` is — `host`, `port`, `proto`, `client`,
`client_class`, `method`, `url`, against a tool ask's server, tool and arguments — so
it splits for the same reasons, and `control-plane/holds.py` splits with it along a
seam it already has. The in-memory registry (events, deadlines, waiter counts, the
caps) is keyed by approval id and is entirely payload-agnostic; `_group_key` and
`_list_pending`'s SELECT and `persist_options` are not. The gateway brings its own
rim and reuses the core.

What must **not** split is the pending queue: one list, one SSE stream, one saturation
accounting. The principle is not that reads merge — it is that a union is worth
serving only when the union is itself the object. Nobody asks for every rule across
every subsystem, which is why the rules views stay per-surface. "How many decisions
are waiting, how long have I got, and is the queue at capacity" is asked constantly
and cannot be answered one surface at a time.

What forces it is that **a partly connected merged view is indistinguishable from an
empty one.** A pending decision is time-bounded and blocks work; with one stream,
"disconnected" is a single honest boolean the UI can show, whereas two streams merged
in the browser render a silent subset when one drops — and a subset of a queue looks
exactly like an empty queue. Saturation reporting pulls the same way, though less
hard: over a cap a request fails closed *without raising a card* (the invisibility
`_SATURATION` exists to fix), and an operator should not have to check two banners to
learn that governance is refusing things.

An earlier draft of this section rested the argument on a shared worker pool as well.
That leg is gone: a tool ask no longer pins a control-plane worker and no longer draws
on `MAX_WAITERS` (see "An `ask` answers immediately" in `DESIGN.md`), so the two
surfaces have separate capacity and the "one queue empties while another consumes the
pool" case cannot arise. The decision stands on the stream.

Merging the queue merges little else. The tables are separate, `resolve` keeps
per-surface action sets (dispatched on the card's kind, since the approval id already
determines its table), each surface renders its own card, and the merged payload is a
union of two per-surface builders rather than one query over a discriminator column.

## The store, and the URLs

*Schema note (read before adding a column).* The store is a long-lived named volume
that deliberately outlives container and image churn, and `CREATE TABLE IF NOT
EXISTS` is a no-op on an existing table, so a column added to the DDL alone is
missing on every store already in the field. **Migration is therefore not optional
here, and the alternative to it is `make destroy`** — which discards the policy rules
and the audit history, i.e. the crown jewels. The mechanism (a stamped schema
version, append-only ordered steps, and the three edits a new column needs) lives in
`control-plane/store.py`: read the NOTE below `_init_db` before touching the schema.

Standing egress policy has all three verbs — create, revoke, and change as one atomic
operation rather than revoke-then-create (see "Changing a rule is one operation, not
two") — and no config surface beside them: the per-proxy values are fail-closed bounds
rather than policy, so they stay env vars and a second governed service names its own
(see "Hold bounds are fail-closed, so their values stay env vars"). Tool policy is a
table of its own (see "Tool policy gets its own table" above), so the gateway needs no
config surface either.

**URLs carry the surface.** `/api/egress/rules` is the standing-policy view and
`/api/egress/rules/{id}/revoke` takes a rule back; `/api/mcp/rules` and
`/api/mcp/servers` are the gateway surface's, and they precede the gateway rather
than arriving with it — tool policy is configuration first, so the surface that
states it is usable before anything consumes it. A bare `/api/rules` would be a
generic name on a specific thing. The lifecycle endpoints keep
unprefixed names (`/approvals`, `/approvals/stream`, `/approvals/{id}/resolve`), so
the URL shape states the split itself: prefixed is per-surface policy, unprefixed is
the one queue every surface feeds (see "`approvals` splits the same way" above). The
shape rejected is `/api/rules?surface=…`, a discriminator over a single path
— the storage mistake "Tool policy gets its own table" above turns down, wearing an API
hat.
