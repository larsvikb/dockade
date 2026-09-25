# Tier 2 — the local-model sandbox and its inference service

The design of tier 2 (this directory and `run-opencode-sandbox.sh`) and of the
inference service it is the only consumer of (the `llm-*` profiles in
`docker-compose.yml`). The evidence behind it is in `opencode-sandbox/NOTES.md`. What
tier 2 shares with tier 1 stays in the root: the two tiers side by side, the firewall
both run, and why tier 1 cannot reach the service, in `DESIGN.md` → "Local inference —
an ungoverned LLM tool".

**Status: built and verified on the Intel/WSL path; not reachable by tier 1 — it is
tier 2's sole destination (see "Built — the tier-2 local-model sandbox").**
`docker-compose.yml` defines `llm-intel`, `llm-nvidia` and `llm-vulkan`, each behind
a compose profile so none starts with a plain `docker compose up -d`. They serve
`llama-server` (an OpenAI-compatible API) on sandbox-net at `172.30.0.20:8080`.
Only `llm-intel` has been exercised on real hardware; `llm-vulkan` is designed but
unverified (see "Accelerator independence").

## Why it is ungoverned rather than governed

The taxonomy in `DESIGN.md` → "Data plane — ungoverned tools" requires an ungoverned
tool to have **no independent egress**. This one satisfies that by construction rather
than by policy, which is a stronger claim than the pull-through cache can make:

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

## The GPU goes to the service, not the sandbox

Passing a GPU device into the *sandbox* would breach "never give the sandbox a
direct path to anything" — it is a host device, and on the Intel/WSL path it also
requires bind-mounting the host's driver directory. Instead the device is granted
to the inference container, and the agent reaches inference the way it reaches
every other capability: as a service on the internal network. The sandbox keeps
zero direct device access.

## Accelerator independence

The three profiles are mutually exclusive and deliberately share both the
sandbox-net address and the network alias `llm`. So the consumer endpoint
(`http://llm:8080`) and any future firewall `/32` are identical whichever
accelerator the host has, and switching hardware is a profile flag rather than a
rewiring. Enabling two profiles at once is an address conflict, which is the
intended failure.

- **Intel (WSL2)** — `--device /dev/dxg` plus `/usr/lib/wsl:ro`, because there is no
  `/dev/dri` render node under WSL and the in-image Level Zero runtime reaches the
  paravirtual D3D12 device via `libdxcore.so` from that mount. SYCL is the path, not
  Vulkan (`opencode-sandbox/NOTES.md` explains why Vulkan cannot work there).
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
anything in this repo — see `opencode-sandbox/NOTES.md` → "Accelerator ecosystem
survey". ROCm stays a documented manual image swap for anyone on MI-series hardware.

## Tuning decisions (the evidence is in `opencode-sandbox/NOTES.md`)

The compose entrypoint carries a handful of `llama-server` flags that all exist for the
same reason: on this hardware the *silent* failure modes are the dangerous ones, so each
flag converts one into either a loud failure or a bounded cost. Measurements and the full
"learned the hard way" list are in `opencode-sandbox/NOTES.md` → "Local inference".

- **`--reasoning off`** — thinking defaults to on, and a trivial prompt once produced
  6,099 reasoning tokens over 7.4 minutes. Set **server-side**, not per-request, because
  a client that can disable it merely fixes itself while one that cannot (opencode) has
  no recourse. Overridable with `DOCKADE_LLM_REASONING=on`; if switched on, bound it
  with `--reasoning-budget N` rather than leaving it at `-1`.
- **`--parallel 1`** — the prompt cache is per-slot and slots are assigned LRU, so a
  multi-turn conversation could land on a slot that never saw it and re-prefill the
  whole history. Concurrency was never real anyway: two in-flight requests contend for
  the same GPU.
- **`--no-context-shift`** — the default silently discards the oldest tokens, which for
  an agent means evicting its system prompt and tool definitions mid-conversation.
  Presents as the model becoming inexplicably confused rather than as an error.
- **`-c 32768`, and the client's `limit.context` must be materially SMALLER** — a ratio
  guarded by `make consistency` (`CTX_HEADROOM`). The invariant is DIRECTIONAL: *the
  server's window exceeds what the client believes*, by enough to absorb the client's
  undercount. Equality was the original guard and is the wrong one — **a cross-component
  agreement check is only as good as its direction**, and two numbers matching is not
  the same as two components agreeing (the overshoots that showed it, and what the base
  prompt costs before the first turn, are in `opencode-sandbox/NOTES.md`). The ceiling
  on `-c` is not the memory pool but LIVENESS: cold-load time grows with it until the
  load outruns the healthcheck's `start_period`, at which point the service works,
  reports unhealthy, and the tier-2 launcher refuses to run in front of it — so those
  two knobs move together across `docker-compose.yml` and `run-opencode-sandbox.sh`. The
  rest of the sizing sits beside the flag in compose.
- **One model at a time**, and `temperature: 0` for extraction/classification.

Two properties worth carrying in the reader's head, because they shape task design more
than model choice does: decode is memory-bandwidth-bound while prefill is compute-bound
(a ~10x asymmetry, so this machine is good at prompt-heavy short-output work and bad at
long-form generation), and **a healthy container is not evidence of acceleration** —
llama.cpp falls back to CPU silently and the health check still returns 200.

## What the local model is for

The asymmetry above is a role constraint rather than a tuning detail: this model is fast
at reading and slow at writing, so its work is **large input, small output, with a human
or a deterministic gate on the far side.** Two shapes qualify. Offline questions, asked
in a session where every answer is read before it is acted on — which needs no governance
because nothing is produced and nothing leaves. And narrow reduction over local data:
select, classify, rank, deduplicate, or check a supplied claim against a supplied file,
where the output is a pointer or a label rather than prose.

What it is not for is agent work, or anything that becomes an artifact of record. Its
failure mode compounds the hardware's: it is reliable about text it was given and
fabricates about text it was not (`opencode-sandbox/NOTES.md`), so any output asserting
that something is *absent* — a prose summary included, since "here is what mattered"
implies nothing else did — cannot be trusted without reading the input it was supposed
to replace. The test a candidate task must pass is whether its output can be
spot-checked more cheaply than it can be produced.

Two things follow. Tier 2 is sized for a long conversation rather than an agent run,
because the harness fixed cost is paid once per session and the prompt cache carries the
rest. And the local model's real advantage is not that it is cheap — a frontier model
through the governed proxy is better and faster at nearly everything — but that it is
**confined**: it is the only inference here that can read data which must not leave the
host, and the only one that works with no egress at all. Candidate tasks should be chosen
for that property, not for token cost.

## Built — the tier-2 local-model sandbox

Built as `opencode-sandbox/` + `run-opencode-sandbox.sh`; launch with `make opencode`.
Its posture beside tier 1's, and the firewall modes both tiers share, are in
`DESIGN.md` → "Tier 2, and what it shares with tier 1".

**Its grant is defined by subtraction.** The launcher passes `SANDBOX_MODE=local`,
`LLM_IP`, `LLM_PORT` and git identity — and deliberately *no* proxy env, *no*
`UPSTREAM_DNS`, *no* `--dns`. Absence of capability is the mechanism; there is no
setting to get wrong. The launcher also refuses the standalone-network fallback
(`sc_ensure_network ... false`) — both creating a non-internal bridge and *adopting*
one a tier-1 standalone run left behind, since by name the two are the same network —
because either would hand a no-egress agent exactly the egress its design forbids.
It treats a missing `llm`
service as **fatal** rather than degraded: an opencode sandbox with no model is
broken, not reduced. `LLM_IP` is discovered from the running container rather than
hardcoded, so a compose subnet change cannot silently break the firewall's `/32`.

**Verified empirically, not asserted.** `boundary-check.sh` branches on mode and
*inverts* the Anthropic check — in LOCAL mode, reachability is a failure. From
inside a running tier-2 sandbox all checks pass, including the two that only this
tier can make:

- `api.anthropic.com unreachable (correct: LOCAL mode has no egress)`
- `inference service reachable (172.30.0.20:8080) — the one permitted destination`

alongside control-plane isolation, IPv6 denied, zero capabilities, `no_new_privs`,
and no Docker socket. This is the strongest containment evidence the repo has: an
agent that can reach precisely one thing, holds nothing, and proves both from its own
capability-less security context.

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

**The harness's prompt budget is trimmed by capability, not by taste.** On a 24k window
the base prompt is over a third of it and tool descriptions are its largest part
(decomposition in `opencode-sandbox/NOTES.md`), so `opencode.json` disables the tools
this tier has no capability behind — the rationale is in `tier-setup.sh`, beside the
list. The system prompt is deliberately left as upstream ships it. Replacing it is
supported and would save more, but this tier earns its keep partly as a harness we do
**not** control, and that property is protocol-level — a client that cannot send
`chat_template_kwargs`, per the paragraph below. Withholding tools that do not exist
here does not touch it; authoring the agent's own system prompt starts to.

**Why the server sets the reasoning default.** opencode cannot send
`chat_template_kwargs` per request, so with llama-server's default (`--reasoning
auto` → on) every turn paid the full thinking cost — the observed "somewhat slow"
behaviour. Fixed at the server (`--reasoning off`, see *Tuning decisions*),
which is the right layer: a per-request workaround only helps clients able to send
one, and this tier's whole point is hosting consumers that are not under our
control.
