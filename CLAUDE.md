# dockade

Docker environment for running an AI coding agent (Claude Code) in a
capability-limited sandbox that lets it do strong work in a controlled,
efficient, high-quality way. Two goals: **governance** (every consequential
action goes through an auditable choke point) and **enablement** (the agent
image and skills encode a paved road so good work patterns happen by default).

**Read `DESIGN.md` for the architecture, topology, and rationale.** This file is
only the invariants and conventions for working in the repo.

## Invariants — never violate

- **Never give the sandbox a direct path to anything** — especially not network
  egress or the control plane. All capability goes through the data plane.
- **The agent must never reach the control plane.** Enforced by network
  segmentation (`sandbox-net` vs `control-net`); don't attach the sandbox to
  `control-net` or bridge them.
- **No ungoverned egress, ever.** Ungoverned tools stay on `sandbox-net` with no
  outbound route.
- **Default-deny for governed capabilities.** Unknown → held for human approval.
- **Sandbox holds only credentials it must, scoped to read-only/self-use.**
  Anthropic session credentials (from Claude subscription login, persisted in the
  config volume — not an injected API key) and low-risk read-only tokens may live
  in the sandbox.
  Write-capable / high-impact creds (e.g. git push token) stay governed behind
  a proxy — never in the sandbox.
- **The boundary is capability, not configuration.** Enforcement lives in what the
  container exposes and what egress it permits — the firewall, the non-root user,
  dropped caps, no control-plane route. **No Claude Code settings file is a
  containment boundary.** In particular a root-owned local `managed-settings.json`
  is *not* an enforcement lever here: under org auth Claude Code loads the org's
  *remote* managed source as the sole managed tier and ignores the local file
  entirely (verified — see DESIGN.md). Hard policy that must not be bypassable
  belongs in the **org admin console**, not a repo-delivered file.
- **`settings.json` is mistake-prevention, not a security control.** User-scope
  settings (e.g. a `permissions.deny`) usefully stop *accidents* and steer the
  agent onto the paved road, but the yolo agent can edit them or relaunch around
  them, so they never count against *malicious* intent. Use them freely for
  ergonomics; never rely on them for containment. (Notably WebSearch runs
  server-side over api.anthropic.com, so neither the firewall/proxy nor a local
  settings deny can hard-block it — a `deny` only discourages accidental use;
  WebFetch is client-side and genuinely network-governed.)
- **Everything consequential is audited.** No governed path bypasses the log.

## Conventions

- Prefer simple, direct, auditable implementations. This is infrastructure —
  clarity over cleverness.
- New recurring workflow → make it a skill. Skills encode good practice, not
  just access.
- Don't commit or push unless asked.

### Where writing goes

Five places, and the test is **what would have to change for this to become
wrong.** Getting this wrong is how `DESIGN.md` grew a 616-line section called
"Build status" that was really the architecture rationale.

| Write it in | When | Rot risk |
|---|---|---|
| **code comment** | it explains the code it sits next to | none — moves with the code |
| **commit message** | how a specific bug was found and fixed | none — dated, immutable |
| **`DESIGN.md`** | a decision or invariant that constrains *this* repo | rots if it restates the other four |
| **`NOTES.md`** | evidence: measurements, environment behaviour, dead ends | low — facts about the world |
| **`CLAUDE.md`** | an invariant or convention to follow *while working* | rots fastest — keep it lean |

Consequences worth stating, because each was learned by violating it:

- **A decision goes in `DESIGN.md`; the evidence for it goes in `NOTES.md`.** If a
  paragraph would still be true in someone else's repo on the same hardware, it is
  evidence. If changing it means changing code here, it is a decision.
- **Never narrate an incident in `DESIGN.md`.** The commit that fixed it already
  says it, in more detail, with a date. `DESIGN.md` keeps the resulting invariant
  and nothing else — one or two sentences.
- **Don't restate a code comment.** Reference the file. The comment is closer to
  the code and cannot drift from it; a copy in a document silently can.
- **`DESIGN.md` earns its length only on reasoning that spans files** — "these
  four guards together close clickjacking, none alone does" has no home in any
  single file, which is exactly why it belongs there.
- **No status logs, no DONE lists, anywhere but `## Status`.** They only grow. One
  table that tracks sequence, so nothing else needs editing when a step lands.
- **Numbers in prose rot.** Don't restate a test count or a version a command
  reports; name the command.
