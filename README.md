# dockade

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

> **Status: v1 — single container.** The full control-plane / data-plane
> architecture in [`DESIGN.md`](DESIGN.md) is the target, not yet the reality.
> What's built today is the sandbox image plus a launch script; egress is
> governed by an in-container default-deny firewall. See
> [Roadmap](#roadmap) for what's next.

## Quickstart

**Prerequisites:** Docker, and a git identity on the host
(`git config --global user.name` / `user.email`) if you want to commit from
inside the sandbox.

```bash
# From the directory you want the agent to work in:
/path/to/dockade/run-claude-sandbox.sh

# Or point it at a specific workspace:
/path/to/dockade/run-claude-sandbox.sh /path/to/project

# Rebuild the image (after changing the Dockerfile, or switching hosts):
/path/to/dockade/run-claude-sandbox.sh --rebuild
```

On first run the image builds and you'll be dropped into a shell in the
container. **Authenticate once** by starting Claude Code and completing the
interactive Claude subscription login — the credentials persist in a named
volume, so you won't need to log in again on later runs.

Then, inside the container:

```bash
claude          # normal, permission-prompting mode
claude-yolo     # bypass-permissions mode — a conscious opt-in (see below)
```

## What the launcher does

`run-claude-sandbox.sh`:

- **Builds the image** if missing (or on `--rebuild`), matching the sandbox
  user's uid/gid to the host so bind-mounted files stay writable from both
  sides (important on WSL).
- **Creates `sandbox-net`** — a user-defined bridge network (idempotently) — and
  attaches the container to it, instead of Docker's default bridge. This gives
  the container Docker's embedded DNS and isolation from other containers.
- **Mounts your workspace** at `/workspace` (read-write) and a named config
  volume at `/config` (isolated from the host's `~/.claude`).
- **Forwards your host git identity** into the container (it is not baked into
  the image).
- **Runs the container** as non-root, with `--cap-drop=ALL` plus only the few
  capabilities the firewall setup needs, `--security-opt no-new-privileges`, and
  memory/CPU/pids limits.

There is deliberately **no flag to disable the firewall** — a yolo agent with
open egress is exactly the state this sandbox exists to prevent.

## How containment works (v1)

| Layer | Mechanism |
|-------|-----------|
| **Network egress** | Default-deny `iptables` + `ipset` allowlist (`init-firewall.sh`), set up by the entrypoint as root before dropping privileges. Egress is permitted to the *IP ranges* of Anthropic API/auth, GitHub, and npm/PyPI; IPv6 is fully denied. **This is IP-level, not domain-level:** several of those hosts sit behind shared CDNs (Cloudflare/Fastly), so SNI/`Host`-fronting can still reach other origins on the same infrastructure. True domain-level egress control arrives with the egress proxy (see [Roadmap](#roadmap)). |
| **Privilege** | Non-root `sandbox` user; `--cap-drop=ALL` + minimal adds; `no-new-privileges`; no host Docker socket. |
| **Filesystem** | Only the bind-mounted `/workspace` and the `/config` volume are writable state. |
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
blocked, Anthropic reachable, IPv6 blocked, agent holds no capabilities,
`no_new_privs` set, no Docker socket. It exits non-zero on any violation, so it
doubles as a regression baseline to run before and after future changes (e.g. the
egress proxy). This is separate from the boot-time checks, which run as root
before the privilege drop and only warn.

## Yolo mode

Bypass-permissions mode is available via the `claude-yolo` alias but is **never
forced** — starting in it is a conscious opt-in. The image pre-accepts the
bypass-mode disclaimer (in the baked user settings) so the acceptance survives
restarts and volume wipes; it does not start Claude in yolo automatically.

## Layout

```
dockade/
  README.md                 # this file
  CLAUDE.md                 # invariants + conventions for working in the repo
  DESIGN.md                 # architecture, topology, and rationale (read this)
  run-claude-sandbox.sh     # build + launch the sandbox
  claude-sandbox/           # the agent image
    Dockerfile
    entrypoint.sh           # root: firewall + config, then drops to non-root
    init-firewall.sh        # default-deny egress firewall
    boundary-check.sh       # on-demand smoke test of the containment boundary
    user-settings.json      # baked template, materialized to /config each boot
    statusline.sh           # sandbox-indicator status line
    dotfiles/               # .bashrc (incl. claude-yolo), .gitconfig, ...
```

## Roadmap

The target architecture (see [`DESIGN.md`](DESIGN.md)) is a multi-container
setup with the agent on an isolated network and all meaningful capability
exposed through governed data-plane services. Next steps toward it:

1. **`docker-compose.yml` + network topology** — add `control-net` / `egress-net`
   alongside `sandbox-net`, and flip `sandbox-net` to `internal: true` once
   there's an egress path that isn't the direct firewall.
2. **Egress HTTP(S) proxy** — the central choke point: domain allow/block/hold +
   audit. Lets the transitional GitHub/npm/PyPI firewall entries be removed.
3. **Skills + quality-gate hooks** in the image — the enablement half of the
   paved road.
4. **Pull-through package cache** — fast, governed dependency installs.

## Documentation

- [`DESIGN.md`](DESIGN.md) — architecture, topology, threat model, and the
  rationale behind every decision. Start here to understand *why*.
- [`CLAUDE.md`](CLAUDE.md) — the invariants that must never be violated and the
  conventions for working in this repo.
</content>
</invoke>
