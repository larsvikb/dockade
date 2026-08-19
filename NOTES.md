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

## SQLite: `INSERT OR IGNORE … ON CONFLICT DO UPDATE` still updates

The `OR IGNORE` is inert when an `ON CONFLICT` clause names the same constraint — the
upsert wins:

```python
c.execute("CREATE TABLE t(path TEXT PRIMARY KEY, v INT)")
c.execute("INSERT INTO t VALUES ('a', 1)")
c.execute("INSERT OR IGNORE INTO t(path, v) VALUES ('a', 2) "
          "ON CONFLICT(path) DO UPDATE SET v=excluded.v")
c.execute("SELECT v FROM t").fetchone()      # -> (2,)   not (1,)
```

Documented behaviour ("the upsert … takes precedence"), but it reads like belt-and-
braces and is not. Worth knowing in both directions: a stray `OR IGNORE` in front of an
upsert is harmless rather than a silent no-write bug — and, in the other direction, an
`OR IGNORE` added *as* a mutation to test whether something advances is not a mutation
at all. One of these was written as a mutation-testing case and reported SURVIVED before
it turned out to be a no-op.

## Python's `sqlite3` leaves DDL outside its implicit transaction

SQLite itself has transactional DDL — a `DROP TABLE` inside a transaction is undone by
a rollback. Python's driver in legacy mode does not put it in one: it opens an implicit
transaction before `INSERT`/`UPDATE`/`DELETE`/`REPLACE` and nothing else, so a `DROP`
issued on a default connection runs in autocommit and is already permanent by the time
anything can roll it back.

```python
c = sqlite3.connect(":memory:")
c.execute("CREATE TABLE t(x)"); c.execute("INSERT INTO t VALUES (1)"); c.commit()
c.execute("DROP TABLE t")
c.in_transaction        # -> False        (no transaction was ever opened)
c.rollback()
c.execute("SELECT * FROM t")               # -> OperationalError: no such table: t
```

Take control explicitly and the engine behaves as advertised:

```python
c.isolation_level = None                   # autocommit; we issue the statements
c.execute("BEGIN IMMEDIATE")
c.execute("DROP TABLE t")
c.in_transaction        # -> True
c.execute("ROLLBACK")
c.execute("SELECT * FROM t").fetchall()     # -> [(1,)]   the table is back
```

Measured on CPython 3.13.5 / SQLite 3.46.1. This is the documented legacy
`isolation_level` behaviour rather than a version quirk, so it applies to the
`python:3.12-slim` the control-plane image is built on — but only the 3.13 figure above
was actually run.

The consequence is entirely about **table-rebuild migrations**, which are the only
shape here that mixes DML and DDL in one operation: copy the rows aside, drop the
original, rename. On a default connection those three are three separate autocommits,
so a failure between the copy and the drop is not undone — on a long-lived store that
is the policy rules gone. Nothing warns; the code reads as if it were atomic.

An additive `ALTER TABLE ADD COLUMN` needs none of this, which is why the distinction
is worth writing down rather than "always wrap migrations": the cheap shape is safe on
its own and the expensive shape silently is not.

On ext4/overlayfs, `os.remove(p)` followed immediately by recreating `p` typically
reuses the just-freed inode number, so `st_ino` is unchanged. Any rotation detection
keyed on the inode therefore cannot be tested by delete-then-recreate — the test passes
or fails on allocator luck. Write to a sibling path and `os.replace()` it over the
target instead: the new file's inode is allocated while the old one is still linked, so
it is guaranteed to differ.

## Claude Code reads a managed `CLAUDE.md` even where it ignores managed settings

The two managed tiers do not behave alike, and the asymmetry is easy to walk into.

Under org auth, a local `managed-settings.json` is ignored entirely — the org's
remote managed source is the sole managed *settings* tier (verified separately; the
consequence for this repo is in DESIGN.md). A local managed **`CLAUDE.md`** is not
ignored. Dropping one in `/etc/claude-code` and running `/context` lists it:

| Type | Path |
| --- | --- |
| Managed | `/etc/claude-code/CLAUDE.md` |
| User | `$CLAUDE_CONFIG_DIR/CLAUDE.md` |
| Project | `<repo>/CLAUDE.md` |

So `/etc/claude-code` being the natural place to bake a *template* collides with it
also being a place Claude Code *looks*: the baked copy and the installed copy both
load, and identical text enters context twice. `user-settings.json` never had this
problem only because settings are not discovered by that filename.

Two things follow. Any file baked into a managed directory as a source-of-truth
copy needs a name Claude Code does not look for — `.template` is enough. And
"managed settings are inert here" must not be generalised to "managed anything is
inert here"; the memory tier is live.

Also confirmed while establishing this: with `CLAUDE_CONFIG_DIR` set, user-scope
memory follows it (`/config/CLAUDE.md`, not `~/.claude/CLAUDE.md`), and `~/.claude`
holds only a downloads cache.

## An MCP server can be declared four ways, and `settings.json` is not one of them

Measured in-container on Claude Code 2.1.234, because the channel that looks most
natural for this repo — an `mcpServers` block in the baked `user-settings.json` —
turns out not to exist.

The probe is the useful part: give the server a command that leaves a trace and see
whether it was ever launched, rather than trusting a listing.

```
--mcp-config '{"mcpServers":{"p":{"command":"/bin/sh",
    "args":["-c","touch /tmp/probe; exec /bin/false"]}}}' -p 'reply with: ok'
```

| Channel | Server launched? |
| --- | --- |
| `--mcp-config` (inline JSON string *or* file path) | yes |
| `--settings '{"mcpServers":…}'` | **no** — silently ignored, no error |
| `claude mcp add -s user` → `$CLAUDE_CONFIG_DIR/.claude.json` | yes |
| `claude mcp add -s user` + `--strict-mcp-config` on the same run | **no** — suppressed |

The two flags are not alternatives — one supplies servers, the other changes how the
sources are resolved. With a user-scope server registered throughout:

| Invocation | user-scope server | `--mcp-config` server |
| --- | --- | --- |
| neither flag | runs | — |
| `--mcp-config X` | runs (**additive**) | runs |
| `--mcp-config X --strict-mcp-config` | suppressed | runs |
| `--strict-mcp-config` alone | suppressed | — none supplied |

The last row is the non-obvious one: strict with nothing to be strict about yields
**zero** MCP servers rather than erroring or falling back.

Four facts worth not re-deriving:

- **`mcpServers` in a settings file does nothing.** No warning, no error — the run
  succeeds and the server simply never starts. `mcpServers` and `strictMcpConfig` do
  both appear as strings in the binary, so grepping the bundle does *not* settle it;
  only the launch probe does.
- **`--mcp-config` accepts an inline JSON string**, so no file has to exist anywhere.
- **`--strict-mcp-config` really is exclusive** — the user-scope server registered a
  moment earlier did not launch under it, and `--mcp-config` on its own does *not*
  suppress it. That is the property that makes a launcher-supplied set authoritative
  for a session, and it belongs to the strict flag alone.
- **`--mcp-config` is variadic and greedy.** `claude --mcp-config '<json>' mcp list`
  consumes `mcp` and `list` as further config paths and fails with "MCP config file
  not found: mcp". It is also a main-command option only: `claude mcp list
  --mcp-config …` errors with `unknown option`. Put it before an option-shaped
  argument (`-p`), not before a subcommand.

Separately, `claude mcp get`/`list --help` state that servers from a project
`.mcp.json` show as `⏸ Pending approval` and are *not connected to* until approved —
and `enableAllProjectMcpServers` / `enabledMcpjsonServers` in settings pre-approve
it. **That gate does not hold in `-p` mode.** Same launch probe, `.mcp.json` dropped
in a fresh directory: the server started, with and without
`--dangerously-skip-permissions`, on a run that had no approval state to inherit —
no `enableAllProjectMcpServers` anywhere, empty `enabledMcpjsonServers`, no managed
settings file, and no `projects` entry for that path in `.claude.json`. So in
headless runs a directory can start a process merely by containing a file. The
interactive path was **not** tested and may well prompt; the point is that the
prompt is not what makes it safe.

## An MCP server container really does honour `HTTPS_PROXY` — and how to tell

Measured against `ghcr.io/github/github-mcp-server:v1.9.0` in `http` mode on an
internal network whose only reachable peer is the egress proxy. The interesting part
is the probe order, because most of the obvious checks prove less than they appear to.

**What the proxy log shows when it works.** A tool call that reaches GitHub leaves a
row naming the *server's* address, which is the only observation that distinguishes
"this server used the proxy" from "something on that network can":

```
{"decision": "allow", "host": "api.github.com", "port": 443,
 "client": "172.28.0.2", "reason": "allowed by rule (.github.com)", "central": true}
```

…and the tool returns `GET https://api.github.com/user: 401 Bad credentials`. **A 401
from GitHub is the success case**: it proves a full TLS round trip completed, so the
egress path can be verified with a deliberately bogus token and no real credential.

**Four probes, in increasing strength.** Each of the first three fails to answer the
question, which is why the order matters:

| Probe | Outcome | What it actually proves |
| --- | --- | --- |
| audit log, before any tool call | no rows | *nothing* — an idle MCP server makes no API calls |
| `curl https://api.github.com` from the network | could not resolve host | weak — only that the resolver has no upstream |
| `curl http://1.1.1.1` (literal, port 80) | `rc=7` at **0 ms** | strong — no route at all; DNS-independent |
| a real `tools/call`, then the log | row with the server's IP | the actual claim |

The 0 ms matters: an internal network has no gateway, so the kernel fails locally
rather than timing out. Same reasoning as `boundary-check.sh` probing a raw IP.

**"OAuth" means two unrelated things in this server, which is why the docs read as
contradictory.** `docs/oauth-login.md`: *"OAuth login applies to the **stdio** server
only. The remote server and the `http` command have their own authentication."* That
one is the server acting as an OAuth **client** to obtain a token for itself —
interactive, browser or device code, callback port, in-memory only. Separately, the
`http` server acts as an OAuth **resource** server: it advertises
`/.well-known/oauth-protected-resource/mcp` and verifies a bearer, which is what the
`WWW-Authenticate` below is doing and what `OAuth protected resource endpoints
registered` in its startup log means. `docs/host-integration.md` documents that
discovery dance only for GitHub's *hosted* remote server, so the self-hosted `http`
mode implementing it is undocumented rather than absent. Net effect: in `http` mode
the server never acquires a credential, only verifies one.

**Driving the server by hand needs three surprises handled.** In `http` mode it is an
**OAuth-protected resource**: an unauthenticated `initialize` gets `401` plus
`Www-Authenticate: Bearer resource_metadata=…/.well-known/oauth-protected-resource/mcp`.
Any bearer value is accepted at `initialize` (it is not validated there), so
`-H 'Authorization: Bearer ghp_0000…'` is enough to proceed. It runs **stateless** —
no `Mcp-Session-Id` is issued, so `tools/call` needs no session header. And replies
arrive as **SSE** (`text/event-stream`), so the result is a `data:` line, not a JSON body.

**`GITHUB_PERSONAL_ACCESS_TOKEN` is unused in `http` mode.** Settled with one valid
read-only PAT in the container's environment, varying only where the credential came
from — the audit log is what makes the first row unambiguous, since a local refusal
and a GitHub rejection both surface as "401":

| env token | `Authorization` header | Result | New audit row |
| --- | --- | --- | --- |
| valid | omitted | `Unauthorized` (HTTP 401) | **no** — never called GitHub |
| valid | bogus | `401 Bad credentials` *from GitHub* | yes |

So there is no env fallback, and when a bearer is present it **takes precedence** —
the valid env token was ignored in favour of a deliberately broken header.

**The upstream documentation says otherwise, and the code agrees with the
measurement.** PR github/github-mcp-server#1216 and the changelog both describe HTTP
mode as falling back to `GITHUB_PERSONAL_ACCESS_TOKEN` when no header is present.
`pkg/http/middleware/token.go` on `main` contains no such fallback: it parses the
`Authorization` header and returns 401 with `WWW-Authenticate` exactly when the header
is missing, and offers no static-token flag. The fallback was the original intent and
the docs were not updated — worth knowing before believing any account of this
server's auth that is not the middleware itself.

Two things follow, and both are favourable. A gateway fronting this server **must**
inject `Authorization`, because without it the server never reaches GitHub at all.
And the precedence runs the safe way round: a stale env credential cannot shadow the
token the gateway supplies. The container therefore needs no credential of its own,
which is why the env var was removed from `mcp-servers.yml` rather than kept as a
fallback — an ineffective credential slot still shows up in `docker inspect`.

## Publishing a host port: the private range is the wrong instinct on WSL2

Choosing a port for Docker to publish on the host, the principled-looking answer is
IANA's dynamic/private range (49152–65535) — no registered service claims it. On a
Windows host running WSL2 it is the worst band available, for two independent reasons:

- **It overlaps the Linux ephemeral range.** `cat /proc/sys/net/ipv4/ip_local_port_range`
  reports `32768 60999` here, so anything from 32768 up can already be held by some
  outbound connection's source port at the moment the daemon tries to bind. The failure
  is intermittent and load-dependent, which is the expensive kind.
- **Windows reserves blocks inside it.** Hyper-V / WinNAT take ranges out of 49152–65535
  for their own use; `netsh interface ipv4 show excludedportrange protocol=tcp` lists
  them. A publish that lands in one fails with "An attempt was made to access a socket
  in a way forbidden by its access permissions" — which reads like a permissions problem
  and is nothing of the kind.

So the usable band is *above* the crowded 8000–9000 development block and *below* the
ephemeral floor: roughly 20000–32767.

## `curl` reads `http_proxy` in lower case only

Same host, same shell, curl 8.14.1. `example.com` carries a **block** rule, so reaching
the proxy yields a fast 403 and not reaching it yields a DNS failure — which
discriminates cleanly without raising a hold:

| proxy specified via | reached the proxy? | result |
|---|---|---|
| `HTTP_PROXY` (upper) | **no** — resolved the target itself | `Could not resolve host`, ~1 ms |
| `http_proxy` (lower) | yes — `GET http://example.com/` | `403` from the block rule |
| `HTTPS_PROXY` (upper) | yes — `CONNECT example.com:443` | `403` from the block rule |
| `--proxy` (explicit) | yes | `403` from the block rule |

The giveaway is that curl **was** doing proxy-environment resolution in the first row —
it printed `Uses proxy env variable NO_PROXY == '…'` — and then resolved the target
anyway. So it consults the proxy env vars and declines to use the uppercase
`HTTP_PROXY` specifically. `HTTPS_PROXY` and `NO_PROXY` are honoured in either case.

**It is a security mitigation, so it will not change.** Under CGI a client-supplied
`Proxy:` request header arrives in the environment as `HTTP_PROXY`; honouring it would
let a remote caller redirect a server's outbound HTTP through a proxy of their choosing
(httpoxy, CVE-2016-5385). Setting the lowercase variable is the only fix — and setting
both cases is the conventional pairing, since other tools split the other way.

No man page ships in the sandbox image, so the above is the measurement rather than a
quote from the documentation.

## mitmproxy decodes IDNA on the authority it parses, but not on the Host header or SNI

Measured against `mitmproxy 12.2.3` — the version pinned by digest in
`proxies/egress/Dockerfile`. A client sending `CONNECT xn--bcher-kva.de:443` (the
A-label of `bücher.de`, which is what curl and browsers put on the wire) produces:

| property | value | why |
|---|---|---|
| `request.host` | `bücher.de` | **decoded** |
| `request.pretty_host` | `xn--bcher-kva.de` | not decoded |
| `request.authority` | `bücher.de:443` | decoded |
| `ClientHello.sni` | `xn--bcher-kva.de` | ASCII only |

The asymmetry is one function taking two types. `url.parse_authority` decodes when
handed **bytes** and does not when handed **str**: the HTTP/1 reader passes the raw
wire bytes (`net/http/http1/read.py`, both the authority-form and absolute-form
branches), whereas `pretty_host` re-parses the Host header, which `Headers` has
already turned into a `str`. `ClientHello.sni` is `.decode("ascii")` with no IDNA
path at all (`mitmproxy/tls.py`). Under HTTP/2 the split moves: `host_header` returns
`authority`, which *is* decoded, so `pretty_host` is Unicode there too.

Sending raw UTF-8 on the wire instead of the A-label is **not** an alternative route
to the same state — the request line fails to parse (`ValueError: Bad HTTP request
line`) before any of this.

Measured by installing the same version and parsing synthetic request heads through
`mitmproxy.net.http.http1.read_request_head`, not by reading the changelog.

## In the C locale, `curl` rejects a literal IDN even with `libidn2` linked in

`curl https://bücher.de` fails with `(3) URL using bad/illegal format` in ~6 ms —
before any request leaves, so in a terminal it reads exactly like a proxy denial and
is not one. curl has `libidn2`; what it lacked was a UTF-8 locale to convert *from*.
Bypassing the proxy separates parsing from egress, since error 3 means curl never
built a URL and error 6 means it did:

| `LC_ALL` | result |
|---|---|
| unset (C locale) | `(3)` — not parsed |
| `C.UTF-8` | `(6) Could not resolve host: bücher.de` — parsed fine |
| `en_US.UTF-8` | `(3)` — not installed in the image, so it falls back to C |

Debian 13's glibc carries `C.utf8` built in, so the fix was one `ENV LANG=C.UTF-8`
(now in both sandbox Dockerfiles) and not a `locales` package — the locale was always
there, only the variable was missing. Worth knowing anyway when reaching for a
national locale in a slim image: `en_US.UTF-8` is *not* present and silently degrades
to C rather than erroring.

Probing an internationalized host with its **A-label** (`xn--bcher-kva.de`) sidesteps
the question entirely, and is the more faithful test regardless — the A-label is what
any client puts on the wire.

## Two `uvicorn.Server`s in one event loop still both stop on SIGTERM

Running two servers from one `asyncio.gather` looks like it should break signal
handling, and the reasoning is sound as far as it goes: `serve()` wraps itself in
`capture_signals()`, which calls `signal.signal(sig, self.handle_exit)`, so the
second server installs over the first and only the second's `handle_exit` runs.

It works anyway, because `capture_signals()` is a context manager that cleans up
after itself in two steps. On exit it restores the handler it displaced — the
first server's — and then re-raises the signals it captured
(`signal.raise_signal`, LIFO). The re-raised SIGTERM lands on the restored
handler, so the first server shuts down too.

Measured on uvicorn **0.34.0** (the pin in `control-plane/requirements.txt`), two
servers on one loop, `kill -TERM` on the process: two `Finished server process`
lines and the process gone in about 500 ms.

The corollary is the part worth writing down: hand-rolled handlers added "to be
safe" do nothing here. `loop.add_signal_handler` installs through asyncio's own
`signal.signal` hook, which `capture_signals` then displaces, so a handler
registered before `serve()` never fires. Removing one changed neither the timing
nor the log — it was inert code that read as load-bearing.

## `toLocaleString()` renders one instant six ways

One instant — `2026-08-06T22:30:05Z`, rendered in `Europe/Stockholm`:

| locale | `toLocaleString()` | `toLocaleTimeString()` |
|---|---|---|
| `en-US` | `8/7/2026, 12:30:05 AM` | `12:30:05 AM` |
| `en-GB` | `07/08/2026, 00:30:05` | `00:30:05` |
| `sv-SE` | `2026-08-07 00:30:05` | `00:30:05` |
| `de-DE` | `7.8.2026, 00:30:05` | `00:30:05` |
| `fr-FR` | `07/08/2026 00:30:05` | `00:30:05` |
| `ja-JP` | `2026/8/7 0:30:05` | `0:30:05` |

`8/7/2026` and `07/08/2026` are the same moment written by two readers who would each
report a different date if asked.

Three things worth knowing, none of them obvious from the API:

- **With no locale argument the runtime picks**, and in a browser that comes from the
  **browser's language preference** (`navigator.languages`), *not* the operating
  system's regional format setting. A browser installed in English renders US dates on
  a machine configured entirely otherwise — which is how this was noticed.
- **The page's `<html lang>` has no effect on it.** Setting `lang="en"` documents the
  content language for assistive technology and does not reach `Intl`.
- **`Number(null)` and `Number("")` are `0`, not `NaN`.** So a missing timestamp
  survives an `isFinite` guard and formats as `1970-01-01` — a plausible-looking date
  where the honest output is nothing at all. Only `undefined` and non-numeric strings
  produce `NaN`.

Node behaves the same way and defaults to `en-US` on a stock runner
(`Intl.DateTimeFormat().resolvedOptions().locale`), which matters for tests: a
locale-driven format cannot be asserted, only shape-checked, and a UTC-defaulted CI
runner will not catch a UTC-for-local mix-up. Pinning `TZ` to a zone with a non-zero
offset is what makes that assertable.

## Local inference on an Intel Arc 140V iGPU (Lunar Lake, WSL2)

Decisions these numbers produced are in DESIGN.md → "Local inference"; this is the
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
