# Notes — tier 2 and local inference

The evidence behind `opencode-sandbox/DESIGN.md`, kept beside it because tier 2 is
the only consumer of the local model. The split rule is the root `NOTES.md`'s: a
measurement or observation lives here, and the decision it produced lives in the
design.

## Local inference on an Intel Arc 140V iGPU (Lunar Lake, WSL2)

Decisions these numbers produced are in `opencode-sandbox/DESIGN.md`; this is the
evidence.

**Every figure below comes from one host** — an Intel Arc 140V (Lunar Lake, Xe2) iGPU
with ~16.9 GB of shared memory, under WSL2 on Windows, with Docker installed natively in
WSL rather than Docker Desktop. That is n=1, so treat these as calibration for this class
of hardware rather than as benchmarks, and add a second host as further rows rather than
as a correction to these.

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
- **The context floor is the consumer, not the hardware.** 8k is not merely tight but
  unusable for an agent harness — opencode's base prompt (system + tool schemas)
  exceeds it before the first user turn, measured at **~8.6k tokens**, so 26% of a
  32k window is gone before the agent does anything. The *upper* bound is not memory —
  see "Measuring shared-memory use" below. Avoid `-c 0` (load from model), which would
  size the allocation from the model's native window.
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

### Measuring shared-memory use

There is no instrument on the Linux side. `xpu-smi` and `intel_gpu_top` want a
`/dev/dri` node and i915 debugfs, neither of which exists under WSL2; sysman is absent
(above), so llama.cpp cannot report free memory; and this build prints **no buffer-size
lines at all** — between `load_model: loading model` and `model loaded` the log carries
nothing but the sysman warning repeated eleven times, at every `-c` tried. What works is
a host-side Windows counter, read on the Arc's own adapter instance (the other instances
are the Basic Render Driver and never move):

    Get-Counter '\GPU Adapter Memory(*)\Shared Usage'

Qwen3.5-9B-Q4_K_M, 5.29 GiB on disk, `--parallel 1`, sampled *after* `/health` returns
200:

| `-c` | shared usage | above idle | cold load |
| --- | --- | --- | --- |
| — (idle) | 1.27 GiB | — | — |
| 32768 | 7.51 GiB | 6.24 GiB | 87.9 s |
| 49152 | 8.70 GiB | 7.43 GiB | 162.9 s |

**Sample only after the health check passes**, or the number is meaningless: mid-load
readings sit ~90–110 MiB above idle, because the weights reach the device late.

**Marginal KV cost is 78 KB/token** (1.19 GiB for 16,384 tokens), which differences out
the weights, the compute buffers and the driver's baseline, and is the figure to use for
"can I afford more window". Treat the per-token total as bracketed rather than pinned:
at 78 KB/token the KV for 32768 tokens would already exceed the whole 6.24 GiB measured
at that setting, so the two rows do not reconcile under a single linear model and the
32k row is the suspect one. Somewhere between 50 and 78 KB/token, and a third data point
would settle it.

**Cold-load time grows with `-c`** — 75 s for 1.19 GiB, so roughly **63 s per GiB**
allocated, presumably the driver committing and zeroing shared memory through the
paravirtual D3D12 path. This is what makes the ceiling a liveness problem:

- **Memory** would allow ~125k tokens (pool, less idle, less weights, at 78 KB/token).
- **The healthcheck's 300 s `start_period`** is exceeded around ~78k, and past that the
  server works while reporting unhealthy — which `run-opencode-sandbox.sh` treats as
  fatal. So `start_period` and `-c` have to move together.

The binding constraint is therefore the health gate, at roughly 60% of the memory
ceiling. The earlier reading here — that 32768 fits and 64k does not — was two health
checks taken about two minutes into what is a nearly four-minute load; 49152 loads and
serves, and nothing was ever short of memory.

### Tier 2 end to end, measured

One `opencode run` turn in a live tier-2 sandbox — write a file, read it back, run
`wc -c` on it — with no egress. It completed, and the file was correct on disk.

| Phase | Tokens | Time |
| --- | --- | --- |
| Session/title request | 574 prompt | 4.9 s prefill |
| First turn (base prompt + task) | 8635 prompt, 44 out | 36 s prefill (~240 tok/s), 13 tok/s decode |
| First turn, after the tool prune | 7138 prompt, 10 out | 30.9 s prefill (231 tok/s), 13.7 tok/s decode |
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

### What opencode's 8.6k base prompt is made of, and which parts are configurable

Read from opencode's source at the version the image installs. Sizes below are
**characters** of the prompt text on disk — `chars/4` is the rough bridge to tokens, and
the measured result is at the end of this section.

- **The system prompt is a quarter of it, not half.** `SystemPrompt.provider()` picks a
  prompt file by substring-matching the model id — `gpt`, `gemini-`, `claude`, `kimi`,
  `trinity`, `muse`. The id here is `local`, which matches nothing, so tier 2 gets
  `prompt/default.txt`: **8,528 chars**, call it ~2.1k tokens.
- **Tool descriptions are the largest part.** `tool/*.txt` plus `shell.txt` total
  **16,342 chars**, ~4.1k tokens, before the JSON parameter schemas that accompany them.
  The five with no capability behind them in a no-egress tier — webfetch, websearch,
  task, lsp, todowrite — are 7,403 of those characters.
- **An agent's `prompt` REPLACES the built-in rather than appending to it.**
  `session/llm/request.ts`: `input.agent.prompt ? [input.agent.prompt] :
  SystemPrompt.provider(input.model)`. Worth knowing before writing one.
- **A `false` in the top-level `tools` map removes the schema from the request**, via a
  deny rule that `Permission.visibleTools` applies while assembling it. But `write` and
  `patch` normalize to the `edit` permission, shared by `edit`, `write` and
  `apply_patch`, so those three can only be disabled together.
- **The config format is mid-migration and the two halves disagree on names.** What
  `opencode.json` validates against — the published schema its `$schema` pins — uses
  `agent`, `prompt`, `permission`, `disable`, `tools`. The internal v2 shape
  (`ConfigV2.Agent`) uses `agents`, `system`, `permissions`, `disabled`. A v2 key written
  into today's config is silently inert, which is the failure mode to expect from
  reading the TypeScript instead of the schema.
- **Instruction files: first match wins at each level, and they do not stack.** Global is
  `$XDG_CONFIG/opencode/AGENTS.md`, else `~/.claude/CLAUDE.md`. Project is `AGENTS.md`,
  else `CLAUDE.md`, else `CONTEXT.md`, walking up from the working directory. A global
  file and a project file both load; two candidates at the same level do not. The
  top-level `instructions` array adds more, resolving absolute paths, `~/`, globs, and
  http(s) URLs — the last fetched with a 5s timeout, so a URL entry in a no-egress tier
  is a guaranteed 5s stall.
- **opencode parses the file as JSONC**, so comments would load fine — but this repo's
  `make consistency` reads it with Python's `json.load`, which would not.

**Measured, after disabling the five and adding a global AGENTS.md.** First-turn prompt
fell from **8,635 to 7,138 tokens**, prefill from 36 s to 30.9 s. Same instrument
(llama-server's `prompt eval time` line), same hardware: 230.8 tok/s prefill against
~240, and decode 13.7 against 13, so the whole gain is tokens rather than speed. The
session/title request was unchanged at 553.

Two things that reading does *not* establish. The two changes were made in one step, so
the tool saving and the AGENTS.md cost are not separated — the prediction was ~1.85k out
and ~420 back in, which brackets the observed −1,497 but does not confirm either term.
And the image installs `opencode-ai@latest`, so the two readings are not guaranteed to be
the same opencode version; the base prompt can move upstream between them without
anything failing. A pinned version is what would make this a controlled comparison.

### It is reliable on presence and fabricates absence

Asked to review this repository and propose improvements, the local model produced a
document whose errors all pointed the same way. Everything it got right was a
*presence* claim about text it had actually read — its three "critical" security
findings were transcriptions of `SECURITY.md` → "Known open findings", down to the
remediation wording, including the one that section says restating is duplicate work.
Everything it got wrong was an *absence* claim about the parts it had not read: no type
hints (the modules are annotated), no contribution guidelines (`CONTRIBUTING.md`), no
logging config (the egress addon configures a rotating JSON logger), no health
endpoints, no compose profiles, no security headers (the UI already sends CSP,
`x-frame-options`, `nosniff` and `referrer-policy`). Every code snippet it offered
called an API that does not exist — three invented functions on `store.py`, which
defines five. Its own summary table miscounted three of four rows while the total came
out right, the same signature as the 2-byte file above.

The asymmetry is structural, not a prompting defect: asserting absence requires
exhaustive search, and a model that read part of a repo will generalise to the whole.
Three consequences for task design, the last of which decides what is worth offloading:

- **Never ask it what is missing.** Supply the evidence in the prompt and ask for a
  judgement about that evidence only.
- **A prose summary is an absence claim in disguise** — "here is what mattered" implies
  nothing else did, and the errors are omissions, which are invisible without reading
  the source. Reduce large input to a *pointer or a label* (line numbers, IDs, one of k
  classes) so the output can be checked against ground truth in seconds. Summaries are
  fine when their job is to send a human to the source, and unsafe when they replace it.
- **Acceptance test before offloading anything:** can the output be spot-checked more
  cheaply than producing it? Reviewing that document took longer than generating it did.

## Accelerator ecosystem survey

Why the compose profiles are shaped the way they are — the design is in
`opencode-sandbox/DESIGN.md` → "Accelerator independence"; this is the hardware
reasoning behind it.

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
