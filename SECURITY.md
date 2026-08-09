# Security policy

dockade is a **capability-limited sandbox for an AI coding agent**. Its whole
purpose is to be a security boundary, so a report that the boundary can be crossed
is genuinely valuable and welcome.

It is also a boundary with a **documented ceiling**, and that shapes this policy
more than anything else here. [`DESIGN.md`](DESIGN.md) states plainly where
containment stops, and this file carries two lists that follow from it:
[Known open findings](#known-open-findings) — real defects, already found, not yet
fixed — and [Out of scope](#out-of-scope) — weaknesses reasoned about and
consciously accepted rather than overlooked. Reading both first will save you time,
because between them they cover most of what a first look at this repo turns up.

## Reporting a vulnerability

Use **GitHub's private vulnerability reporting**: go to the repository's
**Security** tab and choose **Report a vulnerability**. That opens a private thread
visible only to you and the maintainer, and it is the only channel — please do not
open a public issue for something in scope.

No email address is published on purpose. A private advisory thread keeps the
report, the discussion and the eventual fix in one place, and there is no inbox to
migrate or address to harvest.

**There is no bug bounty.** This is an unfunded personal project; reports are
answered because they are useful, not because they are paid.

**Response is best-effort from a single maintainer.** No SLA is promised, because
none could be honoured. Expect an acknowledgement within a few days and a slower
substantive reply. If a report goes unanswered longer than that, a nudge on the same
thread is welcome.

**Testing is entirely local.** There is no hosted instance and no service to attack
— you run the whole thing yourself with `docker compose up`, so no authorisation
question arises and nothing you do while testing affects anyone else.

## In scope

Anything that **crosses a boundary the design claims to hold**:

- **Egress that evades the governed proxy** — any route off-box from a sandbox that
  is not the egress proxy: a direct route, a DNS-based path, IPv6, or a data-plane
  service acting as an unintended egress hole.
- **A route from a sandbox to the control plane.** The agent must never reach it
  (`sandbox-net` vs `control-net`). A way to reach `control-net`, the control-plane
  backend, or the approvals API from inside a sandbox is the highest-severity class
  here, because reaching `POST /approvals/{id}/resolve` means the agent can approve
  its own held egress.
- **Making the proxy allow a host that policy blocks or holds** — SNI or Host-header
  fronting, a CONNECT-authority mismatch, a hostname-normalisation difference between
  the proxy's guard and the control plane's `_decide`, or anything else that turns a
  deny/hold into an allow.
- **Defeating the audit record** — causing a governed request that is never logged,
  or forging, altering or suppressing audit rows. "Everything consequential is
  audited" is a stated invariant; a governed path that bypasses the log is a bug even
  if the request itself was legitimate.
- **Privilege escalation or escape inside a sandbox** — regaining dropped
  capabilities, defeating `no-new-privileges`, becoming root, or escaping the
  container.
- **Credential exposure** — anything that puts the Anthropic session credential, or
  any credential the design says stays *outside* the sandbox, somewhere it should not
  be.
- **A control-plane-ui relay bypass** — reaching `POST /authorize` or any
  non-allowlisted backend path through the frontend, or overriding the pinned
  upstream host.
- **A browser-facing vector that survives the current guards** — a DNS-rebinding,
  CSRF or clickjacking path that still works against the Host allowlist, the
  cross-origin refusal, the embedding refusal and the CSP.

## Known open findings

Distinct from [Out of scope](#out-of-scope): those are accepted and will not change.
These are **known, not accepted, and not yet fixed** — each is waiting on a design
decision, a test environment, or a change riskier than the defect itself. They are
published so a reporter does not spend a weekend rediscovering one, and because they
are all readable from the source anyway; listing them costs nothing that is not
already given away by the code.

A report that **deepens** one of these is welcome and useful — a working exploit, a
consequence wider than described here, or a case the note misses. A report that
restates one is duplicate work, which is what this section exists to prevent. The
reasoning for each lives in `DESIGN.md` or beside the code and is referenced rather
than restated.

- **The relay guard's resolve branch is beatable by DNS rebinding.**
  `proxies/egress/addon.py` `_forbidden_reason` re-resolves instead of pinning the
  resolved address, so a name can change answers between the check and the dial. The
  *consequence* is capped rather than the gap closed — see the API-surface split in
  `DESIGN.md`, which is why even a total bypass reaches a listener that can only ask
  a policy question. What that cap does **not** cover is the metadata IP, the Docker
  host and the LAN: they answer on the same `:80`/`:443` the port gate permits. On
  the developer-machine target this is a pivot onto the operator's own network rather
  than cloud-credential theft, which is why it is ranked here and not higher. The fix
  is to dial the address that was checked, or re-check the peer after connect.

- **Standalone mode can reach every port on the Docker host.**
  `sandbox-common/init-firewall.sh` pins the host-gateway rule to a `/32`, which
  removes the sibling-container surface, but the rule still allows *all ports* on the
  host itself. Governed mode — the default — drops the rule entirely, so this is
  standalone-only and compounds the accepted "standalone is weaker" position below.
  Scoping it needs a proxy-less host to validate, because it can break DNS and egress
  in ways a governed host cannot exercise.

- **One sandbox can exhaust the hold queue for every other sandbox.** In
  `control-plane/holds.py` `_reserve_hold`, the global cap counts *waiters* while the
  per-client cap counts *cards*, and the join path returns before the per-client check
  is reached. Grouped duplicates from a single client can therefore fill the global
  cap (`CONTROL_MAX_PENDING`) and every other sandbox is refused until those holds
  drain. Refusals fail closed, so this costs availability, not containment. The fix
  needs a per-client *waiter* bound — a semantics decision rather than a patch.

- **A duplicate can join a card in the instant after it is decided.** `resolve`
  commits the decision outside `_LOCK` and closes the group inside it, so a request
  can arrive between the two and inherit an outcome it did not wait for. Bounded: a
  joiner is identical by group key, and a deny is fail-safe. The gap and the reason it
  is tolerated are at the `_close_group_locked` call site in `control-plane/app.py`.

## Out of scope

These are **known and accepted**, each with its reasoning in `DESIGN.md`. They are
not secrets and not oversights, so please raise them as **public issues** if you
disagree with the reasoning — that is a design discussion, not a vulnerability
report.

- **A process already running on the host forging an approval.** The approval API is
  reachable by anything on the host, and the frontend's guards are browser-enforced,
  so they do not constrain it. Authentication would not close it either: any
  credential at rest on the host is readable by that same process. Closing it needs a
  human-presence gesture the host cannot replay. See *Browser-facing guards on the
  frontend (and their honest limit)*.
- **The read-write workspace bind mount as a delayed path to host execution.**
  Anything the agent writes there — a git hook, a build script, `.envrc`, an editor
  task file — runs outside the sandbox the next time the host touches the repo. The
  launcher's workspace guard hard-refuses dangerous roots and warns on nearby
  credentials, which narrows the path rather than closing it. This is the
  acknowledged cost of the one deliberate host coupling.
- **`WebSearch` being unauditable.** It executes server-side on Anthropic
  infrastructure, so no local firewall or proxy can see it. It is read-only and left
  enabled as a standing, revisitable decision. See *Server-side execution: accepted
  governance blind spots*.
- **Any Claude Code settings file being bypassable**, including a
  `permissions.deny` and a local `managed-settings.json`. No client settings file is
  a containment boundary here, and under organisation authentication the local
  managed file is not even loaded. Hard policy belongs in the org admin console. See
  *Managed settings are NOT an enforcement lever here*.
- **Resource limits not containing anything.** They bound blast radius so the wrong
  container is not OOM-killed; nothing in the threat model rests on them. See
  *Resource limits — blast radius, not boundary*.
- **The policy store having no atomic *edit*.** Revoking an operator-created rule is
  built (`POST /api/rules/{id}/revoke`, in the Policy view); what is not is changing a
  rule's pattern or flipping its action in one step — that stays revoke-then-persist,
  two audit rows for one intent, with a window in which the host is held rather than
  allowed or blocked. A persist that would contradict an existing rule is refused
  rather than applied (nothing here overwrites a rule). Known and on the roadmap — see
  the rule-mutation item under *Future improvements*.
  *(This bullet also said a persisted rule's scope came from the requested hostname,
  a leading dot being a subdomain wildcard. That is no longer true and is corrected:
  the requester cannot choose the scope. A leading dot is normalised away, and the
  operator picks from a bounded candidate set the backend derives and re-validates on
  resolve, with the exact host as the default. Reported scope escalation is still very
  much in scope — a way to make a persist store something outside that set is a real
  finding.)*
- **Resource exhaustion and availability, except where pressure becomes permission.**
  There is no service here and no other tenant to deny — the agent making its own
  sandbox slow, filling its own workspace or burning its own CPU is a local nuisance,
  not a finding. What *is* in scope is anything that converts load into a decision
  going the wrong way: a cap that fails open under pressure, a saturated queue that
  lets a request through undecided, or an audit line dropped because the system was
  busy. The known availability defect that stays on the fail-closed side of that line
  is listed under [Known open findings](#known-open-findings).
- **Base images pinned by tag rather than digest.** A deliberate
  rebuild-to-update choice, consistent across every image here.
- **The standalone (proxy-less) fallback being weaker than governed mode.** It
  keeps a narrow, allowlisted, *unaudited* direct path by design, for hosts that
  cannot run the infrastructure. Governed mode is the default and the one the
  invariants describe.
- **Components that do not exist yet** — the git proxy, secrets broker, package
  cache, skills and quality-gate hooks are described in `DESIGN.md` as planned. They
  cannot have vulnerabilities until they are built.
- **"The control-plane UI has no authentication."** Correct, and deliberate: it is
  bound to host loopback behind structural browser-facing guards, and see the first
  item for why adding auth would not address the threat that actually matters. A
  *specific* bypass of those guards is in scope; their absence-of-auth is not.

## Supported versions

There is no version table, because there is nothing to put in it. This repository
has **no releases, no tags and no published images** — fixes land on `main`, and
there are no backports. Run `main`, and `git pull` to get a fix.

## Disclosure

If a report is confirmed, the intent is to fix it on `main` and publish a GitHub
security advisory describing it, requesting a CVE where that is warranted. You will
be credited unless you would rather not be. Please give a reasonable window before
disclosing publicly — with a single maintainer and no release process, "reasonable"
is a conversation on the advisory thread rather than a fixed number of days.

Because there are no releases and no distributed artefacts, there is no fleet to
patch and no embargo to coordinate: the fix is a commit, and anyone running the
project pulls it.
