# dockade

[![check](https://github.com/larsvikb/dockade/actions/workflows/check.yml/badge.svg?branch=main)](https://github.com/larsvikb/dockade/actions/workflows/check.yml)

Run an AI coding agent (Claude Code) in a **capability-limited Docker sandbox**
so it can do strong work in a controlled, auditable way.

Two goals:

- **Governance** — every consequential action goes (eventually) through an
  auditable choke point. Containment is by *capability*, not configuration: the
  blast radius is exactly what the sandbox can reach directly, kept near zero.
- **Enablement** — the agent image encodes a "paved road" so good work patterns
  happen by default.

The agent can run in **yolo mode** (`--dangerously-skip-permissions`) without
per-action prompts. That's safe not because the agent is trusted, but because
the sandbox is deliberately impoverished: no direct network egress beyond a
strict allowlist, non-root user, dropped Linux capabilities, no host Docker
socket, and (by design) no route to a control plane.

> **Status.** The sandbox has no direct egress: a governed **egress proxy** is the
> sole path off-box, and it defers every decision to a **control plane** the agent
> cannot reach, where a human approves an unknown host in a live UI and every
> decision is audited and browsable. Third-party tools reach the agent only through
> the **MCP gateway**, under per-tool allow/deny/ask policy. What has landed and what
> is still planned is one table, [`DESIGN.md` → "Status"](DESIGN.md#status).

## Quickstart

**Prerequisites:** Docker, and a git identity on the host
(`git config --global user.name` / `user.email`) if you want to commit from
inside the sandbox.

```bash
# 1. Bring up the shared infrastructure once (egress proxy + control plane + UI).
#    Sandboxes route their traffic through it and it audits every connection.
cd /path/to/dockade && make up   # or: docker compose -f docker-compose.yml \
                                 #       -f mcp-servers.yml up -d --build

# 2. From the directory you want the agent to work in, launch a sandbox:
/path/to/dockade/run-claude-sandbox.sh

# Or point it at a specific workspace:
/path/to/dockade/run-claude-sandbox.sh /path/to/project

# Rebuild the sandbox image (after changing the Dockerfile, or switching hosts):
/path/to/dockade/run-claude-sandbox.sh --rebuild
```

Step 1 is optional: without it, `run-claude-sandbox.sh` still runs the sandbox
**standalone** (direct egress governed by the in-container firewall, no proxy
audit). Standalone needs kernel ipset support, which stock WSL2 kernels lack —
there, bring the infra up and use the proxy path. With the infra up, the launcher auto-detects the proxy, routes the
sandbox's HTTP(S) through it, and allowlists it in the firewall. You can start
**several sandboxes** against one proxy — each gets a unique name (override with
`SANDBOX_NAME`). Held requests are approved, and the audit trail browsed, in the UI
at `http://localhost:28090` (the default `DOCKADE_UI_PORT`); the raw stream is
`docker compose logs -f egress-proxy` (or the `dockade-egress-audit` volume).

On first run the image builds and you'll be dropped into a shell in the
container. **Authenticate once** by starting Claude Code and completing the
interactive Claude subscription login — the credentials persist in a named
volume, so you won't need to log in again on later runs.

Then, inside the container:

```bash
claude          # normal, permission-prompting mode
claude-yolo     # bypass-permissions mode — a conscious opt-in (see below)
```

## Two sandbox tiers

Both tiers run the **same** boundary implementation (`sandbox-common/`) and differ
only in the capability granted:

| | Brain | Egress | Credentials | Launch |
|---|---|---|---|---|
| **Tier 1** | Claude (API) | governed — egress proxy → allowlist | Anthropic session | `make claude` |
| **Tier 2** | local LLM on `sandbox-net` | **none at all** | **none at all** | `make opencode` |

Tier 2 is an [opencode](https://opencode.ai) agent driven by a local model served
in-cluster by llama.cpp, and its grant is defined by *subtraction*: no proxy, no
upstream DNS, no credentials, no route anywhere except the inference service. That
makes it both a genuinely offline agent and the sharpest test of the boundary — its
`boundary-check.sh` **inverts** the Anthropic check, asserting the API is
*unreachable*. It needs a model running first:

```bash
# put a GGUF in ./models, set DOCKADE_LLM_MODEL in .env, then:
docker compose --profile llm-intel up -d llm-intel    # Intel/WSL (SYCL)
docker compose --profile llm-nvidia up -d llm-nvidia  # NVIDIA (CUDA)
docker compose --profile llm-vulkan up -d llm-vulkan  # AMD or Intel, native Linux
```

The three are mutually exclusive — they share one address and one `llm` alias, so
the agent's endpoint is `http://llm:8080` whatever the host has. `llm-vulkan` also
needs `DOCKADE_RENDER_GID` (the host gid owning `/dev/dri/renderD128`) and is the
one variant not yet verified on hardware.

See [`opencode-sandbox/DESIGN.md`](opencode-sandbox/DESIGN.md) for the accelerator
setup, the tuning decisions, and why the LLM service is ungoverned (it has no egress
of its own to govern); [`opencode-sandbox/NOTES.md`](opencode-sandbox/NOTES.md) has
the measured throughput behind those decisions.

## What the launcher does

`run-claude-sandbox.sh`:

- **Builds the image** if missing (or on `--rebuild`), matching the sandbox
  user's uid/gid to the host so bind-mounted files stay writable from both
  sides (important on WSL).
- **Attaches to `sandbox-net`** — the internal network owned by the compose
  infra — and **discovers the egress proxy** on it, pointing the sandbox's
  `HTTPS_PROXY` at it and allowlisting it in the firewall. If the infra isn't up
  it falls back to creating a plain bridge for standalone use. You can run
  **several sandboxes** against one proxy (unique names; override `SANDBOX_NAME`).
- **Mounts your workspace** at `/workspace` (read-write) and a named config
  volume at `/config` (isolated from the host's `~/.claude`).
- **Mounts your plugin marketplaces** at `/marketplaces` (**read-only**) when
  `~/.config/dockade/marketplaces` exists, and registers every one of them at
  boot — see *Plugins and marketplaces* below.
- **Forwards your host git identity** into the container (it is not baked into
  the image).
- **Runs the agent** as non-root — the container starts as root only to arm the
  firewall and materialize config, then drops to the `sandbox` user via gosu —
  with `--cap-drop=ALL` plus only the capabilities that root setup needs,
  `--security-opt no-new-privileges`, and memory/CPU/pids limits.

There is deliberately **no flag to disable the firewall** — a yolo agent with
open egress is exactly the state this sandbox exists to prevent.

## How containment works (v1)

| Layer | Mechanism |
|-------|-----------|
| **Network egress** | With the infra up, `sandbox-net` is `internal: true` — the sandbox has **no route to the internet at all**; the only path off-box is the **egress proxy**, which reaches the internet on a separate `egress-net`. The proxy enforces a **domain**-level allowlist with per-connection audit (closing the shared-CDN/fronting gap that an IP-level rule can't). The in-container firewall (`init-firewall.sh`) is now **defense-in-depth**: in governed mode it permits only the proxy, the tool gateway and embedded DNS, so even if it failed there's no route out. IPv6 fully denied. Without the infra, the launcher falls back to **standalone** mode (non-internal net, direct `ipset` IP-allowlist) for proxy-less use. |
| **Privilege** | Non-root `sandbox` user; `--cap-drop=ALL` + minimal adds; `no-new-privileges`; no host Docker socket. |
| **Filesystem** | Only the bind-mounted `/workspace` and the `/config` volume are *persistent* writable state (the rest of the container filesystem is writable but ephemeral). |
| **Config** | `CLAUDE_CONFIG_DIR=/config`; user settings are re-materialized from a baked template on every boot, so config always matches the repo and volume wipes lose only credentials/runtime state. |

**Not a containment boundary:** no Claude Code settings file. Under
organization authentication, Claude Code loads the org's *remote* managed
settings and ignores any local managed file, so hard policy belongs in the org
admin console — not in this repo. User-scope `settings.json` is used only for
mistake-prevention/steering. See [`DESIGN.md`](DESIGN.md) and
[`CLAUDE.md`](CLAUDE.md) for the full reasoning.

**Known blind spot:** `WebSearch` runs server-side on Anthropic infrastructure,
so the firewall cannot see or block it. It's read-only and consciously left
enabled in v1; details in [`DESIGN.md`](DESIGN.md).

**Verifying the boundary:** run `boundary-check.sh` inside the container (as the
agent) for an on-demand pass/fail check of the invariants — arbitrary egress
blocked, IPv6 blocked, the control plane unreachable on any of its internal
networks, agent holds no capabilities, `no_new_privs` set, no Docker socket, plus a
set of attempts to abuse the egress proxy (non-443 CONNECT, SNI fronting, relaying
to a control network by name, by IP and by IPv4-mapped IPv6, relaying to the
metadata IP, and plaintext HTTP going through the proxy rather than around it). It
prints a pass/fail line each and an aggregate; run it rather than counting them
here.
It is **tier-aware**: tier 1 asserts Anthropic is reachable *via the proxy*, tier 2
asserts it is unreachable and that the inference service is the one destination that
answers. It exits non-zero on any violation, so it doubles as a regression baseline
to run before and after changes — `make boundary` runs it in a live sandbox. This is
separate from the boot-time checks, which run as root before the privilege drop and
only warn.

## Yolo mode

Bypass-permissions mode is available via the `claude-yolo` alias but is **never
forced** — starting in it is a conscious opt-in. The image pre-accepts the
bypass-mode disclaimer (in the baked user settings) so the acceptance survives
restarts and volume wipes; it does not start Claude in yolo automatically.

## Plugins and marketplaces

The sandbox has no governed git path, so it cannot clone a marketplace itself.
Instead **you** clone marketplace repos on the host and the sandbox reads them:

```bash
mkdir -p ~/.config/dockade/marketplaces
git clone https://github.com/some/marketplace ~/.config/dockade/marketplaces/some-marketplace
printf 'a-plugin@some-marketplace\n' >> ~/.config/dockade/plugins
./run-claude-sandbox.sh          # mounts and registers them; no flag needed
```

Claude Code takes a marketplace from a local **directory** and uses it in place —
no clone, no copy, no egress, no credential — so the mount is **read-only**, and
that is deliberate: a writable plugin tree is a channel for the agent to edit a
skill or hook that lands in its own context, or executes, on the next boot.
Updating a marketplace is a `git pull` on the host.

Registration is **re-derived on every boot** from what is mounted, so deleting a
checkout removes it with no stale state. Enabling is separate — a marketplace
only makes plugins *installable* — and must be declared host-side too, because
the sandbox's `settings.json` is re-materialized from the image on every boot: a
`/plugin install` inside a session does not survive a restart.

| Path | Holds |
|---|---|
| `~/.config/dockade/marketplaces/` | marketplace checkouts (or one checkout directly) |
| `~/.config/dockade/plugins` | `plugin@marketplace` ids to enable, one per line, `#` comments ok |
| `~/.config/dockade/secrets/` | MCP client credentials (`MCP_SECRETS`) |

Durable per-machine config lives **outside this repo** on purpose: a sandbox
pointed at dockade as its workspace bind-mounts this tree read-write, so anything
configured from inside it is agent-writable — and what decides which code loads
into the agent must not be. `XDG_CONFIG_HOME` is honoured; `DOCKADE_CONFIG_HOME`
overrides the lot. Per-launch overrides: `SANDBOX_MARKETPLACES_DIR` (host path;
when set explicitly, a missing directory is a launch failure rather than a shrug)
and `SANDBOX_PLUGINS` (the id list, comma- or space-separated).

## Layout

```
dockade/
  README.md                 # this file
  CONTRIBUTING.md           # how to build, test and submit a change
  CLAUDE.md                 # invariants + conventions for working in the repo
  DESIGN.md                 # architecture, topology, and rationale (read this)
  NOTES.md                  # lab notebook: measurements, hardware behaviour, dead ends
  SECURITY.md               # how to report a boundary bypass; the accepted-risk ceiling
  LICENSE, NOTICE           # Apache-2.0
  Makefile                  # task entry points (make claude / make opencode / make check)
  ruff.toml, .yamllint, .hadolint.yaml, .shellcheckrc   # pinned linter configs (make check)
  .github/workflows/        # CI: lint + consistency + tests + image builds (make check)
  docker-compose.yml        # shared infra: egress proxy + control plane + UI + local LLM
  mcp-servers.yml           # MCP server catalogue (one container + profile per server)
  run-claude-sandbox.sh     # tier 1: build + launch a Claude sandbox (one or many)
  run-opencode-sandbox.sh   # tier 2: build + launch an opencode/local-LLM sandbox
  sandbox-lib.sh            # launcher plumbing shared by both tiers
  control-plane/            # governance authority BACKEND (agent cannot reach it)
    Dockerfile              #   FastAPI over SQLite; internal nets only, fully internal
    app.py                  #   the process: three listeners, which surface each
                            #   serves, the bind guard, boot, entry point
    api_authorize.py        #   POST /authorize — the egress proxy's question (authorize-net)
    api_tool.py             #   /tool/* — the gateway's bridge (tool-authorize-net)
    api_approvals.py        #   the queue and resolve — the endpoint that grants
    api_egress.py           #   standing egress rules, and leases
    api_mcp.py              #   MCP servers and tool rules
    api_views.py            #   the audit record, the UI's settings, /status — grants nothing
    provenance.py           #   who performed a privileged act, for the record
    store.py                #   SQLite — schema, the audit write, the policy seed
    audit.py                #   the read side: the folded glance, the paged record,
                            #   and the filters both share
    policy.py               #   what a rule pattern matches; peer address -> client
                            #   class; (host, class) -> allow/deny/hold
    holds.py                #   the in-process registry a held request blocks on
    inventory.py            #   what each server exposes, as the gateway last reported it
    ingest.py               #   drains the proxy's and the gateway's audit streams into
                            #   the store — rows and cursor advance in one transaction
    requirements.txt        #   pinned deps (fastapi, uvicorn)
    CLAUDE.md, DESIGN.md    #   how to work here; the approval flow's design
  control-plane-ui/         # UI FRONTEND — serves the UI + reverse-proxies the API
    Dockerfile              #   FastAPI + httpx; control-ui-net (loopback) + control-net
    app.py                  #   static UI at / + streaming reverse proxy to the backend
    app.js                  #   the approval console's behaviour — a file so the CSP can
                            #   say script-src 'self'; pure helpers unit-tested under node
    egress-rules.js         #   what creating, editing or revoking a standing rule is about
                            #   to do, said before the click
    holds.js                #   the pending queue's decisions: what a push changes, when a
                            #   card may go, how a countdown reads, how an arrival is announced
    payload.js              #   a tool ask's payload as the operator sees it — imported by app.js
    index.html              #   static shell + styles for the SSE approval console
    DESIGN.md               #   the browser boundary and the page's mechanics
    requirements.txt        #   pinned deps (fastapi, uvicorn, httpx)
  tool-gateway/             # MCP GATEWAY — governed tool capability; tier 1's `--mcp-config` points at it
    Dockerfile              #   FastAPI + uvicorn; triple-homed, no egress leg
    app.py                  #   the bind guard, liveness, and the agent's MCP endpoint
    protocol.py             #   the MCP wire: JSON-RPC in, JSON-RPC out, no I/O
    surface.py              #   which tools the agent is shown, and under what name
    execute.py              #   authorize, then run: the only thing here that calls a server
    discovery.py            #   dials each server, reconciles against policy, pushes the inventory
    outcomes.py             #   how each call ended — the gateway's own audit stream
    DESIGN.md               #   what the agent is shown, and what runs
    requirements.txt        #   pinned deps, held equal to the control plane's
  policies/                 # seed policy config (loaded into the control plane)
    egress-allowlist.txt    #   default-deny seed for the control plane's egress policy store
  proxies/                  # governed data-plane services (one dir per proxy)
    egress/                 # CONNECT-level egress proxy: control-plane client
      Dockerfile            #   mitmproxy + the policy/audit addon
      addon.py              #   per-connection /authorize + local audit stream
      DESIGN.md             #   the relay guard and the proxy's side of /authorize
  sandbox-common/           # ONE boundary implementation, shared by every tier
    entrypoint.sh           # root: firewall + config, then drops to non-root
    init-firewall.sh        # default-deny egress firewall (governed/local/standalone)
    boundary-check.sh       # on-demand smoke test of the containment boundary
    dotfiles/               # shared .bashrc / .gitconfig / .inputrc / .vimrc, baked into both tiers
  claude-sandbox/           # TIER 1 image — Claude, governed egress
    Dockerfile
    tier-setup.sh           # tier hook: materialize Claude user settings
    claude-wrapper.sh       # the `claude` on PATH: adds --mcp-config when a gateway was found
    user-settings.json      # baked template, materialized to /config each boot
    user-CLAUDE.md          # the agent's user-scope instructions, installed the same way
    statusline.sh           # sandbox-indicator status line
    dotfiles/               # .bashrc.tier — tier-1 shell hook (claude-yolo alias)
  opencode-sandbox/         # TIER 2 image — opencode + local LLM, no egress
    Dockerfile
    tier-setup.sh           # tier hook: materialize the opencode provider config
    opencode.json           # points opencode at the local `llm` service
    user-AGENTS.md          # tier-2 capability facts, materialized to the config dir
    DESIGN.md, NOTES.md     # tier 2 and the local LLM service: rationale, evidence
    dotfiles/               # .bashrc.tier — tier-2 shell hook (oc alias, distinct prompt)
  tests/                    # dependency-free unit tests for the governance logic (make check)
  models/                   # GGUF weights for the local LLM (gitignored)
```

## Roadmap

The target architecture (see [`DESIGN.md`](DESIGN.md)) is a multi-container setup
with the agent on an isolated network and all meaningful capability exposed through
governed data-plane services. The governance half is built: the egress proxy, the
control plane with its approval UI, and the MCP gateway. Still ahead is the
enablement half of the paved road — **skills and quality-gate hooks** in the image
and a **pull-through package cache** — beside the governed git path. Which step is
which, and what each one landed, is one table:
[`DESIGN.md` → "Status"](DESIGN.md#status).

## Documentation

- [`DESIGN.md`](DESIGN.md) — architecture, topology, threat model, and the
  rationale behind every decision. Start here to understand *why*.
- [`NOTES.md`](NOTES.md) — the lab notebook: measurements, hardware behaviour,
  and dead ends. Evidence for what `DESIGN.md` decides.
- [`opencode-sandbox/DESIGN.md`](opencode-sandbox/DESIGN.md) and
  [`opencode-sandbox/NOTES.md`](opencode-sandbox/NOTES.md) — the same pair for tier 2
  and the local LLM service, the one consumer of each.
- [`control-plane/DESIGN.md`](control-plane/DESIGN.md) — the control plane's half of the
  approval flow: how a card grants, holds under load, the audit views, standing policy,
  and why the tool surface gets tables of its own but shares the queue.
- [`control-plane-ui/DESIGN.md`](control-plane-ui/DESIGN.md) — the approval page: its
  browser boundary, how it keeps a failing state visible, how it shows a tool payload,
  and its tests.
- [`proxies/egress/DESIGN.md`](proxies/egress/DESIGN.md) — the egress proxy: what its
  relay guard refuses before policy is asked, and how it asks the control plane.
- [`tool-gateway/DESIGN.md`](tool-gateway/DESIGN.md) — the MCP gateway: which tools the
  agent is shown and under what name, what must hold before a call runs, and the rule
  for tools of its own.
- [`CLAUDE.md`](CLAUDE.md) — the invariants that must never be violated and the
  conventions for working in this repo.
- [`SECURITY.md`](SECURITY.md) — how to report a boundary bypass, and — worth
  reading first — which weaknesses are already known, both the ones consciously
  accepted and the ones still open.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to run the gate, what the tests and
  commit messages are expected to look like, and the licensing position (no CLA).

## License

Licensed under the [Apache License 2.0](LICENSE) — use, modify and redistribute
freely, including commercially, keeping the copyright and license notices. Source
files carry `SPDX-License-Identifier: Apache-2.0` so automated license scanners can
read them without parsing this file.

**Read the warranty disclaimer as meaning it.** This is a security boundary with a
documented ceiling, not a guarantee: [`DESIGN.md`](DESIGN.md) is explicit about what
containment here does *not* cover — notably that the read-write workspace bind mount
is a delayed path to host execution, that `WebSearch` runs server-side and cannot be
seen by the proxy, and that no Claude Code settings file is an enforcement boundary.
Understand those before trusting it with anything that matters.
