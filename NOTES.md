# Notes

Evidence, measurements, and environment behaviour — the **lab notebook** behind
`DESIGN.md`, split out so that document stays about *this* codebase.

The split rule: **DESIGN.md states the decision and the invariant; NOTES.md holds the
measurement or observation that justified it.** If a paragraph would still be true in
somebody else's repo on the same hardware, it belongs here. If changing it would mean
changing code in this repo, it belongs in DESIGN.md. Neither file carries the
blow-by-blow of how a bug was found — that lives in the commit that fixed it, which is
dated and cannot drift.

Nothing here is load-bearing for a change to this repo. It is here so a number never has
to be re-measured and a dead end never has to be re-explored.

## Local inference on an Intel Arc 140V iGPU (Lunar Lake, WSL2)

Decisions these numbers produced are in DESIGN.md → "Local inference"; this is the
evidence.

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

### Operational constraints, learned the hard way

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

## Accelerator ecosystem survey

Why the compose profiles are shaped the way they are — the design is in DESIGN.md →
"Accelerator independence"; this is the hardware reasoning behind it.

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

**Intel under WSL2 specifically.** There is no `/dev/dri` render node; the GPU is a
paravirtual D3D12 device reached through `libdxcore.so` from the `/usr/lib/wsl:ro`
mount, which is why the Intel profile declares `--device /dev/dxg` plus that mount.
Intel's native Vulkan driver (ANV) **cannot** bind under WSL, so Vulkan there would run
through Mesa's Dozen shim over D3D12 — published Arc-on-Linux Vulkan benchmarks do not
transfer, and SYCL is the path.
