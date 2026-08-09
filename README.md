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

> **Status: multi-container, control plane step 2b.** The sandbox lives on an
> internal network with **no direct egress**; a governed **egress proxy** is the
> sole path off-box, and it defers every decision to a **control plane** the
> agent cannot reach (policy + audit in SQLite). An unknown host is **held for
> approval** — a human approves/rejects it in a live UI (backend fully internal;
> a separate `control-plane-ui` frontend carries the loopback UI). The backend's
> API surface is **split across two internal networks**, so the egress proxy can
> ask `/authorize` and cannot reach the approvals API at all. There are now
> **two sandbox tiers** sharing one boundary implementation: tier 1 (Claude,
> governed egress) and tier 2 (opencode against a local LLM, no egress and no
> credentials). Still to come per [`DESIGN.md`](DESIGN.md): audit browsing beyond
> the UI's recent-decisions table (2c) and the git/cache data-plane services. See
> [Roadmap](#roadmap).

## Quickstart

**Prerequisites:** Docker, and a git identity on the host
(`git config --global user.name` / `user.email`) if you want to commit from
inside the sandbox.

```bash
# 1. Bring up the shared infrastructure once (egress proxy + control plane + UI).
#    Sandboxes route their traffic through it and it audits every connection.
docker compose -f /path/to/dockade/docker-compose.yml up -d --build

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
`SANDBOX_NAME`). Audit trail: `docker compose logs -f egress-proxy` (or the
`dockade-egress-audit` volume).

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

See [`DESIGN.md`](DESIGN.md) → *Local inference* for the accelerator setup, the
tuning decisions, and why the LLM service is ungoverned (it has no egress of its own
to govern); [`NOTES.md`](NOTES.md) has the measured throughput behind those decisions.

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
| **Network egress** | With the infra up, `sandbox-net` is `internal: true` — the sandbox has **no route to the internet at all**; the only path off-box is the **egress proxy**, which reaches the internet on a separate `egress-net`. The proxy enforces a **domain**-level allowlist with per-connection audit (closing the shared-CDN/fronting gap that an IP-level rule can't). The in-container firewall (`init-firewall.sh`) is now **defense-in-depth**: in governed mode it permits only the proxy + embedded DNS, so even if it failed there's no route out. IPv6 fully denied. Without the infra, the launcher falls back to **standalone** mode (non-internal net, direct `ipset` IP-allowlist) for proxy-less use. |
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
blocked, IPv6 blocked, the control plane unreachable on either of its internal
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
  run-claude-sandbox.sh     # tier 1: build + launch a Claude sandbox (one or many)
  run-opencode-sandbox.sh   # tier 2: build + launch an opencode/local-LLM sandbox
  sandbox-lib.sh            # launcher plumbing shared by both tiers
  control-plane/            # governance authority BACKEND (agent cannot reach it)
    Dockerfile              #   FastAPI over SQLite; two internal nets, fully internal
    app.py                  #   /authorize on authorize-net; the management API
                            #   (approvals, resolve, the read-only views) on control-net
                            #   + ingest of the proxy's locally-decided audit lines
    requirements.txt        #   pinned deps (fastapi, uvicorn)
  control-plane-ui/         # UI FRONTEND — serves the UI + reverse-proxies the API
    Dockerfile              #   FastAPI + httpx; control-ui-net (loopback) + control-net
    app.py                  #   static UI at / + streaming reverse proxy to the backend
    app.js                  #   the approval console's behaviour — a file so the CSP can
                            #   say script-src 'self'; pure helpers unit-tested under node
    index.html              #   static shell + styles for the SSE approval console
    requirements.txt        #   pinned deps (fastapi, uvicorn, httpx)
  policies/                 # seed policy config (loaded into the control plane)
    egress-allowlist.txt    #   default-deny seed for the control plane's egress policy store
  proxies/                  # governed data-plane services (one dir per proxy)
    egress/                 # CONNECT-level egress proxy: control-plane client
      Dockerfile            #   mitmproxy + the policy/audit addon
      addon.py              #   per-connection /authorize + local audit stream
  sandbox-common/           # ONE boundary implementation, shared by every tier
    entrypoint.sh           # root: firewall + config, then drops to non-root
    init-firewall.sh        # default-deny egress firewall (governed/local/standalone)
    boundary-check.sh       # on-demand smoke test of the containment boundary
    dotfiles/               # shared .bashrc / .gitconfig / .inputrc / .vimrc, baked into both tiers
  claude-sandbox/           # TIER 1 image — Claude, governed egress
    Dockerfile
    tier-setup.sh           # tier hook: materialize Claude user settings
    user-settings.json      # baked template, materialized to /config each boot
    statusline.sh           # sandbox-indicator status line
    dotfiles/               # .bashrc.tier — tier-1 shell hook (claude-yolo alias)
  opencode-sandbox/         # TIER 2 image — opencode + local LLM, no egress
    Dockerfile
    tier-setup.sh           # tier hook: materialize the opencode provider config
    opencode.json           # points opencode at the local `llm` service
    dotfiles/               # .bashrc.tier — tier-2 shell hook (oc alias, distinct prompt)
  tests/                    # dependency-free unit tests for the governance logic (make check)
  models/                   # GGUF weights for the local LLM (gitignored)
```

## Roadmap

The target architecture (see [`DESIGN.md`](DESIGN.md)) is a multi-container
setup with the agent on an isolated network and all meaningful capability
exposed through governed data-plane services. Next steps toward it:

1. **Egress HTTP(S) proxy** — *done* (`docker-compose.yml` + `proxies/egress/`):
   a CONNECT-level domain allowlist with per-connection audit, and (step 1) the
   **sole** egress path — `sandbox-net` is `internal: true` with the proxy's
   internet leg on `egress-net`.
2. **Control plane** — *step 2a done* (`control-plane/`): a FastAPI + SQLite
   governance authority the **agent cannot reach** — it lives only on internal
   networks the sandbox is not attached to. The UI is reachable from the host
   browser at `http://localhost:28090` (the default `DOCKADE_UI_PORT`). The egress proxy asks it
   `POST /authorize` per connection — one call that both decides policy and
   records audit — with the Anthropic lifeline allowed locally so a control-plane
   outage never bricks the agent, and everything else failing closed.
   **Step 2b done:** an unknown host is **held for approval** — the request blocks
   while a human approves/rejects it in a live SSE UI (allow/deny, once or
   persist-as-rule), defaulting to deny after a timeout (2b-1). The card counts
   down to that default-deny, and a persist names the rule it will write and asks
   twice, with exact-host-vs-subdomain-wildcard chosen by the operator from a set
   the backend derives and validates. The UI is a distinct `control-plane-ui`
   frontend on `control-net`; the backend is fully internal and reachable only
   through it (2b-2). **2b-3:** the backend's API surface is split across two
   internal networks — the egress proxy is on `authorize-net` and can reach
   `POST /authorize` and nothing else, while the management API (approvals,
   `resolve`, the read-only views) is served on a separate port bound to the
   `control-net` address alone. The proxy's relay guard is best-effort against DNS
   rebinding, so the design assumes it can be beaten and makes the far side worth
   little: even a total bypass yields a policy *query*, never a self-approval.
   Next: **2c** audit browsing (filter/search/history beyond the recent-decisions
   table already in the UI) + per-proxy config.
3. **Skills + quality-gate hooks** in the image — the enablement half of the
   paved road.
4. **Pull-through package cache** — fast, governed dependency installs.

## Documentation

- [`DESIGN.md`](DESIGN.md) — architecture, topology, threat model, and the
  rationale behind every decision. Start here to understand *why*.
- [`NOTES.md`](NOTES.md) — the lab notebook: measurements, hardware behaviour,
  and dead ends. Evidence for what `DESIGN.md` decides.
- [`CLAUDE.md`](CLAUDE.md) — the invariants that must never be violated and the
  conventions for working in this repo.
- [`SECURITY.md`](SECURITY.md) — how to report a boundary bypass, and — worth
  reading first — which weaknesses are already known and consciously accepted.
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
