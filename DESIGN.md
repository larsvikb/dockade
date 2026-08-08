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
   GOVERNED proxies/tools   (dual-homed: sandbox-net + a control net;
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
and **escalate unknowns** to it. Dual-homed on sandbox-net (serve agent) and a
control net (talk to control plane — the egress proxy uses `authorize-net`, a
narrowed one; see Governance surfaces); outbound ones also on egress-net. Examples:
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

## Networks

- `sandbox-net` (`internal: true`) — sandbox ↔ all data-plane services (governed
  + ungoverned). The agent lives only here.
- `control-net` (`internal: true`) — the MANAGEMENT path: control plane ↔
  control-plane-ui, and later the git/secrets brokers. Sandbox not attached, and
  neither is the egress proxy.
- `authorize-net` (`internal: true`) — the AUTHORIZE path, one conversation only:
  egress proxy → the control plane's `/authorize` listener. Exists so the proxy
  can ask policy questions without gaining a route to the management API; see
  "why three control nets" under Governance surfaces. Sandbox not attached.
- `egress-net` — only outbound-capable governed proxies + internet.
- `control-ui-net` — non-internal bridge carrying ONLY the control-plane-ui
  frontend's host-loopback UI publish; masquerade disabled so it is
  host-reachable but not an egress path. Needed because Docker cannot publish a
  host port from a container that is on an internal network alone. Sandbox not
  attached.

**Status:** all five networks are implemented. `sandbox-net` (internal) and
`egress-net` carry the agent and the sole egress; the two internal control nets
carry the control path, split by surface. The egress proxy is **triple-homed**
(sandbox-net + egress-net + authorize-net) — note `authorize-net`, not
`control-net`. The control-plane **backend** is on both control nets and fully
internal (no `sandbox-net`, no `egress-net`, no published port), serving a
different surface on each; the **control-plane-ui** frontend is on `control-net`
(to reach the backend) plus `control-ui-net` (host-loopback UI). The sandbox is on
`sandbox-net` only — never any control network (asserted by `make check`,
`tests/test_topology.py` and `boundary-check.sh`).

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

## The sandbox image (the agent's "paved road")

Claude Code, with yolo available as a conscious opt-in (`claude-yolo`). This
image is where **enablement** lives:
- curated toolchain + pre-wired linters / formatters / test runners
- baseline Claude Code settings and **hooks as quality gates**
  (e.g. format/test on write, block known-bad patterns) — *hooks: planned, not
  yet in the image (see Status)*
- a default **status line** (`claude-sandbox/statusline.sh`, seeded into user
  settings by the tier-1 setup hook the entrypoint runs — see below)
- the **skills** that are the sanctioned interface to every capability —
  *planned, not yet in the image*
- non-root `sandbox` user, resource limits

### User settings — image-owned config, materialized each boot

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

### Git identity — from the host at launch, not the tree

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

### Managed settings are NOT an enforcement lever here

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

### What the image ships today

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
  true` (the proxy landed — see "Governance surfaces"), so the sandbox has no
  direct route off-box; the launcher's plain-bridge fallback (proxy-less standalone
  use) is non-internal and keeps direct egress via `init-firewall.sh`.

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

- **WebSearch cannot be blocked at the network layer** — that would mean blocking
  api.anthropic.com, which the agent needs. Nor is there any *client-side* lever: a
  yolo agent edits or relaunches around a user-scope `permissions.deny`, and the one
  mechanism that could have outranked it — a root-owned local managed file — is **not
  loaded** under org auth (verified; see "Managed settings are NOT an enforcement lever
  here"). So the only real controls are the **org admin console** (server-managed) or
  **removing the capability**, replacing WebSearch with a governed `websearch` skill
  (below). A user-scope deny stays available as *accident* prevention — none ships in
  the baked template today — never as containment.
- **Disabling it is a threat-model choice, not automatic.** WebSearch is
  read-only: the agent cannot POST/upload through it. Realistic risks are narrow
  — low-bandwidth exfil via crafted query strings (to Anthropic's search, not an
  attacker endpoint) and unaudited information *intake*. If neither is in scope,
  leaving WebSearch on keeps its perf/capability benefit. Disable only if
  unaudited intake or query-string leakage matters for your threat model.
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
  `HTTPS_PROXY`, and `NO_PROXY` (docs use **uppercase**), Node-native, **read once at
  startup**. Basic auth via `user:pass@` in the URL. **No SOCKS.** → *Approach (a) is
  viable: a plain forward proxy injected via `HTTPS_PROXY`, no transparent
  443-redirect needed.* The launcher nevertheless sets **both cases**, because the
  agent is not the only thing in the image making requests: `curl` reads `http_proxy`
  in **lower case only** — a deliberate httpoxy mitigation, not an oversight — so with
  uppercase alone, plaintext HTTP bypassed the governed proxy entirely and died at
  DNS, unheld and unaudited. Two guards, because neither covers the other: `make
  consistency` asserts the pairing in the launcher *source*, and `boundary-check.sh`
  probes `http://` from *inside a running container*, which is the only place an
  environment that drifted — or a sandbox started before the fix — becomes visible.
  Every other governed probe in that script is `https://`, and curl honours
  `HTTPS_PROXY` in either case, so they all passed throughout. `NOTES.md` has the
  measurement.
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

## Governance surfaces

The three services that actually implement governance, and the reasoning each one
rests on. This is the "why it is this way" that a change has to keep true — it was
previously filed under a heading called *Build status*, which is why it kept
growing without anyone noticing.

### Egress proxy — the sole path off-box

**The proxy and the compose split** (`docker-compose.yml` + `proxies/egress/`). The
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

**The proxy is the sole egress at the network layer.**
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

**Control-plane relay guard (the proxy is the bridge, so the proxy must refuse
it).** The egress proxy is the *one* component attached to both `sandbox-net` and
a control network (`authorize-net` — see "The third net" below; it is deliberately
**not** on `control-net`), so segmentation alone does **not** isolate the agent from
the control plane — the proxy could in principle relay a connection onto that
network. It therefore hard-refuses, **before** any policy / permanent-lifeline /
port check, any destination that names a control-plane host (`control-plane`,
`control-plane-ui`) or resolves into either control subnet (`172.31.0.0/24`
control-net, `172.29.0.0/24` authorize-net) — see `_forbidden` in
`proxies/egress/addon.py` (`EGRESS_FORBIDDEN_HOSTS` / `EGRESS_FORBIDDEN_CIDRS`).
Because the guard is checked first and never weighed against policy, no rule, human
approval, or change to the port allowlist can widen it, and a public name whose DNS
is pointed at a control subnet is caught by the resolve step. This makes the
CLAUDE.md invariant ("the agent must never reach the control plane") independently
enforced at the one place segmentation cannot cover; `boundary-check.sh` asserts the
proxy 403s a control-plane host and literal IPs in both control subnets. And the
damage a bypass could do is bounded by the API-surface split (below): the network
the proxy can reach carries only `/authorize`, so even a total bypass reaches a
listener that cannot approve a held request.

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
first by topology and by the port gate: the control plane listens on `:8090` and
`:8091` while CONNECT/HTTP are gated to `:443`/`:80`, so a rebound name is dialed
on a port nothing serves). To keep the guard from being *silently* disabled by
misconfiguration, `load()` calls `_assert_guard_configured()`, which **fails
closed at startup** (refuses to run) if `EGRESS_FORBIDDEN_CIDRS` is empty — an
empty CIDR set would drop both the literal-IP and resolve branches, leaving only
exact-hostname matching. The durable fix for the residual rebind gap was never to
keep hardening the DNS check but to **cap the blast radius of any bypass**, and
that is now built: the API surface is split, so a bypass reaches a listener that
can only *query* `/authorize` and can never self-approve through the management
API — see "the third net, `authorize-net`" below. The call runs in a worker thread
so it never blocks mitmproxy's event loop, and the egress image gains no new
dependency. The canonical allow policy now lives at `policies/egress-allowlist.txt`
(the control plane seeds SQLite from it on first boot, idempotently); the proxy
no longer bakes or reads an allowlist file.

### Control plane — policy, audit, hold-for-approval

**The backend and its isolation** (`control-plane/` + `control-net`). The
governance authority now exists as a service the **agent cannot reach**: it is
not on `sandbox-net`, so the sandbox has no route to it (`boundary-check.sh`
probes the control plane's fixed control-net address and asserts it is
unreachable from the sandbox; `make check` asserts the launcher never attaches
the sandbox to any control network). In 2a it sat on `control-net`
(internal) for the proxy control path and on `control-ui-net` for the human UI;
2b-2 moved `control-ui-net` to the UI frontend, and the API-surface split moved
the proxy's control path off `control-net` onto `authorize-net`, so the backend
now spans the two internal control nets and serves a different surface on each
(see below).
That UI bridge is non-internal **by necessity** — Docker cannot publish a host
port from a container that is on an internal network alone — but has masquerade
disabled, so it carries the loopback UI publish without being an egress path.
The control plane is a small FastAPI app over a SQLite policy+audit store (its
own named volume — the crown-jewel state); the management surface reaches the
host as **loopback only** (`127.0.0.1:8081`), published since 2b-2 by the UI
frontend rather than the backend.

*Design note — why three control nets, and the frontend split.* `control-net` stays
hard-`internal` because it is the **shared** control path for the whole governed
data plane (the UI today; git/secrets proxies later), and egress is granted
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

**The third net, `authorize-net`, and why the API surface is split.** The egress
proxy is no longer on `control-net` at all. It sits on a single-conversation
bridge to the control plane, and the control plane answers a *different surface*
on each of its two networks: the management API (approvals, `resolve`, the
read-only views) on `control-net`, and `POST /authorize` alone on `authorize-net`.

The reasoning is about which failure is worth designing around. The dangerous
endpoint is `resolve`, because it is what grants egress — a caller that reaches it
approves the agent's own held requests, and the governance plane becomes a
formality. Three layers keep the agent off it: no control-net route from the
sandbox, the proxy's relay guard, and the proxy's port gate. But the relay guard's
resolve branch is *best-effort by construction* — it re-resolves, so a name can
change answers between the check and the dial — and hardening it further is racing
DNS rather than addressing the consequence. So the consequence is addressed
instead: after the split, a total relay-guard bypass reaches a listener that can
answer a policy question and nothing else.

**Both halves are load-bearing, and neither is sufficient.** The network split
alone would still put one all-routes listener on the network the proxy can reach.
The port split alone would sit on a network the proxy is already attached to. What
makes them compose is the *bind address*: the management listener binds the
control-net address only, so it is absent from `authorize-net` even on its own
port. A wildcard bind would quietly undo the whole thing while every health check
stayed green and every page in the UI kept working — which is why `app.py` refuses
to start on one rather than trusting compose to be right.

**One process, two sockets**, and that is forced rather than chosen: a held
`/authorize` blocks on a `threading.Event` that `resolve` sets, so the two surfaces
must share memory precisely because they must not share a socket. Splitting them
into two containers would mean externalising the hold registry, which trades a
narrow, checkable property for a distributed-state problem.

Three guards stand where no compiler can: `tests/test_topology.py` reads
`docker-compose.yml` and asserts who is attached to what (the app cannot see its
own topology, and compose cannot see which routes an app serves);
`tests/test_control_plane_api.py` asserts the route partition itself; and
`make split-check` probes *from inside the running proxy*, which is the only place
the claim actually applies — `boundary-check.sh` runs in the sandbox and gets a
relay-guard 403 long before reachability is in question.

That probe covers **both halves separately**, because they fail independently and
only one of them is ours. By NAME (which resolves to the authorize-net address) it
asserts the management API is not served there — that is the bind address doing its
job. By LITERAL `172.31.0.2` it asserts the control-net subnet is unroutable from
the proxy at all — and that is Docker's inter-bridge isolation plus `internal:
true`, not code in this repo, which is exactly why it is measured rather than
assumed. The literal probe hits port **8091 as a positive control**: the authorize
listener binds the wildcard, so it genuinely is listening on `172.31.0.2:8091`, and
only if *that* is unreachable does the `:8090` result mean the subnet is closed
rather than one port being shut.

The probe classifies four outcomes rather than two, because "did not connect" hides
three different meanings. A **dropped** packet is the one that proves a boundary. A
**refused** proves the opposite — the packet arrived and something answered with an
RST, so the subnet is routable and the port is shut by luck — and is reported as a
failure even though the connection did not succeed. **Unresolved** proves nothing at
all, and is never a pass in either direction: Docker's embedded DNS answers only for
running containers, so with the control plane stopped every by-name negative probe
becomes trivially true. That last case was found by reading real output rather than
by design — the check reported `PASS management API is not served on the
authorize-net address` about a name that had not resolved.

The egress proxy is now a **control-plane client** rather than a static-allowlist
enforcer: on every connection it calls `POST /authorize {host, ...}`, which
returns the decision **and** records the audit row in one call — so policy and
audit share the round-trip, and there is no client-side cache (an operator rule
edit applies to the very next connection). Two deliberate properties: (1) the
**permanent lifeline** (Anthropic API/auth) is allowed by a *local* check in the
proxy *before* the control plane is consulted, so a control-plane outage never
bricks the agent's own API; (2) everything else **fails closed** — if the control
plane is unreachable or times out, the request is denied and audited locally.

**Ingesting the decisions the proxy makes alone.** Those two properties, plus the
relay guard, the port gate and the SNI anti-fronting check, mean a real share of
egress decisions are made *in the proxy* and never travel the authorize path. They
were always audited — the invariant held — but only to the proxy's own stream, so
the UI's "Recent decisions" was a record of round-trips, and a **domain-fronting
refusal**, the most alarming line the proxy can emit, appeared nowhere a human was
looking. The control plane now mounts the proxy's audit volume **read-only** and
tails it, ingesting the lines the proxy marks `central: false`.

The reasoning is why this is a *pull* and not a push, which is not obvious and
spans all three components:

- **The file already was the durable queue.** `audit.jsonl` sits on a named volume,
  append-only and ordered, and it cannot be removed regardless — it is the record of
  record during a control-plane outage. Any broker or POST would have been a *second*
  durable store of the same events.
- **Pulling makes the ingest exactly-once for free.** The cursor lives in the same
  SQLite as the audit table, so rows and cursor advance in one transaction. Every
  push design delivers at-least-once, which imports an idempotency key, a unique
  index and a dedup pass — the entire complexity budget, spent on a problem the pull
  simply does not have.
- **It leaves the security-critical image alone.** The egress proxy is `mitmproxy` +
  one stdlib-only file with a pinned base; a push would have put the first pip
  dependency, and a fire-and-forget task, in the component whose compromise is total.
- **The `central` flag is load-bearing, and fails safe.** Every governed request
  writes a proxy line *too*, so without a marker the ingest would duplicate the whole
  log. It is tested for the literal `false`, so a line that predates the field, or
  carries a garbled one, under-reports rather than double-counts.

What this deliberately does **not** buy: decisions made while the control plane is
down still arrive late (on the next drain), and one made while it is down *and* the
volume is lost is gone. That was judged acceptable — the alternative is durability
machinery for rows that are almost all `deny — control-plane unreachable`, recorded
during a window in which nobody could load the UI either.

**Telling an outage denial from a policy denial.** Those `control-plane unreachable`
rows carry the same red `deny` tag as a rule refusing a host, against the same host
column, and they mean the opposite thing: not "your policy refused this" but "no
policy was consulted, because governance was unreachable". `/api/audit` therefore
classifies them (`_audit_view` → `fail_closed`), the row is marked, and a line above
the table states the count in words — colour alone must not be the only cue, and the
words are what the row edge can only imply.

**Which failure each mechanism actually covers** is worth stating, because they look
like one feature and are two. When the control-plane *container* is down the UI
cannot reach its backend either, so both polled views report their own staleness and
the operator is told directly — that is what the stale/cold wording exists for, and
no audit row is even ingested until the service returns. The `fail_closed` marker
covers the *other* shape: the control plane is up, the UI is healthy, the rules look
right, and the **proxy** cannot reach `/authorize` — a partition, a wedged listener,
DNS. Nothing else on the page changes in that state. Classification lives in the
backend rather than the browser so the marker string sits beside the test that pins
it against `addon.py`, which produces it in a different image with no shared module;
if they ever drift the row simply reverts to looking like a policy denial, which is
the safe direction.

**Hold-for-approval.** An unmatched host is no longer denied
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
plane). The two caps count **different sets** — waiters and cards respectively — since
duplicate holds share a card; see "Duplicate holds share one card" below.

### Approval UI — the one surface that can grant egress

**Why the frontend is a separate container.** The approval UI and the API/SSE relay
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
- **Refusal to be embedded** — closes **clickjacking**, which is the gap the first
  three guards structurally cannot see, and it was open until now. An attacker page
  that frames `http://127.0.0.1:8081` produces a request with a perfectly legitimate
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

**The stream had no reconnect, and said otherwise.** The page treated every
`EventSource` error as transient. It is not: an `EventSource` retries *by itself* only
from `CONNECTING`, and a non-200 response or wrong MIME type puts it in `CLOSED`
**permanently**. That state was reachable in ordinary operation — the relay had no
exception handling, so a backend restart made `httpx.ConnectError` a 500, the browser
gave up for good, and the status line went on reading "reconnecting…" while the page was
blind until someone reloaded it. Being blind is the exact failure the traffic light was
built around (an unseen hold default-denies after `CONTROL_HOLD_TIMEOUT`), so this was
the most consequential defect in the UI. Fixed at both ends: the relay answers a clean
**502** on any `httpx.RequestError`, and `app.js` reconnects by hand from `CLOSED` with
doubling backoff (1s → 30s cap, no jitter — jitter de-synchronises a fleet, and this
page has one operator, so determinism is worth more and makes the delay assertable),
reporting `retrying in Ns` instead of a comforting lie.

**The approvals list is keyed, and does not move under the cursor.** Rendering was an
`innerHTML` assignment of the whole list on every push, up to once a second. A hold
expires on its own after ~120s, so a card leaving the *middle* of the queue shifted
every card below it upward — between the operator's eye and their click, on a row of
buttons that **grant egress**. It also discarded the `disabled` state a resolve in
flight had just set. Cards are now created once, keyed by approval id, and updated in
place (`diffPending`); an unchanged push is a no-op. A card that leaves the queue
without the operator deciding it is marked **stale in place** with the reason rather
than yanked out, and swept only when removal cannot move a button under the pointer —
nothing in the list hovered or focused — with a 15s cap so a parked cursor cannot freeze
the list.

That gating turned out to have a hole, found by asking how to *test* the expiry marker
rather than by reading the code: "removal cannot move a button under the pointer" is
true immediately whenever the cursor is **outside** the list, so a stale card was swept
on the next 1s tick. The `expired — default-denied` message — the one departure that
reports a governance failure — was therefore on screen for about a second, readable only
by an operator who happened to be hovering. So `shouldSweep` gained a **minimum dwell**
that hover cannot shorten (`DWELL_MS`: 60s expired, 8s resolved, 5s resolved-elsewhere),
ordered by how much each matters. An expiry gets long enough to read; a resolve less,
because the operator performed it but may move the mouse away before reading the
confirmation (the same bug, and the only reason the outcome message *appeared* to work
is that clicking leaves the pointer inside the list); a card resolved elsewhere least.
Not longer, because stale cards would then compete with real pending holds on what is
fundamentally a work queue — the Decisions tab is the durable record.

Two smaller corrections came out of mutation-testing that fix: a `Math.max(STALE_MAX_MS,
dwellMs)` on the cap line was **dead logic** (the floor check above already guarantees
`ageMs >= dwellMs`) with a comment claiming it was load-bearing, which is worse than not
having it; and `markStale` now takes the whole `{text, dwellMs}` that `departure()`
returns rather than the two as separate arguments, so the message and how long it stays
readable cannot be passed apart — omitting the dwell would have silently given an expiry
the 5s treatment with nothing failing. Cards are built with `createElement`/`textContent` rather than an HTML string,
so for the one list that renders agent-controlled values the question of whether `esc()`
covers every context does not arise.

**A resolve now reports itself.** Previously the only feedback was the card vanishing on
the next SSE tick, so with the feed down — precisely the state above — a successful
approval was indistinguishable from a hung click, and errors arrived as a blocking
`alert()`. The card now shows `✓ allowed · standing rule: .example.com` (or
`· this request only`) inline the moment the POST returns, keeps its buttons disabled,
and on a `409` says so specifically — "no longer pending" is a different fact from a
failure, and re-enabling the buttons would only invite a second failing click. The
pattern in that line comes back from the *backend* rather than being reconstructed from
the click, so it reports what was **stored** — which is also the exact string an
operator would have to go and delete by hand.

**The card shows its deadline (`GET /api/config`).** A held request *blocks the agent*
and is **default-denied** when `CONTROL_HOLD_TIMEOUT` elapses, and that deadline was
the one thing the card could not say: it showed a static `requested 14:32:05` and then
disappeared, so 100 seconds left and 4 seconds left looked identical. Without it the
difference between hold-for-approval and a slow deny is invisible from the interface
built to govern it. The backend now exposes the window (a deliberately narrow
read-only view of non-secret config — `{"hold_timeout": 120.0}`, on the relay
allowlist), and each card carries `expires in 43s` plus a depleting bar, called out in
red under 20s. Three details are load-bearing rather than cosmetic:

- **Two clocks, so both ends are clamped.** `ts` is the backend's `time.time()` and
  `now` is the browser's. They agree in the intended deployment — both are the
  operator's host — and where they don't, `holdRemaining` clamps to `[0, holdTimeout]`
  so a skewed clock makes the countdown *wrong* rather than absurd (never negative,
  never longer than the whole window, and the bar can never exceed 100%).
- **Zero says `expiring now`, not `expired`.** The backend's clock decides, so a click
  at zero may still land; if it doesn't, the existing `409` path reports that honestly.
  Claiming expiry here would be the UI asserting an outcome it cannot know.
- **A departing card now says which way it went.** Expiry is a governance outcome — the
  agent was denied because nobody looked in time — while a card leaving with time on
  the clock means something else resolved it or the control plane restarted. Both used
  to read as the same hedged sentence; `goneReason` distinguishes them, and falls back
  to the hedge when `/api/config` is unreachable, because then we genuinely cannot
  tell. The whole feature fails soft: no config, no countdown, page otherwise
  unaffected.

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
need a new column on `approvals`, which has no migration step (see the NOTE above
`_seed_if_empty`); the rule itself is recorded with its pattern and `source='operator'`
in the rules table, and every later use of it is audited as `allowed by rule (…)`.

**The banner for requests that never became cards.** Over `CONTROL_MAX_PENDING` (16) or
`CONTROL_MAX_PENDING_PER_CLIENT` (4), `/authorize` fails closed **without creating an
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

**`hidden` did not hide, and it was never only the banner.** The banner shipped
permanently visible and empty — a Dismiss button with nothing to dismiss. The script
hides things by setting `.hidden`, which relies on the user-agent rule
`[hidden] { display: none }`, and that rule loses to *any* author rule setting `display`
on the same element, because author beats user-agent at equal specificity.
`.saturation` sets `display: flex`. So does `.countdown` — meaning empty countdown rows
had been rendering on every card before `/api/config` answered, and stale ones staying
put after a resolve, unnoticed for as long as the countdown has existed.

The page was already carrying a `[role="tabpanel"][hidden] { display: none }` rule: the
same bug, met once, patched for the one element that had it, class left open for the
next occurrence to be found from the running UI. It is now a global
`[hidden] { display: none !important; }` — the one context where `!important` is the
right tool rather than a smell, since outranking author `display` declarations on hidden
elements is the rule's entire job. The test guards **the global rule**, not a list of
elements, and fails on a narrower re-patch: one rule makes the class impossible, whereas
an enumeration is something someone has to remember to extend.

**Duplicate holds share one card — and that changes what a click grants.** A retrying
agent asks the same question repeatedly, and each attempt used to become its own card
and its own slot. With `CONTROL_MAX_PENDING_PER_CLIENT` at 4, four retries filled the
client's whole budget with copies of one question and the fifth was refused with no card
at all — the failure the banner above exists to report, reached by the most ordinary
behaviour an agent has. Identical holds now attach to the existing card, keyed on
`(client, host, port, proto)`: `client` is in the key because approving one sandbox's
request must not release another's, and `method`/`url` are out because they are exactly
what varies between retries, so keying on them would defeat grouping in the case that
motivates it.

What made this more than a UI tidy-up is that **the two caps were counting the same set
while protecting different things**, and nothing made that visible until duplicates
stopped being distinct. The global cap bounds *blocked workers* — the availability of
governance for every other sandbox. The per-client cap bounds *cards on the operator's
screen* — attention. Grouping forced them apart: the global cap now counts waiters and a
joined request costs one, while the per-client cap counts cards and a joined request
costs nothing. Skip that split and the feature is cosmetic; the fifth retry is still
refused at four, just after showing one card instead of four.

The governance cost is real and is paid explicitly. **"Allow once" now releases every
request on the card**, which widens what a single click grants — the precise class of
surprise the hold mechanism exists to prevent. What keeps it honest is a count on the
card, and the count is *live*: a retry can join between the render and the click, so a
number frozen at first paint would understate what the button is about to do. Two
invariants sitting in different files back that up. The joining window closes inside the
**same critical section** that records the decision, so nothing can attach to a card
that has already been decided and inherit an outcome it was never displayed beside; and
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
worst of both". Overwriting was the alternative and was declined: nothing in this system
revokes a rule, so `ON CONFLICT DO UPDATE` would make a click the operator was never
shown silently flip standing policy, and it would pre-empt the rule-mutation design
rather than settle it.

Two supporting changes make the refusal legible rather than surprising. `persisted` now
reports **whether a row was written**, read from the insert's `rowcount` instead of from
what was asked for, with `already_present` carrying the harmless same-action case — so a
card stops claiming a write it did not make. And each offered pattern travels with any
rule already holding it, so the confirm panel says so *before* the click. Both halves are
required: the panel is prevention, and the backend check covers the race that is the only
way the conflict arises at all.

**A decision row now says whose request it was.** `/api/audit` selected `stage` — which
nothing rendered — while omitting `client`, which the proxy has always populated with
the sandbox peer address. On a control plane **shared across every sandbox** that is
the difference between a record and half a record: "egress to pypi.org was allowed" does
not answer the question an audit trail exists for once two agents are running.

This is not a reversal of keeping client identity out of the saturation banner's
headline. That banner is a glanceable alert where a bare IP is noise; this table is the
forensic view where *who* is the entire question. The two decisions point the same way —
put the address where someone is reading carefully, not where they are only glancing.

Adding the column immediately showed that the **proxy was not sending the value on one
of its two paths**: `http_connect` had always passed the peer address, and the plaintext
`request` hook never did, so every HTTP decision was stored against no client at all.
Invisible for as long as nothing rendered it, and invisible to the tests too — the
request-flow stub had no `client_conn` for a test to have caught it with. Both hooks
derive it identically now, and a test asserts they agree, because one derivation in two
places is exactly the shape that drifts silently.

What stays out of the response matters as much: `url` is agent-controlled and unbounded,
and `method`/`port`/`proto` are noise in a forty-row glance. All four remain in the
table and in `make logs-cp`. This endpoint is a legible summary, not the record — and
the record is what the invariant is about.

**Timestamps are formatted here, not deferred to the viewer's locale** — a correctness
decision rather than a preference, and the one place in this UI where following the
platform default was actively wrong. `toLocaleString()` with no locale argument takes
its format from the *browser's* language preference, so the same audit row read
`8/7/2026` for one operator and `07/08/2026` for another. Those are different dates, in
a table whose whole purpose is to say when something happened. Deferring to the viewer
is right for a consumer app and wrong for a record: an audit trail needs one rendering
that every reader parses identically. Fixed ISO-8601 ordering, local time, 24-hour, with
the UTC instant on the row's `title` because the visible stamp states no offset —
`NOTES.md` has what six locales actually produce.

Two consequences beyond the reading. The format became **assertable**: while it was
locale-driven the tests could only check its shape, and the suite now pins the exact
string with `TZ` deliberately set to a non-zero offset, since a UTC runner cannot
distinguish local from UTC. And writing that test immediately found a real defect — a
null timestamp rendered as `1970-01-01`, because `Number(null)` is `0` rather than
`NaN` and cleared every plausible numeric guard.

**The list groups; the record does not.** `/api/audit` folds rows whose *displayed*
fields are identical into one, with a count and the span's start. It exists because a
client retrying a permanently-refused host on a timer — a background exporter or
updater denied by a standing rule, once a minute — writes 1440 identical rows a day,
and a forty-row list of them covers under an hour. Everything else, including the
fronting refusal the ingest above was built to surface, falls off the bottom before
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

**An empty state alone would not have fixed the empty state.** The filed defect was that
"nothing has happened yet" and "the poll failed" rendered identically. The sharp part is
that the page's own connection indicator cannot settle it either: `conn` reports the
**SSE stream**, while this table is filled by a **separate poll**, so the header can read
`live` while the decisions view sits indefinitely stale. Adding "none yet" under an empty
table would have made that worse, not better — a positive all-clear on evidence the page
does not have.

So the two facts are tracked separately (has a load *ever* succeeded; did the *last* one
fail) and a failed refresh keeps the rows it already has while saying they may be stale.
Discarding them would throw away the only data the operator has, on the strength of one
failed request. Three states, three sentences, and the third — before the first response
lands — deliberately says nothing at all, for the same reason the saturation banner
renders nothing at zero.

**Both polled lists, not just the decisions one.** Fixing this for the decisions table
and leaving the policy table's `catch` swallowing was the shape of the original bug
repeated one tab over — and worse there, because the consequence of staleness differs.
A stale decisions table is old history. A stale **policy** table misstates what is
currently allowed, which is what an operator reads before deciding a hold. Three things
stopped silently: the rules kept rendering, the count froze, and `policySig` stopped
advancing, so the "policy changed" badge quietly stopped firing.

The three-state logic is therefore shared (`pollStatus`) and only the sentences differ
per view — the wording has to stay separate, and a test asserts the two views cannot
collapse onto one text. Leaving `policySig` untouched on failure is correct, since a
change nobody observed must not be claimed; what was missing was telling the operator
the view had stopped moving.

**The frontend's own tests.** `tests/test_control_plane_ui_js.py` runs the pure helpers
under `node` (skipped when node is absent, the way `make lint` skips a missing linter)
and asserts in Python, so failures read like the rest of `tests/`. It covers the lamp
precedence (blind outranks busy), the backoff bounds and monotonicity, the keyed-diff
properties, the sweep gating, the countdown arithmetic (including the clock-skew clamps
and the `expiring now`-not-`expired` wording), the expiry-vs-resolved-elsewhere
distinction, the persist preview's wildcard flag, the saturation banner's levels,
recency boundary and count-based dismissal, the duplicate-count badge, and the
decisions table's row shaping, repeat-count annotation and empty-versus-stale states —
the last of these for the policy table too, including that the two views' wordings stay
distinct.
Everything that touches the DOM
lives inside `start()`, which runs only in a browser — so requiring the module under
node must be side-effect free, and the test asserts that too: if DOM work migrates to
the top level, `require` throws and the file cannot quietly become untestable again.

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

The **converse** is now asserted too — every relayed route must be called by the page —
and that direction is about a different failure. A route with no caller does not break
anything, which is exactly the problem: nothing would reveal it if it were wrong. Two
had accumulated. `/status` was harmless (a plain-text count summary, mostly duplicating
what the page already shows). `GET /approvals` was not: the non-streaming form of the
pending list, superseded by the SSE stream, still relaying the pending hosts, clients and
URLs to any caller that got past the Host and `Sec-Fetch` guards. Both still serve on
control-net; only the browser's path to them is gone. The test carries no exception list
on purpose — a route that must stay uncalled should arrive with its reason attached, as
an edit someone has to justify.

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
host-local approval forgery — see the provenance note below.

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

**Three tabbed views, with badges carrying the hidden state.** Approvals /
Decisions / Policy, selected by `location.hash` so a reload, a bookmark and the back
button agree, with `role="tablist"` and arrow-key navigation. Tabs were a deliberate
choice over one long page, and they come with a specific hazard that the badges exist
to answer: two views are hidden at any moment, and one of them (standing policy) has
**silent drift** as its failure mode — it accumulated unnoticed for weeks before it
was surfaced at all, so putting it one click away could have re-hidden it. Therefore
the Approvals tab carries a live pending count (amber when non-zero) and the Policy
tab carries a rule count plus an **unseen marker** that appears when the rule set
changes while another view is showing and clears only when the policy view is actually
opened. Both feeds keep polling regardless of the active tab — otherwise a badge could
not report a hidden view's state, which is the entire reason it is there. The change
signature is over pattern+action rather than the row count, because a rule whose
*action flipped* is the change most worth noticing and it leaves the count unchanged.
A real view split for audit browsing (filter/search/history) is step 2c; this is the
navigation it will extend.

**Traffic-light favicon + title count.** An inline SVG data URI — never a file or a
CDN reference, since a governance UI issuing an external request per page load would
be both ironic and a needless third-party signal, and this way there is no extra route
for the relay to allow. Three lamps map onto the three states that matter: **red** the
SSE stream is down (we are not seeing pending approvals), **amber** something is
waiting on a human, **green** connected and clear. All three lamps keep their **own
colour at every state** and only the brightness moves (active at full opacity, the
others at ~0.26): with the inactive lamps greyed out the icon is a dark rectangle
carrying one small coloured dot and stops reading as a traffic light at 16px, which is
the size that actually matters. The honest cost is that dim-to-bright is a weaker
peripheral signal than grey-to-colour, so the title prefix carries most of the
eye-catching load. `href` is only reassigned when the state genuinely changes —
`updateIndicators` runs on every poll, and browsers treat each assignment as a fresh
favicon load, so rewriting it ~15×/minute would be wasted work at best and a
flickering tab icon at worst. Red for "blind" rather than for
"denied" is the deliberate part: an unseen hold default-denies after
`CONTROL_HOLD_TIMEOUT` (~120s), so not-receiving-updates deserves more alarm than
being busy, and a stale green would be a lie. The pre-JS fallback in `<head>` is amber
for the same reason — before the stream connects, "unknown" is the honest state, not
"all clear". The tab title gains a `(n)` prefix so a **background** tab shows the
count, which is the case that actually matters: `mcp-proxy.anthropic.com` expired
unnoticed five times because nobody was watching the page.

**Standing policy is visible in the UI (`GET /api/rules`).** The UI showed
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

**Taking a rule back (`POST /api/rules/{id}/revoke`).** A governance plane that can
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
- **Keyed on `id`, not pattern.** Patterns carry the live normalization gap noted
  under Open decisions (`_match` lowercases but does not strip a trailing FQDN dot),
  so a pattern-keyed delete inherits every such mismatch and can miss the row the
  operator is looking at. The relay bounds that segment to digits, and the bound is
  load-bearing rather than tidy: it lands in a URL path, so a looser class admits
  dot-segments that httpx resolves upstream into a different path than the allowlist
  approved.
- **Deletion, not a tombstone, with the audit row as the history.** Dead rows in the
  rules table would have to be filtered by every reader of it — including `_decide`,
  the one place a mistake is unrecoverable. Provenance is recorded exactly as
  `resolve` records it: editing standing policy is more consequential than any single
  egress decision, and nothing recorded that it had happened at all.

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
large GGUF — `NOTES.md` has the measured load times), and inference is that tier's only
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

- **Intel (WSL2)** — `--device /dev/dxg` plus `/usr/lib/wsl:ro`, because there is no
  `/dev/dri` render node under WSL and the in-image Level Zero runtime reaches the
  paravirtual D3D12 device via `libdxcore.so` from that mount. SYCL is the path, not
  Vulkan (`NOTES.md` explains why Vulkan cannot work there).
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

**AMD is Vulkan here, not ROCm**, and **AMD under WSL2 is not a viable target at all**
(CPU inference only there). Both conclusions rest on an ecosystem survey rather than on
anything in this repo — see `NOTES.md` → "Accelerator ecosystem survey". ROCm stays a
documented manual image swap for anyone on MI-series hardware.

### Tuning decisions (the evidence is in `NOTES.md`)

The compose entrypoint carries a handful of `llama-server` flags that all exist for the
same reason: on this hardware the *silent* failure modes are the dangerous ones, so each
flag converts one into either a loud failure or a bounded cost. Measurements and the full
"learned the hard way" list are in `NOTES.md` → "Local inference".

- **`--reasoning off`** — thinking defaults to on, and a trivial prompt once produced
  6,099 reasoning tokens over 7.4 minutes. Set **server-side**, not per-request, because
  a client that can disable it merely fixes itself while one that cannot (opencode) has
  no recourse. Overridable with `DOCKADE_LLM_REASONING=on`; if switched on, bound it with
  `--reasoning-budget N` rather than leaving it at `-1`.
- **`--parallel 1`** — the prompt cache is per-slot and slots are assigned LRU, so a
  multi-turn conversation could land on a slot that never saw it and re-prefill the whole
  history. Concurrency was never real anyway: two in-flight requests contend for the same
  GPU.
- **`--no-context-shift`** — the default silently discards the oldest tokens, which for
  an agent means evicting its system prompt and tool definitions mid-conversation.
  Presents as the model becoming inexplicably confused rather than as an error.
- **`-c 32768`, and the client's `limit.context` must be materially SMALLER** — a ratio
  guarded by `make consistency` (`CTX_HEADROOM`). 8k is not merely tight but unusable —
  opencode's base prompt exceeds it before the first user turn — and 64k does not fit the
  shared memory pool. Avoid `-c 0`, which would size the allocation from the model's
  native window.

  The headroom is the part that is not obvious, and the repo learned it by getting it
  wrong: the guard originally required the two numbers to be **equal**, on the reasoning
  that a client told the true window would respect it. A client told the true window
  overshoots it anyway (measurements in `NOTES.md`), because its token accounting is its
  own and tool output arrives after the turn is budgeted. So the invariant that survives
  contact is *the server's window exceeds what the client believes*, by enough to absorb
  the client's undercount — and it is free, because `-c` sets the KV allocation and does
  not move. **A cross-component agreement check is only as good as its direction**; two
  numbers matching is not the same as two components agreeing.
- **One model at a time**, and `temperature: 0` for extraction/classification.

Two properties worth carrying in the reader's head, because they shape task design more
than model choice does: decode is memory-bandwidth-bound while prefill is compute-bound
(a ~10x asymmetry, so this machine is good at prompt-heavy short-output work and bad at
long-form generation), and **a healthy container is not evidence of acceleration** —
llama.cpp falls back to CPU silently and the health check still returns 200.

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
provider SDK while egress still exists. Confirmed by running a full agentic turn in
a live sandbox — model round-trip, write tool, bash tool, nothing reachable but the
`llm` service. Worth stating what that test is *for*, because a weaker one looked
sufficient: `opencode --version` passing proves only that the bin shim resolves, and
anything fetched lazily fails at first **tool use**, not at startup. A no-egress tier
has to be verified by doing work in it.
`NPM_CONFIG_PREFIX` points at the user's `~/.local` so `-g` installs work
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
behaviour. Fixed at the server (`--reasoning off`, see *Tuning decisions*),
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

## Testing and CI

**The unit suite is dependency-free on purpose** (`tests/`, run by `make test`).
`fastapi` / `pydantic` / `mitmproxy` are stubbed in `tests/_loader.py` and SQLite runs
on a throwaway temp file, so `python -m unittest` needs no pip installs and no running
services — which is what lets the same gate run on a dev machine, in CI, and inside the
sandbox image that bakes the linters. `make test` reports the current count; it is
deliberately not restated here, because a number in prose only ever rots.

What it covers is the **security-load-bearing decision logic**: the proxy's `_forbidden`
relay guard, `_match`, the permanent-lifeline short-circuit, the mitmproxy hooks via
`SimpleNamespace` fakes (CONNECT ordering, the SNI-vs-authority anti-fronting guard, the
Host/`:authority` gate that catches a fronted header under an authorized CONNECT); and
on the control plane `_decide`, the `authorize` orchestration including
hold-timeout→deny, the resolve handshake, the persist-pattern candidate set and its
server-side validation. Per-function detail belongs in the test names, which read as
sentences for exactly that reason — `tests/` is the inventory, not this file.

Two things are worth stating because the code shape depends on them. The **hold-cap
reservation was extracted** out of the `authorize` handler into `_reserve_hold` /
`_release_hold` (behaviour-preserving) purely so the cap logic could be asserted without
the FastAPI machinery. And these are properties `boundary-check.sh` structurally cannot
reach: it observes reachability from the sandbox's vantage point, so a hold-cap rejection
— which returns the agent the same opaque `403` as any other deny — is invisible there.
Deliberately untested surface is low-weight I/O: `_audit` sinks,
`_post_authorize` / `_setup_audit_file`, and the SSE `approvals_stream`.

**The CI gate runs the same `make` targets rather than reimplementing them**
(`.github/workflows/check.yml`), so there is one definition of "does this repo pass" and
a CI failure reproduces locally with `make check-strict`. Two parallel jobs — lint +
consistency + tests (about a minute) and the five-image build verification — so a
shellcheck typo is not queued behind an image build.

**Strict mode is the part that makes the gate mean anything.** Every stage degrades to a
SKIP when its tool is absent, which is right on a dev machine (running the checks you
*can* run beats running none) and a trap in CI: a runner without hadolint prints `SKIP
hadolint` and passes green, verifying less than the badge claims with nothing anywhere
saying so — the same "silently checks nothing" failure the `LAUNCHERS` glob guard fails
closed against. `DOCKADE_REQUIRE_TOOLS=1` turns every such skip into a failure. It lives
in the **Makefile, not the workflow YAML**, because the Makefile already owns
what-must-be-true and a requirement encoded only in CI is invisible to whoever runs the
checks by hand. Verified in all four directions (docker, hadolint, node, all-present).

**The tooling floats on purpose, so the workflow is built around that.** `ruff.toml`
pins the rule *selection* and lets the binary drift; base images are pinned by tag, not
digest. The accepted cost is that a green commit can go red with no code change, so a
weekly schedule surfaces it on its own rather than ambushing the next pull request, and
every tool's version is printed so a verdict traces to what produced it. Three
consequences that are load-bearing rather than incidental:

- **hadolint's version is *derived* from `claude-sandbox/Dockerfile`'s
  `ARG HADOLINT_VERSION`**, not restated, and the step fails loudly rather than falling
  back to `latest`. It is the one linter this repo pins, so CI and the image cannot
  drift apart and a deliberate bump moves both.
- **The linters install into a private `PIPX_HOME` under `RUNNER_TEMP`.** The runner
  image ships its own pipx tools in a shared, root-owned `/opt/pipx`, where a plain
  install silently no-ops (pinning the version to the image) and `--force` fails
  outright. Ours cannot collide with either.
- **A divergence is harmful when it produces surprise *after* a push** — not "CI should
  be stricter". hadolint was harmful because CI was the stricter side, so the failure
  could only appear post-push. shellcheck is the reverse (the local gate is newer, so it
  catches first) and needs no pin. The interpreter gap is a positive: the services run
  on `python:3.12-slim`, so CI tests closer to production than a dev machine on 3.13.

**The build job earns its minutes.** Its first run found a bug unobservable locally:
`run-opencode-sandbox.sh` was `100644` in the git index while `755` on disk, so it
worked on the machine that wrote it and *any fresh clone* got exit 126. `core.fileMode=false`
— which git sets automatically on a filesystem whose exec bit it cannot trust, i.e. the
bind mount this repo is worked on through — is why nothing flagged it. `make consistency`
now asserts every launcher is `100755` **in the index**; the full reasoning is in that
guard's comment in the `Makefile`, next to the code it constrains.

**No Docker layer cache, and that is settled rather than deferred.** Caching on hosted
runners needs `--cache-to/--cache-from type=gha` on the build, which `docker compose
build` does not accept — so it would mean diverging CI from `make verify-build`, the
property that makes CI reproducible locally. The measured cold build is **~2m 22s** (19s
compose, 68s `claude-sandbox`, 54s `opencode-sandbox`), far below the 5–15 minutes
estimated, so the divergence buys nothing. Image sizes from the same run: both sandbox
tiers ~1.2GB, the three services 142–255MB. Worth knowing that **tier 2 is not the
smaller image** despite being the thinner *tier* — "thin client" describes where
inference runs and what capability it holds, not the toolchain both tiers inherit from
`sandbox-common`.

*Each of the incidents above is recorded blow-by-blow in the commit that fixed it, which
is the copy that is dated and cannot drift. What is kept here is the resulting invariant.*

## Status

| Step | What | State |
|------|------|-------|
| — | sandbox image + launcher, both tiers | **done** |
| 0 | egress proxy, compose infra | **done** |
| 1 | proxy is the sole egress (`sandbox-net` internal) | **done** |
| 2a | control plane: policy store + audit | **done** |
| 2b-1 | hold-for-approval, blocking with default-deny | **done** |
| 2b-2 | `control-plane-ui` split out as its own container | **done** |
| 2b-3 | control-plane API surface split across two internal nets | **done** |
| — | unit suite + CI gate | **done** |
| 2c | audit browsing (filter/search/history), per-proxy config | next |
| 3 | skills + quality-gate hooks in the image | planned |
| 4 | pull-through package cache | planned |
| — | governed git push path | planned |

The rationale for each shipped item lives under **Governance surfaces** above, not here
— a status line goes stale, the reasoning does not. This section is deliberately the
only place that tracks *sequence*; nothing else in this document should need updating
when a step lands.

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

## Future improvements
- Dedicated git proxy that speaks the git protocol (block force-push, restrict
  branches, per-repo policy) instead of HTTPS-through-egress.
- Separate test/build runner containers for isolation + parallelism.
- Selective MITM on credentialed hosts → true brokering so even self-use keys
  leave the sandbox.
- Docs mirror / offline docs tool.
- Progressive auto-approval driven by accumulated policy + audit history.
- **Rule MUTATION on the control plane.** Revocation is **now built** — see "Taking a
  rule back" under the control-plane section. What remains is EDITING: changing a
  rule's pattern or flipping its action is still revoke-then-persist rather than one
  operation, which is two audit rows for one intent and leaves a window in which the
  host is neither allowed nor blocked. Acceptable, because that window fails to
  `hold` rather than to allow, but it is not the same thing as an edit. The two sharp edges it used to carry
  have moved: the agent-controlled-pattern one is closed (`_persist_candidates` derives
  a bounded set and `resolve` validates against it — see "A `+ persist` says what it
  will write"), and the `INSERT OR IGNORE` wart, where `deny_persist` silently writes
  nothing for an existing pattern while still reporting `persisted: true`, is now
  reachable only when the *same* pattern was chosen twice and is covered honestly by
  the card's wording. A governance plane that can grant and revoke but not *edit* a
  rule in one step is still incomplete, and atomic editing is the remaining half.
- **Human-presence on approval (WebAuthn user-presence, or an out-of-band confirm).**
  The *only* thing that closes host-local forgery of an approval — see the
  browser-facing-guards note under "Approval UI". Worth building for that
  specific threat (the agent reaching host execution via the workspace mount), not
  because "the UI has no auth"; naive auth does not help, since any credential at
  rest on the host is readable by the same process.
- **Approval-UI follow-ups (reviewed and specified, not built).** From the same review
  that produced the reconnect / CSP / keyed-rendering work above, in value order. The
  top two — the hold countdown and the persist preview/confirm with an operator-chosen
  pattern — are **now built**; see "The card shows its deadline" and "A `+ persist` says
  what it will write" under the frontend section. What remains:
  - *(A DOM-level test for `start()` was considered here and **declined** — the frontend
    is treated as a convenience layer over a backend that validates every input, with its
    mistakes made detectable rather than prevented. The reasoning, and the condition that
    would reopen it, are under "`start()` is deliberately unverified" above.)*
  - **Rule mutation** — nothing here *replaces* a rule (revoking one is built — see
    "Taking a rule back"), which is why a persist that contradicts an existing one is
    refused rather than applied (see *A persist cannot overwrite* above). The refusal
    is the honest behaviour given the constraint; lifting it — editing a rule's pattern
    or action in one operation rather than revoke-then-persist — is the open item.
  - **Opt-in desktop notification.** `http://localhost` is a secure context, so the
    Notification API is available; with a ~120s fuse and a page nobody watches, this is
    the honest fix for the problem the `(n)` title prefix only mitigates.
  - *(The smaller items filed here — hop-by-hop header stripping on the request side,
    an announcement for arriving approvals, visibility-gated pollers, and "showing N
    of M recorded decisions" — are **now built**. Two of them changed shape on
    contact: a live region belongs on a separate element rather than on the card list,
    whose countdowns rewrite once a second; and the coverage line compares decisions
    with decisions, since the view is grouped and a rows-versus-decisions ratio reads
    as truncation even when nothing was truncated.)*
- Normalize hosts consistently across the control plane. The proxy's relay guard
  strips a trailing FQDN dot but `_decide` / `_match` only lowercase, so `evil.com.`
  misses a persisted **block** rule and lands in a hold instead — fail-safe, but it
  lets an explicit operator denial be re-prompted indefinitely.
- **`boundary-check.sh` manufactures the most alarming rows the audit log can
  hold** — control-plane relay attempts, SNI fronting, metadata-IP SSRF — and they
  are indistinguishable from an agent genuinely attempting them. That is arguably
  correct: the sandbox really did make those requests, and a probe that produced no
  audit row would not be testing the real path. It also sits deliberately opposite
  the decision recorded for the egress proxy's healthcheck in `docker-compose.yml`,
  which is *not* a real CONNECT precisely so a periodic synthetic deny never
  pollutes the record. The distinction that justifies both: that one fires on a
  timer forever, this one only when a human asks. Left as-is, filed because
  "remember what you ran" is not a property of a log.

## Layout

See `README.md` → Layout for the current tree. Companion documents: `NOTES.md` holds
the evidence behind decisions recorded here (see CLAUDE.md → "Where writing goes" for
which file takes what). Still-planned additions to it:

```
dockade/
  tools/              # ungoverned data-plane services (cache, scratch DB, ...)
  claude-sandbox/
    skills/           # sanctioned capability + workflow interface
    hooks/            # quality-gate hooks
```
