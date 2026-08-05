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
  turn — measured at **~8.6k tokens**, so 26% of a 32k window is gone before the
  agent does anything. Avoid `-c 0` (load from model), which would size the
  allocation from the model's native window.
- **A client told the true window still overshoots it.** This is the one that cost
  real agent runs. `-c 32768` on the server and `limit.context: 32768` in
  opencode.json agreed exactly, and the server still rejected three requests across
  a day's sessions: **40840, 37943 and 35980 tokens** against the 32768 window —
  up to 1.25x over. So the client's context accounting is approximate; plausibly it
  does not tokenize with the server's tokenizer, and tool output enters the
  conversation after the turn has been budgeted. **Equality is the wrong
  invariant** — it leaves the client no room to be wrong in the direction it is
  actually wrong in. Give the server headroom over what the client believes
  (`CTX_HEADROOM` in the Makefile) and the client compacts before the server has to
  refuse. Free, too: the server's KV allocation follows `-c`, which does not move.
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
  model becoming inexplicably confused rather than as an error. **Vindicated by the
  three overflows above**: each produced `send_error ... exceeds the available
  context size`, a cancelled task, and then a *smaller* follow-up request from
  opencode (40840 rejected, next request 4549) — it compacted and carried on. Loud
  and recoverable, which is the whole argument for the flag.
- **`ZES_ENABLE_SYSMAN=1` does nothing under WSL2.** The var is set in the compose
  service, and the log still prints `ext_intel_free_memory is not supported
  (export/set ZES_ENABLE_SYSMAN=1 to support), use total memory as free memory` on
  every boot — there is no sysman interface on the paravirtual D3D12 device to
  enable. Consequence: llama.cpp plans allocations against **total** shared memory
  as though all of it were free, so it cannot warn about an overshoot. That is why
  `-c` is hand-sized from measurement rather than trusted to fit. The var is kept
  because it is correct on the native-Linux paths.
- **The CORS / no-API-key warning in the log is not a finding here.** llama-server
  warns that it allows all origins with no key. There is no browser origin and no
  authenticated surface: sandbox-net is `internal: true`, the service publishes no
  port, and its only client is tier 2, whose firewall permits exactly this one
  destination. Adding a key would protect nothing that reachability does not
  already protect.

### Tier 2 end to end, measured

One `opencode run` turn in a live tier-2 sandbox — write a file, read it back, run
`wc -c` on it — with no egress. It completed, and the file was correct on disk.

| Phase | Tokens | Time |
| --- | --- | --- |
| Session/title request | 574 prompt | 4.9 s prefill |
| First turn (base prompt + task) | 8635 prompt, 44 out | 36 s prefill (~240 tok/s), 13 tok/s decode |
| After the write-tool result | 23 new | 3.7 s |
| After the bash-tool result | 27 new | 2.5 s |

Two things worth keeping. **The first turn is ~40 s and almost all of it is
prefilling opencode's own base prompt**, not doing the work — so on this hardware
the fixed cost of the harness dominates any short task, and a long session
amortises far better than several short ones. **`--parallel 1` demonstrably paid
off**: the tool-result turns reported `f_sim_best = 0.997` prefix-cache reuse and
came back in seconds instead of re-prefilling 8.6k tokens.

And a caution about the output rather than the plumbing: the model reported "2 bytes
(the word OK plus the newline character)" for a file that is `OK` with **no**
newline. The tool calls were right and the arithmetic narrating them was wrong,
which is the same lesson as constrained decoding above — verify the artifact, not
the prose about it.

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
