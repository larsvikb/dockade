# Design

Architecture and rationale for dockade. See `CLAUDE.md` for working
conventions and invariants.

## Mental model (read this first)

The whole system is one sentence: **an AI agent can reach nothing directly, so every
consequential action has to travel a governed path that can be policed, approved, and
audited.**

- An **agent** runs in a disposable **sandbox** on `sandbox-net`, an *internal* Docker
  network with no route off-box. It holds near-zero capability by construction (non-root,
  dropped caps, no docker socket), so "yolo mode" is safe not because the agent is trusted
  but because it can't reach anything.
- The one way off-box is the **egress proxy** — a CONNECT-level, default-deny, domain
  allowlist that audits every connection. It's the sole bridge between the sandbox and the
  outside.
- The proxy asks the **control plane** (policy + audit store — the "crown jewels") to
  authorize each connection. An unknown host is **held** for a human, who approves or
  rejects it in a small loopback **UI**; no decision → default-deny after a timeout.
- The agent **cannot reach the control plane**: it's on separate internal networks the
  sandbox isn't attached to. Two components touch both sides — the egress proxy and the
  MCP gateway — and neither relays: the proxy hard-refuses a control subnet as a
  destination, and the gateway serves the agent on one address and dials the control
  plane only to ask about a tool call, so there is no relay in it to turn.

Two agent **tiers**: **tier 1** is Claude with governed egress through the proxy; **tier
2** is a local-LLM agent (opencode) with *no egress and no credentials at all*. Three
egress **modes** in the firewall: **governed** (via the proxy), **local** (tier 2, only
the inference service), **standalone** (proxy-less fallback with a direct allowlist).

The rest of this document is the *why* behind each of those pieces. Everything else in
the repo follows from "containment is by **capability**, not configuration."

## Contents

- [Purpose](#purpose) · [Core idea](#core-idea) · [Architecture](#architecture) ·
  [Networks](#networks)
- [The sandbox image](#the-sandbox-image-the-agents-paved-road) ·
  [Web access](#web-access-verified-empirically-in-sandbox) ·
  [Server-side execution blind spots](#server-side-execution-accepted-governance-blind-spots)
- [Capability inventory](#capability-inventory-v1) ·
  [Governance surfaces](#governance-surfaces)
  (egress proxy · control plane · approval UI · [MCP gateway](#mcp-gateway--governed-tool-capability))
- [Startup ordering](#startup-ordering--running-is-not-ready) ·
  [Resource limits](#resource-limits--blast-radius-not-boundary) ·
  [Local inference](#local-inference--an-ungoverned-llm-tool)
- [Testing and CI](#testing-and-ci) · [Status](#status) ·
  [Open decisions](#open-decisions) · [Future improvements](#future-improvements) ·
  [Layout](#layout)

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

**MCP reaches the sandbox only through the governed MCP gateway** (see "MCP
gateway" under Governance surfaces). Skills stay the interface for capabilities
we design; the gateway is for third-party tool surfaces we did not. Nothing is lost
by the agent adding an MCP server of its own: one it spawns in its own container
inherits the sandbox's capability, which is no egress and no credentials. The
gateway is therefore not merely the *sanctioned* MCP path — it is the only one with
anything behind it.

## Architecture

```
 host browser ──loopback──► control-plane-ui ──► CONTROL PLANE ─┐
                            (stateless frontend)                │ control-net
                                            policy / approval   │ (internal)
                                            / audit / config    │
        ┌───────────────────────────────────────────────────────┘
        │
   GOVERNED proxies/tools   (multi-homed: sandbox-net + a control bridge of
        │   its own; egress ones also on egress-net) ────► internet
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
  "why the control path is more than one net" under Governance surfaces. Sandbox
  not attached.
- `tool-authorize-net` (`internal: true`) — the MCP gateway's authorize path, one
  conversation only: gateway → the control plane's tool listener (decide a call,
  read the roster, push the inventory, claim an approved ask). A second bridge
  rather than a share of `authorize-net`, so a bypassed egress proxy gains no route
  to the gateway's claim endpoint — the one place an approved side effect is
  released. Sandbox not attached.
- `mcp-net` (`internal: true`) — the MCP data path, two conversations only: the
  MCP gateway dialing the server containers, and those containers reaching the
  egress proxy for their own upstream calls. The servers live here and never on
  `sandbox-net`, which is their whole boundary — each holds a credential the agent
  must not have. Sandbox not attached. Its tenants are in `mcp-servers.yml`.
- `egress-net` — only outbound-capable governed proxies + internet.
- `control-ui-net` — non-internal bridge carrying ONLY the control-plane-ui
  frontend's host-loopback UI publish; masquerade disabled so it is
  host-reachable but not an egress path. Needed because Docker cannot publish a
  host port from a container that is on an internal network alone. Sandbox not
  attached.

**Status:** every network above is implemented. `sandbox-net` (internal) and
`egress-net` carry the agent and the sole egress; the internal control nets carry
the control path, split by surface — one shared management path and one
single-conversation bridge per enforcer. The egress proxy is **quadruple-homed**
(sandbox-net + egress-net + authorize-net + mcp-net) — note `authorize-net`, not
`control-net`, and note that the `mcp-net` leg exists only to be DIALED. The
control-plane **backend** is **triple-homed**
(control-net + authorize-net + tool-authorize-net) and fully
internal (no `sandbox-net`, no `egress-net`, no published port), serving a
different surface on each; the **control-plane-ui** frontend is on `control-net`
(to reach the backend) plus `control-ui-net` (host-loopback UI). The sandbox is on
`sandbox-net` only — never any control network (asserted by the launcher guard in
`make consistency` and by `boundary-check.sh`, which probes it from inside a running
sandbox; a sandbox is not a compose service, so `tests/test_topology.py` cannot see
this one).

**On any one network, pin every member's address or pin none.** A mixed network is a
start-order race for a single address rather than a style inconsistency, and it stays
invisible until a host reboot puts the members up in an order `depends_on` has no say
over (`NOTES.md` has the allocator behaviour and the misleading error). Asserted by
`AddressAllocationTests` in `tests/test_topology.py`, which is also why the sandboxes
may stay dynamic: they are launched after the substrate already holds its pins.

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

### Durable host config lives outside the repo (`~/.config/dockade`)

Per-machine settings that outlive a launch — MCP client credentials
(`MCP_SECRETS`), plugin marketplace checkouts, the enabled-plugin allowlist — live
under `$XDG_CONFIG_HOME/dockade` (default `~/.config/dockade`), **never in this
tree**. The reason is specific to this repo rather than convention: a sandbox
launched with dockade as its workspace bind-mounts the tree **read-write**, so
anything configured from inside it is agent-writable. For a credential that is
obvious; it applies just as much to a knob that decides *which code loads into the
agent*, which is why the marketplace configuration sits here and not in a repo
file or a committed `.env`.

The rule, in precedence order: **env var > host config file > baked default.**
Per-launch knobs (`SANDBOX_MEMORY`, `SANDBOX_NAME`, …) stay env vars, because they
are per-invocation by nature. `~/.config/dockade` holds what should persist
without being retyped. Compose service tuning keeps using `.env` (compose reads it
natively, and those knobs configure services, not the agent's capability). A
fourth channel — a config file *inside* the repo — is the one shape to refuse, for
the paragraph above.

The path is necessarily spelled twice, in `sc_config_home` (`sandbox-lib.sh`) and
`DOCKADE_CONFIG_HOME` (`Makefile`), because make cannot source bash and a launcher
cannot read a Makefile. `make consistency` invokes both and compares, including
under a set `XDG_CONFIG_HOME` — the same two-spellings-plus-a-drift-guard shape as
the firewall/policy allowlist check.

### Plugin marketplaces — host-curated, read-only, re-derived each boot

`~/.config/dockade/marketplaces` is auto-mounted at `/marketplaces` **read-only**
when it exists, and every marketplace in it is registered into `settings.json` at
boot (`sc_marketplaces` in `sandbox-lib.sh`; `claude-sandbox/tier-setup.sh`).

This works at all because a Claude Code marketplace can be a local **directory**,
and a directory source is used in place — no clone, no copy, no egress, no
credential (measured; `NOTES.md`). That makes it the right shape for a container
with no governed git path: the human clones on the host, the sandbox reads. It
needs no new capability, so it is enablement, not a widening of the boundary.

Three decisions worth keeping:

- **Read-only, and outside `/workspace`.** A writable plugin tree is a
  cross-session channel rather than a convenience: the agent edits a skill or a
  hook now, and it lands in its own context — or executes — on the next boot,
  outside the diff review that `/workspace` commits get. `:ro` costs nothing (a
  directory marketplace is never written to in normal use) and `make consistency`
  asserts it, because the mitigation is one character wide.
- **Generated, not `claude plugin marketplace add`.** The CLI's only effect is
  writing the same two settings keys, so shelling out to it would make it a second
  writer to the file the boot owns authoritatively — and it would need a `gosu` hop
  to avoid leaving root-owned files in `/config`. Deriving the whole set from what
  is mounted, every boot, also means removing a checkout on the host removes it
  here, with no stale state to clean up.
- **Not a general host-supplied settings overlay**, tempting though one is (a
  single channel, a schema we do not own, room for every future user-scope knob).
  It would reopen precisely what "user settings are image-owned" closes: the
  effective configuration would become a two-file question, and a host file could
  quietly undo a paved-road default. Two narrow keys instead —
  `extraKnownMarketplaces` and `enabledPlugins`.

Enabling is a **separate** decision from availability: a registered marketplace
only makes plugins installable, and a plugin loads only if it is in
`enabledPlugins`. It has to be declared host-side (`~/.config/dockade/plugins`)
for the same reason the yolo disclaimer is baked into the template — settings are
re-materialized every boot, so an in-session `/plugin install` does not survive a
restart. The list travels as an env var rather than a second mount, keeping the
sandbox free of host paths it does not need.

**The trust framing:** a mounted marketplace is code the agent runs — skills,
agents, hooks, MCP declarations. Mounting one is a trust decision on par with what
is in `/workspace`: inside the boundary, not a new hole in it. It does not widen
egress (the firewall is untouched, and a plugin-declared stdio MCP server still
has no route but the proxy), but it does sidestep the curated catalogue in
`mcp-servers.yml`, and plugin hooks execute commands. Host-curated, read-only and
boot-derived is the whole mitigation; the trust decision itself stays the human's.

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
winning source is Anthropic's **remote** server-managed settings, so a local
`/etc/claude-code/managed-settings.json` is **not loaded at all** — measured, with a
deny that never applied, in `NOTES.md` → "Claude Code reads a managed `CLAUDE.md`
even where it ignores managed settings". What follows:

- **The image ships no `managed-settings.json`.** A file that claims enforcement it
  does not provide is worse than none, and it would only take effect on a
  *personal*, non-org login — out of scope for this org-governed sandbox.
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
- **No local `managed-settings.json`** — not loaded under org auth (see "Managed
  settings are NOT an enforcement lever here"); hardening opt-outs are real
  Dockerfile `ENV`. Does **not** force yolo — starting in yolo is a conscious
  opt-in via the `claude-yolo` alias.
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
- Baseline stack: **Node current LTS (24.x)** + pipx, plus baked linters
  (shellcheck / hadolint / ruff / yamllint, all self-contained so they run under
  default-deny egress). Node tracks the latest LTS line (even majors); bump the NodeSource
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
allowlist), and re-confirmed under governed mode — that probe is in `NOTES.md` →
"Claude Code honours `HTTPS_PROXY`, and WebFetch inherits it":

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
  loaded** under org auth (see "Managed settings are NOT an enforcement lever
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
  (inherits the CLI's proxy env), so it is audited through the egress proxy — the
  allow/deny CONNECT rows a fetch leaves are in `NOTES.md` → "Claude Code honours
  `HTTPS_PROXY`, and WebFetch inherits it".
- **Optional `websearch` skill** — *only* if a future threat model decides to turn
  WebSearch off (see decision below). Web search would then become a governed call
  to a third-party search API (Brave / SerpAPI / Google CSE) with its own read-only
  key, routed through the egress proxy. Not planned while WebSearch stays enabled.

## Server-side execution: accepted governance blind spots

Some tools execute **server-side** (on Anthropic infra, within the API turn)
rather than from the container. Their effects never traverse the container's
network boundary, so **the firewall/proxy cannot see, audit, or block them —
governance of those actions is lost.** We accept this initially, but enumerate
it explicitly so it is a conscious tradeoff, revisited as the setup matures.

**Why the list is complete, not a sample:** a tool can only escape governance if
it causes effects *outside* the container. File/process tools (Bash, Read, Edit,
Write, Glob, Grep, Task, NotebookEdit) are inherently local — the firewall
already governs anything they spawn. The tools that reach external networks are
the two web tools, both tested in this sandbox — plus **hosted MCP connectors**,
whose calls originate on Anthropic infra rather than here, which is why the
transport carrying them is blocked rather than accepted.

Registry (re-test when Claude Code adds/changes tools):

| Tool | Executes | Governed? | If allowed, governance lost over… |
|------|----------|-----------|-----------------------------------|
| WebSearch | server-side | **No** | what the agent searches for; unaudited web *intake*; low-bandwidth exfil via query strings (read-only — no upload/POST) |
| WebFetch | client-side | Yes | — (network-governed) |
| Bash / file / Task / Notebook tools | client-side | Yes | — (local, firewall-governed) |
| MCP tools via the gateway | client-side | Yes | — (the gateway is the choke point; see "MCP gateway") |
| claude.ai MCP connectors (hosted) | server-side | **No** | everything a connector does — which is why this one is **closed at the transport** rather than accepted as WebSearch is |

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
  audit. Always-allow api.anthropic.com (see "Anthropic traffic goes through the
  proxy too"). Upstream for the package cache, git host, general web, and
  third-party APIs all flow here.
- **Web search backend** *(deferred — not in v1)* — only needed if WebSearch is
  ever disabled and replaced by the `websearch` skill. While built-in WebSearch
  stays enabled, no backend is required. Would be a third-party search API via the
  egress proxy with a read-only key.
- **Git path** — clone/fetch is low-risk and is all the governed git path is scoped
  to *(not built yet — see "Status")*; **writes are the MCP gateway's**, under its
  per-tool policy, so no second road to a repo exists (see "the MCP gateway owns repo
  writes"). Until then the transitional allowlist lets clone and fetch reach GitHub
  over HTTPS through the egress proxy. A push token is a write-capable credential
  and never enters the sandbox.
- **MCP gateway** *(see "MCP gateway")* — the sole MCP surface offered to
  the sandbox, exposing a curated tool set from configured MCP servers under
  per-tool allow/deny/ask policy. It holds those servers' credentials so the
  sandbox never does, which makes it the first concrete instance of the
  write-capable-credentials invariant rather than a second exception to it.

**Ungoverned (sandbox-net only, no independent egress):**
- **Pull-through package cache** (npm/PyPI/apt) *(not built yet — see
  "Status")*. Ungoverned to the agent (fast, free installs); its upstream fetch is
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
- Governed / not in sandbox: git push token, MCP server credentials (held by the
  gateway), any write-capable / high-impact key.

**No secret belongs in the repo's `.env`.** It is the natural place to reach for —
gitignored, read automatically, already holding host-specific overrides — and the
reason to refuse is not that it is plaintext. The workspace bind-mount is whatever
directory a sandbox was launched from, so this repo's root is inside it exactly when
an agent is working **on dockade** — which is how dockade gets developed. A secret
there is therefore readable by the agent some of the time, decided by a launch
directory nobody is thinking about at the time. Contingent exposure is worse than
constant exposure, because it tests clean. Gitignoring keeps a value out of the
history, not out of a sandbox.

So anything that grants lives **outside the tree**, under `MCP_SECRETS`
(`~/.config/dockade/secrets/`, 0700 with files at 0600) — one file per server, read by
the gateway rather than handed to compose, so no version-dependent `env_file`
behaviour decides whether a missing file is fatal. See "Where the material lives"
under the MCP gateway for the file's shape and why a directory bind beats a compose
`secrets:` entry. `.env` keeps the non-secret overrides it was meant for: DNS, model
selection, the render gid, a port.

**Workspace bind-mount:** the one deliberate direct host coupling. Scope it to a
single project directory, read-write. Because work lands on the host FS directly,
no separate artifact-export path is needed in v1.

## Governance surfaces

The services that actually implement governance, and the reasoning each one
rests on, each of them built. This is the "why it is this way" that a change has to keep true — it was
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
proxy on `sandbox-net`, points the agent's `HTTPS_PROXY` at it, and allowlists it
in the firewall (`EGRESS_PROXY_IP`). Chosen properties: it does **not** weaken the
boundary while we validate — the allowlist is default-deny from the first commit, so
arbitrary egress via the proxy is refused (not allow-all), and `boundary-check.sh`
stays meaningful; and the allowlist was re-read per connection, a cheap stand-in for
"dynamic" until the control plane existed (2a replaced the baked allowlist
entirely — the proxy is a control-plane client now, see "A control-plane client" in
`proxies/egress/DESIGN.md`). Governs by **name**, so it closes the
shared-CDN/fronting gap the IP firewall can't (for proxied traffic).

**Anthropic traffic goes through the proxy too.** The sandbox has no direct route
off-box, so reaching the API is a deliberate path, and it is this one: the CLI honours
`HTTPS_PROXY` for its own API calls, and the proxy always-allows `api.anthropic.com`
by a local check before the control plane is asked — the permanent lifeline, "A
control-plane client" in `proxies/egress/DESIGN.md`. That keeps the agent's own API
traffic in the same audit as everything else, and it is why there is no transparent
redirect of port 443 and no per-domain hole in the firewall for the API: a
network-layer allowlist survives only as the standalone fallback below. What the CLI
reads, what else it reaches, and the probe that confirmed both in this sandbox are in
`NOTES.md` → "Claude Code honours `HTTPS_PROXY`, and WebFetch inherits it". Three
consequences are this repo's:

- **Both cases of every proxy variable**, because the agent is not the only client in
  the image: `curl` reads `http_proxy` in lower case only, and deliberately
  (`NOTES.md` → "`curl` reads `http_proxy` in lower case only"), so uppercase alone
  sends plaintext HTTP around the proxy — unheld and unaudited — while every
  `https://` probe passes. Two guards, because neither covers the other: `make
  consistency` asserts the pairing in the launcher *source*, and `boundary-check.sh`
  probes `http://` from *inside a running container*, the only place an environment
  that drifted, or a sandbox started before the pairing, shows.
- **claude.ai MCP connectors are left to fail closed.** They are on by default and
  ride `mcp-proxy.anthropic.com`, which no rule allows — and not merely because they
  are unused here. A connector's tool call executes **server-side**, so it never
  crosses this container's network boundary and the MCP gateway can neither see,
  audit nor hold it; refusing the transport is what keeps the gateway the only MCP
  path with capability behind it (see "Server-side execution: accepted governance
  blind spots"). `ENABLE_CLAUDEAI_MCP_SERVERS=false` is the switch if their deny-log
  noise ever becomes a nuisance. The CLI's other hosts stay out of that log by the
  image's `DISABLE_AUTOUPDATER=1` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
  (`claude-sandbox/Dockerfile`), or are inside the GitHub rules already.
- **Proxy variables are real process environment** — `docker run -e` from the
  launcher, as the Dockerfile's `ENV` is for the hardening opt-outs — never a shell
  export, because a background or supervisor process does not reliably inherit one.

**Governed vs standalone egress (the firewall is mode-aware).** When the launcher
finds the proxy it sets `EGRESS_PROXY_IP`, and `init-firewall.sh` switches to a
minimal **governed** posture — the sandbox's only `OUTPUT` ACCEPTs become:
loopback (incl. embedded DNS `127.0.0.11`), DNS **to `127.0.0.11` only**, the
proxy `/32:8080`, the tool gateway's `/32:8100` when the launcher found one (see
"MCP gateway"), and `ESTABLISHED,RELATED`;
everything else is REJECTed. Dropped
in governed mode vs the old posture: the direct per-domain IP allowlist (so **no
ipset**), the **upstream DNS forward** (closing the residual DNS-exfil channel —
a crafted name can no longer reach a recursive resolver; sibling names like
`egress-proxy` still resolve locally). Note what closes that channel: the firewall
drops the upstream forward, but the resolver the firewall still permits declines to
recurse on its own — the **engine's** behaviour for a container whose only network
is `internal`, not this repo's. `boundary-check.sh` therefore asserts it rather than
inheriting it, in both tiers that sit on such a network. Also dropped: the
**gateway `/32`** (host-local
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
attached to both, among its four legs (default route via egress-net; a stable
sandbox-net IP the firewall allowlists; the two control legs are in "Networks").
The in-container firewall is now **defense-in-depth**
rather than the sole boundary: even if it failed, the sandbox has no route out.
The launcher refuses to start a sandbox on an internal `sandbox-net` when no
proxy is running. **Standalone** mode (no compose infra → non-internal net +
direct ipset allowlist) remains for proxy-less use. DNS needs nothing extra: the
sandbox resolves only sibling names via the embedded resolver (local, works on
internal nets); external resolution happens at the proxy on egress-net.

What the proxy refuses before any policy is asked — the control-plane relay guard, the
private and special-use ranges, the spelling normalization both rest on, and the honest
limit of the resolve branch — is the proxy's own code, and is designed in
`proxies/egress/DESIGN.md` → "The relay guard — refused before policy is asked".

*One destination, one spelling — the IDNA fold.* The same problem as "A destination,
not a string" in `proxies/egress/DESIGN.md` recurs one layer up, where the consumers
are not CIDR sets but people and stored rules. mitmproxy IDNA-**decodes** the
authority it parses off the wire while leaving the Host header and the TLS SNI in
ASCII (measured in `NOTES.md`), so the raw properties hand three consumers three
spellings of one host — and each fails differently. The approval card and any rule
persisted from it render what they are given, so a homoglyph reaches the operator
wearing the imitated host's face and the rule keeps the disguise; `tls_clienthello`
compares the remembered CONNECT authority against an SNI that is ASCII by RFC 6066, so
a *legitimate* IDN is approved by a human and then refused as domain-fronting, with an
audit line accusing it. No single file owns that reasoning, which is why it is here:
the fix is one fold to the A-label at the point each name enters `addon.py`
(`_a_label`), chosen over normalizing at display time because only the entry point is
upstream of all three consumers. Folding spelling must not fold destinations — a Host
header naming a genuinely different site still gates separately — and the fold falls
back to the input rather than raising, since the `idna` codec rejects underscored and
over-long labels that DNS and the rest of this proxy accept.

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
now spans the internal control nets and serves a different surface on each — one
more of them since the MCP gateway got a bridge of its own (see below).
That UI bridge is non-internal **by necessity** — Docker cannot publish a host
port from a container that is on an internal network alone — but has masquerade
disabled, so it carries the loopback UI publish without being an egress path.
The control plane is a small FastAPI app over a SQLite policy+audit store (its own
named volume — the crown-jewel state — seeded from `policies/egress-allowlist.txt` on
first boot); the management surface reaches the host as **loopback only**
(`127.0.0.1`, on the port `docker-compose.yml` publishes), published since 2b-2 by the
UI frontend rather than the backend.

*Design note — why the control path is more than one net, and the frontend split.*
There are two kinds of net here and the distinction is what keeps the count from
being arbitrary: **one shared management path**, plus **one single-conversation
bridge per enforcer** (`authorize-net` for the egress proxy, `tool-authorize-net`
for the MCP gateway). An enforcer never joins the shared path, and no two enforcers
share a bridge — that second rule is the one that costs a net each time, and it buys
the property that a bypass of one enforcer reaches a policy query and not another
enforcer's surface.

`control-net` stays
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
on each of its networks: the management API (approvals, `resolve`, the read-only
views) on `control-net`, `POST /authorize` alone on `authorize-net`, and — added
later, on the same principle — the gateway's bridge alone on `tool-authorize-net`.

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

**One process, several sockets**, and that is forced rather than chosen: a held
`/authorize` blocks on a `threading.Event` that `resolve` sets, so those two surfaces
must share memory precisely because they must not share a socket. Splitting them
into two containers would mean externalising the hold registry, which trades a
narrow, checkable property for a distributed-state problem.

**The MCP gateway's bridge, `tool-authorize-net`, repeats the shape and does not
share the net.** It is a third listener in the same process, serving four calls —
decide a tool call, read the roster, push the inventory, claim an approved ask — and
nothing that grants. `authorize-net` would have done the job for one line of YAML,
and the reason it is not reused is the design assumption above: the relay guard is
best-effort, so a bypassed proxy is planned for, and it must not land on the
gateway's claim endpoint, which is where an approved side effect is released. Two
enforcers sharing a bridge is the lateral edge the original split bought its way out
of.

The bind asymmetry between the two bridges is deliberate and worth reading once. The
authorize listener binds the **wildcard** — it is the safe surface, it answers a
policy question and nothing more, and the container's healthcheck reaches it over
loopback. The tool listener binds **one address**, like the management one, because
its claim endpoint has a side effect to release; `app.py` refuses to start on a
wildcard there for the same reason it does for management. The consequence of the
wildcard, stated rather than left to be found: the gateway *can* reach `/authorize`.
That is accepted on the same grounds that make it safe for the proxy, and the
direction that mattered — proxy to claim — is the one the bind closes.

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

The same pair now runs against the gateway's bridge, and the wildcard that supplies
the positive control there is the same one: `172.27.0.2:8091` is genuinely listening,
so a drop on it is what makes the drop on `:8092` mean an unroutable subnet. What is
being measured is sharper than for the management API — not "no self-approval path"
but "the proxy cannot spend an approved tool ask".

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

How the proxy uses its bridge — `POST /authorize` on every connection, with no cache;
the permanent lifeline it allows without asking, and scopes by client; failing closed
on its own errors as well as the control plane's — is the proxy's own code, in
`proxies/egress/DESIGN.md` → "A control-plane client".

A consequence worth stating because the topology invites the mistake: adding a
network to the egress proxy adds a **client population**, not just a route. That is what
per-client-class policy below answers.

### Policy is scoped to a client class

**A rule decides for one client population and no other.** `_decide` takes the host
*and* the class of client asking, and a rule carries the class it was written for, so
a host the operator approved for the agent is still unknown to an MCP server and is
held the first time one asks. Without this a single allowlist becomes the union of
every client's needs — least privilege eroding by construction the moment there is a
second consumer, and the second consumer here is the one process holding a
write-capable credential.

**The identity primitive is the ingress network**, not the source address. Topology
is provable; addresses are not, since sandboxes are ephemeral and Docker hands `.2`
to whichever container starts first — a rule keyed to an address would silently
transfer to the next tenant of it. Named ranges live in `policy.CLIENT_CLASSES`, and
`tests/test_topology.py` holds them equal to the compose subnets, non-overlapping,
and equal to the lifeline's range for the agent's class.

**The classification is the control plane's, not the proxy's**, which is the one
placement decision here worth arguing. The proxy observes the peer address and
already CIDR-matches one — but only for the lifeline, and only because the lifeline
is the allow it makes *without* asking. Every other decision belongs to the policy
authority, so the class is derived where the decision is taken. One mapping then
serves however many governed proxies call `/authorize`, rather than each carrying a
copy to drift. The second client population is not the MCP gateway, which never calls
`/authorize` and has no egress, but the server containers on `mcp-net` — class `mcp`.

How the control plane applies the class, what it refuses for a client it cannot place
and how the rules already written were scoped, is in `control-plane/DESIGN.md` →
"Client classes, where the control plane applies them".

**Ingesting the decisions the proxy makes alone.** The lifeline and the fail-closed
deny, plus the relay guard, the port gate and the SNI anti-fronting check, mean a
real share of egress decisions are made *in the proxy* and never travel the authorize
path. They were always audited — the invariant held — but only to the proxy's own
stream, so the UI's "Recent decisions" was a record of round-trips, and a
**domain-fronting refusal**, the most alarming line the proxy can emit, appeared
nowhere a human was looking. The control plane now mounts the proxy's audit volume
**read-only** and tails it, ingesting the lines the proxy marks `central: false`.

The reasoning is why this is a *pull* and not a push, which is not obvious and
spans all three components:

- **The file already was the durable queue.** `audit.jsonl` sits on a named volume,
  append-only and ordered, and survives a control-plane outage — it is the record of
  record while governance is down. Any broker or POST would have been a *second*
  durable store of the same events. It is size-rotated so a long-lived volume cannot
  fill (the proxy's `RotatingFileHandler`); the ingest follows a file across the
  rename by inode and drains rotated siblings oldest-first, so rotation does not
  strand the un-ingested tail — and warns loudly in the one case it could, a backup
  deleted before it was read (`_drain_egress_audit`).
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
resolves holds in a **live SSE UI** served at `/`, and the vocabulary is a ladder
of how far the click reaches: **allow-once / deny-once** (this request only),
**allow-lease** (that host, that client class, until `CONTROL_LEASE_SECONDS`
elapses — see "A lease is the third grant duration" in `control-plane/DESIGN.md`) or **allow-persist /
deny-persist** (also writes a rule so future connections skip the hold — the
progressive-trust path). Concurrency:
one uvicorn worker; a held request blocks its threadpool worker on a
`threading.Event` the resolve endpoint sets; SQLite (`approvals` table) is the
UI's source of truth; stale `pending` rows are expired on startup. **Holds are
bounded**: since each hold pins a threadpool worker and this control plane is
**shared across all sandboxes**, an unbounded queue would let one agent exhaust the
pool and stall every sandbox's governed egress. Over any cap, `/authorize` fails
**closed** (deny) immediately instead of registering another blocking hold — the
worker caps stay comfortably under the pool so fast allow/deny decisions always have
free workers, and the permanent lifeline is unaffected (it never reaches the control
plane). There are **four** caps, because there are two things to protect and two
scopes to protect them at; see "Four hold caps: two nouns, two scopes" in
`control-plane/DESIGN.md`.

### Approval UI — the one surface that can grant egress

The control plane's half of the approval flow is designed in `control-plane/DESIGN.md`,
beside the code that decides it: how a card grants (persist and lease), how holds behave
under load, the audit views, and the three verbs over standing policy. The page presents
those decisions and the backend enforces them. The frontend's own design, its browser
boundary and the page's mechanics and tests, is in `control-plane-ui/DESIGN.md`. What
stays here is why the frontend is a container of its own, when a lease stops counting,
and what no guard in a browser can do.

**Why the frontend is a separate container.** The approval UI and the API/SSE relay
now live in a distinct **`control-plane-ui`** container (FastAPI + httpx: serves
the static UI at `/`, reverse-proxies everything else — including the SSE stream
— to the backend over `control-net`). The **backend is now `control-net`-only
and fully `internal`**: no published port, no non-internal surface, nothing to
exfiltrate even if reached. The frontend carries the sole host-facing surface
(`control-ui-net`), holds no state, and is not on `sandbox-net`. Browsers hit the
published loopback port → frontend → backend; the egress proxy still calls the
backend's `/authorize` directly. See the step-2a design note for the rationale.

**A lease bounds authorization, not connection lifetime.** The egress proxy authorizes
**once per CONNECT tunnel** (`http_connect` in `proxies/egress/addon.py`), and the SNI
stage afterwards only *compares* against the authority it already recorded — no second
call to the control plane. So a tunnel opened while a lease is live keeps carrying
requests after the lease ends, for as long as the client holds it open, and revoking
early stops the *next* connection rather than this one.

This is not new — revoking a standing rule has always had the same property — but it is
worth stating for a feature whose entire value is the time bound, and it is reasoning
that lives in no single file: the deadline is in `policy.py`, the once-per-tunnel
decision is in the proxy, and neither one can see the other. Closing it would mean
re-authorizing mid-tunnel, which the proxy deliberately does not do (the SNI stage's
whole design is a local comparison, so a fronted request cannot buy a second decision).

*What these cannot do.* The frontend's guards (see "Browser-facing guards on the
frontend" in `control-plane-ui/DESIGN.md`) are **browser-enforced**. A process running
on the host sets any header it likes and can still reach the API — and that is not
hypothetical here, because the agent's RW workspace bind mount is an acknowledged
(delayed) path to host execution (see the workspace guard in `sandbox-lib.sh`).
**Authentication would not close it either**, which is the non-obvious part: any
credential at rest on the host — token file, env var, browser cookie — is readable by
that same process, so it adds friction and a secret to manage without changing the
outcome. Closing host-local forgery requires a human-presence gesture the host cannot
replay (WebAuthn user-presence on `resolve`, or an out-of-band confirm). That is a
deliberate *later* decision, and it should be taken for this specific threat rather than
for "the UI has no auth" in general. Ceiling worth stating plainly: while the bind mount
exists and the approval surface must be reachable by a human who is on the host, no
purely host-side control survives host code execution.

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

Not yet built: git/secrets/cache data-plane services, skills, quality-gate hooks.

### MCP gateway — governed tool capability

A data-plane service that speaks MCP to the sandbox on one side and to configured
MCP servers on the other, exposing a **curated** tool set under per-tool
**allow / deny / ask** policy held in the control plane. It is to tool capability
what the egress proxy is to network capability, and it is deliberately built on the
same parts: policy in the crown-jewel store, unknown held for a human, every
decision audited.

**Why it is governed rather than ungoverned, and the reason is not policy.** An
ungoverned tool must have *no independent egress*; an MCP gateway has egress and
credentials by definition, so the classification is forced. The prize is custody:
a GitHub PAT, a Slack token, a Jira key lives in the gateway and never in the
sandbox. Per-tool policy is the visible feature, but credential brokering is what
makes this the first concrete instance of "write-capable credentials stay governed"
rather than a second exception to it.

**What makes the boundary hold, given that no settings file is a boundary here.**
The yolo agent can add an MCP server of its own and it does not matter: a server it
spawns in its own container inherits the sandbox's capability, which is no egress
and no credentials, so it can do nothing `Bash` could not already do. The invariant
therefore needs no client-side enforcement — it rests on the same impoverishment
everything else here rests on. This is why the Core-idea line says the gateway is
the only MCP path *with anything behind it*, rather than the only one permitted.

**One server, one container, and `mcp-net` is the boundary.** Each MCP server runs
as its own container on `mcp-net` (internal), never inside the gateway and never on
`sandbox-net`. That placement *is* the security property, because nothing inside
those containers keeps the agent out: each holds a credential the sandbox must not
have, so a server the agent could dial directly would make per-tool policy
decorative. Running them inside the gateway would be worse still — a third-party
image would inherit the gateway's route to the control plane, a better position
than the agent has. `mcp-servers.yml` is the built catalogue; `docker-compose.yml`
keeps the networks.

Four constraints follow, each closing a different hole:

- **The servers' egress goes through the egress proxy** — set by env, *enforced* by
  placement: `mcp-net` is internal, so the proxy is the only thing reachable and a
  server that ignores `HTTPS_PROXY` fails loudly instead of going direct. This is
  the point the containers-per-server model does not remove but relocates: the
  gateway itself needs no egress, since it only ever dials siblings, while the
  servers need real internet — from a container holding a write-capable credential,
  which is exactly what must not have an unaudited path out.
- **The proxy's leg on `mcp-net` is inbound-only in effect.** It is attached to be
  *dialed*, and the subnet sits inside the private range the relay guard already
  hard-blocks, so the proxy refuses `mcp-net` as a CONNECT target and cannot be
  turned into the agent's way in.
- **The gateway keeps its own single-conversation bridge to the control plane** —
  `tool-authorize-net`, built: on the `authorize-net` pattern but *not*
  `authorize-net` itself, since sharing that bridge
  would create a lateral edge between two enforcers, which is what the original
  split bought its way out of (see "why the control path is more than one net", which
  also has the bind asymmetry between the two bridges). Its agent-facing
  listener binds the `sandbox-net` address only, so it is absent from `mcp-net`:
  otherwise a compromised server container could call the gateway's own tool
  endpoint and drive approved tools laterally.
- **Tier 2 stays out by the mechanism that already keeps tier 1 off the inference
  service** — firewall grants are mode-gated, so the gateway's /32 is issued in
  governed mode only.

None of this is self-enforcing, so it is asserted rather than described:
`tests/test_topology.py` reads both compose files and holds the placement rules
(no `sandbox-net` leg, no egress leg, proxy env present, the probed address pinned),
and `boundary-check.sh` proves from *inside the sandbox* that `mcp-net` is
unreachable — probing the proxy's address there rather than a server's, because no
server runs unless its profile is enabled and "nothing listening" would pass for the
wrong reason.

**The gateway is `tool-gateway`, and it is placed before it speaks.** The service
is **triple-homed** — `sandbox-net`, `mcp-net`, `tool-authorize-net` — and the
asymmetry between the three legs is the design rather than an implementation
detail: one is *served on* and two are only ever *dialled out of*. The rule that
generates it is the same one that decided the control plane's binds, applied from
the other side: a surface lives on the network of whoever is meant to call it, and
nowhere else. `tool-gateway/app.py` carries the refusals and the reasoning beside
the code they constrain.

Stated here because it is the part no single file can check. The gateway's bind
guard can see the address it was given but not the networks it was attached to;
compose declares the legs but cannot see which of them a listener opened. Neither
end can catch a disagreement, so `tests/test_topology.py` holds the two equal —
the bind against the pinned `sandbox-net` leg, the forbidden-CIDR default against
the real subnets — and `boundary-check.sh` probes all three legs from inside a
running sandbox, the agent-facing one as a **positive control** so that a silent
`mcp-net` is a statement about binding rather than about nothing listening.

What the gateway shows the agent and what it runs — the two axes and the order they
landed in, why nothing runs before the control plane has answered, and why every
refusal reaches the agent as a result — is the gateway's own code, and is designed in
`tool-gateway/DESIGN.md` → "What is shown, and what runs". How several servers share
the agent's one namespace, and which of a server's fields cross into it, is beside it
in "What the tool list carries".

**The control plane configures servers; it never starts them.** Adding a server
means starting a container, and that would mean a docker socket on the
crown-jewel container — the one component whose compromise is total. So the split
is: **compose declares, the UI configures.** A human edits `mcp-servers.yml` and
brings the container up; the control plane owns which of the running servers are
enabled and what each of their tools may do. The policy store holds nothing that
grants — it never sees a credential.

**Per-server identity has two different answers, because it is asked on two paths
that do not meet.** Conflating them is the easy mistake here.

- **Tool calls** run sandbox → gateway → server. The gateway *dialled* the server,
  by name, so the identity is the name it used. It needs no address, no resolution
  and no registry lookup to say which server a call is for. This is the identity
  per-tool policy is keyed on. Where that name may *lead* is a separate question,
  answered by placement: Docker DNS answers from every network the gateway shares,
  sandbox-net included, so a server's credential is sent only to an address on
  `mcp-net` (`_placed` in `tool-gateway/discovery.py`, held to compose's subnet by
  `tests/test_topology.py`).
- **A server's own egress** runs server → egress proxy → internet, and the gateway
  is not in it: `HTTPS_PROXY` on each server container points at the egress proxy
  directly. The proxy has a TCP connection and nothing else, so the peer **address**
  is the only handle that exists. This is the identity `rules.client_class` is keyed
  on (see "Policy is scoped to a client class").

The consequence is that the gateway cannot supply the client class. It is absent
from the conversation that needs it, and a server makes egress calls with no tool
call in flight at all — at startup, on a token refresh, on a background poll.

**So the addresses are pinned, in `mcp-servers.yml` beside the server they belong
to.** A dynamic address is not an identity: Docker allocates in start order, so a
restarted server can inherit the address a different server just vacated and have
its egress decided under that server's rules — an ordinary operational race needing
no attacker. Pinning removes it rather than narrowing it, and it costs one line in
the file that is being edited anyway. `tests/test_topology.py` holds every server to
it, since an unpinned server does not fail loudly — it quietly stops being separable.

**Egress classes stay per-network until a second server exists.** With addresses
pinned, splitting `mcp` into one class per server is a one-line change to
`policy.CLIENT_CLASSES` and an `UPDATE` on the rules table — the column is free-form
text, so no schema change and no migration. That cheapness is the argument for
waiting: today one server shares a class with nobody, and what a second server's host
needs look like is not yet known. What is *not* deferrable is the pin, because it has
to be true before any of it is sound.

The bound while it waits, stated so it is known rather than rediscovered: containers
on `mcp-net` share one egress class, so a second MCP server inherits the first's
approved hosts. It reaches nothing approved only for the agent.

**The alternative that would remove the pins is worth naming, and rejecting.** Point
each server's `HTTPS_PROXY` at the *gateway*, have it forward upstream, and it can
annotate every request with the server it came from — per-server identity, no
addresses. It also makes the gateway a forward proxy as well as an MCP gateway, puts
it in the path of every server's TLS, and turns a gateway outage into an egress
outage. Against one line of YAML per server, the trade is not close.

**A credential should live as far from the agent as its server allows — which is
usually the server container, not the gateway.** The rule follows from what each
component is: MCP server containers sit on `mcp-net` with **no route from the
sandbox**, while the gateway is agent-facing *by definition*. Making the gateway hold
every server's token would put the whole credential set on the one surface the agent
can talk to, and concentrate it: one compromise yields every token instead of one.
Per-container credentials keep them off the reachable surface and spread the blast
radius.

*The tempting counter-argument does not survive contact.* "A third-party image with
no standing credential has nothing to steal" is only true while the server is idle —
an injected bearer arrives on **every call**, so a compromised image gets the token
the moment it is used. What per-request injection actually removes is the idle window,
which is worth little against the threat it appears to address.

**GitHub's server leaves no choice, and that is a per-server fact rather than the
rule.** In `http` mode its token middleware requires an `Authorization` header per
request and 401s without one; the env credential is stdio-only, and when a header is
present it wins outright (both measured — see NOTES.md, which also records that the
upstream fallback documentation no longer matches the code). So the gateway must hold
and inject *this* token. The precedence at least runs the safe way round: a stale
environment token cannot shadow what the gateway supplies.

**The injected bearer is a scoped PAT, not an OAuth token.** GitHub's server in
`http` mode is an OAuth *resource* server — it advertises protected-resource metadata
and verifies a bearer, but never acquires one (its interactive OAuth *login* is
stdio-only, by its own documentation). Acquisition is therefore the gateway's job, and
OAuth's advantages here are the enterprise ones: per-user identity, short-lived
tokens, incremental scope. With one operator they pay nothing, while the costs are an
app registration, a browser-reachable redirect bolted onto the control-plane UI, and
storage of a refresh token — a *longer*-lived secret than the PAT it replaced. A
fine-grained PAT scoped to one repo is also tighter than an OAuth token carrying an
account's scopes. Revisit only if dockade ever serves more than one human.

*The option that would beat it, and why it is unavailable.* The server's stdio OAuth
login keeps its token **in memory only** — no env var, nothing in `docker inspect`,
nothing in the gateway — which is better custody than any PAT arrangement, and its
documentation covers running that way in Docker (a published callback port so a host
browser can reach the redirect). Two structural facts rule it out here, neither of
them about security: a client in one container **cannot speak stdio to a server in
another** without the docker socket, which nothing in this design may hold; and a
memory-only token acquired interactively means a human at a browser after every
restart, which an unattended gateway cannot rely on. So the PAT wins on availability
rather than on custody — worth stating in that order, because the reverse claim is
tempting and wrong.

The consequence for the catalogue: the gateway needs the injection capability
regardless, but "the gateway is the vault" is **not** an architectural rule. Prefer an
env credential in the container wherever a server accepts one. If concentration ever
starts to matter with several forced-injection servers, the shape that resolves it is
a per-server header-injecting sidecar on `mcp-net` — more moving parts than one server
justifies, recorded so it does not have to be re-derived.

**How a credential is configured: the control plane stores the descriptor, the
gateway resolves the secret.** These are separable and must stay separate. The store
gets an **auth descriptor** per server — enough to build a request, useless to steal:

```json
{ "auth": { "type": "header", "header": "Authorization",
            "template": "Bearer {secret}" } }
```

`type: none` is the default and covers the preferred case where the server holds its
own credential and the gateway injects nothing. One `{secret}` placeholder covers the
variations that actually occur (`Bearer …`, `token …`, `X-Api-Key: …`) without a
per-server special case anywhere in the code — there is no GitHub-specific branch.

**The secret's path is derived from the server name, never stored as a reference.**
The gateway reads exactly `/run/dockade/secrets/<server>.json` and nothing else. No
prefix is added, because the name already carries one — the server IS `mcp-github`,
so the file is `mcp-github.json`; one string is the container dialled, the policy key
and the filename, and that is what leaves nothing for a reference to disagree with. A
free-text `secret_ref` in the store would let a forged config write point one server
at another's credential; deriving the path makes that cross-wiring **impossible**
rather than validated-against. Same move `_persist_candidates` already makes for
egress rules: the backend derives a bounded set instead of trusting a string the
requester supplied. The cost is that two servers cannot share one token, which is
closer to a feature.

**The file is JSON, holding secret material and nothing else:**

```json
{ "token": "github_pat_...",
  "note": "read-only on dockade, expires 2026-08-26" }
```

Structured rather than a bare token for two reasons beyond the extension being honest
about its contents. It removes a whole class of fragility — a bare-token file is
sensitive to trailing newlines and CRLF, which cost a `tr -d` in the manual probe
before this was settled — and multi-field secrets are coming: a self-hosted GitLab or
Jira needs a token *plus* an instance URL, and a bare string would force a second
mechanism the first time that happens. `note` exists so a token's expiry has a home,
and is never read by anything; it is worth having because an expired credential
returns the same 401 that otherwise signals a working path.

The boundary that keeps this from drifting: **secret fields only.** Another secret
(`client_id`, `client_secret`) belongs here; a non-secret like an endpoint or a header
name does not — that is the descriptor's job, and config split across two stores means
neither is the source of truth.

**Where the material lives:** one file per server on the host, outside this repo,
`~/.config/dockade/secrets/` at 0700 with files at 0600 — not in a Docker volume, not
in the policy store, not in the image, not in git. Hence a property worth having
explicitly: **a crown-jewel backup never contains a credential.**

**Delivered as a read-only bind mount of the directory, deliberately not compose
`secrets:`.** A `secrets:` entry whose `file:` source is absent fails at `up` time for
the whole project — the same eager-validation trap as `${VAR:?}`, and this repo has
paid for that lesson twice. A directory bind degrades instead: a missing file means
*that* server fails closed while every other service is unaffected. Both `make up`
and `make mcp-up` warn on the mode of anything under `MCP_SECRETS`
(`secrets-perm-check` in the `Makefile`); neither creates the directory.

**What the UI can say without ever seeing a value:** whether the secret *resolves*,
per server. That is what makes a forced-injection server fail legibly — "configured,
secret missing" — instead of surfacing an upstream 401 that reads like a policy
problem. Rotation is replacing the file; the gateway reads it on every call, so
there is nothing to restart and nothing to edit in the UI.

**The gateway pulls, and what it may cache is not uniform.** Same shape as the
proxy's `/authorize`, for the same reason: no client-side cache means an operator
edit applies to the very next call. But two questions travel this path, and only one
is per-call. **Execution policy must never be cached** — a `deny` set in the UI that
waits for a TTL is not a deny. The **roster and tool list** are needed at session
start and on change, so they may be polled — and polled is all they are: the gateway
re-reconciles on an interval and a client re-lists, because the stateless transport
has no server-to-client stream and `notifications/tools/list_changed` is deliberately
not advertised (`tool-gateway/protocol.py`). The gateway's bridge therefore answers
four endpoints where the proxy's answers one — decide, roster, inventory, claim — and
the criterion that keeps that width honest is that none of them *grants*: the
inventory push records a claim `_decide_tool` never reads, no rule is written there
and no approval is decided there, which is why `resolve` stays off it and why the
claim can only release what a human already approved.

How the control plane keeps what the gateway brings it — tool policy in a table of its
own rather than a scope on `rules`, tool asks in their own approvals table, and one
pending queue for both surfaces — is the control plane's own code, and is designed in
`control-plane/DESIGN.md` → "The tool surface: its own tables, the one queue".

**The control plane learns a server's tools from the gateway, never by
scanning `mcp-net`.** Before it did, a `tool_rules` row was a name an operator typed
and nothing checked it against anything. That was safe — a missing rule denies, so a
misspelled rule and an unwritten one fail the same closed way — but blind in both
directions: no list to pick a name from, and a typo indistinguishable from a
deliberate deny. The gateway is the only component that can
close this, because it is the only one that ever talks to a server; it already
dials each enabled server by name on its roster poll, and `tools/list` is the same
trip. What comes back is operator-facing metadata and **never an input to
`policy._decide_tool`** — the names and descriptions are server-authored, they would
render in the control plane's own UI, and a discovered tool that could write its own
rule is a server granting itself capability.
Discovery runs only on *enabled* servers, since disabling means the gateway
stops dialling — so an operator enables before seeing the tool list, which is safe
only because the deny default makes an enabled server with no rules able to do
nothing. The rejected alternative is a subnet sweep. Its fatal form is taking the
name from the server's own answer, which under name-keyed policy lets a server
choose its rules; Docker's reverse DNS may well supply an honest name instead, and
it is still the wrong shape, because no MCP port convention exists (8082 is GitHub's
image default) so a sweep means probing guessed ports across containers holding
write-capable credentials, and registering by existence would put anything that
reaches `mcp-net` in front of an operator as a candidate. **The mismatch audit is a
separate report:** the gateway reports a rule naming a tool the server does not
expose, and a tool no rule decides — which needs no store, no new endpoint, and no
server-authored text in the crown jewel, and which runs beside the inventory rather
than having been replaced by it.

**The tool inventory lives in memory, arrives by push, and is audited
only when it changes.** The gateway is the only component that can see what a server
exposes, so it reports; the control plane holds the result for an operator choosing
rules and for nothing else. **In memory, never stored** — it is derived data,
rebuildable by asking the servers again, and a stored copy would both outlive a
server that has been gone for a week while reading as current and put server-authored
text into `make backup`, which is the operator's own decisions and nothing else.
**Pushed, because a pull is impossible**: this process has no leg on the gateway's
networks and must not be given one, since dialling the agent-facing service from the
crown jewel is the lateral edge the gateway's bind guard exists to prevent. That
makes `/tool/inventory` the only WRITE on that bridge, and the criterion keeping the
bridge's width honest survives intact — it records a claim that `_decide_tool` never
reads, so a tool arriving on it is denied exactly as it was before. **Audited on
CHANGE**, not per push: a push lands whenever the roster moves, and a row each time
would bury the one worth keeping — a server's surface growing without a human in the
loop is a supply-chain event. Rows carry tool NAMES, which `policy._TOOL_RE` bounds,
and never descriptions, which nothing bounds. Two distinctions are load-bearing
enough to name: a server that could not be enumerated keeps its last known surface
rather than reading as one that exposes nothing, and a tool whose name falls outside
`_TOOL_RE` is dropped but counted, because no rule could ever be written for it.

**A server enters the store because an operator registered it, not
because a file declared it.** Seeding from a file was considered and set
aside rather than rejected. Reading `mcp-servers.yml` directly means a YAML parser
in the component whose compromise is total, parsing a document almost none of which
concerns it, and coupling the control plane to a compose file's format; a small
purpose-built seed file avoids all three and is this repo's existing idiom, but
costs two files per server plus a drift guard to remove one typing step. What
decides it for now is that seeding removes only the *register* call: `enabled`
stays an operator decision under every option, so the UI surface is the same either
way, and building the registration path answers the question while leaving it open.
The accepted cost is that the name is typed against nothing — this container cannot
enumerate what is running — so a typo registers a server the gateway finds nothing
behind. That is tolerable **because** the gateway's discovery report names it as
`NOT ENUMERATED` within one interval; without that report this decision would be
the wrong one.

**An unconfigured tool is denied and reported, not held — a deliberate divergence
from the egress proxy.** There, an unmatched host is held because the set of hosts
is unbounded and discovered at runtime; default-deny without a human would make the
proxy useless. A server's tool set is **finite and enumerable at connect time**, so
it can be configured in advance and refusing the unknown costs nothing. The gain is
that the exposed tool list becomes a *configuration artifact* rather than a mirror
of upstream: a server upgrade that adds tools raises a notice in the control plane
instead of silently widening what the agent can reach.

**`ask` does not decay, and that is the problem this design has to answer.** Egress
holds collapse duplicates onto one card and persist into rules, so the human's
decision count trends toward zero as trust accrues — the progressive-trust path this
system is built around. Tool payloads are per-call and never repeat exactly, so a
naive `ask` is a permanent tax with no such path. What is needed is an
argument-shaped analogue of `_persist_candidates`: *this call* → *this tool with
these arguments* → *this tool with one field pinned* → *this tool always*. Copy its
shape exactly, because the property that matters is the same one — the backend
derives a **bounded** candidate set, the operator picks from it, and the chosen
value is shown verbatim; nothing is persisted from a string the requester supplied.
Deriving that ladder is server-specific, and it — not rendering — is where "any kind
of MCP server" actually bites. MCP's own `readOnlyHint` / `destructiveHint`
annotations are **server-supplied and therefore untrusted**: they may sort and label
the configuration surface ("this server claims these are read-only"), and they must
never decide.

**An `ask` answers immediately.** The gateway never blocks the agent, and never blocks
a control-plane worker either — two independent choices, both away from the egress
shape. It *registers* the ask with the control plane and takes an id back at
once, rather than having its call held open and woken by a `threading.Event` — so a
tool ask pins no threadpool worker and does not draw on `MAX_WAITERS`, whose whole
purpose is that a slow decision must never starve the `/authorize` path the agent
depends on to work at all. And the gateway answers the *agent* immediately too, with
a **pending result** naming the approval, rather than holding the MCP call open.
Nothing is held open at either end, so there is nothing to withdraw: no stranded
caller exists to cancel.

Blocking the agent was available — a per-server `timeout` raises the first-byte timer
to match, so a long wait is configurable — and is rejected on the shape of the limit
rather than its size: it is a hard wall-clock bound with no extension path (progress
notifications do not lift it), and it defaults to roughly a day. Blocking would mean
a design whose failure mode is a stranded call, tuned by a number whose default is
useless. Answering immediately makes the hold window a **human** parameter, free to
be minutes or hours because nothing is waiting on it. The timers are in NOTES.md and
the numbers are deliberately not repeated here — read them as **documented, not
measured**: NOTES.md says so, and the ~28-hour default in particular is worth probing
in-container before anything depends on its exact value. The decision does not: it
rests on the SHAPE of the limit (a hard wall-clock bound with no extension path),
which the documentation is authority enough for.

Three properties make the pending answer safe rather than merely non-blocking:

- **It is a result, not an error.** "Held for approval, id X, retry later" is
  something an agent can act on — do other work, come back. A failure is not, and an
  agent that records the call as failed is the stranded-caller problem arriving by a
  different route.
- **A grant is bound to the payload, not to the tool.** The approval is keyed by a
  hash of the arguments the human actually read, so a retry with *different*
  arguments cannot ride an approval given for different ones. The same hash is the
  join key: an identical retry attaches to the pending ask instead of raising a
  second card, which is what `_GROUPS` does for egress duplicates.
- **A decided ask is terminal and says so.** `denied` and `expired` must be
  unmistakably distinct from `pending`, and unmistakably final, or an agent retries a
  refusal forever.

**Retry pressure is contained by the caps, not by instructions.** The instruction to
retry travels *in the pending result*, where it cannot be forgotten mid-session and
cannot drift from the gateway that emits it — deliberately not a rule in a
`CLAUDE.md`, which would be a copy of a behaviour, read every session for something
that happens rarely, and stale the moment the gateway changed. Nothing enforces an
instruction anyway, so the containment is structural and mirrors the two caps
`control-plane/holds.py` already distinguishes. A repeated *identical* ask is O(1) —
it joins by hash, raises no card, and costs the agent its own context rather than
costing governance anything. What actually needs bounding is the other loop: an agent
opening *fresh* asks with slightly varied payloads defeats hash-joining and floods the
human, which is the attention-DoS `MAX_PENDING_PER_CLIENT` exists for on the egress
side. The tool surface needs that cap most, and its own, since the two surfaces no
longer share a pool.

**Resumption is a tool, keyed on the approval id.** The agent needs a way back to a
pending ask, and the obvious one — retry the original call — is safe but fragile. Safe
because the gateway re-checks policy before executing, so a still-pending ask returns
pending again rather than running. Fragile because joining by payload hash requires the
agent to reproduce the arguments *byte-identically*, and a model asked to retry
commonly reformulates: a reformulated retry hashes differently and opens a **second**
ask, so the flood the caps exist to bound arrives from ordinary model behaviour rather
than from an adversary. An opaque id is a short token copied verbatim, which is the one
thing a model will not quietly rewrite. Hash-joining stays as the backstop for an agent
that retries the original call anyway; the id is the path the pending result names.

**The gateway executes on resumption, not on approval.** Lazily, when the agent comes
back for the result — never at the instant the human clicks. This makes the
stranded-caller property structural rather than detected: an approved call nobody
returns for simply never runs, so a side effect cannot happen with no one to receive
it. Eager execution is the obvious implementation and it quietly reintroduces the exact
failure that answering immediately was chosen to remove. It also means the agent never
re-sends the payload, so the arguments that execute are necessarily the ones the human
read.

**An approval is not a standing authorization.** Deferring execution to resumption
opens a gap between the decision and the side effect, and two things close it, neither
sufficient alone. The grant gets its **own window** — a second timeout, running from
the human's answer rather than from the ask, so an approval nobody redeems expires
instead of staying live for whenever a forgotten session comes back. And the claim
**re-reads policy** before it releases anything: a server can be disabled or a rule
revoked in that gap, and none of those touch the approvals table, so without the
re-read the operator's stop button would reach the decision endpoint and not the one
surface that releases a side effect. This is why the two bounds in
`control-plane/holds.py` are separate numbers and why the claim endpoint depends on
`policy._decide_tool` at all — a claim that trusted its own row would be the
default-allow this whole path is built to avoid.

**One id at a time, and no roster of pending work.** A lookup scoped to a single
approval is all resumption needs. Listing what is pending is a different capability and
is deliberately not offered: the gateway's agent-facing listener binds `sandbox-net`,
which both tiers share, so a roster would leak approvals the caller never raised — and
past the leak it hands the agent a read on the operator's queue, a nudge surface kept
away from it everywhere else here.

Resume is the gateway's first tool of its own, proxied from no server; the rule the
next one must meet is in `tool-gateway/DESIGN.md` → "Gateway-native tools are a
category".

**Elicitation routes to the wrong human.** Worth naming because it is the protocol's
own answer to everything above: MCP lets a server ask the *client* to prompt its user
(`elicitation/create`), which is the human-in-the-loop primitive this section otherwise
builds by hand. It is unusable for approvals here. The approving human sits at the
control-plane UI, in a different trust domain from the agent's session, so routing the
decision through the agent's own client would put it inside the boundary being governed
— where "no Claude Code settings file is a containment boundary" already applies.
Whether the client implements it is therefore not worth establishing for this purpose.
What MCP does supply is the brokering half: a curated `tools/list` is exactly the
configuration-artifact roster above (the `list_changed` notification that would
announce a change is not offered here — see "The gateway pulls").
What it supplies nothing of is the deferred half — no accepted-come-back-later, no
resumption primitive, no timeout extension (see NOTES.md). That absence argues *for*
answering immediately rather than against it: fail-fast asks the protocol only for what
it natively has, a result now and another tool call later.

How a tool ask's payload is shown to the person deciding — raw and authoritative,
escaped where an invisible character would let the browser reorder it, and why an opaque
identifier is left unresolved — is the page's own code, and is designed in
`control-plane-ui/DESIGN.md` → "Presenting a payload for approval".

**Two failure modes the egress proxy does not have.**
- **Executing after the caller is gone.** If a held call outlives the client's MCP
  tool timeout, a human approves, the gateway executes, and the agent has already
  recorded a failure — a message sent that nobody wanted, invisible to both sides.
  The proxy has no side effects to strand, which is why this appears for the first
  time here. **Answering an `ask` immediately removes it** (see "An `ask` answers
  immediately"): nothing is held open, so there is no caller to lose and no
  disconnect to cancel on. The earlier plan — cancel on disconnect and keep the hold
  window under the client timeout — was written for a blocking gateway and does not
  apply; the timers that killed it are in NOTES.md (documented, not measured — see
  "An `ask` answers immediately").
- **The response is the channel.** The gateway governs the *request*, but what steers
  an agent is the third-party text arriving in its context — an `allow`-ed,
  read-only tool is unaudited intake of the same shape as WebSearch, and content in
  it can name tools (see "Two axes, not one" in `tool-gateway/DESIGN.md`). The audit
  must therefore record the response side, at minimum size and hash, because the
  forensic question is *what entered the agent's context*.
- **A record written after an irreversible act cannot fail closed; it can only
  buffer.** This is why the gateway's outcome stream is a file the control plane
  drains (`tool-gateway/outcomes.py`, the `tool-audit` volume) and not a POST to the
  bridge it already dials. Authorization is a round trip *before* the side effect, so
  an unreachable authority means refuse, nothing ran, and fail-closed is a complete
  answer. The outcome is written *after*, where a failed POST means the call happened
  and nothing recorded it — the hole itself. Buffering also keeps tool-result latency
  off the authority's availability, which nothing else in the data plane does, and
  adds no write surface to the crown jewel. The ingest side is the same arrangement
  the egress proxy already has, and gets exactly-once for free: the cursor lives in
  the same SQLite as the rows, so draining and advancing are one transaction.
- **Only the gateway can say how a call ended, and the reason is structural.** The
  control plane's claim row is written *before* the call runs, so the authority has
  answered and gone by the time the call succeeds or fails; an approved write that
  GitHub then refuses looks, in its trail, exactly like one that landed. The split
  that follows: **outcome** (a status and a reason — cheap, and the question an
  incident actually asks) is separable from **content** (size and hash — what entered
  the context), and they have different destinations. The body belongs in the
  gateway's own rotating file, which is bounded and disposable; only the facts belong
  in the crown-jewel store, which is neither. That is what keeps attacker-authored
  text out of the store, and out of anything that renders it.

**Which servers may be admitted: narrow, named tools.** The three states only have
purchase when a tool is narrow and named. A tool whose payload is a **program** —
SQL, PromQL, a shell string, a generic `http_request` — collapses
allow/deny/ask into allow-everything-or-nothing: per-tool policy governs nothing,
every call is an `ask` a human must read a query to judge, and the credential is the
only real boundary left. Such a capability is admitted only when the **credential
itself** is the boundary (read-only against a replica, where `allow` is safe by
construction), or as a handful of named parameterized operations — and that is a
skill, not MCP. This sharpens the skills-versus-MCP line rather than reversing it:
skills for capabilities we can name, the gateway for third-party tool surfaces we
did not design.

**First server: GitHub — shipped as `mcp-github` in `mcp-servers.yml`, ahead of the
gateway that now fronts it.** It is the capability actually missing rather than a
demonstration — `gh` is deliberately absent, no write-capable credential lives in
the sandbox, and pushing is the human's step, so a fine-grained PAT held beside the
server is what lets the agent finish a unit of work. It is also kind to the parts that are
hard, in ways the alternatives are not: payloads are human-legible (a PR body, a
branch name) rather than opaque IDs, authentication is a static token rather than an
OAuth flow the gateway would have to own, and reads/comments/merges land cleanly on
the three states. And it is unkind in the one place that argues *for* it: issue and
PR bodies are attacker-authored text read by an agent with write access to the same
repo, so the response-side channel above is confronted on server one rather than
discovered on server four — which is also where a per-tool **repo allowlist** earns
itself. Likely order after that: observability (near-all read, so it proves the
topology cheaply), then Slack (opaque channel IDs, and `ask` at its least
decaying), then Atlassian (OAuth plus ADF payloads — the renderer's stress test, not
what should drive its design). Corporate servers also raise a custody question that
GitHub-on-your-own-repos does not; one gateway instance per credential domain is
cheap to decide early and awkward late.

Two consequences of the container-per-server rule, worth stating because they are
choices and not oversights. **Hosted servers are excluded** — Sentry, Atlassian and
Linear all offer one, and none can be a container on `mcp-net`; if one is wanted
later, wrap it in a local container so "the gateway only dials siblings" survives
and the remote hop sits behind the egress proxy where it belongs. And **stdio
servers need an HTTP transport**, because a pipe does not cross a container boundary
and spawning one from the gateway would need the docker socket, which nothing here
may hold. GitHub's server has an `http` subcommand; a stdio-only server needs a shim
in its image, which is a real cost when choosing the next one.

**A server's own restriction flags are defence in depth, never the boundary.** The
GitHub server's `--read-only` was silently inert in `http` mode through v0.32.0 (fixed
in v0.33.0) — write tools stayed in `tools/list` and executed. Set them anyway; rely on
the gateway's deny state, and verify `tools/list` rather than the flag.

**The MCP gateway owns repo writes.** The governed git path is scoped to
clone/fetch. Two governed roads to "write to a repo" under different policy
models is a hole, because an actor — or a confused agent — takes the weaker one,
which is why this was decided before either path existed rather than after both
did. The earlier inclination was the opposite one — a branch and force-push policy
wants to see refs, not JSON — and what overturns it is that **the REST write set
cannot express the destructive operations that policy exists to catch**: a
create-or-update-file style tool appends a commit and fails on a stale blob sha,
and nothing in the set rewrites history or deletes a ref. A wire proxy would be
enforcing against operations the surviving path cannot perform. Nor could it have
owned writes alone whatever else was decided, because `merge_pull_request` moves
the ref **server-side** where nothing watching the wire sees it. The cost, because
it is real: the API path writes **new** commits from file contents rather than
pushing ones already made locally, so a branch must never be written both ways.
Three consequences, in the order they bite. `GITHUB_READ_ONLY` flips off **with the
gateway and not before** — until something fronts the server that flag is the only
thing narrowing it — after which the gateway's `deny` is the whole boundary, which
is what "a server's own restriction flags are defence in depth" has to survive. It
has not flipped yet: `mcp-servers.yml` still defaults it on, so the write set is not
offered, and turning it off is the step that makes the rest of this bullet live
(see "Status"). Per-tool repo scoping stops being optional, putting the unmeasured
`x-mcp-header` override question (NOTES.md) on the gateway's critical path. And a
dispatcher tool means one name decides several operations, so the argument-shaped
`ask` ladder is needed for the write set rather than deferrable past it.
`merge_pull_request` is denied outright: merging stays the human's step, as pushing
is today.

*Reasoned from the REST surface, not measured.* The write tools are enumerable —
`make mcp-tools SERVER=github` with `GITHUB_MCP_READ_ONLY=0` prints their schemas —
and confirming this before allowing any of them is cheap. `push_files` is the one
to look at first and the reason this is flagged rather than asserted: it builds a
commit through the Git Database API, where a ref update *can* carry `force`, so it
is the single tool in the set that could falsify the paragraph above.

**Telling the sandbox it exists: `--mcp-config` + `--strict-mcp-config`, from the
launcher.** Four channels can declare an MCP server, and the choice is not a matter
of taste — a settings block, which would have been the obvious fit for this repo's
materialize-each-boot config, **does not work at all**: `mcpServers` in a settings
file is silently ignored (measured; see NOTES.md). What remains is a project
`.mcp.json`, the per-project `local` scope, the `user` scope in
`$CLAUDE_CONFIG_DIR/.claude.json`, and the `--mcp-config` flag.

`--mcp-config` wins on three counts, and only the first is ergonomic. It takes an
inline JSON string or a file path, so the gateway entry can be a **baked,
root-owned file** the agent cannot edit — the same shape as `statusline.sh`, and one
step past materialize-each-boot, since nothing is written into the config volume to
drift in the first place. And `--strict-mcp-config` makes the launcher's set
**exclusive for the session** — a `claude mcp add` by the agent does not join it,
where `--mcp-config` alone is merely additive (both measured).

`.mcp.json` is disqualified twice over, and the second reason is what makes strict
mode load-bearing rather than tidy. Writing one would put config into the human's
live checkout — but the sharper problem is *reading* one: `/workspace` is a checkout
the agent clones into, so a repo can ship an MCP server with itself, and in `-p` mode
that server starts with no approval gate at all (measured — see NOTES.md). What
arrives is not capability, since a repo-supplied server inherits the same nothing
every agent-added server does; what arrives is **steering** — tool names,
descriptions and outputs entering the agent's context from an untrusted repository,
the same channel as the response-side risk above. Strict mode makes a `.mcp.json`
in the workspace inert, which is the honest fix; the trust prompt is not.

That exclusivity is **mistake-prevention, not containment**, and the distinction is
the same one this design draws everywhere: the agent could relaunch `claude` without
the flags. What makes that harmless is not the flag but the capability argument
above — a server it adds has nothing behind it. The flag's real value is that the
sandbox's tool surface is *reviewable in the repo* rather than accumulated in a
volume.

**Reaching the gateway takes three grants, not one, and two of them are invisible when
missing.** The flags above are only the third. The sandbox needs a firewall `/32` to the
gateway's agent leg — siblings are unreachable by default, per service and never as a
subnet allow — and it needs the gateway **exempted from the proxy environment**. That
second one is the trap: an MCP client that honours `HTTPS_PROXY` sends its requests to
the egress proxy, whose relay guard hard-blocks private ranges, so the agent gets
`egress denied by policy` for its own tool gateway. The error names the wrong component
and describes a refusal governance never made. Both failures look like a gateway that is
down, which is why `tests/test_sandbox_wiring.py` holds all three together — no single
file can see more than one of them.

**The flags belong in a root-owned wrapper on `PATH`, not in the `.bashrc.tier`
alias.** An alias is only expanded by an interactive shell, so `claude -p` from a
script or a hook would silently run without them — and `-p` is precisely where a
workspace `.mcp.json` was measured starting a server unprompted. The wrapper also
keeps `claude-yolo` correct for free, since that alias resolves through it.

Whether the entry is passed at all belongs to the **launcher's mode decision**,
alongside governed-versus-standalone — not a runtime probe, because "running is not
ready" (see Startup ordering) and a dead entry costs a startup error and misleads
the agent about its own capability. The two flags are separable, and that decides
the gateway-absent case cleanly: pass `--strict-mcp-config` **unconditionally** and
add `--mcp-config` only when the gateway is up. Strict with nothing supplied yields
zero MCP servers (measured), so "no gateway" means a provably empty tool surface
rather than whatever the config volume happens to have accumulated. The baked
`CLAUDE.md` must also say that a gateway tool call can come back as a pending id, and
that resuming it may wait on a human, or an `ask` is indistinguishable from a failure.

## Startup ordering — "running" is not "ready"

Every infrastructure service declares a `healthcheck`, and both launchers gate on it via
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
`/healthz` for the Python services. In particular the proxy is **not** probed
by making a real CONNECT through itself: that would exercise the policy path end
to end, but it would also write an audit record every interval, and a periodic
synthetic `deny` is exactly the signal a human reviewing the log is watching for.
A probe must not pollute the evidence it is protecting.

Tier 2's gate is about capability rather than audit: `llama-server` binds its port
immediately but returns 503 until the model is loaded and offloaded (minutes for a
large GGUF — `opencode-sandbox/NOTES.md` has the measured load times), and inference is
that tier's only capability, so `run-opencode-sandbox.sh` waits rather than letting
opencode's first turn fail. `sc_wait_healthy` treats `unhealthy` as retryable, since a load that
outruns its `start_period` passes through that state on the way up; only the
timeout is fatal. A container that declares no healthcheck is not gated at all —
absence of a probe is not evidence of a problem.

## Resource limits — blast radius, not boundary

Both sandbox tiers are capped by their launcher — tier 1 at 4g, tier 2 at 2g, both
`--cpus=4 --pids-limit=512` and both overridable per launch via `SANDBOX_MEMORY` /
`SANDBOX_CPUS`. The infra services are capped in `docker-compose.yml`
(1g/512m/256m). Everywhere, swap is disabled by setting the swap ceiling equal to
the memory ceiling (`--memory-swap` / `memswap_limit`): Docker otherwise defaults
swap to 2x memory, so a bare 4g cap really means 4g RAM + 4g swap — on a 15 GiB
host, a ceiling above what exists is no ceiling at all. These are **not** a
containment boundary and nothing about the threat model rests on them — the
boundary is capability (network segmentation, dropped caps, non-root, no
control-plane route).

The sandbox numbers are sized for the **workload, not the agent**: a measured tier-1
session peaked well under the cap (figures in `NOTES.md`), so the agent process is
never what needs the headroom — `tsc`, `jest`, `cargo` or a language server on a
large tree is. Hence a modest default plus a per-workspace
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

The inference service (the `llm-*` profiles in `docker-compose.yml`) and tier 2, the
local-model sandbox that is its only consumer, are designed in
`opencode-sandbox/DESIGN.md`, with the evidence in `opencode-sandbox/NOTES.md`: why the
service is ungoverned, the accelerator profiles, the tuning, what the model is for, and
how the tier is built. What stays here is where tier 2 meets the rest of the system:
why tier 1 cannot reach the service, and the firewall both tiers run.

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

### Tier 2, and what it shares with tier 1

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

How it is built, and what verified it, is in `opencode-sandbox/DESIGN.md` → "Built —
the tier-2 local-model sandbox". What it shares with tier 1:

**One boundary implementation, two grants.** The extraction into `sandbox-common/`
(`init-firewall.sh`, `entrypoint.sh`, `boundary-check.sh`) plus `sandbox-lib.sh` for
launcher plumbing means both tiers run *byte-identical* enforcement; each image
supplies only a `tier-setup.sh` hook for its own declarative config. A firewall fix
lands in both tiers at once, and neither tier can drift into a weaker posture
unnoticed. Tier 1 re-verified unchanged after the extraction (15/15, including
`api.anthropic.com reachable via proxy` and all six proxy refusals).
`init-firewall.sh` gained a **third mode** alongside GOVERNED and STANDALONE:

| Mode | Selected by | Permits |
| --- | --- | --- |
| GOVERNED | `EGRESS_PROXY_IP` set | loopback, embedded DNS, established, `/32` → egress proxy, `/32` → tool gateway (tier 1, when found) |
| LOCAL | `SANDBOX_MODE=local` | loopback, embedded DNS, established, `/32` → `llm:8080` |
| STANDALONE | neither | direct `ipset` IP-allowlist (proxy-less fallback, tier 1 only) |

LOCAL **fails closed**: if `SANDBOX_MODE=local` and `LLM_IP` is unset or not an
IPv4 address, the firewall aborts rather than booting an agent whose one intended
destination is unreachable. The egress-proxy allow is explicitly gated off in this
mode, so the proxy is not merely unused but unreachable.

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
  egress-proxy client — which is now a solved shape rather than an open question:
  give its network a class in `policy.CLIENT_CLASSES` and its rules are its own
  (see "Policy is scoped to a client class"). Still not needed until a worker task
  needs to fetch something.
- **Where does worker output go, and is it audited?** An unattended job's output
  *is* its consequential action, but with no egress there is nothing at the
  network choke point to log. "Everything consequential is audited" currently has
  no answer for an agent whose only output is a file.
- **Unattended blast radius.** Wall-clock, iteration and token budgets are
  required, not optional — see the 7.4-minute greeting in
  `opencode-sandbox/DESIGN.md` → "Tuning decisions".
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
consistency + tests (about a minute) and the build verification of every image — so a
shellcheck typo is not queued behind an image build.

**The build job then runs `make check-boundary`, which is the only automated evidence
that containment still holds.** Everything else in the gate reads the boundary rather
than crossing it: shellcheck reads `init-firewall.sh` as text, `tests/test_topology.py`
reads `docker-compose.yml` as YAML. Between them they assert what the boundary is
*declared* to be, which leaves a firewall regression invisible to every other check here
— the strongest containment evidence the repo has was a manual `make boundary`. So the
job stands the stack up and runs `boundary-check.sh` inside a real tier-1 sandbox, from
the agent's own security context. It rides on the build job because that job has already
compiled the images; a third job shares no layer cache and would rebuild every image
to run a two-minute check. What CI cannot cover stays manual and is named in the
workflow: tier 2 needs a GPU, standalone mode needs a host with no compose infra.

**Strict mode is the part that makes the gate mean anything.** Every stage degrades to a
SKIP when its tool is absent, which is right on a dev machine (running the checks you
*can* run beats running none) and a trap in CI: a runner without hadolint prints `SKIP
hadolint` and passes green, verifying less than the badge claims with nothing anywhere
saying so — the same "silently checks nothing" failure the `LAUNCHERS` glob guard fails
closed against. `DOCKADE_REQUIRE_TOOLS=1` turns every such skip into a failure. It lives
in the **Makefile, not the workflow YAML**, because the Makefile already owns
what-must-be-true and a requirement encoded only in CI is invisible to whoever runs the
checks by hand.

**The tooling floats on purpose, so the workflow is built around that.** `ruff.toml`
pins the rule *selection* and lets the binary drift; base images are pinned by tag, not
digest. The accepted cost is that a green commit can go red with no code change, so a
weekly schedule surfaces it on its own rather than ambushing the next pull request, and
every tool's version is printed so a verdict traces to what produced it. Three
consequences that are load-bearing rather than incidental:

- **hadolint's version and checksum are *derived* from `claude-sandbox/Dockerfile`'s
  `ARG`s**, not restated, and the step fails loudly rather than falling back to
  `latest` or installing an unverified binary. It is the one linter this repo pins, so CI and the image cannot
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
property that makes CI reproducible locally. The measured cold build came in far below
the 5–15 minutes that would have justified the divergence, so it buys nothing (that
figure and the image sizes from the same run are in `NOTES.md`). Worth knowing that
**tier 2 is not the smaller image** despite being the thinner *tier* — "thin client"
describes where inference runs and what capability it holds, not the toolchain both
tiers inherit from `sandbox-common`.

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
| 2c-1 | audit browsing — filters + the paged record view | **done** |
| 2c-2 | egress rule editing (atomic mutation) | **done** |
| 3 | skills + quality-gate hooks in the image | planned |
| 4 | pull-through package cache | planned |
| — | governed git path — clone/fetch (writes are the gateway's) | planned |
| — | GitHub write set — `GITHUB_READ_ONLY` off behind the gateway, with per-tool repo scoping | planned |
| — | `mcp-net` + MCP server catalogue (`mcp-servers.yml`) | **done** |
| — | per-client-class egress policy | **done** |
| — | tool policy: store (`tool_rules`, `mcp_servers`) + config API (`/api/mcp/…`) | **done** |
| — | tool asks: `tool_approvals`, the ask registry, one merged queue, `resolve` split | **done** |
| — | the tool card — raw payload, per-surface actions | **done** — no schema-driven view; the raw payload is the whole of it |
| — | timed grants (`leases`) — `allow_lease`, the live-lease strip, revoke | **done** — exact host only; no breadth ladder |
| — | the gateway's bridge — `tool-authorize-net`, third listener, decide/roster/inventory/claim | **done** |
| — | `tool-gateway` placement — triple-homed, agent leg only, bind guard | **done** |
| — | gateway discovery — roster pull, `tools/list`, policy-vs-server report | **done** — reports to the log; writes nothing back |
| — | MCP client credentials — read-only mount, path derived from the server name | **done** — the gateway is the only holder |
| — | MCP server registration — UI tab: register, enable/disable, revoke | **done** — servers only; tool rules not yet |
| — | tool inventory — gateway pushes, control plane holds it in memory | **done** — audited on change; no UI yet |
| — | tool policy UI — pick from the inventory, write allow/ask/deny, promote | **done** — every row in `tool_rules` now has an operator surface |
| — | curated tool list on the agent leg — MCP listener, `tools/list` | **done** |
| — | MCP gateway — per-tool allow/deny/ask on `tools/call`, the pending ask, `resume_tool_call` | **done** |
| — | tell the sandbox it exists — firewall grant, proxy exemption, `--mcp-config` from a wrapper | **done** |
| — | tool audit is joinable — `server`/`tool`/`approval_id` columns, filled by every writer | **done** |
| — | the gateway records how a call ENDED — its own JSONL stream | **done** |
| — | outcomes ingested — second stream, `status` column, `outcome` in the audit vocabulary | **done** — one approval id now selects the hold, the answer, the claim and the result |

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

The unattended worker tier's questions are kept with it, under "Tier 2, and what it
shares with tier 1". What is open here:

- **HTTPS inspection depth** — CONNECT/SNI (domain-level, no CA in sandbox) vs
  full MITM (URL/body-level, needs a generated CA in the sandbox). Likely start
  CONNECT-level, allow MITM per-domain later. Both are documented-supported by
  the CLI (MITM via `NODE_EXTRA_CA_CERTS` / `CLAUDE_CODE_CERT_STORE`, in `NOTES.md`
  → "Claude Code honours `HTTPS_PROXY`, and WebFetch inherits it"), so the choice is
  ours, not gated by tool support.
- **Web search backend** — which third-party search API for the `websearch`
  skill (Brave / SerpAPI / Google CSE).

## Future improvements
- Dedicated git proxy that speaks the git protocol, per-repo, instead of
  HTTPS-through-egress — scoped to clone/fetch, since writes are the gateway's (see "the
  MCP gateway owns repo writes").
- Separate test/build runner containers for isolation + parallelism.
- Selective MITM on credentialed hosts → true brokering so even self-use keys leave the
  sandbox.
- Docs mirror / offline docs tool.
- Progressive auto-approval driven by accumulated policy + audit history.
- **Human-presence on approval (WebAuthn user-presence, or an out-of-band confirm).**
  The *only* thing that closes host-local forgery of an approval — see the
  browser-facing-guards note under "Approval UI". Worth building for that specific
  threat (the agent reaching host execution via the workspace mount), not because "the
  UI has no auth"; naive auth does not help, since any credential at rest on the host is
  readable by the same process. The MCP gateway raises the stakes of this item without
  changing its reasoning: a forged egress approval lets a socket out, a forged `ask`
  sends the mail.
- **Approval-UI follow-ups (reviewed and specified, not built).** From the same review
  that produced the reconnect / CSP / keyed-rendering work in
  `control-plane-ui/DESIGN.md`, in value order. The top two — the hold countdown and the
  persist preview/confirm with an operator-chosen pattern — are **now built**; see
  "Frontend mechanics" in `control-plane-ui/DESIGN.md` (the hold countdown) and "A
  `+ persist` says what it will write" in `control-plane/DESIGN.md`. What became of
  the rest:
  - *(A DOM-level test for `start()` was considered here and **declined** — the frontend
    is treated as a convenience layer over a backend that validates every input, with its
    mistakes made detectable rather than prevented. The reasoning, and the condition that
    would reopen it, are under "`start()` is deliberately unverified" in
    `control-plane-ui/DESIGN.md`.)*
  - *(The **opt-in desktop notification** is **now built** — see the traffic-light
    paragraph in `control-plane-ui/DESIGN.md`. It gained one property on contact that the
    specification did not have: a notice is closed by its hold leaving the queue,
    because a notification still asking for a decision that has already default-denied
    is worse than no notification.)*
  - *(The smaller items filed here — hop-by-hop header stripping on the request side,
    an announcement for arriving approvals, visibility-gated pollers, and "showing N
    of M recorded decisions" — are **now built**. Two of them changed shape on
    contact: a live region belongs on a separate element rather than on the card list,
    whose countdowns rewrite once a second; and the coverage line compares decisions
    with decisions, since the view is grouped and a rows-versus-decisions ratio reads
    as truncation even when nothing was truncated.)*
- **`boundary-check.sh` manufactures the most alarming rows the audit log can hold** —
  control-plane relay attempts, SNI fronting, metadata-IP SSRF — and they are
  indistinguishable from an agent genuinely attempting them. That is arguably correct:
  the sandbox really did make those requests, and a probe that produced no audit row
  would not be testing the real path. It also sits deliberately opposite the decision
  recorded for the egress proxy's healthcheck in `docker-compose.yml`, which is *not* a
  real CONNECT precisely so a periodic synthetic deny never pollutes the record. The
  distinction that justifies both: that one fires on a timer forever, this one only when
  a human asks. Left as-is, filed because "remember what you ran" is not a property of a
  log.

## Layout

See `README.md` → Layout for the current tree. Companion documents: `NOTES.md` holds
the evidence behind decisions recorded here (see CLAUDE.md → "Where writing goes" for
which file takes what). A component whose reasoning is its own keeps it beside its code:
tier 2 in `opencode-sandbox/DESIGN.md` and `opencode-sandbox/NOTES.md`, the control
plane in `control-plane/DESIGN.md`, its frontend in `control-plane-ui/DESIGN.md`, the
egress proxy's relay guard and its side of `/authorize` in `proxies/egress/DESIGN.md`,
and what the MCP gateway shows and runs in `tool-gateway/DESIGN.md`. Still-planned
additions to the tree:

```
dockade/
  tools/              # ungoverned data-plane services (cache, scratch DB, ...)
  claude-sandbox/
    skills/           # sanctioned capability + workflow interface
    hooks/            # quality-gate hooks
```
