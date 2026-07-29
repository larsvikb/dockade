# Design

Architecture and rationale for dockade. See `CLAUDE.md` for working
conventions and invariants.

## Purpose

Run an AI coding agent (Claude Code) in a **capability-limited sandbox** that
lets it do strong work in a **controlled, efficient, high-quality** way —
automatically. Two orthogonal goals:

- **Governance** — every consequential action goes through a choke point that
  can be policed, approved, and audited.
- **Enablement** — the agent image and skills encode a "paved road" so good
  work patterns (tests, linting, sensible workflows) happen by default.

## Core idea

The agent can run in **yolo mode** (`--dangerously-skip-permissions`, a conscious
opt-in via the `claude-yolo` alias — not forced) with no per-action prompting.
This is safe **not** because we trust the agent, but because the sandbox is
deliberately impoverished — and the safety holds regardless of mode:

- no direct network egress
- restricted, minimal filesystem
- non-root user, dropped capabilities, no host Docker socket
- no route to the control plane (see Networks)

Containment is by **capability, not by permission**. Blast radius = exactly
what the sandbox can reach directly, which we keep near zero.

The agent reaches capabilities only by talking to **data-plane services** over
the internal sandbox network. Meaningful/risky capabilities are exposed as
**governed proxies/tools**; safe high-value capabilities are exposed as
**ungoverned tools**. Capabilities are surfaced to the agent as **skills**.

MCP is out of scope. Capabilities are exposed as skills, not MCP servers.

## Architecture

```
 host browser ──loopback──► CONTROL PLANE ─┐
                                           │ control-net (internal)
                        policy / approval  │
                        / audit / config   │
        ┌──────────────────────────────────┘
        │
   GOVERNED proxies/tools   (dual-homed: sandbox-net + control-net;
        │   egress ones also on egress-net) ─────────────► internet
        │
════════╪═══ sandbox-net (internal) ═══════════════════════════════
        │
     SANDBOX (Claude, yolo) ──────► UNGOVERNED tools (sandbox-net only, NO egress)

     The sandbox has NO route to control-net / the control plane.
```

### Control plane (control path only — agent cannot reach it)
The management app. Responsibilities:
- **Policy store** — allow/block rules per governed proxy/tool.
- **Approval queue + UI** — governed proxies escalate unknown requests here;
  human approves/rejects in real time. "Approve once" vs "approve + persist
  rule". Blocking caller waits with a timeout → default deny.
- **Audit aggregator** — every governed request logged centrally, browsable.
- **Config** — enable/disable/configure each governed proxy/tool.

Because the agent has no network route to the control plane, governance is
**control-path-only**: data flows agent → proxy; policy/approval/audit flow
proxy ↔ control plane.

**The policy + audit store is the crown-jewel state.** The accumulated
allow/block rules and audit history grow more valuable over time — they are the
institutional memory of "what's known-safe" that lets the system auto-approve
progressively more (less human-in-the-loop as trust accrues) and enables
analytics / anomaly detection. Persist it in a versioned, exportable store,
backed up independently of any container.

### Data plane — governed proxies/tools
Single-purpose services that **enforce** policy pulled from the control plane
and **escalate unknowns** to it. Dual-homed on sandbox-net (serve agent) and
control-net (talk to control plane); outbound ones also on egress-net. Examples:
- HTTP egress proxy (domain/URL allow-block, hold-for-approval, logging)
- git / repo access
- secrets broker (scoped credentials never exposed to the sandbox)
- artifact / file exchange

### Data plane — ungoverned tools
Exposed fully to the agent, no policy, no approval. **Must have no independent
egress** — sandbox-net only. These make good practices cheap and fast. Examples:
- test runner / build service
- language servers, linters, formatters
- local scratch DB, cache, docs mirror
- headless browser **only if** its egress is forced through the governed proxy
  (otherwise it is an ungoverned egress hole — treat as governed)

### Sandbox (the agent image — the "paved road")
Claude Code, with yolo available as a conscious opt-in (`claude-yolo`). This
image is where **enablement** lives:
- curated toolchain + pre-wired linters / formatters / test runners
- baseline Claude Code settings and **hooks as quality gates**
  (e.g. format/test on write, block known-bad patterns)
- a default **status line** (`claude-sandbox/statusline.sh`, seeded into user
  settings by the entrypoint — see below)
- the **skills** that are the sanctioned interface to every capability
- non-root `sandbox` user, resource limits

#### User settings — image-owned config, materialized each boot

`/config/settings.json` is **declarative configuration owned by the image**, not
mutable state in the volume. The entrypoint **overwrites it authoritatively on
every boot** from a baked template (`/etc/claude-code/user-settings.json`, via
`install`). Consequences: the effective config always matches the repo and is
reviewable; wiping the config volume for a clean slate never loses it (only
credentials and runtime state — `.credentials.json`, `.claude.json` — live in the
volume); and any in-session edits the yolo agent makes are transient by design.
This is **not** an enforcement layer — no client settings file is one here (see
"Managed settings are NOT an enforcement lever here"); `settings.json` holds only
enablement defaults and mistake-prevention steering. **File permissions are a
non-goal here:** the file is owned by `sandbox` and `/config` is agent-writable, so
a read-only `chmod` is theater — the owner can re-`chmod` it, or replace it via the
writable directory. There is no client-side immutability against the agent;
containment is capability + egress, not file bits or any settings scope.

The default **status line** ships this way. It shows a sandbox indicator, model,
directory, git branch, and **used context window** (tokens + %). The **script
itself stays root-owned and baked** at `/etc/claude-code/statusline.sh`, so the
non-root agent can toggle the pointer but not tamper with the code it runs — an
acceptable trade-off for a cosmetic feature. User-scope delivery (not managed
settings) is *required*, not just pragmatic: a managed-scope command status line
is gated behind an interactive approval dialog the yolo launch flow suppresses,
so it would never apply. User scope needs only workspace trust (already granted).

The **yolo disclaimer acceptance ships this way too** —
`skipDangerousModePermissionPrompt: true` in the template. This is a consequence
of the materialize-each-boot mechanism, not an exception to it. In current Claude
Code (verified on 2.1.207) accepting the `--dangerously-skip-permissions`
disclaimer writes `skipDangerousModePermissionPrompt: true` into **user settings**
(`/config/settings.json`) — the old global `bypassPermissionsModeAccepted` flag in
`.claude.json` was migrated away and is deleted on startup, so it no longer
persists the acceptance. Because the entrypoint overwrites `settings.json` from the
template every boot, an interactively-accepted disclaimer would be wiped on the
next start and the agent would be re-prompted every launch; baking the flag into
the template is what makes the acceptance survive restarts and volume wipes. This
does **not** force yolo — starting in bypass mode is still a conscious opt-in via
the `claude-yolo` alias; the flag only pre-accepts the disclaimer for a sandbox
built precisely for that mode.

Context usage is read from the **transcript file Claude Code already writes**
(handed to the script as `.transcript_path` on stdin; the latest main-chain
turn's `usage` is the current context), **not** from any API call. This is a
deliberate consequence of the no-egress invariant: a status line renders
constantly and has no governed path to `api.anthropic.com`, so a network call
there would be blocked and would need embedded credentials — while the exact
figure is already local and free. The parse is cached on the transcript's mtime
so a large JSONL is only re-read when it changes; the win is avoiding re-parse
latency, not a (nonexistent) network round-trip.

#### Git identity — from the host at launch, not the tree

Git **conventions** are shared and safe to ship, so the baked `~/.gitconfig`
carries only non-personal defaults (`init.defaultBranch = main`, `pull.rebase =
true`). Git **identity** is per-person and would make the image host-specific, so
it is *not* committed. `run-claude-sandbox.sh` reads `user.name`/`user.email` from
the **host's** git config at launch and forwards them as `GIT_USER_NAME` /
`GIT_USER_EMAIL`; the entrypoint materializes them into the sandbox user's global
config each boot (same materialize-each-boot pattern as `settings.json`). Keeping
this at **runtime** rather than build time sidesteps the build-context limit
(`~/.gitconfig` lives outside the `claude-sandbox/` context a `COPY` can reach) and
means the image is a generic artifact — the same build works for anyone, and
changing identity needs no rebuild. If the host has no identity, the launcher
**warns but does not fail**: the container still starts and git errors only at
commit time (`Author identity unknown`), which is legible and recoverable.

Claude Code's managed-settings tier is **single-source**: when more than one
managed source exists, one wins and the others are ignored — they do **not**
merge. Under **organization authentication** (an org-governed Claude account), the
winning source is Anthropic's **remote** server-managed settings, cached locally
at `/config/remote-settings.json` and `/config/policy-limits.json` (un-editable by
the sandbox). The Dockerfile-delivered `/etc/claude-code/managed-settings.json` is
therefore **not loaded at all** in this configuration.

**Verified in the running container:**
- `/status` reports setting sources as *"User settings, Enterprise managed
  settings (remote)"* — the local file is **absent** from the list.
- A live `WebSearch` call **succeeded** despite the (now-removed) local file's
  `permissions.deny: ["WebSearch"]` — the deny never applied.

Consequences, now settled:

- **The local `managed-settings.json` has been removed from the build.** Shipping
  a file that claims enforcement it does not provide is worse than not shipping it.
  (It would only take effect on a *personal*, non-org login — out of scope for
  this org-governed sandbox.)
- **The real enforcement levers are capability containment and egress control** —
  the firewall, the non-root user, dropped caps, and no route to the control plane
  — plus, for policy that must not be bypassable, the **org admin console**, which
  is the only managed source Claude Code honors here. This is the position the
  CLAUDE.md invariants also state: containment is by capability, not by any client
  settings scope.
- **`settings.json` (user scope) is retained for mistake-prevention only.** A
  `permissions.deny` there stops *accidents* and steers the agent onto the paved
  road, but the yolo agent can edit it or relaunch around it, so it never counts
  against *malicious* intent. Use it freely for ergonomics; never for containment.
- **A settings opt-out is not a containment control.** Hardening env flags
  (`DISABLE_TELEMETRY`, `DO_NOT_TRACK`, and content-export-off
  `OTEL_LOG_TOOL_CONTENT=0` / `OTEL_LOG_USER_PROMPTS=0`) are set as real process
  `ENV` — the robust, unshadowable place for them — but they are defense-in-depth,
  not the boundary. Whatever the sandbox didn't configure and can't see coming (a
  telemetry exporter enabled upstream, any other unexpected egress) is stopped by
  the default-deny firewall, like everything else. Trust the firewall for
  containment, not the env flag; don't claim telemetry is off until export behavior
  is actually checked.

Skills do double duty: they are the capability interface **and** they encode the
right *way* to do a task, steering the agent onto the paved road.

## Web access (verified empirically in the current sandbox)

Tested inside this sandbox's default-deny firewall (iptables + ipset allowlist):

| Tool | Executes | Governed by network layer? | Evidence |
|------|----------|----------------------------|----------|
| `curl` / bash | client-side | **yes** | reddit → conn refused |
| `WebFetch` | **client-side** | **yes** | example.com → `ECONNREFUSED` |
| `WebSearch` | **server-side** (Anthropic infra) | **no** | returned blocked-domain content |

Key correction: it is **not** true that both built-in web tools are server-side.
**WebFetch is client-side** and already contained by the firewall/proxy.
**WebSearch is server-side** — it runs on Anthropic infra and returns results
over the allowed `api.anthropic.com` connection, so no local firewall or proxy
can see or block it. WebSearch is the one genuine ungoverned web channel.

This was verified thoroughly: every search provider (Google, Bing, DuckDuckGo,
Brave, SerpAPI) is unreachable from the container (`000`), yet WebSearch still
returned live blocked-domain content — so there is no alternate egress rule; the
search runs server-side over api.anthropic.com. It is an instance of the general
**server-side tool-execution pattern** (the API runs the tool within the turn,
no client round-trip) — a latency/capability win, and a pattern worth reusing in
our own control/data plane for governed remote execution.

Consequences:

- **WebSearch cannot be blocked at the network layer** (that would require
  blocking api.anthropic.com, which the agent needs). The remaining levers are
  server-managed policy (the **org admin console**) or removing the capability
  entirely (replace with a governed skill). A user-scope
  `permissions.deny: ["WebSearch"]` only discourages *accidental* use — the local
  managed file that could have enforced it is not loaded under org auth (verified).
- **Disabling it is a threat-model choice, not automatic.** WebSearch is
  read-only: the agent cannot POST/upload through it. Realistic risks are narrow
  — low-bandwidth exfil via crafted query strings (to Anthropic's search, not an
  attacker endpoint) and unaudited information *intake*. If neither is in scope,
  leaving WebSearch on keeps its perf/capability benefit. Disable only if
  unaudited intake or query-string leakage matters for your threat model.
- **No client-side lever enforces a WebSearch block here.** A yolo agent can edit
  or relaunch around a user-scope deny, and the one mechanism that could have
  outranked the agent — a root-owned local managed file — is **not loaded** under
  org auth (see "Managed settings are NOT an enforcement lever here", verified). So
  the only real controls are the **org admin console** (server-managed) or
  **removing the capability** by replacing WebSearch with a governed `websearch`
  skill (below). A user-scope deny is still worth keeping as *accident* prevention,
  not as containment.
- **WebFetch** is already network-governed; keep it. It also honors `HTTPS_PROXY`
  (inherits the CLI's proxy env), so it is audited through the egress proxy —
  **verified**: allow/deny CONNECT entries for a fetched allowlisted vs blocked
  host appear in the proxy audit log (see "Claude Code proxy support").
- **Optional `websearch` skill** — *only* if a future threat model decides to turn
  WebSearch off (see decision below). Web search would then become a governed call
  to a third-party search API (Brave / SerpAPI / Google CSE) with its own read-only
  key, routed through the egress proxy. Not planned while WebSearch stays enabled.

### Reaching the Anthropic API while governing everything else
The sandbox has no direct internet route (`internal: true`), so Anthropic access
is a deliberate path, decided one of two ways:
- **(a) Always-allow api.anthropic.com at the egress proxy** and route the CLI
  through it — cleanest, keeps Anthropic traffic audited. The docs say the CLI
  *does* honor `HTTPS_PROXY` for its own API calls (see "Claude Code proxy
  support"), so this is the expected path; only if an in-sandbox test contradicts
  that do we fall back to transparent-redirecting outbound 443 at the network
  layer.
- **(b) Narrow network-layer firewall allowlist** (iptables/ipset init
  container) permitting only api.anthropic.com directly, dropping the rest —
  forcing all other web through the proxy skill.

Lean (a) so nothing escapes audit; fall back to (b) if the CLI won't proxy.

### Claude Code proxy support (documented — Layer 1; CONFIRMED in-sandbox — Layer 2)

The (a)-vs-(b) choice above hinges on whether the CLI honors `HTTPS_PROXY` for
its own API calls. Per Anthropic's official
[Enterprise network configuration](https://code.claude.com/docs/en/network-config)
docs, **it does** — which points us at (a). This is now **verified in this
sandbox** (see the Layer-2 result at the end of this subsection), so we are
committed to (a). Documented points, with the design consequence of each:

- **Standard proxy env vars are honored.** Claude Code reads `HTTP_PROXY`,
  `HTTPS_PROXY`, and `NO_PROXY` (docs use **uppercase** — use uppercase),
  Node-native, **read once at startup**. Basic auth via `user:pass@` in the URL.
  **No SOCKS.** → *Approach (a) is viable: a plain forward proxy injected via
  `HTTPS_PROXY`, no transparent 443-redirect needed.*
- **WebFetch inherits the same proxy.** The docs describe no separate mechanism,
  so the client-side WebFetch uses the same env vars (this resolves the
  "untested" note in the Web access section — still documentation-level, not yet
  re-tested here). One concrete wrinkle: WebFetch fires a **domain-safety
  preflight to `api.anthropic.com` on every fetch**, disableable with
  `skipWebFetchPreflight: true`. → *WebFetch becomes auditable at the proxy for
  free once the CLI is proxied.*
- **TLS interception is a first-class, documented scenario, not a hack.** Custom
  CA via `NODE_EXTRA_CA_CERTS=/path/ca.pem`; `CLAUDE_CODE_CERT_STORE` (default
  `bundled,system`) selects trust stores; mTLS via `CLAUDE_CODE_CLIENT_CERT` /
  `CLAUDE_CODE_CLIENT_KEY` / `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE`. → *Full MITM is
  a supported later step (see "HTTPS inspection depth" in Open decisions); v1 of
  the proxy can stay CONNECT/SNI-level with no CA in the sandbox.*
- **CONNECT-level is enough to close the CDN-fronting gap.** A forward proxy that
  gates each `CONNECT host:443` against a **domain allowlist** governs on the
  *requested hostname*, not the shared-CDN IP — which is precisely the
  IP-vs-domain hole called out for v1's IP-level firewall. (Verifying the
  ClientHello SNI matches the CONNECT host defends against a client that lies
  about the CONNECT target.)
- **The CLI's endpoint set is wider than our current allowlist — decide each.**
  The docs enumerate what Claude Code reaches; mapped to our posture:
  `downloads.claude.ai` / `storage.googleapis.com` (native installer +
  auto-updater) are **moot** — we set `DISABLE_AUTOUPDATER=1`;
  `raw.githubusercontent.com` (`/release-notes` changelog) is cosmetic and
  already inside the GitHub ranges; **`mcp-proxy.anthropic.com`** carries
  claude.ai MCP connectors, which are **on by default** — either disable them
  (`ENABLE_CLAUDEAI_MCP_SERVERS=false`) or they fail closed against our
  default-deny. The last is the one worth an explicit decision rather than a
  silent failure.
- **`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is the right lever** for the
  statsig/feature-flag/telemetry chatter that would otherwise flood the proxy's
  deny log — and the Dockerfile **already sets it**. Good confirmation, not new
  work.
- **Caveat for the proxy phase: deliver proxy vars as real process env, not a
  shell export.** The docs warn that background-agent/supervisor processes don't
  reliably inherit a shell's environment; the robust channel is the settings
  `env` block or the process environment. In our container that means passing
  them as Dockerfile `ENV` / `docker run -e` (the same pattern the hardening and
  DNS vars already use), never a `.bashrc` export.

**Layer 2 result — CONFIRMED.** With the step-0 proxy running and the sandbox in
governed mode (`HTTPS_PROXY=http://egress-proxy:8080`, firewall allowing only the
proxy + embedded DNS), `boundary-check.sh` reported `api.anthropic.com reachable
(via proxy http://egress-proxy:8080)` — Claude's own API traffic flows through
the proxy, so the CLI honors `HTTPS_PROXY`. The same run confirmed the proxy
refuses a non-allowlisted domain (`egress proxy denies non-allowlisted
example.com`) and that direct egress, IPv6, and direct external DNS are all
blocked. So (a) stands and the transparent-redirect fallback (b) is not needed.
WebFetch was separately confirmed: fetching an allowlisted host (`pypi.org`)
succeeded and a non-allowlisted one (`example.com`) failed with `Socket is
closed`, and the proxy audit log shows the matching `allow`/`deny` CONNECT
entries — so WebFetch is client-side and proxy-governed (had it run server-side
like WebSearch, `example.com` would have succeeded and never hit the proxy).

## Server-side execution: accepted governance blind spots

Some tools execute **server-side** (on Anthropic infra, within the API turn)
rather than from the container. Their effects never traverse the container's
network boundary, so **the firewall/proxy cannot see, audit, or block them —
governance of those actions is lost.** We accept this initially, but enumerate
it explicitly so it is a conscious tradeoff, revisited as the setup matures.

**Why the list is complete, not a sample:** a tool can only escape governance if
it causes effects *outside* the container. File/process tools (Bash, Read, Edit,
Write, Glob, Grep, Task, NotebookEdit) are inherently local — the firewall
already governs anything they spawn. The only tools that reach external networks
are the two web tools, both tested in this sandbox.

Registry (re-test when Claude Code adds/changes tools):

| Tool | Executes | Governed? | If allowed, governance lost over… |
|------|----------|-----------|-----------------------------------|
| WebSearch | server-side | **No** | what the agent searches for; unaudited web *intake*; low-bandwidth exfil via query strings (read-only — no upload/POST) |
| WebFetch | client-side | Yes | — (network-governed) |
| Bash / file / Task / Notebook tools | client-side | Yes | — (local, firewall-governed) |

Scope: this concerns governing *actions on external systems*. The conversation
itself always goes to Anthropic (that is the model, not a tool action) and is
out of scope for network governance.

**Maintenance rule:** for any new tool, ask "can it cause effects that don't
originate from the container?" If yes, test client vs server-side and add a row.
If no, it is local by construction.

**Decision (standing):** WebSearch runs server-side and is left enabled. It is
read-only (no upload/POST), its risk is narrow (see Web access section), and its
capability/latency benefit is real — there is no requirement to disable it. This
blind spot is consciously accepted, not a deferred TODO. "Standing" rather than
"permanent" because it is contingent on the threat model: the reversal path below
and the `websearch`-skill open decision keep it revisitable, not open by default.

**If that ever changes** (a threat model where unaudited intake or query-string
leakage matters): there is no client-side hard-block here, so the move would be to
block it via the org admin console and *replace* it with a client-side `websearch`
skill (third-party search API through the egress proxy) so search becomes governed
and audited. The same pattern applies to any *future* server-side tool whose blind
spot is less acceptable than WebSearch's.

## Capability inventory (v1)

Workload: greenfield coding. Work I/O: git remotes (clone/pull/push) + a
host-bind-mounted workspace. Dependencies: pull-through cache.

**Governed (through control plane):**
- **Egress HTTP(S) proxy** — the central choke point. Domain allow/block/hold +
  audit. Always-allow api.anthropic.com (per decision above). Upstream for the
  package cache, git host, general web, and third-party APIs all flow here.
- **Web search backend** *(deferred — not in v1)* — only needed if WebSearch is
  ever disabled and replaced by the `websearch` skill. While built-in WebSearch
  stays enabled, no backend is required. Would be a third-party search API via the
  egress proxy with a read-only key.
- **Git push path** — clone/pull is low-risk; **push is the governed-risky
  direction**. v1: git over HTTPS through the egress proxy, push allowed to
  allowlisted repos/orgs, unknown → hold. The push token is a write-capable
  credential and stays governed (not in the sandbox).

**Ungoverned (sandbox-net only, no independent egress):**
- **Pull-through package cache** (npm/PyPI/apt). Ungoverned to the agent (fast,
  free installs); its upstream fetch is governed via the egress proxy. The one
  tool with upstream reach, and that reach is itself governed.
- **Toolchain in the sandbox image** — test runner, build, linters, formatters,
  language servers. In-image for v1 (part of the paved road), not separate
  containers yet.
- **Local scratch DB** (optional v1) — for apps the agent builds during dev.

**Credentials:**
- In the sandbox: Anthropic session credentials (must, self-use) — established by
  interactive **Claude subscription login** on first run (any subscription works,
  personal or org) and persisted as `.credentials.json` in the config volume, *not*
  an injected `ANTHROPIC_API_KEY`. When the account is **org-governed**, that login
  is also what places the sandbox under the org's remote server-managed settings —
  a personal-subscription login would not carry those (see "Managed settings are
  NOT an enforcement lever here"). Low-risk
  read-only tokens (e.g. a search API key) are also acceptable here.
- Governed / not in sandbox: git push token, any write-capable / high-impact key.

**Workspace bind-mount:** the one deliberate direct host coupling. Scope it to a
single project directory, read-write. Because work lands on the host FS directly,
no separate artifact-export path is needed in v1.

### Clear future improvements
- **DONE — Python unit tests for the governance-critical logic** (`tests/`, wired
  into `make check` via the `test` target). A dependency-free `python -m unittest`
  suite (`fastapi`/`pydantic`/`mitmproxy` stubbed in `tests/_loader.py`, SQLite on
  a throwaway temp file — no pip installs, no running services) now covers the
  security-load-bearing decision functions: the proxy's `_forbidden` relay guard
  (name / literal-IP / resolves-into-control-net branches), `_match`, the
  permanent-lifeline check, and env parsing; and the control plane's `_decide`
  (block-wins-over-allow across distinct patterns, subdomain semantics,
  default-hold, case-folding) and `_match`. Per the note below, the **hold-cap
  reservation was extracted** out of the `authorize` handler into
  `_reserve_hold` / `_release_hold` (behavior-preserving) so the cap logic is
  testable directly — its global cap, per-client cap, per-client-disable (`0`),
  `None`-client-bypasses-per-client-but-not-global, and slot-release behavior are
  now asserted. These are exactly the properties `boundary-check.sh` is a poor fit
  for — it can only observe reachability from the sandbox's vantage point, so
  availability/concurrency properties like the hold cap (which returns the same
  opaque `403` to the agent as any other deny) can't be asserted there.

  The suite was then extended to the **async decision flow** the pure-function
  tests couldn't reach (61 tests total, still dependency-free): the proxy's
  `_authorize` (permanent-lifeline short-circuit with no control-plane call, and
  fail-closed on any control-plane error / non-`allow` decision — `_post_authorize`
  monkeypatched, no network) and the mitmproxy hooks via small `SimpleNamespace`
  flow/context fakes — `http_connect` (forbidden-before-port-before-host ordering,
  authority recorded only on allow), `tls_clienthello` (the SNI-vs-CONNECT-authority
  anti-fronting guard, incl. no-recorded-authority fails closed), `request` (gates
  **both** transport host and Host/`:authority`, so a fronted Host header is denied
  even under an authorized CONNECT), and `client_disconnected` cleanup. On the
  control-plane side the FastAPI stub leaves handlers callable, so the `authorize`
  orchestration (allow/deny without holding, over-cap immediate fail-closed, hold
  **timeout→deny** leaving the row `expired`) and the **resolve handshake** (a
  backgrounded blocked `authorize` woken by `allow_persist` — which also writes the
  operator rule so a re-decide skips the hold — vs `deny_once` which persists
  nothing; plus bad-action `400` and unknown-id `409`) and `_seed_if_empty`
  (lowercasing, comment/blank skipping, idempotency) are all covered. Remaining
  untested surface is low-weight I/O: `_audit` sinks, `_post_authorize`/`_setup_
  audit_file`, and the SSE `approvals_stream`.
- Dedicated git proxy that speaks the git protocol (block force-push, restrict
  branches, per-repo policy) instead of HTTPS-through-egress.
- Separate test/build runner containers for isolation + parallelism.
- Selective MITM on credentialed hosts → true brokering so even self-use keys
  leave the sandbox.
- Docs mirror / offline docs tool.
- Progressive auto-approval driven by accumulated policy + audit history.
- Split the control-plane UI into a distinct `control-plane-ui` frontend
  container (own the `control-ui-net` surface; backend returns to
  `control-net`-only/internal). Planned with 2b — see "Control plane — step 2a
  shipped" for the rationale.

## Networks

- `sandbox-net` (`internal: true`) — sandbox ↔ all data-plane services (governed
  + ungoverned). The agent lives only here.
- `control-net` (`internal: true`) — governed proxies/tools ↔ control plane.
  Sandbox not attached.
- `egress-net` — only outbound-capable governed proxies + internet.
- `control-ui-net` — non-internal bridge carrying ONLY the control-plane-ui
  frontend's host-loopback UI publish; masquerade disabled so it is
  host-reachable but not an egress path. Needed because Docker cannot publish a
  host port from a container that is on an internal network alone. Sandbox not
  attached.

**Status:** all four networks are implemented. `sandbox-net` (internal) and
`egress-net` carry the agent and the sole egress; `control-net` (internal)
carries the control path. The egress proxy is **triple-homed** (sandbox-net
+ egress-net + control-net). The control-plane **backend** is on `control-net`
only and fully internal (no `sandbox-net`, no `egress-net`, no published port);
the **control-plane-ui** frontend is on `control-net` (to reach the backend)
plus `control-ui-net` (host-loopback UI). The sandbox is on `sandbox-net` only —
never `control-net`/`control-ui-net` (asserted by `make check` and
`boundary-check.sh`).

### DNS on `sandbox-net` (a non-obvious gotcha — read before touching DNS/firewall)

Putting the sandbox on a *user-defined* network (which we do, and must — it's how
data-plane services get name resolution) changes DNS in two ways that together
broke the agent, and the fix spans both `run-claude-sandbox.sh` and
`init-firewall.sh`. The chain, so nobody has to rediscover it:

1. **Embedded resolver.** On a user-defined network the container's `resolv.conf`
   is always `127.0.0.11` — Docker's embedded DNS. It answers sibling-container
   names itself and **forwards** everything else to an upstream. (On the *default*
   bridge there is no embedded resolver; `resolv.conf` holds real IPs directly.
   That's why "drop `--network` and it works" appears to fix things — it sidesteps
   all of this, at the cost of the isolation `sandbox-net` exists to provide. Not
   an acceptable fix.)

2. **The upstream it auto-selects is often unreachable from the container.** The
   embedded resolver derives its upstream (`ExtServers`) from the *host's*
   `/etc/resolv.conf`. On any systemd-resolved host — most WSL2/Docker Desktop
   setups, much stock Linux, anything on a split-DNS VPN — that file is just the
   stub `127.0.0.53`, which is meaningless inside the container. Result: **every
   external lookup `SERVFAIL`s, with or without the firewall.** This is generic
   Docker behavior, not a broken host (host DNS and the default bridge both work).
   Fix: `run-claude-sandbox.sh` pins the upstream explicitly with `--dns`, sourced
   from the host's real uplink resolvers (`/run/systemd/resolve/resolv.conf`),
   falling back to public `8.8.8.8/8.8.4.4`, overridable via `SANDBOX_DNS` for
   locked-down networks. `resolv.conf` stays `127.0.0.11`, so service discovery is
   preserved.

3. **The firewall must not break the resolver, and must permit its forward.** Two
   traps in `init-firewall.sh`:
   - It must **not flush the `nat` table** — Docker's embedded-DNS `DNAT` lives
     there, and flushing it kills all resolution *before* default-deny even arms.
     (An earlier save/restore dance tried to undo this; it's unreliable under the
     nf_tables iptables backend. Simpler and correct: leave `nat` alone. `nat` is
     not a containment boundary — egress is governed entirely in the `filter`
     table.)
   - The embedded resolver's upstream forward **egresses the `filter` OUTPUT
     chain**, so those same upstream IPs must be whitelisted on port 53 (passed in
     as `UPSTREAM_DNS`) or runtime DNS dies the instant default-deny arms.

`run-claude-sandbox.sh` computes the resolver list **once** and uses it for both
`--dns` (the resolver's upstream) and `UPSTREAM_DNS` (the firewall allow-list), so
the two can't drift apart. Containment is unaffected: `--dns` only sets an
upstream, DNS is still pinned to *named* resolvers (no "any nameserver" hole), and
egress remains filter-table default-deny + the ipset allowlist.

## Layout (planned)

```
dockade/
  docker-compose.yml
  control-plane/      # management app: policy, approval UI, audit, config
  proxies/            # governed data-plane services (one dir per proxy/tool)
    egress/
  tools/              # ungoverned data-plane services
  claude-sandbox/     # Claude agent image
    Dockerfile
    skills/           # sanctioned capability + workflow interface
    hooks/            # quality-gate hooks
    settings/         # baseline Claude Code config
  policies/           # seed allow/block config
```

## Open decisions

- **RESOLVED — control-plane stack is Python (FastAPI) over SQLite.** Shipped in
  2a (`control-plane/`): FastAPI app, stdlib `sqlite3` store, plain `def`
  endpoints (FastAPI's threadpool keeps the blocking DB off the event loop). The
  SSE/websocket approval UI arrives with 2b on the same FastAPI app. `mitmproxy`
  remains the egress-proxy engine.
- **HTTPS inspection depth** — CONNECT/SNI (domain-level, no CA in sandbox) vs
  full MITM (URL/body-level, needs a generated CA in the sandbox). Likely start
  CONNECT-level, allow MITM per-domain later. Both are documented-supported by
  the CLI (MITM via `NODE_EXTRA_CA_CERTS` / `CLAUDE_CODE_CERT_STORE`; see "Claude
  Code proxy support"), so the choice is ours, not gated by tool support.
- **RESOLVED — policy storage is SQLite** (2a), in its own named volume
  (`dockade-control-state`), seeded from `policies/egress-allowlist.txt`.
- **RESOLVED — Anthropic reachability is (a): always-allow via the egress proxy.**
  The end-state choice was (a) always-allow via egress proxy vs (b) network-layer
  firewall allowlist. The blocker for (a) — does the CLI honor `HTTPS_PROXY` for
  its own API calls — is now **confirmed in-sandbox** (see the Layer-2 result in
  "Claude Code proxy support": with governed mode active, `boundary-check.sh`
  reports api.anthropic.com reachable via the proxy and direct egress blocked), so
  (a) stands and the transparent-redirect fallback (b) is not needed. Governed mode
  is the default and implements (a) today; (b) survives only as the **standalone**
  (proxy-less) fallback, where the firewall directly allowlists api.anthropic.com.
- **Web search backend** — which third-party search API for the `websearch`
  skill (Brave / SerpAPI / Google CSE).
- **RESOLVED — the local managed file is not an enforcement lever under org auth.**
  Verified in-container: `/status` shows the managed source as *remote* (org
  server-managed); the local `/etc/claude-code/managed-settings.json` is not loaded,
  and a live WebSearch succeeded despite its `deny`. The yolo-vs-managed-deny
  question is moot here — the file simply isn't a source. The design no longer
  relies on managed settings; hard policy goes to the org admin console, and the
  local managed file has been removed from the build. See "Managed settings are NOT
  an enforcement lever here".

## Build status

**v1 sandbox scaffolded** (`claude-sandbox/` + `run-claude-sandbox.sh`) — a
single-container image and launcher centered on this design. Notable properties:
- **Isolated persistent config** via `CLAUDE_CONFIG_DIR=/config` backed by its
  own named volume — no host `~/.claude` sharing.
- **No local managed-settings enforcement layer** — removed after verifying it is
  shadowed by the org's remote managed source under org auth (see "Managed settings
  are NOT an enforcement lever here"). Hardening opt-outs live as real Dockerfile
  `ENV`; any `permissions.deny` would go in user-scope `settings.json` as
  mistake-prevention only. Enforcement is the firewall + capability containment;
  hard policy is the org admin console. Does **not** force yolo — starting in yolo
  is a conscious opt-in via the `claude-yolo` alias.
- **Baseline user settings** (`claude-sandbox/user-settings.json`) baked in and
  materialized to `/config/settings.json` authoritatively on each boot — the
  default status line ships this way; config is image-owned, the volume holds
  only credentials/runtime state.
- **Base image `debian:13-slim`** (Debian 13 "trixie") — chosen over `ubuntu:24.04`
  for a leaner base with no default uid-1000 user to evict (dropping a `userdel`
  step) and archive utilities as fresh or fresher; the firewall (iptables-nft on
  both) and host-uid matching behave identically, verified by a build +
  `boundary-check.sh` pass on the Debian base. The Ubuntu LTS+ESM support window is
  the one trade-off, minor under rebuild-to-update.
- Baseline stack: **Node current LTS (24.x)** + `gh` + pipx, plus baked linters
  (shellcheck / hadolint / ruff, all self-contained so they run under default-deny
  egress). Node tracks the latest LTS line (even majors); bump the NodeSource
  `setup_NN.x` major to move it. Firewall allowlist trimmed to this design
  (Anthropic + GitHub + npm + PyPI).
- **Own user-defined bridge `sandbox-net`** (owned by `docker-compose.yml`,
  created idempotently by the launcher when the compose infra is absent), not
  Docker's default bridge — gets embedded DNS (`127.0.0.11`, which the firewall
  already expects), name resolution for the data-plane services, and isolation from
  other default-bridge containers. Under the compose infra it is now `internal:
  true` (the proxy landed — see "Step 1 shipped" below), so the sandbox has no
  direct route off-box; the launcher's plain-bridge fallback (proxy-less standalone
  use) is non-internal and keeps direct egress via `init-firewall.sh`.

**Egress proxy — step 0 shipped** (`docker-compose.yml` + `proxies/egress/`). The
multi-container phase has begun, with a deliberate split the topology relies on:
`docker-compose.yml` owns the **shared, long-lived infrastructure** (the data
plane — currently just the egress proxy), and `run-claude-sandbox.sh` still
launches the **ephemeral sandbox(es)** that attach to it (`docker run -it --rm`,
one or many, each per-workspace with its own firewall/DNS/git wiring). Sandboxes
are intentionally *not* compose services: they are interactive, disposable, and
plural, which `compose run` models poorly.

The proxy itself is `mitmproxy` in regular (forward) mode with a policy/audit
addon (`proxies/egress/addon.py`): **CONNECT-level, default-deny domain
allowlist, per-connection JSON audit, no TLS interception** (HTTPS is tunnelled
via `ignore_connection`, so no CA in the sandbox). The launcher discovers the
proxy on `sandbox-net`, points the agent's `HTTPS_PROXY` at it (the CLI honors it
— see "Claude Code proxy support"), and allowlists it in the firewall
(`EGRESS_PROXY_IP`). Chosen properties: it does **not** weaken the boundary while
we validate — the allowlist is default-deny from the first commit, so arbitrary
egress via the proxy is refused (not allow-all), and `boundary-check.sh` stays
meaningful; and the allowlist is re-read per connection, a cheap stand-in for
"dynamic" until the control plane exists. Governs by **name**, so it closes the
shared-CDN/fronting gap the IP firewall can't (for proxied traffic).

**Governed vs standalone egress (the firewall is mode-aware).** When the launcher
finds the proxy it sets `EGRESS_PROXY_IP`, and `init-firewall.sh` switches to a
minimal **governed** posture — the sandbox's only `OUTPUT` ACCEPTs become:
loopback (incl. embedded DNS `127.0.0.11`), DNS **to `127.0.0.11` only**, the
proxy `/32:8080`, and `ESTABLISHED,RELATED`; everything else is REJECTed. Dropped
in governed mode vs the old posture: the direct per-domain IP allowlist (so **no
ipset**), the **upstream DNS forward** (closing the residual DNS-exfil channel —
a crafted name can no longer reach a recursive resolver; sibling names like
`egress-proxy` still resolve locally), and the **gateway `/32`** (host-local
surface). Without a proxy the firewall keeps the fuller **standalone** allowlist
(ipset + upstreams + gateway). Net effect: with the proxy up, the sandbox's only
paths off-box are the proxy (HTTP/S, domain-governed + audited) and — narrowly —
the embedded resolver for sibling names. This is effectively step 1's egress
posture, reached early. Consequence to remember: a tool that does its own
external DNS or connects direct *without* honoring `HTTPS_PROXY` now fails closed
(by design — proxied tools hand the hostname to the proxy, which resolves it).

**WSL2 kernel gotcha (`xt_set`).** On stock WSL2 kernels `ipset create` can
succeed while iptables `-m set --match-set` fails with `Can't open socket to
ipset` — the kernel ships enough of `ip_set` for the userspace tool but not the
`xt_set` match module. This is a *kernel*-capability gap, unrelated to container
caps (`ipset`/xtables need `NET_ADMIN`, never `NET_RAW` — re-adding `NET_RAW`
does not help and re-opens a hole). Governed mode sidesteps it entirely (no ipset
path); standalone mode fails **closed** with a pointer to use the proxy. Do not
"fix" ipset errors by adding capabilities — use the proxy.

**Step 1 shipped — the proxy is the sole egress at the network layer.**
`sandbox-net` is now `internal: true` (no route off-box for anything on it) and a
separate `egress-net` carries the only internet path, with the egress proxy
**dual-homed** across both (default route via egress-net; a stable sandbox-net IP
the firewall allowlists). The in-container firewall is now **defense-in-depth**
rather than the sole boundary: even if it failed, the sandbox has no route out.
The launcher refuses to start a sandbox on an internal `sandbox-net` when no
proxy is running. **Standalone** mode (no compose infra → non-internal net +
direct ipset allowlist) remains for proxy-less use. DNS needs nothing extra: the
sandbox resolves only sibling names via the embedded resolver (local, works on
internal nets); external resolution happens at the proxy on egress-net.

**Control plane — step 2a shipped** (`control-plane/` + `control-net`). The
governance authority now exists as a service the **agent cannot reach**: it is
not on `sandbox-net`, so the sandbox has no route to it (`boundary-check.sh`
probes the control plane's fixed control-net address and asserts it is
unreachable from the sandbox; `make check` asserts the launcher never attaches
the sandbox to `control-net`/`control-ui-net`). It sits on `control-net`
(internal) for the proxy control path and on `control-ui-net` for the human UI.
That UI bridge is non-internal **by necessity** — Docker cannot publish a host
port from a container that is on an internal network alone — but has masquerade
disabled, so it carries the loopback UI publish without being an egress path.
The control plane is a small FastAPI app over a SQLite policy+audit store (its
own named volume — the crown-jewel state), with the management surface published
to **host loopback only** (`127.0.0.1:8081`).

*Design note — why two control nets, and the planned split.* `control-net` stays
hard-`internal` because it is the **shared** control path for the whole governed
data plane (egress proxy today; git/secrets proxies later), and egress is granted
**only** by `egress-net` membership — a non-internal `control-net` would silently
hand ungoverned egress to every service on it (see "Networks"). Publishing a host
port, though, forces *some* non-internal surface, so `control-ui-net` quarantines
it to a single-member bridge. Honest caveat: with only the control-plane on
`control-net` today, this split's *present* security gain is ~nil (the control-
plane carries `control-ui-net`'s soft-egress surface itself), and collapsing to
one non-internal `control-net` would be observably equivalent now — but it would
bake in a non-internal shared control path that becomes a real hole the moment a
must-stay-egress-free tenant (secrets broker) joins. Keeping the split is cheap
insurance against that footgun. **Done in 2b-2:** the UI is now a **distinct
`control-plane-ui` frontend container** (its own lifecycle) that depends on the
`control-plane` backend and talks to it over `control-net`. The frontend owns the
`control-ui-net` non-internal surface; the backend is now `control-net`-only and
fully `internal`, so the crown-jewel container has **zero non-internal exposure**
— the split now buys something concrete, not just future insurance. This is
deliberately a **separate service**, not a co-located sidecar: the frontend is a
stateless reverse proxy + static server (FastAPI + httpx), holds no state, and no
governance decision depends on it (the egress proxy calls the backend directly).

The egress proxy is now a **control-plane client** rather than a static-allowlist
enforcer: on every connection it calls `POST /authorize {host, ...}`, which
returns the decision **and** records the audit row in one call — so policy and
audit share the round-trip, there is no client-side cache (an operator rule edit
applies to the very next connection), and there is no separate audit channel.
Two deliberate properties: (1) the **permanent lifeline** (Anthropic API/auth)
is allowed by a *local* check in the proxy *before* the control plane is
consulted, so a control-plane outage never bricks the agent's own API; (2)
everything else **fails closed** — if the control plane is unreachable or times
out, the request is denied and audited locally.

**Control-plane relay guard (the proxy is the bridge, so the proxy must refuse
it).** The egress proxy is the *one* component attached to both `sandbox-net` and
`control-net`, so segmentation alone does **not** isolate the agent from the
control plane — the proxy could in principle relay a connection onto control-net
(where the agent could, e.g., approve its own held requests). It therefore
hard-refuses, **before** any policy / permanent-lifeline / port check, any
destination that names a control-plane host (`control-plane`, `control-plane-ui`)
or resolves into the control-net subnet (`172.31.0.0/24`) — see
`_forbidden` in `proxies/egress/addon.py` (`EGRESS_FORBIDDEN_HOSTS` /
`EGRESS_FORBIDDEN_CIDRS`). Because the guard is checked first and never weighed
against policy, no rule, human approval, or change to the port allowlist can widen
it, and a public name whose DNS is pointed at control-net is caught by the
resolve step. This makes the CLAUDE.md invariant ("the agent must never reach the
control plane") independently enforced at the one place segmentation cannot cover;
`boundary-check.sh` asserts the proxy 403s both a control-plane host and a literal
control-net IP. The call runs in a worker thread
so it never blocks mitmproxy's event loop, and the egress image gains no new
dependency. The canonical allow policy now lives at `policies/egress-allowlist.txt`
(the control plane seeds SQLite from it on first boot, idempotently); the proxy
no longer bakes or reads an allowlist file.

**Hold-for-approval — step 2b-1 shipped.** An unmatched host is no longer denied
outright: `_decide` returns **`hold`**, and `/authorize` records a pending
approval and **blocks** the request until a human resolves it or
`CONTROL_HOLD_TIMEOUT` (default 120s) elapses → default-deny. The proxy is
unchanged except for a longer authorize timeout to cover the wait — it still
sees only allow/deny (the hold is internal to the control plane). A human
resolves holds in a **live SSE UI** served at `/`: **allow-once / deny-once**
(this request only) or **allow-persist / deny-persist** (also writes a rule so
future connections skip the hold — the progressive-trust path). Concurrency:
one uvicorn worker; a held request blocks its threadpool worker on a
`threading.Event` the resolve endpoint sets; SQLite (`approvals` table) is the
UI's source of truth; stale `pending` rows are expired on startup. **Holds are
bounded** (`CONTROL_MAX_PENDING`, `CONTROL_MAX_PENDING_PER_CLIENT`): since each
hold pins a threadpool worker and this control plane is **shared across all
sandboxes**, an unbounded queue would let one agent exhaust the pool and stall
every sandbox's governed egress. Over either cap, `/authorize` fails **closed**
(deny) immediately instead of registering another blocking hold — the cap stays
comfortably under the worker pool so fast allow/deny decisions always have free
workers, and the permanent lifeline is unaffected (it never reaches the control
plane).

**Frontend split — step 2b-2 shipped.** The approval UI and the API/SSE relay
now live in a distinct **`control-plane-ui`** container (FastAPI + httpx: serves
the static UI at `/`, reverse-proxies everything else — including the SSE stream
— to the backend over `control-net`). The **backend is now `control-net`-only
and fully `internal`**: no published port, no non-internal surface, nothing to
exfiltrate even if reached. The frontend carries the sole host-facing surface
(`control-ui-net`), holds no state, and is not on `sandbox-net`. Browsers hit
`http://localhost:8081` → frontend → backend; the egress proxy still calls the
backend's `/authorize` directly. See the step-2a design note for the rationale.

Step 2c: the audit-browser UI and per-proxy config surface (rows accumulate
from 2a).

Not yet built: audit browser (2c), git/secrets/cache data-plane services,
skills, quality-gate hooks.

**Transitional firewall entries (remove at the proxy/cache phase):** the sandbox
firewall currently whitelists package registries (npm/PyPI) and GitHub only
because the data plane that should mediate them doesn't exist yet. In the target
design the sandbox has *no direct egress* — packages come from the pull-through
cache (upstream via the egress proxy) and git via the governed git path. Only
the Anthropic API/auth lifeline is permanent (and even that routes through the
proxy as an always-allow in the end state). The allowlist is grouped PERMANENT
vs TRANSITIONAL in `init-firewall.sh` to make this explicit.
