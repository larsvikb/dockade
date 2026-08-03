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

- no direct network egress (governed mode — the default; the proxy-less
  standalone fallback keeps a narrow, allowlisted, unaudited direct path)
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
 host browser ──loopback──► control-plane-ui ──► CONTROL PLANE ─┐
                            (stateless frontend)                │ control-net
                                            policy / approval   │ (internal)
                                            / audit / config    │
        ┌───────────────────────────────────────────────────────┘
        │
   GOVERNED proxies/tools   (dual-homed: sandbox-net + control-net;
        │   egress ones also on egress-net) ─────────────► internet
        │
════════╪═══ sandbox-net (internal) ═══════════════════════════════
        │
     SANDBOXES ─────► UNGOVERNED tools (sandbox-net only, NO egress;
                      e.g. the local-LLM inference service)

     tier 1: Claude (yolo) — governed egress through the proxy
     tier 2: opencode + local LLM — NO egress, NO credentials

     No sandbox tier has a route to control-net / the control plane.
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
  (e.g. format/test on write, block known-bad patterns) — *hooks: planned, not
  yet in the image (see Build status)*
- a default **status line** (`claude-sandbox/statusline.sh`, seeded into user
  settings by the tier-1 setup hook the entrypoint runs — see below)
- the **skills** that are the sanctioned interface to every capability —
  *planned, not yet in the image*
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

The default **status line** ships this way. It shows a sandbox indicator,
directory, git branch, **used context window** (tokens + %), and 5h/7d
subscription rate-limit usage with reset countdowns (the model id is read only
to size the context window, not displayed). The **script
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
it is *not* committed. The launcher (`sandbox-lib.sh`, shared by both tiers) reads
`user.name`/`user.email` from the **host's** git config at launch and forwards
them as `GIT_USER_NAME` /
`GIT_USER_EMAIL`; the entrypoint materializes them into the sandbox user's global
config each boot (same materialize-each-boot pattern as `settings.json`). Keeping
this at **runtime** rather than build time sidesteps the build-context limit
(`~/.gitconfig` lives outside the `claude-sandbox/` context a `COPY` can reach) and
means the image is a generic artifact — the same build works for anyone, and
changing identity needs no rebuild. If the host has no identity, the launcher
**warns but does not fail**: the container still starts and git errors only at
commit time (`Author identity unknown`), which is legible and recoverable.

#### Managed settings are NOT an enforcement lever here

Claude Code's managed-settings tier is **single-source**: when more than one
managed source exists, one wins and the others are ignored — they do **not**
merge. Under **organization authentication** (an org-governed Claude account), the
winning source is Anthropic's **remote** server-managed settings, cached locally
at `/config/remote-settings.json` and `/config/policy-limits.json` (un-editable by
the sandbox). The `/etc/claude-code/managed-settings.json` the Dockerfile used to
deliver was therefore **not loaded at all** in this configuration — and has since
been removed from the build (see below).

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
  (No deny ships in the baked template today — it carries only the status line
  and the disclaimer flag.)
- **A settings opt-out is not a containment control.** Hardening env flags
  (`DISABLE_TELEMETRY`, `DO_NOT_TRACK`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) are set as real process
  `ENV` — the robust, unshadowable place for them — but they are defense-in-depth,
  not the boundary. Whatever the sandbox didn't configure and can't see coming (a
  telemetry exporter enabled upstream, any other unexpected egress) is stopped by
  the default-deny firewall, like everything else. Trust the firewall for
  containment, not the env flag; don't claim telemetry is off until export behavior
  is actually checked.

Skills do double duty: they are the capability interface **and** they encode the
right *way* to do a task, steering the agent onto the paved road.

## Web access (verified empirically in-sandbox)

Tested in the pre-proxy **standalone** posture (default-deny iptables + ipset
allowlist); the conclusions were re-confirmed under governed mode in the Layer-2
result below:

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
  skill (below). A user-scope deny remains available as *accident* prevention
  (none ships in the baked template today), never as containment.
- **WebFetch** is already network-governed; keep it. It also honors `HTTPS_PROXY`
  (inherits the CLI's proxy env), so it is audited through the egress proxy —
  **verified**: allow/deny CONNECT entries for a fetched allowlisted vs blocked
  host appear in the proxy audit log (see "Claude Code proxy support").
- **Optional `websearch` skill** — *only* if a future threat model decides to turn
  WebSearch off (see decision below). Web search would then become a governed call
  to a third-party search API (Brave / SerpAPI / Google CSE) with its own read-only
  key, routed through the egress proxy. Not planned while WebSearch stays enabled.

### Reaching the Anthropic API while governing everything else

**RESOLVED — (a).** Kept here for the rationale; the confirmation is the Layer-2
result below. The sandbox has no direct internet route (`internal: true`; the
proxy-less standalone fallback is the exception), so Anthropic access is a
deliberate path, decided one of two ways:
- **(a) Always-allow api.anthropic.com at the egress proxy** and route the CLI
  through it — cleanest, keeps Anthropic traffic audited. The docs say the CLI
  *does* honor `HTTPS_PROXY` for its own API calls (see "Claude Code proxy
  support"), so this is the expected path; only if an in-sandbox test contradicts
  that do we fall back to transparent-redirecting outbound 443 at the network
  layer.
- **(b) Narrow network-layer firewall allowlist** (iptables/ipset init
  container) permitting only api.anthropic.com directly, dropping the rest —
  forcing all other web through the proxy skill.

The lean was (a) so nothing escapes audit — since confirmed; (b) survives only
as the standalone (proxy-less) fallback.

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
  so the client-side WebFetch uses the same env vars — since **confirmed
  in-sandbox**: the Layer-2 result below shows WebFetch's allow/deny CONNECT
  entries in the proxy audit log. One concrete wrinkle: WebFetch fires a **domain-safety
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
  default-deny. **Decision: left to fail closed.** The connectors are unused here
  (MCP is out of scope — see Core idea), so failing closed costs nothing; set the
  env only if deny-log noise from `mcp-proxy.anthropic.com` ever becomes a
  nuisance.
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
refuses a non-allowlisted domain (`egress proxy does not allow non-allowlisted
example.com (held or denied)` — the wording covers 2b's hold-then-deny path) and
that direct egress, IPv6, and direct external DNS are all blocked. So (a) stands and the transparent-redirect fallback (b) is not needed.
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
- **Pull-through package cache** (npm/PyPI/apt) *(not built yet — see Build
  status)*. Ungoverned to the agent (fast, free installs); its upstream fetch is
  governed via the egress proxy. The one tool with upstream reach, and that
  reach is itself governed.
- **Toolchain in the sandbox image** — test runner, build, linters, formatters,
  language servers. In-image for v1 (part of the paved road), not separate
  containers yet.
- **Local scratch DB** (optional v1, not built) — for apps the agent builds
  during dev.
- **Local LLM inference** (`llm-intel` / `llm-nvidia` / `llm-vulkan` compose
  profiles) — an
  OpenAI-compatible `llama-server` on sandbox-net. Ungoverned because it has **no
  egress at all**, not merely a governed one: weights are pre-fetched on the host
  into a read-only mount, so the service never makes an outbound request. Built
  and verified; **deliberately not reachable by tier 1** — it is tier 2's one
  permitted destination. See "Local inference".

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

### Recently completed
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
  tests couldn't reach (**105 tests total**, still dependency-free — the count
  includes the `control-plane-ui` relay host-pinning regression tests that came
  with the 2b-2 split, the mapped-IPv6 relay-guard regression suite, and the
  frontend's Host / cross-origin / relay-allowlist / provenance guards, the
  standing-policy rules view, and the fresh-schema guard on the provenance column): the proxy's
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
- **DONE — `control-plane-ui` frontend split** (step 2b-2; see Build status).

### Clear future improvements
- Dedicated git proxy that speaks the git protocol (block force-push, restrict
  branches, per-repo policy) instead of HTTPS-through-egress.
- Separate test/build runner containers for isolation + parallelism.
- Selective MITM on credentialed hosts → true brokering so even self-use keys
  leave the sandbox.
- Docs mirror / offline docs tool.
- Progressive auto-approval driven by accumulated policy + audit history.
- **Rule MUTATION / revocation on the control plane** (listing is now done — see
  "Standing policy" below). Rules can be GRANTED — a `*_persist` resolution writes
  one — but still never edited or removed: there is no mutating rule endpoint, and
  the seed file is re-read only when the table is empty, so a mistaken persist is
  permanent short of hand-editing SQLite in the volume. Two sharp edges come with it
  — `resolve` persists the **agent-controlled** host string verbatim as a pattern, so
  a request for `.evil.com` turns one approval into a *subdomain wildcard* (`_match`
  treats a leading dot that way; the rules view now at least NAMES that scope, but
  does not prevent it); and the `INSERT OR IGNORE` means `deny_persist` silently
  writes nothing when a pattern already exists while still reporting
  `persisted: true` — reachable because retrying clients routinely produce two holds
  for one host. A governance plane that can grant but not revoke is incomplete; pair
  the mutation API with validating that a persisted pattern is a bare hostname and
  making wildcard-vs-exact an explicit operator choice at approval time.
- **Human-presence on approval (WebAuthn user-presence, or an out-of-band confirm).**
  The *only* thing that closes host-local forgery of an approval — see the
  browser-facing-guards note under "Frontend split". Worth building for that
  specific threat (the agent reaching host execution via the workspace mount), not
  because "the UI has no auth"; naive auth does not help, since any credential at
  rest on the host is readable by the same process.
- Normalize hosts consistently across the control plane. The proxy's relay guard
  strips a trailing FQDN dot but `_decide` / `_match` only lowercase, so `evil.com.`
  misses a persisted **block** rule and lands in a hold instead — fail-safe, but it
  lets an explicit operator denial be re-prompted indefinitely.
- **Split the control-plane API surface to cap the blast radius of any relay-guard
  bypass (planned).** The egress proxy only ever needs `/authorize` (+ `/healthz`);
  the *dangerous* surface is the management API (`/approvals`, `/approvals/{id}/
  resolve`, `/api/audit`), because `resolve` lets a caller approve held egress —
  i.e. self-approve. Today the agent is kept off all of it by three layers: network
  topology (no control-net route), the egress proxy's relay guard (`_forbidden`),
  and the local port gate (control plane on `:8090`, while CONNECT/HTTP are gated to
  `:443`/`:80`, so even a DNS-rebound name is dialed on a port nothing serves). The
  relay guard's resolve branch is best-effort (TOCTOU/rebind, resolution failure),
  so the robust improvement is to make a *bypass* non-catastrophic rather than to
  keep hardening the guard: **serve the proxy-facing `/authorize` and the
  human-facing management API on separate ports, and give the proxy a route only to
  the `/authorize` port** (e.g. a dedicated proxy↔backend network carrying only
  authorize; the management port reachable solely from `control-plane-ui`). Then
  even a total relay-guard bypass can, at worst, *query* a policy decision — it can
  never approve a hold or read the audit store. This addresses the impact
  (self-approval) instead of racing the mechanism (DNS), and composes with the
  topology refinement noted under "why two control nets" (the proxy comes off the
  broad `control-net` so it also has no route to future control-net tenants — the
  git/secrets brokers). Pairs with a `boundary-check.sh` assertion that the proxy
  can reach the authorize port but **not** the management port. See the
  "Control-plane relay guard" note under Build status for the guard this backstops.

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
broke the agent, and the fix spans both the launcher plumbing (`sandbox-lib.sh`)
and `init-firewall.sh`. The chain, so nobody has to rediscover it:

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
   Fix: the launcher (`sandbox-lib.sh`) pins the upstream explicitly with `--dns`, sourced
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
     chain**, so in STANDALONE mode those same upstream IPs must be whitelisted on
     port 53 (passed in as `UPSTREAM_DNS`) or runtime DNS dies the instant
     default-deny arms. (Governed mode blocks that forward on purpose — proxied
     tools hand the hostname to the proxy — and allows only the embedded resolver
     itself, which still answers sibling names.)

`sandbox-lib.sh` computes the resolver list **once** and uses it for both
`--dns` (the resolver's upstream) and `UPSTREAM_DNS` (the firewall allow-list), so
the two can't drift apart. Containment is unaffected: `--dns` only sets an
upstream, DNS is still pinned to *named* resolvers (no "any nameserver" hole), and
egress remains filter-table default-deny plus the mode's allowlist (ipset in
standalone; proxy-only in governed).

## Startup ordering — "running" is not "ready"

Every compose service declares a `healthcheck`, and both launchers gate on it via
`sc_wait_healthy` in `sandbox-lib.sh`. This is an **audit-integrity** measure
before it is an ergonomic one.

The egress proxy fails closed when the control plane is unreachable (`addon.py`
`_authorize`). That is the right behaviour, but it means a proxy that accepts
traffic *before* the backend serves `/authorize` does not stall the agent's first
requests — it **denies them and writes those denials to the audit log**, where
they are indistinguishable from policy decisions. The audit log is the artifact
this whole design exists to keep trustworthy, so boot ordering must not be able to
forge entries in it. Two gates close the window: `depends_on: condition:
service_healthy` (proxy and UI both wait for the backend), and the tier-1 launcher
waiting on the proxy's own health before starting a sandbox.

The probes are deliberately shallow — a TCP connect for the proxy, a static
`/healthz` for the two Python services. In particular the proxy is **not** probed
by making a real CONNECT through itself: that would exercise the policy path end
to end, but it would also write an audit record every interval, and a periodic
synthetic `deny` is exactly the signal a human reviewing the log is watching for.
A probe must not pollute the evidence it is protecting.

Tier 2's gate is about capability rather than audit: `llama-server` binds its port
immediately but returns 503 until the model is loaded and offloaded (minutes for a
large GGUF — see "Operational constraints"), and inference is that tier's only
capability, so `run-opencode-sandbox.sh` waits rather than letting opencode's first
turn fail. `sc_wait_healthy` treats `unhealthy` as retryable, since a load that
outruns its `start_period` passes through that state on the way up; only the
timeout is fatal. A container that declares no healthcheck is not gated at all —
absence of a probe is not evidence of a problem.

## Resource limits — blast radius, not boundary

Both sandbox tiers are capped by their launcher — tier 1 at 4g, tier 2 at 2g, both
`--cpus=4 --pids-limit=512` and both overridable per launch via `SANDBOX_MEMORY` /
`SANDBOX_CPUS`. The three infra services are capped in `docker-compose.yml`
(1g/512m/256m). Everywhere, swap is disabled by setting the swap ceiling equal to
the memory ceiling (`--memory-swap` / `memswap_limit`): Docker otherwise defaults
swap to 2x memory, so a bare 4g cap really means 4g RAM + 4g swap — on a 15 GiB
host, a ceiling above what exists is no ceiling at all. These are **not** a
containment boundary and nothing about the threat model rests on them — the
boundary is capability (network segmentation, dropped caps, non-root, no
control-plane route).

The sandbox numbers are sized for the **workload, not the agent**: a measured
tier-1 session (linters, a 68-test suite, ~25 compiles) peaked at 353 MB, so the
agent process is never what needs the headroom — `tsc`, `jest`, `cargo` or a
language server on a large tree is. Hence a modest default plus a per-workspace
override, rather than carrying the worst case for every launch. Tier 2 is lower
still on the merits: opencode is a thin client (inference lives in the `llm`
service) and the tier has no egress, so `npm install` / `pip install` cannot fetch
— its workload cannot grow the way tier 1's can.

What they buy is blast radius. The host OOM killer scores by footprint and kills
across the whole host, so an unbounded egress proxy under a connection flood could
get the **control plane** killed instead of itself. Per-container caps turn "the
kernel picks a victim" into "the container that misbehaved is the one contained."
The agent cannot exhaust host RAM directly, but it can drive proxy memory through
connection volume, and that is the residual path.

Every direction of failure here is fail-safe: lose the control plane and the addon
denies; lose the proxy and `sandbox-net` (internal) leaves no egress at all. The
caps are therefore sized to *never fire* in normal operation, because an OOM-killed
control plane writes denials to the audit log — the same pollution the readiness
gating exists to prevent.

**The LLM services are deliberately uncapped.** `llama.cpp` mmaps the GGUF, so the
weights are reclaimable page cache charged to the cgroup: a tight limit thrashes
the disk instead of OOM-ing, which is a slow failure rather than a loud one. And on
the Intel iGPU "VRAM" is host RAM allocated through `/dev/dxg` by the driver, so
whether it is charged to the container cgroup is not safe to assume — guessing
wrong means an OOM-kill mid-load plus a `restart: unless-stopped` crash loop. What
bounds that service is `-c`/`-ngl` against the ~16.9 GB shared pool, which is a
budget rather than a kill threshold.

## Local inference — an ungoverned LLM tool

**Status: built and verified on the Intel/WSL path; not reachable by tier 1 — it is
tier 2's sole destination (see "Built — the tier-2 local-model sandbox").**
`docker-compose.yml` defines `llm-intel`, `llm-nvidia` and `llm-vulkan`, each behind
a compose profile so none starts with a plain `docker compose up -d`. They serve
`llama-server` (an OpenAI-compatible API) on sandbox-net at `172.30.0.20:8080`.
Only `llm-intel` has been exercised on real hardware; `llm-vulkan` is designed but
unverified (see "Accelerator independence").

### Why it is ungoverned rather than governed

The taxonomy above requires an ungoverned tool to have **no independent egress**.
This one satisfies that by construction rather than by policy, which is a stronger
claim than the pull-through cache can make:

- sandbox-net only (`internal: true`), never egress-net, no published port;
- model weights are pre-fetched **by the human on the host** into a read-only
  bind mount, so the service has no reason to reach the network and no way to.

There is therefore nothing to audit at a choke point, because nothing crosses one.
Verified in-container: a TCP probe to a public address returns `Network is
unreachable` (not a timeout, not a filtered drop — no route exists), `docker
inspect` reports an empty `Gateway`, and `docker port` is empty.

The rejected alternative was to let the service fetch models itself with
`llama-server -hf`, proxying that upstream through the egress proxy — the
package-cache pattern. It is more convenient and would have been defensible, but
it trades a *structural* guarantee for a *policy* one. Pre-fetching costs one
manual download and keeps the guarantee provable by `docker inspect`.

### The GPU goes to the service, not the sandbox

Passing a GPU device into the *sandbox* would breach "never give the sandbox a
direct path to anything" — it is a host device, and on the Intel/WSL path it also
requires bind-mounting the host's driver directory. Instead the device is granted
to the inference container, and the agent reaches inference the way it reaches
every other capability: as a service on the internal network. The sandbox keeps
zero direct device access.

### Accelerator independence

The three profiles are mutually exclusive and deliberately share both the
sandbox-net address and the network alias `llm`. So the consumer endpoint
(`http://llm:8080`) and any future firewall `/32` are identical whichever
accelerator the host has, and switching hardware is a profile flag rather than a
rewiring. Enabling two profiles at once is an address conflict, which is the
intended failure.

- **Intel (WSL2)** — `--device /dev/dxg` plus `/usr/lib/wsl:ro`. There is no
  `/dev/dri` render node under WSL; the GPU is a paravirtual D3D12 device, and the
  in-image Level Zero runtime reaches it via `libdxcore.so` from that mount. A
  consequence worth recording: Intel's native Vulkan driver (ANV) **cannot** bind
  under WSL, so Vulkan there would run through Mesa's Dozen shim over D3D12 —
  published Arc-on-Linux Vulkan benchmarks do not transfer. SYCL is the path.
- **NVIDIA** — `gpus: all` (Compose ≥ 2.30) and the `server-cuda` image. The
  nvidia-container-toolkit injects driver libraries itself, so unlike the Intel
  path there is no device node or driver mount to declare.
- **Vulkan, native Linux only** (`llm-vulkan`) — **designed, not yet verified on
  hardware.** `--device /dev/dri/renderD128` and the `server-vulkan` image. One
  profile covers both AMD and native-Linux Intel because RADV and ANV are userspace
  Mesa drivers over the same DRM render node. `/dev/kfd` is deliberately *not*
  granted: that is the ROCm/HIP compute node and nothing here uses HIP, so the
  render node is the smaller capability that suffices. The render node is normally
  `root:render 0660`, so the container process must hold that group. `group_add`
  takes either a name or a numeric gid, and the difference matters: **only the
  number crosses the boundary.** Group *names* are a userspace lookup in the
  *container's* `/etc/group`, while the *gid* is what the kernel actually checks
  against the device node's owner. A name is therefore wrong twice over — it may
  not exist in the image at all (error), or it may exist and resolve to a
  different number than the host's (container `video` = 44 vs host `render` = 104),
  which silently fails the permission check. Hence the host's numeric gid supplied
  as `DOCKADE_RENDER_GID`; it varies by distro. This assumes ordinary rootful
  Docker: under `userns-remap` or rootless Docker, gids are remapped and the device
  grant needs rethinking rather than a different number.

  That variable is given an unresolvable-name default rather than compose's `:?`
  required form on purpose, and so is `DOCKADE_LLM_MODEL` in all three profiles.
  **Compose interpolates the entire file before profiles select which services
  run** — verified: with `DOCKADE_LLM_MODEL` empty, a plain `docker compose up -d`
  failed on `services.llm-intel.entrypoint` and refused to start the three infra
  services, even though no llm profile was active. So a `:?` inside a profile-gated
  service is silently a whole-project requirement, defeating the point of gating it.
  Requiredness has to be enforced where the container is *created*, not where the
  file is *parsed*; the trade is that `restart: unless-stopped` turns the misconfig
  into a crash-loop on that one service rather than a single clean error.
  A wrong gid does not fail loudly —
  llama.cpp falls back to CPU and the health check still returns 200, so **a healthy
  container is not evidence of acceleration.** Confirm with
  `/app/llama-server --list-devices` or the Vulkan device line in the startup log.

No variant needs an accelerator runtime installed on the host distro: the
llama.cpp images bundle the full Intel NEO / Level Zero and Mesa stacks themselves.

**Why AMD is Vulkan here rather than ROCm.** On supported hardware ROCm/HIP beats
Vulkan by roughly 10–20%, and by much more on long context, MoE, and multi-GPU
(Vulkan lacks row split); Vulkan tends to win short-context dense prefill and is
far less fussy about hardware. Two things settle it for this repo: upstream
ggml-org publishes no ROCm tag (only `cuda`, `vulkan`, `musa`, `intel`), so ROCm
means AMD's own `rocm/llama.cpp` images, and those are validated for MI-series
datacenter cards rather than consumer Radeon. Vulkan is therefore the default AMD
path, with ROCm left as a documented manual image swap (`+ /dev/kfd`) for anyone
running MI hardware. Note also that **AMD under WSL2 is not a viable target at
all** — the amdgpu module lives on the Windows side, `rocm-smi`/`amd-smi` are
unsupported there, ROCm-in-Docker-under-WSL is community-workaround territory, and
Vulkan hits the same Dozen problem as Intel. An AMD laptop on Windows means CPU
inference.

### Measured characteristics (Intel Arc 140V iGPU, Lunar Lake)

| Workload | Result |
| --- | --- |
| Decode, 4B Q4_K_M | ~30 tok/s |
| Decode, 9B Q4_K_M | ~15–17 tok/s |
| Prefill, 9B | ~164 tok/s (275-token prompt) |
| Cold model load, 9B | ~70 s |
| Two concurrent requests | per-request decode roughly halves |

Decode is **memory-bandwidth-bound** — the iGPU shares LPDDR5X with the host at
~135 GB/s, and measured throughput sits at ~55% of that ceiling. Prefill is
compute-bound and benefits from Xe2's matrix engines, giving a ~10x asymmetry.
**This machine is good at prompt-heavy, short-output work and bad at long-form
generation**, which should drive task design more than model choice does.

Verified working: OpenAI-style tool calling (correct function and arguments) and
`response_format: json_schema` constrained decoding. `--jinja` is required for
tool calls and is set in the compose entrypoint. Tool calling was re-verified
**with `--reasoning off`**, i.e. in the shipped configuration — the model picks the
function and argument in 27 tokens with no thinking step, so bounding reasoning
costs nothing here.

### Operational constraints (learned the hard way)

- **One model at a time.** ~16.9 GB of shared memory will not hold two useful
  models concurrently, and swapping costs a container recreate plus the cold load
  above. Every consumer shares one model; "cheap classifier plus capable agent
  simultaneously" is not available on this hardware.
- **Reasoning models need bounding.** Qwen3.5 has thinking on by default
  (`llama-server --reasoning` defaults to `auto`, which resolves to on). A trivial
  self-verification prompt ("say hi in five words") produced 6,099 reasoning tokens
  over 7.4 minutes — it found valid answers immediately, then looped re-checking.
  Genuine tasks reason proportionally (~50 tokens for a tool-call decision), so
  this is a tail risk, not a constant tax. **The default is therefore set
  server-side**: `--reasoning off` in the compose entrypoint, overridable with
  `DOCKADE_LLM_REASONING=on`. Measured on the same prompt: 6,099 tokens / 441 s
  with reasoning on, 8 tokens / 0.5 s with it off, and no `reasoning_content` field
  emitted at all. Server-side rather than per-request because a client
  that *can* send `"chat_template_kwargs":{"enable_thinking":false}` merely fixes
  itself, while one that cannot (opencode) has no recourse — so the fix belongs
  where every consumer inherits it. Keep a hard `max_tokens` and a client timeout
  regardless, and if reasoning is switched back on, bound it with
  `--reasoning-budget N` instead of leaving it unrestricted (`-1`).
- **A schema does not constrain reasoning.** With thinking on, `reasoning_content`
  can consume the whole `max_tokens` budget and return empty `content` — the
  grammar never applies. Unbounded reasoning defeats the reliability guarantee
  that constrained decoding is adopted for.
- **Constrained decoding guarantees shape, not values.** A first trial returned
  schema-perfect JSON reading `"critical"` for a line beginning `ERROR`, and
  expanded the component `db-pool` to `"database-connection-pooling-layer"`.
  Explicit "verbatim, do not expand" instructions fixed both. The lesson is not
  that the model is incapable but that its errors are *semantically* wrong while
  *structurally* valid, so nothing throws. Prefer parsing deterministic fields
  deterministically and giving the model only the genuinely fuzzy ones.
- **Use `temperature: 0`** for extraction and classification. The server default
  is non-deterministic and buys nothing on these tasks.
- **32k context is the working ceiling, and agents need most of it.** ~4.6 GB of
  f16 KV on top of ~5.5 GB of weights fits the ~16.9 GB shared pool with headroom;
  64k does not. 8k is not merely tight but unusable for an agent harness —
  opencode's base prompt (system + tool schemas) exceeds it before the first user
  turn. Both ends must agree: `-c` on the server and `limit.context` in the client
  config, guarded by `make consistency`. Avoid `-c 0` (load from model), which
  would size the allocation from the model's native window.
- **The prompt cache is per-slot, so run one slot.** llama-server defaults to 4
  slots assigned by LRU; a multi-turn conversation can land on a slot that never
  saw it and re-prefill the entire history. `--parallel 1` keeps the prefix stable.
  Concurrency was never real anyway — two in-flight requests contend for the same
  GPU (~331 tok/s prefill solo vs ~22 tok/s with two running).
- **Prefill throughput decays with depth.** ~331 tok/s for the first 2k tokens,
  ~236 marginal by 6k, as attention cost grows with context. Short-prompt
  measurements (the ~164 tok/s figure from the 275-token request in the table
  above) are dominated by
  fixed overhead and overstate the cost of long prompts while understating deep
  ones. Budget roughly half a minute for an 8k prompt.
- **Overflow should fail, not silently truncate.** `--no-context-shift`: the
  default discards the oldest tokens, which for an agent means evicting its system
  prompt and tool definitions mid-conversation — degradation that presents as the
  model becoming inexplicably confused rather than as an error.

### Why tier-1 Claude cannot reach it — deliberately

Sibling containers on sandbox-net are not reachable from the tier-1 sandbox: they
hit the default `REJECT` in `init-firewall.sh`. Confirmed from inside a running
sandbox — the name `llm` resolves via the embedded resolver, then the connection is
rejected in ~1 ms.

That gap is intentional. Default-deny means a capability is granted when it has a
consumer, not when it becomes technically possible, and the case for tier-1 Claude
using a 9B local model is weak: it is strictly less capable at everything the
agent already does well. The arguments that survive scrutiny are narrow —
**embeddings** (where the quality gap to a frontier model is small and the
capability is genuinely absent today), use as a **test fixture** for the repo's own
LLM-shaped development, and **bulk triage** where delegating keeps content out of
the agent's context *and* out of the network. None is currently pressing.

The mechanism to close it now exists — the LOCAL-mode `/32` allow described below —
so granting it later is a launcher change, not a design change. It stays ungranted
until a consumer justifies it.

### Built — the tier-2 local-model sandbox

The concrete motivation for the wiring above is not tier-1 Claude but a **second
agent tier driven by the local model**, which is a genuinely different containment
posture:

| | Brain | Egress | Credentials | Governed by |
| --- | --- | --- | --- | --- |
| Tier 1 | Claude (API) | proxy → allowlist | Anthropic session | egress proxy + control plane |
| Tier 2 | local LLM | **none** | **none** | nothing to govern — no egress exists |

Tier 2 holds **no credentials at all** — the credential invariant's "only what it
must" reduces to nothing. Built as `opencode-sandbox/` + `run-opencode-sandbox.sh`;
launch with `make opencode`.

**Its grant is defined by subtraction.** The launcher passes `SANDBOX_MODE=local`,
`LLM_IP`, `LLM_PORT` and git identity — and deliberately *no* proxy env, *no*
`UPSTREAM_DNS`, *no* `--dns`. Absence of capability is the mechanism; there is no
setting to get wrong. The launcher also refuses the standalone-network fallback
(`sc_ensure_network ... false`), because a non-internal bridge would hand a
no-egress agent exactly the egress its design forbids, and treats a missing `llm`
service as **fatal** rather than degraded: an opencode sandbox with no model is
broken, not reduced. `LLM_IP` is discovered from the running container rather than
hardcoded, so a compose subnet change cannot silently break the firewall's `/32`.

**One boundary implementation, two grants.** The extraction into `sandbox-common/`
(`init-firewall.sh`, `entrypoint.sh`, `boundary-check.sh`) plus `sandbox-lib.sh` for
launcher plumbing means both tiers run *byte-identical* enforcement; each image
supplies only a `tier-setup.sh` hook for its own declarative config. A firewall fix
lands in both tiers at once, and neither tier can drift into a weaker posture
unnoticed. `init-firewall.sh` gained a **third mode** alongside GOVERNED and
STANDALONE:

| Mode | Selected by | Permits |
| --- | --- | --- |
| GOVERNED | `EGRESS_PROXY_IP` set | loopback, embedded DNS, established, `/32` → egress proxy |
| LOCAL | `SANDBOX_MODE=local` | loopback, embedded DNS, established, `/32` → `llm:8080` |
| STANDALONE | neither | direct `ipset` IP-allowlist (proxy-less fallback, tier 1 only) |

LOCAL **fails closed**: if `SANDBOX_MODE=local` and `LLM_IP` is unset or not an
IPv4 address, the firewall aborts rather than booting an agent whose one intended
destination is unreachable. The egress-proxy allow is explicitly gated off in this
mode, so the proxy is not merely unused but unreachable.

**Verified empirically, not asserted.** `boundary-check.sh` branches on mode and
*inverts* the Anthropic check — in LOCAL mode, reachability is a failure. From
inside a running tier-2 sandbox all checks pass, including the two that only this
tier can make:

- `api.anthropic.com unreachable (correct: LOCAL mode has no egress)`
- `inference service reachable (172.30.0.20:8080) — the one permitted destination`

alongside control-plane isolation, IPv6 denied, zero capabilities, `no_new_privs`,
and no Docker socket. Tier 1 re-verified unchanged after the extraction (15/15,
including `api.anthropic.com reachable via proxy` and all six proxy refusals).
This is the strongest containment evidence the repo has: an agent that can reach
precisely one thing, holds nothing, and proves both from its own capability-less
security context.

**Runtime deps are baked at build time**, since the firewall only arms at container
*start* — so the image installs opencode and the `@ai-sdk/openai-compatible`
provider SDK while egress still exists. Confirmed working with no egress at
runtime. `NPM_CONFIG_PREFIX` points at the user's `~/.local` so `-g` installs work
as the non-root user, which also means the agent can npm-install at runtime without
ever holding root.

The provider config (`opencode.json`, `baseURL: http://llm:8080/v1`) is **not** a
containment control and is documented as such in-place: the agent can rewrite it,
and a rewritten `baseURL` buys nothing, because the firewall permits exactly one
destination — pointing opencode elsewhere yields a rejected connection, not egress.
That is the "capability, not configuration" split in miniature.

**Why the server sets the reasoning default.** opencode cannot send
`chat_template_kwargs` per request, so with llama-server's default (`--reasoning
auto` → on) every turn paid the full thinking cost — the observed "somewhat slow"
behaviour. Fixed at the server (`--reasoning off`, see *Operational constraints*),
which is the right layer: a per-request workaround only helps clients able to send
one, and this tier's whole point is hosting consumers that are not under our
control.

**Open — the unattended worker tier, deliberately undesigned:**

- **Is the scheduled worker tier 2 in a different mode, or a tier 3?** What is
  built is the *interactive* local-model sandbox. An unattended scheduled worker
  differs in the way that matters most: with no human present, every hold degrades
  to a deny, so a worker can only ever do what is already pre-approved and can
  never escalate. That is a different policy posture, not a different
  configuration — which argues for a separate tier even though the image would be
  near-identical.
- **Should tier 2 ever get governed egress?** Today it has none, which is what
  makes its boundary evidence so clean. Granting it would make tier 2 a second
  egress-proxy client and immediately forces the per-client-class question below.
  Not needed until a worker task needs to fetch something.
- **Per-client-class policy.** A single global allowlist becomes the union of
  every client's needs, which erodes least privilege as soon as there is a second
  consumer — and the approval semantics above mean the postures genuinely differ.
  The identity primitive should be the **ingress network**, matching this design's
  existing idiom (topology is provable; source IPs are not, since sandboxes are
  ephemeral with dynamic addresses). Keep **one** proxy so the audit stream stays
  single. Cheap step available now: give policy rules and audit rows a
  client-class dimension even while only one class exists — adding a column to a
  young schema is free, retrofitting one into accumulated crown-jewel state is not.
- **Where does worker output go, and is it audited?** An unattended job's output
  *is* its consequential action, but with no egress there is nothing at the
  network choke point to log. "Everything consequential is audited" currently has
  no answer for an agent whose only output is a file.
- **Unattended blast radius.** Wall-clock, iteration and token budgets are
  required, not optional — see the 7.4-minute greeting above.
- **No concrete first task yet.** The worker tier should not be designed further
  in the abstract.

## Layout

See `README.md` → Layout for the current tree. Still-planned additions to it:

```
dockade/
  tools/              # ungoverned data-plane services (cache, scratch DB, ...)
  claude-sandbox/
    skills/           # sanctioned capability + workflow interface
    hooks/            # quality-gate hooks
```

## Open decisions

- **RESOLVED — control-plane stack is Python (FastAPI) over SQLite.** Shipped in
  2a (`control-plane/`): FastAPI app, stdlib `sqlite3` store, plain `def`
  endpoints (FastAPI's threadpool keeps the blocking DB off the event loop). The
  SSE approval UI shipped with 2b-1 and moved to the separate `control-plane-ui`
  app in 2b-2. `mitmproxy` remains the egress-proxy engine.
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
plane — the egress proxy at this step; the control plane + UI frontend and the
profile-gated inference service joined as later steps landed), and the
`run-*-sandbox.sh` launchers still
launch the **ephemeral sandbox(es)** that attach to it (`docker run -it --rm`,
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
meaningful; and the allowlist was re-read per connection, a cheap stand-in for
"dynamic" until the control plane existed (2a replaced the baked allowlist
entirely — the proxy is a control-plane client now, see below). Governs by
**name**, so it closes the shared-CDN/fronting gap the IP firewall can't (for
proxied traffic).

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
the sandbox to `control-net`/`control-ui-net`). In 2a it sat on `control-net`
(internal) for the proxy control path and on `control-ui-net` for the human UI;
since 2b-2 the backend is `control-net`-only and the UI frontend carries
`control-ui-net` (see below).
That UI bridge is non-internal **by necessity** — Docker cannot publish a host
port from a container that is on an internal network alone — but has masquerade
disabled, so it carries the loopback UI publish without being an egress path.
The control plane is a small FastAPI app over a SQLite policy+audit store (its
own named volume — the crown-jewel state); the management surface reaches the
host as **loopback only** (`127.0.0.1:8081`), published since 2b-2 by the UI
frontend rather than the backend.

*Design note — why two control nets, and the frontend split.* `control-net` stays
hard-`internal` because it is the **shared** control path for the whole governed
data plane (egress proxy today; git/secrets proxies later), and egress is granted
**only** by `egress-net` membership — a non-internal `control-net` would silently
hand ungoverned egress to every service on it (see "Networks"). Publishing a host
port, though, forces *some* non-internal surface, so `control-ui-net` quarantines
it to a single-member bridge. Honest caveat: before 2b-2 — when the backend
itself carried `control-ui-net`'s soft-egress surface — the split's security
gain was ~nil, and collapsing to one non-internal `control-net` would have been
observably equivalent; but that would have baked in a non-internal shared
control path that becomes a real hole the moment a must-stay-egress-free tenant
(secrets broker) joins. Keeping the split was cheap insurance against that
footgun. **Done in 2b-2:** the UI is now a **distinct
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
control-net IP.

The same guard also hard-blocks the **private / special-use ranges** —
cloud-metadata / link-local (`169.254.0.0/16`), loopback (`127.0.0.0/8`), RFC1918
(`10/8`, `172.16/12`, `192.168/16`), `0.0.0.0/8` (`connect(0.0.0.0)` reaches
localhost on Linux — loopback under another spelling), CGNAT/overlay
(`100.64.0.0/10`), protocol-assignment and benchmarking (`192.0.0.0/24`,
`198.18.0.0/15`), and multicast/reserved/broadcast (`224.0.0.0/4`, `240.0.0.0/4`),
plus their IPv6 equivalents — via a
separate `EGRESS_PRIVATE_CIDRS` set (`_forbidden`, checked before policy). The
proxy's default route is egress-net, a masquerading bridge with a path to the
cloud instance-metadata service (a credential-theft target), the Docker host, and
the host's internal network; without a hard block, reaching those would rest
solely on the default-deny allowlist, so a single mistaken rule or human approval
could turn the proxy into an SSRF pivot. The proxy's only legitimate upstreams are
public hosts (sibling data-plane services are reached by the agent *directly* on
sandbox-net, never through the proxy), so blocking every private range costs
nothing; RFC1918 `172.16/12` also transitively covers control-net and sandbox-net.
Kept a *separate* env from `EGRESS_FORBIDDEN_CIDRS` so the control-net guard's
fail-closed startup assertion stays specifically about control-net. Override
`EGRESS_PRIVATE_CIDRS` (narrow, don't empty) only for a deployment that
legitimately proxies to a private target such as an internal package mirror.
`boundary-check.sh` asserts the proxy 403s `169.254.169.254`.

*A destination, not a string — spelling normalization (fixed bug, keep the
regression tests).* Both hard-blocked sets are lists of **IP ranges**, but what
arrives is a **hostname field**, and the two are only equivalent after
normalization. The guard therefore lowercases, strips a trailing FQDN dot, strips
the brackets an IPv6 literal wears in an authority (`[::1]`), and — the part that
was missing — folds the IPv6 forms that carry an **embedded IPv4 address** down to
the v4 address they actually dial: v4-mapped (`::ffff:a.b.c.d`), the deprecated
v4-compatible (`::a.b.c.d`), NAT64 (`64:ff9b::/96`) and 6to4 (`2002::/16`); see
`_embedded_ipv4` / `_blocked_cidr`. Without that fold, `::ffff:169.254.169.254` was
**the metadata service under a spelling neither deterministic branch recognized**
— `ipaddress` containment never crosses address families, so it matched none of
the v4 ranges, and the resolve branch was no help because `getaddrinfo` returns the
same mapped form straight back, while `connect()` on a v4-mapped address delivers
to the v4 host. It was reachable in practice because metadata serves on `:80`,
already a permitted HTTP port; only the host policy's default-deny stood behind
it, which is exactly the "one mistaken rule or approval" case this guard exists to
remove. Teredo (`2001::/32`) is deliberately *not* folded — its embedded v4 is the
client's own NAT, not a destination anything delivers to. The fold is precise
rather than a blanket v6 ban: `::ffff:8.8.8.8` still goes to policy like any other
host. `boundary-check.sh` now probes the **mapped** spelling of both control-net
and metadata alongside the dotted-quad ones — the dotted-quad probes passed
throughout this bug, so they cannot catch its return.

*Defense-in-depth, not the sole control — and honest about the resolve branch.*
The hostname and literal-IP checks are **deterministic** (decided from the request
alone); the resolve branch is **best-effort** — it depends on a DNS lookup, so it
carries a TOCTOU/rebind gap (mitmproxy re-resolves when it dials) and can be
skipped on resolution failure (logged, returns "not forbidden" — safe, because an
unresolvable name is also undialable, and reaching the control plane is prevented
first by topology and by the port gate: the control plane listens on `:8090` while
CONNECT/HTTP are gated to `:443`/`:80`, so a rebound name is dialed on a port
nothing serves). To keep the guard from being *silently* disabled by
misconfiguration, `load()` calls `_assert_guard_configured()`, which **fails
closed at startup** (refuses to run) if `EGRESS_FORBIDDEN_CIDRS` is empty — an
empty CIDR set would drop both the literal-IP and resolve branches, leaving only
exact-hostname matching. The durable fix for the residual rebind gap is not to
keep hardening the DNS check but to **cap the blast radius of any bypass** by
splitting the control-plane API surface so a reached backend can only *query*
`/authorize`, never self-approve via the management API — see the API-surface-split
item under "Clear future improvements". The call runs in a worker thread
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

**Browser-facing guards on the frontend (and their honest limit).** The frontend
publishes the only API that can GRANT egress — `POST /approvals/{id}/resolve` is
self-approval if reached — on host loopback, with no auth. Loopback binding is not a
defense against **DNS rebinding**: a page the operator visits can point its own name
at `127.0.0.1`, at which point it is *same-origin* with the frontend, so there is no
preflight and responses are readable — it can list the pending approvals (obtaining
the `uuid4` ids a blind attacker cannot guess) and resolve them. That, not CSRF, is
the vector that matters here; plain CSRF is largely self-blocking, because a JSON
body forces a preflight that fails for want of CORS headers and `resolve` needs an
unguessable id. Three structural guards now sit in front (`control-plane-ui/app.py`):

- **Host allowlist** (`CONTROL_UI_ALLOWED_HOSTS`, default `127.0.0.1,localhost,::1`)
  — closes rebinding. Rebinding *requires* the attacker's own name in `Host`, and
  `Host` is a forbidden header JS cannot set, so this removes the attack class
  rather than raising its cost. Compared without the port on purpose: the port is
  irrelevant to the guard, and ignoring it keeps one list valid for both the
  published `:8081` and the in-container healthcheck's `:8090`. Import fails closed
  on an empty list (`_assert_host_guard_configured`, mirroring the egress addon).
- **Cross-origin state changes refused** — `Sec-Fetch-Site` when present, else
  `Origin`; both absent means no browser is calling, so there is no CSRF to stop and
  refusing would only break scripting. Belt-and-braces for a future endpoint that is
  less lucky than the current ones.
- **Relay path allowlist** — only the paths the UI actually uses are proxied. The
  backend surface is not uniformly browser-appropriate: reaching `POST /authorize`
  from a page means forged audit rows and consumed hold slots on the governance
  authority. The old catch-all `/{path:path}` relayed it.

*What these cannot do.* They are **browser-enforced**. A process running on the host
sets any header it likes and can still reach the API — and that is not hypothetical
here, because the agent's RW workspace bind mount is an acknowledged (delayed) path
to host execution (see the workspace guard in `sandbox-lib.sh`). **Authentication
would not close it either**, which is the non-obvious part: any credential at rest
on the host — token file, env var, browser cookie — is readable by that same
process, so it adds friction and a secret to manage without changing the outcome.
Closing host-local forgery requires a human-presence gesture the host cannot replay
(WebAuthn user-presence on `resolve`, or an out-of-band confirm). That is a
deliberate *later* decision, and it should be taken for this specific threat rather
than for "the UI has no auth" in general. Ceiling worth stating plainly: while the
bind mount exists and the approval surface must be reachable by a human who is on
the host, no purely host-side control survives host code execution.

**Standing policy is now visible in the UI (`GET /api/rules`).** The UI showed
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
this makes policy reviewable, it does not add mutation.

**Approval provenance — detection where prevention is not available.** Given that
ceiling, the frontend and backend at least make a forged approval *visible*. The
relay strips client-supplied provenance headers (`X-Dockade-Actor`,
`X-Forwarded-*`, `Forwarded`, `X-Real-IP`) and re-adds `X-Dockade-Actor` with the
peer address it actually observed; the backend's `_actor` records that on the
approvals row (`resolved_by`) and carries it into the audit reason, so the log
answers "who granted this egress" rather than merely "a human did". The labels keep
trust levels apart: `peer=` is observed by the backend (but is the *relay* for
anything via the UI), `via-ui=` is asserted by the relay, and `origin=` / `ua=` are
self-reported and forgeable — recorded anyway because they are usually what betrays
a non-browser caller. None of this was recorded before: an operator's click and a
scripted POST were indistinguishable after the fact.

*Schema note (read before adding a column).* `resolved_by` ships with **no migration
step**, which is safe only because of a one-time circumstance: the single store that
predated the column was migrated in place with `ALTER TABLE ADD COLUMN` before the
migration code was removed, and every store created since gets the column from the
`CREATE TABLE`. That is not a general pattern — `CREATE TABLE IF NOT EXISTS` is a
no-op on an existing table and this store is a long-lived named volume that
deliberately outlives container and image churn, so **the next additive column will
need its own explicit `ALTER` for existing volumes**, or every statement naming it
fails at runtime. The only alternative is `make destroy`, which discards the policy
rules and the audit history — i.e. the crown jewels. See the NOTE in `_init_db`.

Step 2c: audit *browsing* — filter/search/history beyond the live
recent-decisions table the UI already renders — and the per-proxy config
surface (rows accumulate from 2a).

Not yet built: audit browsing beyond the recent-decisions table (2c),
git/secrets/cache data-plane services, skills, quality-gate hooks.

**Transitional allowlist entries (remove at the cache/git phase):** the
**standalone-mode** firewall allowlist and the control-plane policy seed still
whitelist package registries (npm/PyPI) and GitHub only because the data plane
that should mediate them doesn't exist yet. (Governed mode — the default —
already gives the sandbox no direct egress at all; there the entries live on as
proxy policy, not firewall rules.) In the target design packages come from the
pull-through cache (upstream via the egress proxy) and git via the governed git
path. Only the Anthropic API/auth lifeline is permanent, and it already routes
through the proxy as an always-allow in governed mode. The allowlist is grouped
PERMANENT vs TRANSITIONAL in `init-firewall.sh` to make this explicit.
