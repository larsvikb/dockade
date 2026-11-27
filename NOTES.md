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

## A cached `.pyc` can stand in for a mutant, and `-B` alone does not stop it

CPython reuses `__pycache__/<module>.cpython-*.pyc` while the source's size and
modification time, in whole seconds, match what the cache recorded. So a mutation
script that writes a mutant the same length as an earlier one within the same second
runs the earlier one's bytecode. `-B` only stops a run writing a cache; it still reads
one that is there:

```sh
echo 'X = 1' > m.py; python3 -c 'import m'                   # caches X = 1
echo 'X = 2' > m.py; python3 -B -c 'import m; print(m.X)'    # -> 1
```

Delete `__pycache__` before the first mutant and run every mutant with `-B`. Seen in a
review on Python 3.13, where a mutant reported caught in fact survived.

## Python's `sqlite3` leaves DDL outside its implicit transaction

SQLite itself has transactional DDL — a `DROP TABLE` inside a transaction is undone by
a rollback. Python's driver in legacy mode does not put it in one: it opens an implicit
transaction before `INSERT`/`UPDATE`/`DELETE`/`REPLACE` and nothing else, so a `DROP`
issued on a legacy-mode connection runs in autocommit and is already permanent by the
time anything can roll it back.

```python
c = sqlite3.connect(":memory:", autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL)
c.execute("CREATE TABLE t(x)"); c.execute("INSERT INTO t VALUES (1)"); c.commit()
c.execute("DROP TABLE t")
c.in_transaction        # -> False        (no transaction was ever opened)
c.rollback()
c.execute("SELECT * FROM t")               # -> OperationalError: no such table: t
```

Take control explicitly and the engine behaves as advertised:

```python
c.execute("CREATE TABLE t(x)"); c.execute("INSERT INTO t VALUES (1)"); c.commit()
c.isolation_level = None                   # autocommit; we issue the statements
c.execute("BEGIN IMMEDIATE")
c.execute("DROP TABLE t")
c.in_transaction        # -> True
c.execute("ROLLBACK")
c.execute("SELECT * FROM t").fetchall()     # -> [(1,)]   the table is back
```

Measured on CPython 3.13.5 / SQLite 3.46.1. This is the documented legacy
`isolation_level` behaviour rather than a version quirk, so it applies to the
control-plane image's Python, on a connection in legacy transaction control — but
only the 3.13 figure above was actually run.

The consequence is entirely about **table-rebuild migrations**, which are the only
shape here that mixes DML and DDL in one operation: copy the rows aside, drop the
original, rename. On a legacy-mode connection those three are three separate
autocommits, so a failure between the copy and the drop is not undone — on a
long-lived store that is the policy rules gone. Nothing warns; the code reads as if it
were atomic.

An additive `ALTER TABLE ADD COLUMN` needs none of this, which is why the distinction
is worth writing down rather than "always wrap migrations": the cheap shape is safe on
its own and the expensive shape silently is not.

On ext4/overlayfs, `os.remove(p)` followed immediately by recreating `p` typically
reuses the just-freed inode number, so `st_ino` is unchanged. Any rotation detection
keyed on the inode therefore cannot be tested by delete-then-recreate — the test passes
or fails on allocator luck. Write to a sibling path and `os.replace()` it over the
target instead: the new file's inode is allocated while the old one is still linked, so
it is guaranteed to differ.

## Claude Code honours `HTTPS_PROXY`, and WebFetch inherits it

What the CLI does with a proxy, per Anthropic's
[Enterprise network configuration](https://code.claude.com/docs/en/network-config)
docs, and the probe that confirmed the parts `DESIGN.md` leans on. Documented:

- It reads `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` — the docs spell them in upper
  case — Node-natively and **once at startup**; basic auth as `user:pass@` in the URL;
  no SOCKS. A plain forward proxy named in the environment is therefore enough, with
  no transparent redirect of port 443.
- WebFetch uses the same variables — the docs describe no separate mechanism — and
  fires a domain-safety **preflight to `api.anthropic.com` on every fetch**, which
  `skipWebFetchPreflight: true` disables.
- TLS interception is a documented scenario, not a hack: a custom CA via
  `NODE_EXTRA_CA_CERTS=/path/ca.pem`, `CLAUDE_CODE_CERT_STORE` (default
  `bundled,system`) to pick the trust stores, and mTLS via `CLAUDE_CODE_CLIENT_CERT` /
  `CLAUDE_CODE_CLIENT_KEY` / `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE`.
- Beyond `api.anthropic.com` it reaches `downloads.claude.ai` and
  `storage.googleapis.com` (the native installer and auto-updater),
  `raw.githubusercontent.com` (the `/release-notes` changelog) and
  `mcp-proxy.anthropic.com`, which carries the claude.ai MCP connectors and is on by
  default (`ENABLE_CLAUDEAI_MCP_SERVERS=false` turns them off). The statsig,
  feature-flag and telemetry chatter goes with
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.
- Background and supervisor processes do not reliably inherit a shell's environment,
  so the channels the docs call robust for the proxy variables are the settings `env`
  block and the real process environment.

Measured with the proxy running and the sandbox in governed mode
(`HTTPS_PROXY=http://egress-proxy:8080`, the firewall allowing only the proxy and
embedded DNS): `boundary-check.sh` reported `api.anthropic.com reachable (via proxy
http://egress-proxy:8080)`, so the CLI's own API traffic went through the proxy, and
the same run had the proxy refuse a non-allowlisted domain — `egress proxy does not
allow non-allowlisted example.com (held or denied)` — with direct egress, IPv6 and
direct external DNS all blocked. WebFetch separately: fetching an allowlisted host
(`pypi.org`) succeeded, a non-allowlisted one (`example.com`) failed with `Socket is
closed`, and the proxy's audit log showed the matching `allow` and `deny` CONNECT
rows. That is what makes WebFetch client-side and proxy-governed: had it run
server-side like WebSearch, `example.com` would have succeeded and never reached the
proxy.

## Claude Code reads a managed `CLAUDE.md` even where it ignores managed settings

The two managed tiers do not behave alike, and the asymmetry is easy to walk into.

Under org auth, a local `managed-settings.json` is ignored entirely — the org's
remote managed source is the sole managed *settings* tier. Measured in the running
container, with `CLAUDE_CONFIG_DIR=/config`: `/status` reports the setting sources
as *"User settings, Enterprise managed settings (remote)"*, with the local file
absent from the list, and a live `WebSearch` call succeeded despite that file's
`permissions.deny: ["WebSearch"]` — the deny never applied. The remote source is
cached at `/config/remote-settings.json` and `/config/policy-limits.json`, which
the sandbox cannot edit. (The consequence for this repo is in `DESIGN.md` →
"Managed settings are NOT an enforcement lever here".) A local managed
**`CLAUDE.md`** is not ignored. Dropping one in `/etc/claude-code` and running
`/context` lists it:

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

## Claude Code records the yolo disclaimer in user settings

Measured on Claude Code 2.1.207: accepting the `--dangerously-skip-permissions`
disclaimer writes `skipDangerousModePermissionPrompt: true` into user settings
(`/config/settings.json`, under `CLAUDE_CONFIG_DIR=/config`). The older global
`bypassPermissionsModeAccepted` flag in `.claude.json` is migrated away and deleted on
startup, so it no longer persists the acceptance — a template that set only the old
flag would re-prompt on every launch.

## Claude Code puts its session id in every Bash call's environment

Measured on Claude Code 2.1.296: `CLAUDE_CODE_SESSION_ID` holds the session's UUID, and
the session's transcript is named by it, `<id>.jsonl` beside a directory `<id>/` under
`$CLAUDE_CONFIG_DIR/projects/<workspace slug>/`. A subagent's Bash call sees the same
value and the same `CLAUDE_PID`, and the subagent's transcript sits under the parent's
`<id>/subagents/`, so a session and the subagents it starts share one id.

Measured by the operator on 2026-11-11 in the tier-1 sandbox, whose image runs Claude
Code 2.1.322:
`/clear` gives the session a new `CLAUDE_CODE_SESSION_ID`, and `--resume <id>` keeps
it — `echo $CLAUDE_CODE_SESSION_ID` printed the same id before an `/exit` and after
resuming. That matches `claude --help`, which describes `--fork-session` as "When
resuming, create a new session ID instead of reusing the original". Not measured:
`--continue` and the picker form of `--resume`.

## Claude Code's Bash tool runs each command in a `bash -c` carrying its full text

Measured on Claude Code 2.1.332 in the tier-1 sandbox, 2026-11-26: every Bash call runs
as `/bin/bash -c source <shell snapshot> … && eval '<the command>'`, so the command's
own text is in a live process's command line while it runs. `pgrep -af "<marker>"`
for a marker no other process holds exits 0 and lists that shell. A wait that polls
`pgrep -f` for a run's name therefore never sees the run end: a task 383 worker's
`until … || ! pgrep -f "tasks.test.js"; do sleep 5; done` outlived its finished
`make ui-test` by about 18 minutes. Bash's `run_in_background` notifies the agent when
the command exits, a subagent included, so no poll is needed.

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

## An MCP tool call has four timers, and the one that bites defaults to ~28 hours

From the Claude Code MCP documentation (`https://code.claude.com/docs/en/mcp`), read
while deciding whether a gateway `ask` may block the agent, and re-read 2026-09-23,
by which time the page had gained a fourth timer and a client feature. **Documented,
not measured** — the defaults below have not been probed in-container, and the
28-hour figure especially is worth pinning before anything depends on it.

| Timer | Scope | Default |
| --- | --- | --- |
| `MCP_TIMEOUT` | server **startup** | not stated in the docs |
| `MCP_TOOL_TIMEOUT` | per tool call, when no per-server `timeout` is set | ~28 hours |
| per-server `timeout` (ms, in the server's config entry) | per tool call | unset |
| first-response-byte, HTTP/SSE/WS servers only | per request | the greatest of 60 s, the server's `timeout` and `MCP_TIMEOUT` — "the 28-hour default of an unset `MCP_TOOL_TIMEOUT` doesn't enter that comparison" |
| idle — no response *and no progress notification* | per tool call, HTTP/SSE/WS/connector | **5 min** (30 min stdio; IDE and in-process exempt), `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, v2.1.187+ |

What follows, in the order it matters:

- The per-server `timeout` is a **hard wall-clock limit**, and **progress
  notifications do not extend it** — but they are **required against the idle
  timer**: a call that intends to sit quietly for more than five minutes needs them.
  Keepalive exists in this client; it buys idle time, never wall-clock time.
- **60 s is the floor a portable server has to fit inside** — the one number no
  configuration can lower. The first-byte timer is the greatest of 60 s, the server's
  `timeout` and `MCP_TIMEOUT`, so an unconfigured HTTP server gets exactly 60 s, a
  value below 60 cannot shorten it, and a server that intends to answer slowly must
  set `timeout`; there is nothing to set on the first-byte timer directly.
- Unset means ~28 hours, not "a sensible minute or two". Any design that blocks the
  caller and relies on the default to bound it hangs the agent for a day rather than
  failing — the failure mode arrives in someone else's config, not the author's.
- **One field settles the three that can be set.** A per-server `timeout` of at least
  1000 ms sets the wall clock, raises the first-byte timer to match, and (v2.1.203+)
  acts as a floor on the idle timeout.
- **Automatic backgrounding (v2.1.212+) is the real answer to blocking — in this
  client only.** "An MCP tool call in the main conversation that is still running after
  two minutes moves to a background task instead of blocking the session. Claude
  receives the task ID immediately and keeps working, and the result arrives as a task
  notification when the call settles." Threshold via
  `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`. **Not** applied to subagent calls, IDE servers,
  or non-interactive runs unless `CLAUDE_AUTO_BACKGROUND_TASKS=1`.
- **Elicitation is supported**, and a call waiting on an open elicitation dialog is
  explicitly *not* backgrounded — it blocks the session until the dialog closes.
- **Resumable Streamable HTTP (`Last-Event-ID`) is not documented for this client.**
  Nothing to build on.

## An `http` MCP server's `headers` reach the server on every request

Measured in a tier-1 container on 2026-11-20, Claude Code 2.1.329. A Python listener on
`127.0.0.1:18765` logged each request's headers, and then:

```
claude --mcp-config '{"mcpServers":{"probe":{"type":"http",
    "url":"http://127.0.0.1:18765/mcp",
    "headers":{"X-Dockade-Sandbox":"0123456789abcdef0123456789abcdef"}}}}' \
  -p 'reply with: ok'
```

Every request carried the header with its literal value: four POSTs and one GET to
`/mcp`. `claude-wrapper.sh` hands `/etc/claude-code/mcp-gateway.json` to the same
`--mcp-config` flag, so this is the path the sandbox id takes
(`claude-sandbox/tier-setup.sh` writes `X-Dockade-Sandbox` into that entry).

Why the value is written literally rather than as a `${VAR}` for Claude Code to expand:
the id is minted in tier-setup.sh's root phase and is in no environment the agent's
Claude Code inherits; and expansion has a blanking rule of its own. The docs list
specific credential variable names that read as empty in an MCP config (Claude Code's
own, the cloud providers', `HTTPS_PROXY`, `NPM_TOKEN`:
`https://code.claude.com/docs/en/mcp#credential-variables-that-read-as-empty`), and
anthropics/claude-code issue #100930 reports `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`
blanking values that look like credentials, as a 32-hex id may.

The probe shows the client half only. The whole chain (tier-setup mints an id, the
gateway relays it, a card shows the prefix that boot printed, and two sandboxes show
two) held on the reference machine on 2026-11-20, at commit 4f1db3d: two Claude
sandboxes from that commit's image printed `Sandbox id  -> c52e5e04` and `-> 6ede8175`,
each called a tool that held, and each card showed `sandbox <prefix>` with its own
sandbox's prefix. Whether the
id also reaches the shared config volume (a transcript, a debug log), where a sibling
could read it, is to be noted here when seen.

## A directory marketplace is used in place, and `settings.json` is the whole declaration

Measured in-container on Claude Code 2.1.236, with a fixture marketplace
(`.claude-plugin/marketplace.json` plus one plugin holding one skill) and a
throwaway `CLAUDE_CONFIG_DIR`. Four facts, each of which changed a design choice.

**A local path becomes a `directory` source, referenced in place.** `claude plugin
marketplace add /tmp/mkt/local-demo` recorded
`{"source":{"source":"directory","path":"/tmp/mkt/local-demo"}}` with
`installLocation` equal to that same path, and copied nothing into
`plugins/marketplaces/` (a *github* source clones there — the shipped
`claude-plugins-official` entry does). No network, no credential: the add succeeded
in a config dir holding no `.credentials.json`. `installLocation` being the path as
given is why the container path has to be the in-container one.

**Installing from a read-only tree works.** With the fixture `chmod -R a-w`,
`claude plugin install hello@local-demo` still succeeded — the tree is read, never
written. So `:ro` costs nothing.

**The CLI's only effect is two keys in user `settings.json`.** `marketplace add`
wrote `extraKnownMarketplaces`, `install` wrote `enabledPlugins`, both merged into
an existing file (a copy of this repo's baked template kept its `statusLine` and
`skipDangerousModePermissionPrompt`). `plugins/known_marketplaces.json` is derived
cache, not the declaration: hand-writing both keys and running a *session*
materialized it, after which `claude plugin details hello@local-demo` printed the
plugin and its skill inventory with no install ever run. The reconciliation happens
at **session** start — the `plugin` subcommands read the cache, so immediately
after hand-writing settings, `plugin list` and `plugin details` still say "not
found" while a session would load it fine. Both halves matter: settings alone are
sufficient, and the CLI is not a way to check that they are.

**The marketplace key is the manifest's `name`, not the directory name.** A
checkout at `dir-name-differs/` whose manifest said `manifest-name` was keyed
`manifest-name`. Plugin ids are `plugin@<manifest name>`, so deriving the key from
the directory yields an allowlist that silently matches nothing.

Failure modes, for a boot script that must not abort on one bad checkout: a missing
path and a directory without `.claude-plugin/marketplace.json` both exit 1;
re-adding an existing marketplace is idempotent and exits 0. And an `enabledPlugins`
id naming a marketplace that does not exist is **inert, not fatal** — a session with
`nope@unknown-marketplace` declared started and answered in 11s against 7s for the
same session without it, and triggered no fetch (nothing appeared in
`known_marketplaces.json`). Worth knowing because the opposite would have been
worse than an error: an unresolvable id that made Claude Code try to clone would
hang on the proxy's hold-for-approval path at every boot.

## `--settings` is command-line scope: it outranks a project's `enabledPlugins: false`

Measured in-container on Claude Code 2.1.310, with the fixture marketplace of the
section above (one plugin, one skill), a throwaway `CLAUDE_CONFIG_DIR` and a launch
file holding `extraKnownMarketplaces` and `enabledPlugins`. Each run was `claude -p`
with `--output-format stream-json --verbose`, reading the `init` event's `plugins`.
That event is printed before the first API call, so a config dir with no
credentials is enough: the run then ends "Not logged in", having loaded everything.

**Loading.** A plugin enabled only in the file passed as `--settings <file>` loads,
and without the flag it does not. The user `settings.json` is unchanged afterwards:
the file is layered in, never written back.

**Two `--settings`: the last replaces the first.** `--settings <file> --settings
'{}'` loaded nothing, and `--settings '{}' --settings <file>` loaded the plugin.
They are not merged.

**Scope.** The file outranks every other settings scope:

| `hello@demo-mkt` enabled by | turned off by | loaded? |
| --- | --- | --- |
| user `settings.json` | nothing | yes |
| user `settings.json` | project `<repo>/.claude/settings.json` `false` | no |
| user `settings.json` | project `<repo>/.claude/settings.local.json` `false` | no |
| `--settings <file>` | project `<repo>/.claude/settings.json` `false` | **yes** |
| `--settings <file>` | project `<repo>/.claude/settings.local.json` `false` | **yes** |
| `--settings <file>` | user `settings.json` `false` | **yes** |

The file only wins per plugin: `enabledPlugins` merges across scopes. With
`hello@other-mkt: true` in user `settings.json` and `--settings` enabling
`hello@demo-mkt`, both loaded, whether or not user scope also registered
`other-mkt` (the cache already knew it).

`claude --settings <file> plugin disable --scope user` (and `--scope project`) exits
0 and writes `false` to that scope, saying "still enabled this session: --settings
keeps it on", and the next session loads the plugin anyway. The interactive
`/plugin` menu was not driven, but it writes the same scopes.

**Behind the prepends.** With `--settings <file> --mcp-config <file>
--strict-mcp-config` in front, `--version`, `plugin list`, `mcp list`, `plugin
marketplace list`, `update --help` and `--help` all exit 0. Appended instead,
`claude plugin list --settings <file>` fails with `unknown option '--settings'`.

**The cache stays shared.** A session started with the launch file wrote its
marketplace into the config dir's `plugins/known_marketplaces.json`, and `plugin
list` behind the file wrote an `installed_plugins.json` entry with scope "user"
(its `installPath` under `plugins/cache/` was never created; the plugin is read in
place). Two launch files over one config dir, as two siblings would have, each
session loaded only its own plugin, but afterwards `plugin marketplace list` showed
both marketplaces, and `plugin list` showed the other's plugin as disabled. A
launch file enabling `hello@other-mkt` without registering `other-mkt` loaded it,
through the cache the other session had written. In the fixture both paths are
visible to both sessions. Not measured: two real containers, where the cached
`/marketplaces/...` path resolves in the reader's own mount, so it reads that
container's checkout or nothing.

## A backslash-u escape typed into a tool parameter arrives as the character

Text meant to hold an escape as text (a backslash, `u` and four hex digits, as when a
change about bidi overrides names U+202E) does not survive every path the same way:

- **Claude Code's `Write` and `Edit`, and a Bash command**, a heredoc or a plain
  argument, decode it: the file gets the real character. Seen 2026-09-26 (U+0430 in a
  commit-message file written with `Write`), and three times on 2026-10-12, twice in a
  commit body written through a heredoc and once as raw characters in a fixture
  written with `Write`. Three 2026-09-19 cases, two commit messages and a PR body,
  came through a tool parameter or a quoted heredoc; which one was not recorded.
  Re-observed 2026-11-06 on Claude Code 2.1.319: an escape for U+0430 typed into
  `Write`, and into a Bash call as the quoted argument of `echo` and of `printf '%s'`,
  wrote the bytes `d0 b0`. Neither of those two decodes an escape in its argument
  (Bash's `printf` does in its format string), so the call arrived decoded.
- **The gateway's `create_pull_request` and `update_pull_request`** pass a single
  backslash through literally and a doubled one as two backslashes (2026-09-26, PR
  #160). So doubling fixes one path and breaks the other.
- **The task registry's `tasks__propose`** decoded one once (2026-11-05, task 270
  revision 1). A single observation, not a rule about the gateway.

Nothing failed any of those times; the intent and the bytes differed silently. What
works: spell a character as `U+XXXX` in prose, and where the escape itself must
appear, build the text in Python with `chr(92)` for the backslash and read the result
back. What caught every 2026-10-12 case was a scan of each changed file and of the
commit body for characters of category Cf, Zl, Zp or Cc other than tab and newline,
before anything was handed over. The body goes on stdin, the changed files as
arguments:

    git log -1 --format=%B | python3 -c 'import sys, unicodedata as u; texts = [("body", sys.stdin.buffer.read().decode("utf-8"))] + [(f, open(f, encoding="utf-8").read()) for f in sys.argv[1:]]; [print(n, i, hex(ord(c))) for n, t in texts for i, c in enumerate(t) if u.category(c) in ("Cf", "Zl", "Zp", "Cc") and c not in (chr(9), chr(10))]' <changed files>

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
**OAuth-protected resource**: an unauthenticated request gets `401` plus
`Www-Authenticate: Bearer resource_metadata=…/.well-known/oauth-protected-resource/mcp`.
The bearer is not *validated* locally, but it is **format-checked**, which is a third
refusal distinct from the two in the table below: a token that does not look like a
GitHub one is rejected with `bad request: Authorization header is badly formatted`
without anything reaching GitHub. So a dummy has to be well-formed —
`-H 'Authorization: Bearer ghp_0000…'` (36 characters after the prefix) proceeds where
`Bearer not-a-real-token` does not. It runs **stateless** — no `Mcp-Session-Id` is
issued, so no session header is needed and `tools/list` answers with no `initialize`
handshake at all. And replies arrive as **SSE** (`text/event-stream`), so the result is
a `data:` line, not a JSON body.

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

## What `mcp-github` v1.9.0 actually exposes

Reproduce with `make mcp-tools SERVER=github`. Captured against
`ghcr.io/github/github-mcp-server:v1.9.0` with the catalogue's defaults
(`GITHUB_TOOLSETS=context,repos,issues,pull_requests`, `GITHUB_READ_ONLY=1`) and a
**dummy** `ghp_` token — enumeration never calls GitHub, so the whole tool surface is
readable before any credential is configured. A gateway can therefore build its roster
at startup without holding one.

**25 tools, every one `readOnlyHint: true` — and the flag is what does it.** Re-running
the same probe with `GITHUB_MCP_READ_ONLY=0` and nothing else changed returns **41
tools, 16 of them not read-only**, so `GITHUB_READ_ONLY` really filters in v1.9.0 rather
than being the silently inert flag it was through v0.32.0
(github/github-mcp-server#2156, fixed in v0.33.0 by #2208). The toolset selection is
not what produced the
read-only list. This is defence in depth and still not the boundary — the flag was
believed to work before, too.

**The 16 it removes are the ones that make "which governed path owns repo writes" a real
question**, because several reach ref-changing capability without ever speaking the git
wire protocol: `create_or_update_file`, `push_files`, `delete_file`, `create_branch`,
`update_pull_request_branch`, `merge_pull_request`, `fork_repository`,
`create_repository`. `merge_pull_request` is the awkward one — the ref moves
server-side, so a git proxy watching the wire never sees it. The rest are
`create_pull_request`, `update_pull_request`, `issue_write`, `sub_issue_write`,
`add_issue_comment`, `add_comment_to_pending_review`,
`add_reply_to_pull_request_comment`, `pull_request_review_write`.

**Four tools are dispatchers rather than operations**, two on each side of the flag:
`pull_request_read` takes a required `method` enum of nine values (`get`, `get_diff`,
`get_status`, `get_files`, `get_commits`, `get_review_comments`, `get_reviews`,
`get_comments`, `get_check_runs`), `issue_read` five (`get`, `get_comments`,
`get_sub_issues`, `get_parent`, `get_labels`), `pull_request_review_write` five
(`create`, `submit_pending`, `delete_pending`, `resolve_thread`, `unresolve_thread`)
and `issue_write` two (`create`, `update`). Anything keyed on the tool name alone
decides all nine together.

`make mcp-tools` surfaces these by printing any **required enum**, which is a broader
net than "dispatcher" and deliberately so — but the two are not the same thing, and
`add_comment_to_pending_review`'s `subjectType: FILE, LINE` is the case that shows it:
a parameter with two legal values, not one name standing for two operations.

**`owner` and `repo` are annotated `x-mcp-header` on 19 of the 25**, i.e. the server
accepts them as HTTP headers and not only as body fields. With the write tools exposed
it is **34 of 41** — so 15 of the 16 writes carry it too, and the annotation is not a
read-path convenience. The sole write-side exception is `create_repository`, which has
no `owner` to pin because it makes a repository rather than acting on one; nothing is
lost by it.

Whether a header *overrides* a conflicting body field is **not measured**, and that is
the whole question: 34 tools accepting a header means nothing if the body wins.
Overriding would let repo scope be pinned in the transport, for writes included, rather
than by validating arguments.

The six read tools with neither field — `get_me`, `get_teams`, `get_team_members`,
`search_code`, `search_commits`, `search_repositories` — are the ones no repo scoping
reaches at all, by either route.

**55% of the reply is icons**: 36.7 KB of a 67 KB payload, base64 PNG, two per tool
(light and dark). The share is per-tool rather than fixed overhead — the 41-tool reply is
113,844 bytes and still 55%. The result also declares `ttlMs: 0` and
`cacheScope: "public"` — the server's own answer to whether its list may be cached is no.

## `mcp-github` v1.9.0 → v1.12.2: lockdown was never on, and what else moved

Read from the upstream source and tool snapshots (`pkg/github/__toolsnaps__/`) at both
tags, not measured against a running container.

**`GITHUB_LOCKDOWN_MODE` was inert on v1.9.0.** In http mode the effective flag was
`d.lockdownMode && ghcontext.IsLockdownMode(ctx)` (`pkg/github/dependencies.go`) — the
server setting AND a per-request `X-MCP-Lockdown` header, which nothing in dockade sends.
v1.10.0 (github/github-mcp-server#3112) made it `||`, so the server setting is an upper
bound on its own. It is the same shape as the read-only flag through v0.32.0: set,
documented, and doing nothing. The consequence of the bump is that lockdown turns ON —
public-repo content from authors without push access is filtered from results.

**Tool surface within `context,repos,issues,pull_requests`**, from the snapshots:

- two new write tools in the snapshots: `update_issue_comment`, and
  `delete_repository`. The latter completes only through a multi-round-trip
  elicitation read from `req.Params.InputResponses`, which the gateway does not relay
  (it sends `name` and `arguments`, nothing else), and needs the `delete_repo` scope —
  and the running server does not list it at all (below);
- `merge_pull_request` gains optional `expectedHeadSha` (GitHub rejects the merge if the
  head moved); `issue_write` gains `parent_*` for atomic sub-issue creation;
  `create_or_update_file` gains `allow_symlink_write`, and symlink writes now need it;
- no dispatcher's `method` enum grew. `pull_request_read`, `issue_read`,
  `pull_request_review_write` and `issue_write` keep the operation sets listed above.

**With `GITHUB_MCP_TOOLSETS=all` and `GITHUB_MCP_READ_ONLY=0`** — measured with `make
mcp-tools SERVER=github` on v1.12.2: 90 tools, 34 not read-only. Against the snapshots:

- `delete_repository` is NOT listed. The server withholds it here — the snapshot has
  it, the running server does not offer it — so it is absent rather than merely
  unable to complete. The three reaction removals are absent too: they are in the
  granular toolsets, which `all` does not include;
- new write tools that ARE listed: `update_issue_comment`, and from a new Governance
  toolset `create_repository_ruleset` and `custom_properties_write`, beside
  `custom_properties_read` and `repository_ruleset_read` — the last a new
  dispatcher, five read methods under one name;
- `projects_write` gains `create_project_view`, `update_project_view` and
  `delete_project_view`, so a rule written for it on v1.9.0 now also decides deleting
  a project view; `projects_get` and `projects_list` gain view reads;
- the two notification-subscription tools now declare `destructiveHint: true`.

**Per-call OAuth scope challenges (v1.11.0) do not apply to a PAT.** The middleware acts
only on `gho_` OAuth tokens. The classic-PAT tool filter still skips itself when the
scope lookup fails, so the gateway's dummy `ghp_` enumeration is unaffected; a real
classic PAT is filtered under the rewritten visibility rules, a fine-grained one not at
all.

**The server's new request-body cap is 5 MiB** (github/github-mcp-server#3111), above
the gateway's own, so the gateway's stays the one a caller meets.

## A stateful MCP server talks back on a call's stream

Measured by 2026-11-21 in the tier-1 sandbox, against the reference Python SDK (PyPI
`mcp` 2.3.0, `MCPServer`, streamable HTTP, stateful, on loopback).

**A tool that elicits sends the request on the call's own stream and waits.** A tool
calling `ctx.elicit(...)` sent `elicitation/create`, carrying id 1, on the `tools/call`
POST's SSE stream, then held the call open until the client answered. It did so even
when the client had declared no elicitation capability. With no answer the call hung.
POSTing a JSON-RPC error back for that id, in the same session, made the server end
the call at once with an error result.

**Not every server request comes that way.** The SDK sends `sampling/createMessage` to
the standalone GET stream instead. A stateless server (`stateless_http=True`) refuses
elicitation at once with "this transport context has no back-channel".

**The SDK keys each POST's response stream by request id.** With two overlapping
`tools/call`s in one session both carrying id 1, one result was lost and its call
hung, although the tool ran. With ids 1 and 2, both returned. The spec forbids a
requestor reusing an id within a session.

**The SDK's SSE replies ping every 15 s**, so a call whose response never comes keeps
its stream busy, and a per-read timeout never fires on it.

## What a sandbox session and a cold build actually cost

Two sets of figures DESIGN.md used to carry inline, where they were both evidence for a
decision and a number waiting to rot. The decisions they justify — a modest memory
default plus a per-workspace override, and no Docker layer cache in CI — are in
DESIGN.md; these are the measurements behind them, taken on the reference machine
(15 GiB, WSL2).

**A tier-1 session peaks nowhere near its cap.** A measured session — linters, a
68-test project suite, ~25 compiles — peaked at **353 MB** against a 4g default. The
agent process is not what needs the headroom; a `tsc`, `jest`, `cargo` or language
server on a large tree is, which is why the cap is a launch-time override rather than a
number sized for the worst case. Tier 2 is lower still on the merits: opencode is a
thin client and the tier has no egress, so `npm install` / `pip install` cannot fetch,
and its workload cannot grow the way tier 1's can.

**The cold build is minutes, not tens of minutes.** ~**2m 22s** total with no cache: 19s
for the compose services, 68s `claude-sandbox`, 54s `opencode-sandbox`. That is the
number that settled the CI cache question — the estimate that would have justified
diverging CI from `make verify-build` was 5–15 minutes. Image sizes from the same run:
both sandbox tiers ~**1.2 GB**, the services **142–255 MB**. Both tiers were large for
the same reason, and it is the toolchain they share from `sandbox-common` rather than
anything about what either tier is allowed to do. Tier 1 has since gained a browser,
measured in the next section.

## Chromium in the tier-1 image: what it costs, and what it calls

Measured 2026-09-30 on the reference machine. The shell measured was
`154.0.8037.57-1~deb13u1`; the full browser came from the same archive the same day.

**Size.** Each Debian package installed as one layer on top of the existing 1.20 GB
`claude-sandbox`: `chromium` adds **701 MB** (42s to build the layer),
`chromium-headless-shell` **542 MB** (31s). The image this repo builds with the shell
came out 543 MB over the one without. The gap is smaller than the names suggest, most
likely because most of either is the engine and the libraries they share. What the
extra 160 MB buys is the current headless mode: Chrome 132 removed the old one from
the browser, and it survives only as the separate headless-shell build, which Debian's
package description calls the "old headless shell"
([Chrome's announcement](https://developer.chrome.com/blog/removing-headless-old-from-chrome)).
It is not abandoned — it is built for every Chrome release, it gets the same Debian
security updates (one source package), and Playwright uses its own pinned copy of it
for headless runs by default — but it is a separate implementation from the browser an
operator runs. Both logged a screenful of D-Bus connection errors on every run we made
(there is no bus in the container) without being affected by it.

**Under the launcher's limits, either build leaves most of the budget.** Inside a
sandbox started by `run-claude-sandbox.sh` (no capabilities, `no-new-privileges`, 4 GiB
with no swap, 512 pids), with the browser holding the control-plane UI open — every
module loaded — and `make test` running:

| Build | Threads | Processes | PSS | Container anon, peak |
| --- | --- | --- | --- | --- |
| `chromium` | ~110 | 9 | 391 MiB | 216 MiB |
| `chromium-headless-shell` | 69 | 7 | 199 MiB | 129 MiB |

The rows are not quite like for like: the full browser ran with the quieting flags
listed below and the shell with none, though the full browser's one unflagged run came
out much the same (113 threads in 9 processes). Claude Code adds 13 threads of its own;
with it and the full browser the container peaked at **139 of 512** tasks. The pids
limit counts threads, so what would reach it is concurrent browsers: with Claude Code
and `make test` holding about 24, seven shells fit and an eighth would not, four full
browsers and not a fifth. PSS came out higher than everything the container was
charged, most likely because it counts the browser's share of its mapped binary, whose
page cache the kernel had charged to an earlier container that loaded it first. The
anon column is the whole container, `make test` included, Claude Code not.

**Full Chromium calls Google at start, and flags do not stop it.** Through `https_proxy`
each call is a held request — `www.google.com`, `update.googleapis.com`,
`redirector.gvt1.com` and `clients2.google.com` arrived as approval cards on the first
run. With the standard quieting flags (`--disable-background-networking`,
`--disable-component-update`, `--disable-sync`, `--disable-default-apps`,
`--disable-domain-reliability`, `--no-first-run`, `--metrics-recording-only`) its netlog
still names `accounts.google.com`, `android.clients.google.com`,
`content-autofill.googleapis.com`, `optimizationguide-pa.googleapis.com`,
`update.googleapis.com`, `clients2.google.com` and `www.google.com`; with
`--no-proxy-server` those went direct and failed at DNS. Part of the reason is
`/etc/chromium.d/`, which Debian's `/usr/bin/chromium` wrapper sources on every launch:
it exports Debian's public Google API keys, presumably what switches on the signed-in
services among those hosts. The same directory adds `--disable-dev-shm-usage` whenever
`/dev/shm` is under 3.8 GB, which covers the sandbox's 64 MB.

**The headless shell calls nothing.** The same netlog, the same page, named no host
but the page's, and 20 seconds of an idle shell under the real proxy settings raised no
approval card. It gets neither half of the above: `/usr/bin/chromium-headless-shell` is
a script that does not source `/etc/chromium.d/`, and the package installs nothing
there — so no API keys, and no automatic `--disable-dev-shm-usage`, which a caller must
pass itself inside a 64 MB `/dev/shm`.

**The shell ignores `--no-proxy-server`.** It takes its proxy from the environment:
`https_proxy` for the outside (asked for `https://1.1.1.1/` with `--no-proxy-server`
set, it raised an approval card for 1.1.1.1) and `no_proxy` for the local page, which
loaded direct. Two things do keep it off the proxy, each checked by the absence of a
card for the same URL: `--proxy-server=direct://`, and running it with the four proxy
variables unset. So the netlog run above, flag and all, was already under the real
proxy settings.

**Playwright drives Debian's shell without its own download.** Measured 2026-10-01:
`playwright-core` 1.63.0, which pins Chromium 153, launched Debian's
`chromium-headless-shell` 154 through `executablePath` with
`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` set, with `--no-sandbox` (its reason is in
`claude-sandbox/Dockerfile`) and the two flags above, and loaded a page in about 0.3 s.
The package is a single one with no dependencies, 14 MB installed. Through one version
of skew, 1.63 drives the 154 shell through the whole UI harness scenario.

**The browser the pinned version names agrees with Debian's on the expected-value tests.**
Measured 2026-11-08, on the first `ui-e2e` run in CI (`make ui-test` on
`ubuntu-latest`, node 24.22.2): `playwright-core` 1.63.0's own install fetched Google
Chrome for Testing's headless shell 153.0.8010.12, a 114.3 MiB download, plus a 2.3 MiB
FFmpeg the tests do not use. The whole install step, its apt libraries included, took
15 s. The 19 tests in `tests/ui-harness/approval.test.js` passed in 20.2 s. The same
tests pass on the tier-1 image's Debian shell, since moved to 156.0.8078.79, so three
versions of skew changed no verdict. The tests assert on DOM, text and requests, never
pixels.

## The UI harness had three races, all between it and the page

Measured 2026-11-04 and again 2026-11-05 on `playwright-core` 1.63.0 and the Debian
shell above, with the harness as it was before it queued answers. No page reads a time
the clock does not fake: every read in `control-plane-ui/`, `tasks-ui/` and
`ui-common/` is `Date.now()` or `new Date(n)`, which `page.clock` covers. The
differences between runs came from three races, each made visible by delaying every
answer at the route by a random 0–60 ms:

- **A step could end between an answer and the request it causes.** A settle that
  counts only requests open at the route sees nothing in the turns where the page is
  reading a body or deciding what to send next, and the request then went into the
  next step's events or, if it arrived while a snapshot was taken, nowhere. In 6 of
  12 undelayed sequential tasks runs a read moved or went missing, mostly a
  `GET /api/notes/flagged` after a write, and in one the read answered late changed
  the title's count. Under the delay the approval page lost the refreshes after
  "server enabled" (two steps before the one reported) or "tool rule widened", and the
  tasks page lost reads at most of its steps.
- **Concurrent reads are answered in the order the network gives.** The page's
  module files arrive in any order, so of 20 undelayed sequential approval runs 6
  differed only in the order the modules were listed. Under the delay the tool picker
  was drawn from the servers and inventory before the tool rules arrived, chose
  `get_me` as its first unruled option, and kept that choice when the rules came (2 of
  10 runs): the state reported at "tool rule picked", there from the first step.
- **`page.clock.runFor` is not atomic.** It yields a real task turn after each timer
  it fires, so an answer that arrives during the run is handled at whatever fake time
  the run has reached. A lease row drawn mid-run and one drawn after it floored a
  second apart; under the delay every one of 10 approval runs read its countdowns a
  second off the undelayed ones at the clock steps after the lease steps.

Playwright's clock offers `runFor`, `fastForward` and `pauseAt`, and nothing that stops
at the next timer. Holding every answer to quiet costs about 50 ms of real time per API
request: an approval run went from about 7 s to about 15 s, a tasks run from about 8 s
to about 21 s (10 sequential runs each, 2026-11-06).

In 8-way parallel runs the harness failed or hung (task 236); 4 at once is the most
these measurements used.

## The JDK variant: what it costs, and what its JUnit jar bundles

Measured 2026-11-01 on the reference machine: `openjdk-21-jdk-headless` (`javac
21.0.13`) and `junit-platform-console-standalone` 1.13.4.

**Size.** `claude-sandbox-jdk21` came out 2012 MiB against the default image's
1721 MiB: **291 MiB** for the JDK and the jar.

**Memory stays small at the sizes measured.** Each run was a `docker run` of the
variant with the launcher's resource flags (4 GiB with no swap, `--cpus` at most 4,
512 pids) and `--network none`, not a launcher session.
`memory.peak` is the container cgroup's peak, page cache included.
- Compiling a 4-test JUnit 5 sample and running it with the console launcher peaked at
  **108 MiB**.
- Compiling a real libGDX `core` module (4 source files against 3 dependency jars,
  copied by Gradle on the host; it had no tests) peaked at **85 MiB**.

Neither run approaches the JVM's default heap, a quarter of the container's limit;
no larger suite was measured.

**The console jar bundles JUnit 4, not only JUnit 5.** Its
`META-INF/shadowed-artifacts` lists the same artifacts in 1.13.4 and 1.14.4, the Jupiter,
Platform and Vintage ones each at its own release's version:
- `junit:junit:4.13.2` and `org.hamcrest:hamcrest-core:1.3`;
- of its release, the Jupiter api, engine and params; eight Platform artifacts
  (commons, console, engine, launcher, reporting, suite-api, suite-commons,
  suite-engine); and the Vintage engine, but not `junit-platform-testkit`;
- `opentest4j`, `apiguardian-api` and `open-test-reporting-tooling-spi`.

A project's own copies of these would sit on the classpath beside the bundled ones,
which is why the README's Maven copy excludes `junit` and `hamcrest-core` along with
the JUnit 5 groups.

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

## WSL2 in mirrored networking mode accepts a connection to a closed loopback port

On the development host (`wslinfo --networking-mode` prints `mirrored`), binding an
ephemeral port on `127.0.0.1`, closing it, and connecting to it succeeds:
`connect_ex` returns `0` where Linux returns `111` (`ECONNREFUSED`). An HTTP request
sent over that connection then fails with `ECONNRESET` when the response is read.
Measured 2026-09-24.

So on this host "nothing listens there" cannot produce a refused connection, and
anything that treats a refusal as proof nothing was delivered sees a reset instead.
The gateway's `_ask_control` reads that as "maybe delivered" (`Unanswered`), which is
the safe reading of what it observed. The one test that needs a real refusal,
`test_a_refused_connection_is_not_delivered`, checks for this first and skips.
Traffic between the containers crosses Docker bridge networks rather than the
distro's loopback, so this concerns tests and tools run on the host distro.

## A worktree made in the sandbox is unreadable to git on the host

The sandbox sees the checkout at `/workspace/<folder-name>`; the development host has
it on a case-insensitive `/mnt/c`. A `git worktree add` in the sandbox writes absolute
paths both ways (the worktree's `.git` says
`gitdir: /workspace/<folder-name>/.git/worktrees/<name>`), and
container git (2.47.3) predates `worktree add --relative-paths` (2.48). On the host,
every git command inside such a worktree fails, so the doc guards' `_tracked()` returns
nothing and each fails its `checked > 0` assertion. Seen 2026-09-30.

- **Host git can delete them.** It lists the sandbox's worktrees as `prunable`, since
  their `/workspace/...` paths do not exist there, and `git worktree prune` deletes their
  `.git/worktrees/<name>` entries, which breaks them inside the sandbox. Reproduced
  2026-10-01 in a throwaway repo with a gitdir pointed at a missing path: `prune -v`
  printed `Removing worktrees/wt: gitdir file points to non-existent location`. `git
  gc` left that entry alone while it was fresh, and removed it once its index file was
  backdated past `gc.worktreePruneExpire` (three months by default), so a sandbox
  worktree idle that long goes at the next host `gc`, the automatic one included.
- **The other direction fails the same way.** A worktree made on the host and
  launched as the workspace has a `.git` file naming a `/mnt/c/...` gitdir the
  container does not have, so every git command run inside it fails in the sandbox.
  Seen 2026-10-01, when such a launch died at boot in the entrypoint's git identity
  block; that block now runs from `/` (see its comment).
- **A move of the mount breaks them, and `git worktree repair` mends them.** Probed
  2026-11-09 with git 2.47.3, while planning the move from `/workspace` to
  `/workspace/<folder-name>`, and re-run 2026-11-17: once the checkout is at its new
  path, `git worktree repair` run in it, given each worktree's *new* path, exits 0 and
  repairs it, and the worktree no longer shows `prunable`. Given the old path, the one
  `git worktree list` still shows, it fails with "Invalid path" and exit 128. A locked
  worktree needs no unlock first, and stays locked. A worktree made on the host keeps
  showing `prunable` from inside the container whatever is repaired, for the reason
  above.
- **A worktree carries only tracked files.** The main checkout's `.env` (the LLM
  model, the GitHub server's toolsets and read-only flag) and `models/` are not in it.
  The Makefile's compose reads `.env` from the directory it runs in, so compose run
  from a worktree falls back to the defaults (`mcp-servers.yml`'s read-only, four
  toolsets), and `DOCKADE_MODELS_DIR` to the worktree's empty `./models`. The compose
  file names the project `dockade` wherever it runs, so the containers it starts
  replace the live stack's and outlive the worktree; and it names its volumes
  (`dockade-control-state` among them), so the branch's code runs against the live
  policy and audit store. A branch that migrates the store past `main`'s schema
  leaves `main`'s control plane refusing to start ("newer than this code's"). That is
  compose's documented lookup, read against `.env`'s keys, the compose file and
  `control-plane/store.py`; Docker was not run.
- The host has no node. Under strict mode (`DOCKADE_REQUIRE_TOOLS=1`) the `app.js`
  tests error there instead of skipping, which stops `make check` before
  `verify-build`; they run in the sandbox instead.

## A worktree-isolated session refuses a command that might run git elsewhere

In a session isolated to a worktree, Claude Code reads each Bash command before it
runs and refuses one where it cannot show that git stays in that worktree, with a
message naming the reason. Nothing runs. Which forms it refuses has differed between
observations; see below.

**2026-11-06, Claude Code 2.1.319**, a subagent launched with `isolation: "worktree"`
(how this repo's workers run), in bypass-permissions mode, with git run only on
throwaway repositories under `/tmp`:

- Refused: setting `HOME` before git ("injecting git configuration"); `$(…)` around
  `git worktree list`; a `for` loop whose body runs `git -C "$d"`; an unquoted
  variable as a `sed` argument, with no git in the command; `bash -c '… source x'`; a
  Python heredoc whose text contains the word git; `git status --short; echo $X`,
  where `echo $X` alone was allowed.
- Allowed: `git -C` on a repository under `/tmp`; `cd /tmp/… && git …`; `git init` and
  `git clone` to a path under `/tmp`; two git commands chained with `&&`; `D=…; git
  -C "$D" …`; a `for` loop whose body runs `git worktree list` with literal arguments;
  a double-quoted variable as a `sed` argument; `git diff A B > /tmp/file`; `git log
  … | python3 -c '…'` whose Python text does not name git; `head` and `sed` on another
  checkout's files. Python tests that spawn git themselves (`make test`) are not
  inspected.
- Not re-checked: `git -C` on the shared repository or another checkout, and a
  session moved into a worktree with the `EnterWorktree` tool, which a subagent
  pinned to its own worktree can only use to switch into another existing one.

**2026-10-01**, a session in a named worktree under the shared repo, most likely moved
there with `EnterWorktree`; release and permission mode not recorded. Refused then:
`git -C <other path>`, `cd <tmp> && git …`, setting `HOME` before git, a `for` loop or
`$(…)` around `git worktree list`, a variable as a `sed` argument, `bash -c '… source
x'`, and a Python heredoc naming git. Of those, `cd /tmp/… && git …`, `git -C` on a
`/tmp` path, and a `for` loop around a literal `git worktree list` passed on
2026-11-06 in a subagent; whether the change is the release or the setup is open.

What held both times: one plain command per call, run from the worktree; literal
paths in place of variables; `git diff A B > /tmp/file` for each side, then `diff`
the files; reading another checkout's files with `cat` or `sed`. Outside Bash,
the `Edit` tool made the edits a Python rewrite naming git would have been refused
for; git in another checkout is left to the maintainer.

## Stock WSL2 kernels ship `ip_set` without the `xt_set` match

`ipset create` succeeds, and `iptables -m set --match-set` then fails with `Can't open
socket to ipset`: the kernel carries enough of `ip_set` for the userspace tool and not
the `xt_set` match module. It is a kernel gap, not a container one — `ipset` and
xtables need `NET_ADMIN`, which the firewall step holds, and never `NET_RAW` — so no
capability makes the match work. The first `--match-set` rule is the probe that tells
the two apart (`init-firewall.sh`); the fix is a kernel with the module, or a path
that needs no ipset at all, which is what governed mode is.

## Docker's dynamic IP is the lowest free one, so it collides with `.2`

A container attached to a user-defined bridge without an `ipv4_address` is not given
an arbitrary address: the allocator walks the subnet and hands out the lowest free
one. On a `/24` the gateway takes `.1`, so the first dynamic member gets `.2` — the
same address a hand-pinned member is most likely to have been given, because `.2` is
also the obvious thing to write down. A network that mixes a pinned member with a
dynamic one is therefore not "mostly fine": the two want the identical address.

Which one gets it is start order, and `depends_on` does not decide that after a host
reboot. It orders `compose up`; a reboot has the daemon bring `restart: always`
containers back on its own, in an order that need not match. So the collision is
invisible in every tested path and appears only on a reboot, as:

    "Error": "failed to set up container networking: Address already in use"

That message reads like a published *host port* conflict, which is the expensive part
— the stack it appeared in published exactly one port, from a container that started
fine. It is container-side IPAM, and the container it collides with is whoever is
holding the address:

    docker network inspect <net> \
      --format '{{range .Containers}}{{printf "  %-18s %s\n" .Name .IPv4Address}}{{end}}'

That view is the allocator's own record, so it also shows an address held by a stale
endpoint with no live container — the other way this fails after an unclean shutdown,
cured with `docker network disconnect -f` or by recreating the network.

## Docker's embedded resolver refuses to forward off an internal network — and `dig` exits 0 saying so

Measured inside a governed tier-1 sandbox on `sandbox-net` (`internal: true`), Docker
Engine on WSL2, dig 9.20.29. `/etc/resolv.conf` names `127.0.0.11` and *does* list the
host's upstreams in its comment block — `ExtServers: [192.168.1.1 192.168.68.1]` — so
the file reads as though forwarding were configured. It is not:

    $ dig example.com
    ;; ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 47537
    ;; WARNING: recursion requested but not available
    ;; Query time: 0 msec

    $ echo $?
    0

Two things worth keeping. First, the refusal is immediate (0 ms) and comes from the
embedded resolver itself, which answers but will not recurse for a container whose only
network is internal — the engine's behaviour, not anything configured here. The
`ExtServers` comment describes what the resolver *knows*, not what it will *do*.

Second, and the trap: **`dig` exits 0 on SERVFAIL.** It exits non-zero only when it
reaches no server at all (rc 9). So `if dig name >/dev/null; then` reads a working
refusal as a successful lookup — precisely backwards for a leak check. The three
usable signals:

| probe | forwarding works | refused (SERVFAIL) | no resolver |
|---|---|---|---|
| `getent hosts NAME` | rc 0 | rc 2 | rc 2 |
| `dig +short NAME` | address on stdout | empty | empty |
| `dig NAME` \| `status:` | `NOERROR` | `SERVFAIL` | line absent |

`getent` is the one to assert a leak on: it walks the same NSS path curl, git and ssh
do, and it separates "resolved" from "did not" by exit status. It cannot, though,
separate a resolver that refuses from one that is absent — both are rc 2 — so a leak
check built on it alone passes vacuously in a container with no DNS. The `status:`
line is what distinguishes those, which is why `boundary-check.sh` reads both.

Note also that the container's own hostname resolves from `/etc/hosts` (Docker writes
it there), so resolving it proves nothing about the resolver being alive.

## Docker drops a packet routed to another network's address on a shared neighbour

Measured on the maintainer's host (rootful Docker Engine on WSL2) on 2026-10-12, by the
task registry's host check. A throwaway `alpine:3` container on `mcp-net`, given
`--cap-add NET_ADMIN`, routed the operator network through the registry, whose other
leg is on it, and dialled the registry's operator listener there:

    ip route add 172.26.0.0/24 via 172.28.0.3
    wget -T 4 -O- http://172.26.0.2:8111/api/repos
    Connecting to 172.26.0.2:8111 (172.26.0.2:8111)
    wget: download timed out

The kernel inside the registry would take such a packet (Linux answers for any of its
addresses on any interface), so the drop is the engine's, on this host; which rule does
it, the bridge's netfilter or an internal network's isolation, was not looked for. One
host, and nothing here leans on it: the registry refuses an off-network peer
(`task-registry/serve.py`, `OnlyFrom`), and every service drops the capabilities that
routing or forging such a packet takes (`EveryServiceDropsEveryCapabilityTests`).

## A unix socket under a read-only bind mount still accepts connections

Read-only stops writes to regular files, directories and symlinks; a unix socket's
`connect()` and a FIFO's `write()` only need write permission on the inode, which a
read-only mount does not take away. Measured on the development host (WSL2) on
2026-10-01: a host process listened on `/tmp/sockref/s`, and a container run with
the host uid and `-v /tmp/sockref:/r:ro` connected to `/r/s` and sent a message,
which the host process received (`host got: b'through ro'`).

So mounting a directory read-only does not make it safe to hand over if it holds a
socket a host process serves — a tmux server or an ssh-agent takes commands from any
peer with the same uid. This is why the read-only mount guard refuses the runtime
directories and directories with sockets near the top (`_sc_ro_mount_reason` in
`sandbox-lib.sh`).

## An allowed read directory loads nothing from it

Measured in-container on Claude Code 2.1.319, 2026-11-06. Each run was a nested
`claude -p --permission-mode default --output-format stream-json --verbose
--strict-mcp-config`, with real credentials copied into a scratch
`CLAUDE_CONFIG_DIR` (deleted after the run), a scratch cwd, and the repo's
`claude-sandbox/user-settings.json` as the scratch `settings.json`, its `/refs`
rewritten to a scratch refs root. Unless a row says otherwise, a launch-shaped
`--settings` file was in front: `extraKnownMarketplaces` and `enabledPlugins`, plus
one `SessionStart` hook so its loading shows. The fixture ref `<root>/demo/` held
`.claude/` with a canary skill, agent and command and a `settings.json` hooking
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` and `Stop`, and a
canary `CLAUDE.md` in `demo/` and `demo/src/`. Each run read `demo/x.txt` (and where
it says so `demo/src/y.txt`), then was asked to list any canary skill, agent or
command, so the answer comes after the reads. "Loaded" is the canaries in the
`init` event and the final answer; "fired" is the files the hooks touched; the
`CLAUDE.md` canary asked for a marker word that appeared only in the control.

| run | the allow | reads | loaded | hooks fired |
| --- | --- | --- | --- | --- |
| no allow | none | refused: "Claude requested permissions to read from …, but you haven't granted it yet." | nothing | launch `SessionStart` |
| user scope | the template's key naming `<root>` | both run | nothing | launch `SessionStart` |
| user scope, naming `demo/` itself | the key naming `<root>/demo` | both run | nothing | launch `SessionStart` |
| project scope, untrusted cwd | the key in `<cwd>/.claude/settings.json` | refused | nothing | launch `SessionStart` |
| project scope, trusted cwd | the same | both run | nothing | launch `SessionStart` |
| command-line flag on `demo/` | the flag, no key | runs | skill, agent, command; not `CLAUDE.md` | launch `SessionStart` |
| control: cwd is `demo/` | none | runs | all, and `CLAUDE.md` | launch, and all five of the ref's |

- **Untrusted, the project key is ignored aloud.** Stderr: "Ignoring 1
  permissions.additionalDirectories entry from .claude/settings.json: this workspace
  has not been trusted." Trusted, it works but is missing from the `init` event's
  `additional_directories`, which lists user-scope and flag entries.
- **An allowed directory that does not exist is dropped** from `additional_directories`
  with nothing on stderr, under `-p`. Interactive startup in the real image with no
  refs mounted showed no warning either (host check below).
- **A caller's own `--settings` merges this key; it does not replace it.** With the
  template's allow at user scope and a `--settings` file (no hook) allowing a second
  root, `additional_directories` listed both and both reads ran; the launch hook did
  not fire, since that file was not given. So "the last `--settings` replaces the
  first" (above, "`--settings` is command-line scope") is between flag files: user
  scope's allow survives a caller's file whatever it carries.
- **Interactive, in the real images, on the host (2026-11-06).** Tier 1, Claude Code
  2.1.319, plain `claude` behind the wrapper, with a canary ref of the same shape
  mounted at `/refs/canary`: neither read asked, no ref hook fired, no canary skill,
  agent or command was in the reply or the `/` menu, and the `CLAUDE.md` marker
  never appeared. With no refs mounted, startup showed no warning. Tier 2, opencode
  1.19.5, with the ref also holding a canary `AGENTS.md`: the config loaded without
  error, listing `/refs` and both reads asked nothing, and neither the `AGENTS.md`
  marker nor the canary skill loaded.

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

## Node's fetch reads the proxy variables only with `NODE_USE_ENV_PROXY`

Measured 2026-11-17 in the tier-1 sandbox, Node 24.22.3 (the image's nodesource
`setup_24.x`), against the governed proxy (mitmproxy 12.2.3) and against a stub proxy
on loopback. Without `NODE_USE_ENV_PROXY=1` (or `--use-env-proxy`, also through
`NODE_OPTIONS`), Node's `fetch` and its `http`/`https` agents read none of the proxy
variables: a fetch of allowlisted `api.anthropic.com` resolves the name itself and
fails `getaddrinfo EAI_AGAIN`, with no hold and no audit row. With it, the same fetch
gets the origin's 404 through the proxy; `sc_governed_egress` in `sandbox-lib.sh` sets
it. Both cases of the variables work, and `NO_PROXY` is honoured.

- **Plain `http://` with `fetch` is tunnelled**, as `CONNECT host:80`, so the port
  gate refuses it even for an allowed host (#348). `http.get` sends an absolute-form
  `GET`, governed as curl's is.
- **A local server** answers at `localhost` and `127.0.0.1`, which are in `NO_PROXY`.
  At the container's hostname or `0.0.0.0`, fetch and curl both go to the proxy and
  get its 403 (#350).
- **A refused `https.get` never exited**, though its error printed at once; `fetch`
  exits either way, and `https.get` to an allowed host exits. Two halves: after a
  non-200 CONNECT Node keeps its socket to the proxy open (`lib/https.js` destroys it
  only for an error without a status code; the same on Node's main branch, read; no
  upstream issue found), and mitmproxy keeps a refused CONNECT's connection open for a
  next request. Against the stub, a 403 or 503 left open held `https.get` until a 15 s
  `timeout` killed it; the same response with `Connection: close` and a close let it
  exit at once. No Node variable or option changes it. mitmproxy closes the client
  connection after a response carrying `Connection: close`, which every refusal from
  `_deny` in `proxies/egress/addon.py` now does.
- **The close, run against the governed proxy** with a raw socket: `CONNECT
  api.anthropic.com:8443` (the port gate's local 403, needing no rule or card) with
  `Connection: close` on the request got the 403 and EOF at once, by the same
  mitmproxy rule; without it the connection was still open 15 s later.
  `boundary-check.sh`'s port-gate probe requires the header on the refusal.
- **The close, run against the addon itself**: mitmproxy 12.2.3 from PyPI (the
  image's version, not its digest) running `mitmdump -s proxies/egress/addon.py` on
  loopback, as the image's entrypoint does, with the control plane unreachable. The
  port gate's 403 (`CONNECT api.anthropic.com:8443`), the relay guard's 403
  (`CONNECT control-plane:443`), the 503 for an unreachable control plane and a
  plain-HTTP port-gate 403 each came back with `Connection: close` and then EOF
  within 0.02 s, with no header on the request. Node's `https.get` with the variable
  printed the 403 or 503 and exited 0, and the port-gate probe passed. The same run
  against the addon before the header: no EOF in 5 s, `https.get` killed by a 10 s
  `timeout`, and the probe failed.
- **Claude Code under the variable**: `claude -p` (2.1.325) with
  `NODE_USE_ENV_PROXY=1` showed the `gateway` MCP server `connected` and answered.

What Node shows of a held host was not measured: it costs the operator a card.

## What a client shows of the proxy's two refusals

Measured 2026-11-16 in the tier-1 sandbox, against a stub proxy on loopback that
answers exactly as `_deny` in `proxies/egress/addon.py` does: the policy 403 (`egress
denied by policy`, `x-proxy-error: blocked-by-allowlist`) and the not-decided 503
(`egress not decided: … - retry later`, `Retry-After: 60`), each with a
`content-length` and the connection left open, as mitmproxy left it before every
refusal carried `Connection: close`. Every proxy variable, both cases, pointed at the
stub; the stub counted the requests, which is the "tries" column. The Node rows were
measured 2026-11-17 with `NODE_USE_ENV_PROXY=1`, both with the connection left open
and with the close.

| client | refused CONNECT, 403 / 503 | plain-HTTP 403 / 503 | tries (CONNECT; plain HTTP) |
|---|---|---|---|
| curl 8.14.1 | `CONNECT tunnel failed, response 403` (or `503`), exit 56; no body | the body, exit 0 (no `-f`) | 1; 1 |
| GNU Wget 1.25.0 | `Proxy tunneling failed: Forbidden` (or `Service Unavailable`), exit 4 | `ERROR 403: Forbidden.` (or `503: Service Unavailable.`), exit 8; no body | 1; 1 |
| git 2.47.3 | `CONNECT tunnel failed, response 403` (or `503`) | `remote: <the body>`, then `The requested URL returned error: 403` (or `503`) | 1; 1 |
| pip 25.1.1 | `ProxyError('Cannot connect to proxy.', OSError('Tunnel connection failed: 403 Forbidden'))` (or `503 Service Unavailable`), then `No matching distribution` | `No matching distribution` alone, for either status (needs `--trusted-host`) | 6 over ~8-11 s for either status; 403: 1, 503: 6 over ~319 s, a minute apart as `Retry-After` asks |
| npm 11.19.1 | `E403 403 Forbidden - GET https://…` (or `E503 503 Service Unavailable`): it reports the CONNECT status as the server's | the same words | 403: 2 at once, no retry; 503: 3 over ~75 s. Plain HTTP: 403: 1; 503: 3 over ~76 s |
| Python 3.13.5 `urllib` | `URLError: Tunnel connection failed: 403 Forbidden` (or `503 Service Unavailable`); no headers, no body | `HTTPError` with the status, the headers and the body | 1; 1 |
| Node 24.22.3 `fetch` | `fetch failed`, cause `Request was cancelled.`, its cause `Proxy response (403) !== 200 when HTTP Tunneling` (or `503`) | tunnelled as `CONNECT host:80`, so the CONNECT column | 1; 1 |
| Node 24.22.3 `https.get` | `Failed to establish tunnel to <host>:443 via <proxy>: HTTP/1.1 403 Forbidden` (or `503 Service Unavailable`); left open, the process never exits | (sends no plain HTTP) | 1; - |
| Node 24.22.3 `http.get` | (sends no CONNECT) | the response, with its status; exits either way | -; 1 |
| Chromium headless shell 156.0.8078.79 | `net::ERR_TUNNEL_CONNECTION_FAILED` for either status | renders the body as the page | 1; 1 |
| WebFetch, Claude Code 2.1.325 | 403 with the header: `Access to <host> is blocked by the network egress proxy.` (`error_type` `EGRESS_BLOCKED`); 403 without it, and 503: `proxy refused the connection` | upgrades `http://` to `https://`, so the CONNECT column | 1; 1 |

So only a plain-HTTP response shows the body (a Node `fetch` has none, since it
tunnels plain HTTP too), and over HTTPS, which is nearly everything, the status is
what a client has: curl, wget, git, pip, npm, urllib and Node print it, Chromium and
WebFetch do not. WebFetch is the one client that reads the
header, and it reads only the header: the pre-change 403, which had none, and the 503
both reach it as `proxy refused the connection`. Its domain preflight goes to
`api.anthropic.com`, which the proxy passes on the lifeline. WebFetch's own 60 s
timeout is inside the control plane's default hold window, so a fresh hold reaches it as
a timeout and never as the 503; only a request that joins a card late in its window
can see the 503. That was not measured here, as no stub holds.

Retrying by itself: pip's urllib3 retries a refused CONNECT whatever the status, so
over HTTPS the 503 changes nothing for it; over plain HTTP it retries the 503 five
times, waiting the `Retry-After` minute each time. npm retries a 503, over either
transport, twice with backoff, and never a 403. Nothing else retried. On a real proxy
each retry after an expiry is a new request, so it raises a new card.

A site's own 403 is a third shape, and it is not the proxy's: the CONNECT succeeds and
the 403 comes over TLS. Seen 2026-11-10 on a site behind a Cloudflare bot
challenge (`server: cloudflare`, `cf-mitigated: challenge`): WebFetch showed `The
server returned HTTP 403 Forbidden`, and `curl -D -` showed the `200 Connection
established` and then the origin's `403`. Neither carries `x-proxy-error`.

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

## mitmproxy decodes a raw path byte to a lone surrogate

Measured against `mitmproxy 12.2.3`, the version pinned in `proxies/egress/Dockerfile`,
by parsing synthetic request heads through `read_request_head` in a venv.

An origin-form request line with a raw `0xff` in its path, `GET /a<0xff>b HTTP/1.1`,
parses. That is the form a request takes inside an intercepted CONNECT tunnel, so the
agent can send it to any host it may reach. The path is decoded with
`surrogateescape`, so `path` and `pretty_url` carry the lone surrogate U+DCFF where the
byte was. `json.dumps` writes it as an ASCII escape, and `json.loads` turns the escape
back into the surrogate, which UTF-8, and so SQLite, cannot encode. The proxy sends
`pretty_url` to `/authorize` as `url` and writes it to its audit line.

The absolute form, `GET http://example.com/a<0xff>b HTTP/1.1`, is refused at parse
with `ValueError: Bad HTTP request line`.

## mitmproxy binds an IP-literal listener before any script loads, and swallows an exception from `load`

Measured against `mitmproxy 12.2.3`, the version pinned in `proxies/egress/Dockerfile`,
by running `mitmdump` with the egress addon and its entrypoint flags, except that it
listened on `127.0.0.1:18080` rather than `0.0.0.0:8080`, with `socket.listen` and
`script.load_script` wrapped to log when they run.

**With an IP-literal `--listen-host`, the listener is bound before the addon exists.**
`Master.run` turns on asyncio's eager task factory, and `setup_servers` runs eagerly
while the script loader's watcher task has yielded once and not yet loaded the script.
With an IP literal, `asyncio`'s `create_server` skips `getaddrinfo` and reaches
`listen()` without suspending. `listen()` came before `load_script` in 20 runs out of
20 on `127.0.0.1`, and in 10 out of 10 on `0.0.0.0`, the Dockerfile's host. With a
hostname (`localhost`) or mitmdump's default empty host, `create_server` waits on
`getaddrinfo` in an executor, and the script loaded first in 10 runs out of 10. So with
the image's flags nothing in an addon's `load` can keep the port from opening; a check
that must run before the bind belongs before `mitmdump` starts. The
`HTTP(S) proxy listening` log line lags the bind, so its absence does not mean nothing
bound.

**An `Exception` from `load` does not stop mitmproxy.** `Script.loadscript` wraps
`addons.register` (which calls `load`) in `addonmanager.safecall()`, which re-raises
only `AddonHalt` and `OptionsError` and logs anything else as "Addon error". The
script's namespace is never kept, so none of its hooks is registered. An exception
while the module is imported is caught earlier, in `load_script`, and logged as
"error in script". Either way, what ends the process is a separate addon:
every master (mitmdump, `mitmproxy`, `mitmweb`) installs `ErrorCheck`, which records
ERROR-level log lines during startup. `Master.run` awaits its `shutdown_if_errored`
after the listeners are set up, and that calls `sys.exit(1)` on finding one.

**A `BaseException` from `load` passes `safecall`** and ends the event loop from
inside the script loader, with the exception's message as the last line on stderr.
The same holds while the module is imported: `load_script` catches only
`ImportError` and `Exception`, so a `SystemExit` raised at import passes it and
`safecall` both. Measured with a typo in each of four of the addon's settings
(`EGRESS_FORBIDDEN_CIDRS`, `EGRESS_CONNECT_PORTS`, `EGRESS_CONTROL_TIMEOUT`,
`EGRESS_AUDIT_BACKUPS`): exit 1, "Loading script" and then the message, nothing else.

What the window between bind and exit does, on `127.0.0.1` with a client that sends a
proxied `GET` to a local backend the moment `listen()` returns, 20 runs each:

| what fails | client connects | request reaches the backend |
|---|---|---|
| `load` raises `RuntimeError` (exits via `ErrorCheck`) | yes | 0 of 20 |
| `load` raises `SystemExit` | yes | 0 of 20 |
| import-time `ValueError`, from `int("44x")` (exits via `ErrorCheck`) | yes | 0 of 20 |

The process exits 1 every time. The control, a no-op script in place of the addon, did
forward, about 36 ms after `listen()`: the probe sees forwarding when it happens. The
zeros are measured, not structural. The paths through `ErrorCheck` run a few more loop
iterations before exiting, and nothing but timing keeps a request from getting through.

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

## Several `uvicorn.Server`s in one event loop all stop on SIGTERM

Running two servers from one `asyncio.gather` looks like it should break signal
handling, and the reasoning is sound as far as it goes: `serve()` wraps itself in
`capture_signals()`, which calls `signal.signal(sig, self.handle_exit)`, so the
second server installs over the first and only the second's `handle_exit` runs.

It works anyway, because `capture_signals()` is a context manager that cleans up
after itself in two steps. On exit it restores the handler it displaced — the
first server's — and then re-raises the signals it captured
(`signal.raise_signal`, LIFO). The re-raised SIGTERM lands on the restored
handler, so the first server shuts down too.

Measured on uvicorn **0.34.0**, two servers on one loop, `kill -TERM` on the
process: two `Finished server process` lines and the process gone in about 500 ms.

Re-measured on uvicorn **0.53.0** (2026-09-23, the bump from 0.34.0) with the
control plane's real three listeners, run from a venv rather than the image: three
`Finished server process` lines and the process gone in about 520 ms, against about
620 ms for 0.34.0 on the same three-listener run. `capture_signals` is unchanged
between the two versions — restore, then `signal.raise_signal` LIFO — so the chain
nests one level per `serve()` rather than pairwise.

Re-measured on uvicorn **0.54.0** (2026-11-21, the bump from 0.53.0), the same way,
five runs against five on 0.53.0 in the same session: three `Finished server
process` lines every run, and timings indistinguishable between the two: both land
in the same band of about 565 to 616 ms, on the same ~50 ms steps.
`capture_signals` is identical between the two; 0.54.0's changes are HTTP/2-only.

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

## `requireInteraction` buys persistence and costs a Close button

A web `Notification` created with `requireInteraction: true` stays on screen until it
is acted on instead of fading after a few seconds. Chrome answers that by rendering a
**Close** button on the notification itself — in addition to the dismiss affordance
every toast already has. The label is the browser's, so a page cannot reword it to
"Dismiss" or suppress it; the only lever is whether to ask for persistence at all.

Two facts that decide how much the persistence is worth on Windows:

- **A faded notification is not gone.** It moves into the OS notification centre and
  stays there, which is where someone who was away from the desk looks anyway. The
  choice is therefore between "on screen until handled" and "on screen briefly, then
  filed" — not between seen and lost.
- **`close()` reaches the notification centre too**, so a page that knows its notice
  has gone stale can withdraw it from there rather than leaving a question that was
  answered hours ago sitting in the bell menu.

Found by using it: the Close button is invisible in code review and obvious in the
first toast.

## An agent CLI's shell tool sees the shared `.bashrc`'s aliases only if its shell reads it

The shared `.bashrc` aliases `rm` to `rm -I`, and the alias reaches a command only
through a shell that reads `.bashrc` and expands aliases. A non-interactive
`bash -c` does neither. The probe, the same for every tier, is one command through the
agent's own shell tool:

    type rm; echo $-; shopt expand_aliases

- **Claude Code (tier 1): seen.** Measured 2026-11-20 on Claude Code 2.1.329 in the
  tier-1 sandbox, from both the main session's Bash tool and a subagent's. Each
  printed ``rm is aliased to `rm -I'`` (bash 5.2.37 opens the quote with a
  backtick), flags `hmBc` and `expand_aliases on`. The Bash tool runs each command
  in a shell built from its shell snapshot, which carries the aliases.
- **opencode (tier 2): not seen.** Measured 2026-11-20 with `opencode-linux-x64@1.19.6`,
  npm `latest` that day, which is what the image's `OPENCODE_VERSION=latest`
  installs. It ran in a scratch dir, with the XDG dirs under `/tmp` and `~/.bashrc`
  the shared one, and no model is needed:

      opencode debug agent build --pure --tool bash --params '{"command":"type rm; echo $-; shopt expand_aliases","description":"probe"}'

  It printed `rm is /usr/bin/rm`, flags `hBc` and `expand_aliases off`. Through the
  same tool, `rm a b c; echo exit=$?` printed `exit=0`, and all three files were
  deleted. The run without `--pure`, with `opencode-sandbox/opencode.json` as its
  config, gave the same. On the reference machine the same day, in a `make opencode`
  session on 1.19.6 with the image's own config, the command without `--pure`
  printed the same three lines. 1.19.5, on 2026-11-11, gave the same. The source
  says why: `cmd()` in `packages/opencode/src/tool/shell.ts` spawns the command with
  `shell` set to `Shell.acceptable(cfg.shell)`, which is `$SHELL` (`/bin/bash` in
  the image), so it runs `/bin/bash -c <command>`.

  The model-free debug command is the probe for opencode. Asking the model to run
  `type rm` is not: on the reference machine the image's local model, given that
  request, ran a different and destructive command through its shell tool, so that
  probe gave no result.
- **agy (Antigravity CLI, tier 1): not seen.** Measured 2026-11-20 on agy 1.5.13
  in the tier-1 sandbox, with a scratch HOME holding copies of the shared `.bashrc`
  and the image's `.profile` (the setup is in "agy behind dockade's proxy, measured
  before its tier"). No model-free way to call a tool was found in agy's CLI, so
  the model was asked, in print mode, to run the probe through `run_command`, with
  the output sent to a file:

      { type rm; echo $-; shopt expand_aliases; } > <file> 2>&1

  The model's reply was not the evidence. The stream-json events show the
  `run_command` call with that exact command line. The file was written by that
  command, and the same command's environment carried
  `ANTIGRAVITY_SOURCE_METADATA` naming that call. In each of two sessions the file
  read `rm is /usr/bin/rm`, flags `hBc` and `expand_aliases off`.

So the instructions' `rm` bullet holds for Claude's tier and is false for tier 2,
where a bulk `rm` deletes, and for agy's tier, so neither includes it. The bullet is the
`sandbox-common/instructions/bashrc-shell/rm.md` fragment, and the rule for which
tiers include it is in `sandbox-common/instructions/render.py`'s docstring. The
image installs opencode `latest`: a re-run on a later release that prints "aliased"
means tier 2 needs the include again.

## agy behind dockade's proxy, measured before its tier

Google's Antigravity CLI (`agy`), measured on 2026-11-20 on agy 1.5.13 (linux_amd64)
in the tier-1 sandbox, before any agy tier exists. Everything agy touched sat in one
scratch dir under `/tmp`: its HOME, its install, a fixture dir that was its cwd, and a
logging relay. Each run was `env -i` with only the scratch HOME, `PATH`, `SHELL`,
`TERM`, `LANG`, `TZ`, `USER`, `AGY_CLI_DISABLE_AUTO_UPDATE` and the proxy variables in
both cases. The proxy variables pointed at the relay, a small Python forward proxy on
`127.0.0.1` that logged every request line (CONNECT and absolute-form HTTP) and passed
it on to `egress-proxy`. `NO_PROXY` named the gateway as `sandbox-lib.sh` sets it.
The operator did the login and answered the hold cards; a session drove everything
else from Bash. A re-run on a later version repeats the commands below.

**Install.** `https://antigravity.google/cli/install.sh` (`/install.sh` on the same host
is a 404 page). Read, then run under `bash -x` with HOME in the scratch dir:

- It fetches a per-platform manifest,
  `antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json`,
  holding `version`, `url` and `sha512` and no signature. The `url` was a tarball on
  `storage.googleapis.com/antigravity-public/antigravity-cli/1.5.13-…/linux-x64/`.
  It checks the sha512 and nothing else.
- It extracts one binary, about 270 MB, to `$HOME/.local/bin/agy` (`--dir` moves it),
  then runs `agy install`, which here wrote nothing. No root is needed.
- Auto-update: every run spawns a background check against the same Cloud Run host,
  skipped if the last one was under 15 minutes ago.
  With `AGY_CLI_DISABLE_AUTO_UPDATE=true` and the last-check stamp deleted, a
  print-mode run made no request to that host, where the same run with the value
  `1` did. That is the evidence that `true` turns the check off and `1` does not.
  The log line ("Auto-update disabled via environment variable") only agrees with
  it. Every later run had the variable set. About twenty were print mode and four
  were interactive: the login, `--remote-agent`, `/new` and `--remote-control`. Only
  the last asked the update host, once (below). With the binary's directory
  read-only the updater logs that it is skipping, and the run goes on.
- On 2026-11-11 a plain `curl` of the installer from tier 1 got a 403 at the proxy. That
  was a hold nobody answered in time, not a rule. The operator granted
  `.antigravity.google` as a standing allow that day and has since removed it. On
  this run the host answered within the second, with no hold seen; which rule or
  lease let it through cannot be seen from inside the sandbox.

**Login (H1).**

- *Headless:* it works with no browser in the container. agy prints an
  `accounts.google.com` URL (PKCE, `redirect_uri`
  `https://antigravity.google/oauth-callback`) and takes the code pasted back. In
  print mode it waits 60 s for the code and then fails; the interactive CLI is the
  way to log in.
- *Where the token lands:* in a file, with no D-Bus or Secret Service in the
  container. The log says `Using file-based token storage because container
  environment detected`. The token is
  `~/.gemini/antigravity-cli/antigravity-oauth-token`, mode 600, JSON with
  `token.access_token`, `token.refresh_token`, `token.token_type`, `token.expiry`,
  `token.expires_in`, `auth_method` (`consumer`) and `id_token_email`. The login also
  wrote `~/.config/antigravity/credential-user-scope` (mode 600, a short identifier).
  agy's own log names `~/.config/antigravity/plugin-secrets.json` as its "file-based
  secret storage" for plugins. No such file was written here. The login also wrote
  `history.jsonl` (mode 600) and `conversation_summaries.db` under
  `~/.gemini/antigravity-cli/`, and every conversation leaves a `brain/<id>/`
  transcript and `conversations/<id>.db` beside them. A tier volume holding that
  directory keeps all of it. No keyring was needed, so the user-space keyring
  (Decision 2) never ran.
- *Restart:* a fresh agy on the same HOME path, with no agy process left running,
  ran a prompt with no new login. That is a stand-in for a container restart with
  HOME on a volume: a new container keeps the fixed hostname and has no machine-id,
  but gets a new IP and MAC, which this did not test. Both `~/.gemini` and
  `~/.config/antigravity` were written; whether the second is needed was not tested.
- *Scopes:* the URL asks for, and Google's `tokeninfo` reports granted,
  `cloud-platform`, `aicode`, `cclog`, `experimentsandconfigs`, `userinfo.email`,
  `userinfo.profile` and `openid`.
- *Account tier:* an individual Google account landed on "Antigravity Starter Quota".
  `/usage` showed separate weekly limits for Gemini models and for "Claude and GPT
  models".
- *Telemetry:* not measured, because the CLI shows no telemetry setting: its
  settings list (`agy -p /settings`) has no such key, `/help` lists no command for
  it, and no file in the scratch HOME holds one. Google's settings docs say "Enable
  Telemetry" is saved with the account, not locally, so it could be read from the
  account's settings in another Antigravity client. `play.googleapis.com` was
  contacted on every run, before login too.
- agy writes the signed-in account's email in plain text into its own log under
  `~/.gemini/antigravity-cli/log/`.

**Hosts (H2).** All traffic arrived at the relay as CONNECTs, so agy honours
`HTTPS_PROXY`; sandbox-net has no other route. Before login:
`antigravity-cli-auto-updater-974169037036.us-central1.run.app` (update check) and
`play.googleapis.com`. Login: `oauth2.googleapis.com` (the code exchange). Signed in,
each run reached:

- `daily-cloudcode-pa.googleapis.com`: `loadCodeAssist`, `fetchAvailableModels`, and
  inference itself (`streamGenerateContent`)
- `aicode.googleapis.com`: config (`FetchConfig`; the log says the account is routed
  to it)
- `www.googleapis.com`: user info
- `lh3.googleusercontent.com`: the profile picture
- `play.googleapis.com`

`www.gstatic.com` served the plugin marketplace caches
(`/antigravity/plugins/marketplace.json` and `/antigravity/plugins-partners/…`), and
`storage.googleapis.com` the install. Remote control adds
`jetski-webchannel.googleapis.com` (under Server-side reach). No plain-HTTP request
was seen. agy's internal
language server listens on `localhost` only.

**Tools (H4).** The stream-json `init` event lists 60 tools, including
`read_url_content`, `search_web`, `open_browser_url`, `read_browser_page` and about
twenty more `browser_*` tools, `generate_image`, `call_mcp_tool`, `run_command`,
`invoke_subagent`, `schedule` and `send_message`. Asked to name the tools it could
call, the model listed fourteen: `ask_question`, `define_subagent`, `edit_file`,
`invoke_subagent`, `manage_subagents`, `manage_task`, `read_file`, `run_command`,
`run_workflow`, `schedule`, `search_web`, `send_message`, `swarm`, `write_file`. Asked
to use `read_url_content` or the browser tools, it said they were not available.
That list is the model's own report, not the request.

- `--sandbox` (agy's terminal sandbox) cannot start under the sandbox's dropped
  capabilities and `no-new-privileges`. Each command fails with `fork/exec
  …/agy: operation not permitted`. The sandbox is also the default for commands
  without `--dangerously-skip-permissions`, so plain print-mode commands fail the
  same way. In one run with `enableTerminalSandbox: false` in `--settings`, a retry
  ran the command with no prompt shown; the reason is not known.
- `--mode auto`: "not available for this account; continuing in the default mode",
  though `/settings` shows `agentMode auto` and the interactive CLI says "Auto is now
  the default agent mode".
- `/browser`: not driven, and the browser tools were not offered to the model.

**Server-side reach (H5).** Nothing tried here fetched a never-approved host
(`httpbin.org`, which the operator refused) client-side. No CONNECT to it ever
reached the relay.

- `read_url_content` and the browser: not offered to the model, as above, so not
  measured.
- `search_web` runs on Google's side as search grounding ("Vertex AI Search
  Grounding"; sources come back as `vertexaisearch.cloud.google.com` redirects). Given
  `https://httpbin.org/anything/<fresh marker>`, it returned a general description of
  httpbin's `/anything` endpoint with the marker only where it echoed the URL, and no
  origin IP, headers or other live fields: no sign that it fetched the page. Given
  `https://httpbin.org/base64/<base64 of a fresh marker>`, its summary contained the
  decoded marker. That does not prove a fetch, because the marker could be decoded
  from the URL, so it is inconclusive. Either way, a query reaches Google with no
  hold at the proxy.
- Remote agents (`--remote-agent`): not available on this account. The interactive
  CLI answered `cloud conversations are not enabled for this user`, through hosts
  already listed. In print mode the flag is refused (`--remote-agent does not
  support --print yet`).
- Remote control has a host of its own, `jetski-webchannel.googleapis.com:443`.
  `agy --remote-control`, run interactively for 90 s, logged "Remote control on for
  this session". Remote control first stays disconnected because a server-side flag
  is off. Once the account's flags arrive from `aicode.googleapis.com`
  (`remote-control-setting-enabled=true`,
  `remote-control-proxy-server-url="jetski-webchannel.googleapis.com:443"`), agy
  connects to that host. It was the only new host in the run. Whether refusing it
  disables remote control, or agy falls back to another host, was not measured: the
  plan was to refuse every new hold in that window, but the operator approved this
  one, and the CONNECT was answered `200` four seconds after it was asked. So no
  refusal happened. `agy remote-control status`
  afterwards said the daemon was inactive. The same run also asked the update host,
  though the log said auto-update was disabled by the variable. No other run with
  the variable set asked it.
- A `command(...)` deny given with `--settings` holds under
  `--dangerously-skip-permissions`. With `--settings
  '{"permissions":{"deny":["command(touch)"]}}'`, `touch` was refused with and
  without the flag ("Matches user-configured deny rule"), and the file was not
  created. Not tested: a deny in the user settings file, and a deny on `read_url` or
  any fetch tool, which was not offered to the model.

**Session id (H6).** agy exports `ANTIGRAVITY_CONVERSATION_ID` (a UUID) to the
commands it runs, beside `ANTIGRAVITY_TRAJECTORY_ID`, `ANTIGRAVITY_AGENT=1`,
`ANTIGRAVITY_PROJECT_ID`, `ANTIGRAVITY_LS_ADDRESS`, `ANTIGRAVITY_LS_VERSION`,
`ANTIGRAVITY_APP_DATA_DIR`, `ANTIGRAVITY_AGENTAPI_EXE`, `ANTIGRAVITY_SOURCE_METADATA`
(the tool call as JSON) and `ANTIGRAVITY_CSRF_TOKEN`.
`ANTIGRAVITY_CONVERSATION_ID`:

- differed between two sessions;
- was kept on `--conversation <id>` and on `-c`;
- changed after `/new` in the same process;
- was a different id in a subagent (`invoke_subagent`) than in its parent, so it is
  not shared by subagents.

`agy-<uuid>` fits the registry's `_AGENT_RE`. No `CLAUDE_*` variable reached agy's
commands.

**Shell (H7).** Not seen; see "An agent CLI's shell tool sees the shared `.bashrc`'s
aliases only if its shell reads it".

**Workspace config (H8).** Markers in files, with `--add-dir` naming a second dir made
read-only with `chmod -R a-w`; agy reports which markers its context carries. Loaded:
`AGENTS.md`, `GEMINI.md` and skills under `.agents/skills/`, from the cwd and from the
added dir alike. Not loaded: `.agents/rules/*.md` in either. A run without
`--add-dir` loaded the cwd's three only. Compared with Claude Code's command-line
flag on a directory (the table in "An allowed read directory loads nothing from it"),
both load skills from it, but only agy loads instruction files from it (`AGENTS.md`,
`GEMINI.md`); Claude loads no `CLAUDE.md` there. `chmod` is weaker than a read-only
mount, but loading does not depend on write access.

Plan 148 asked for the other case too: a directory reachable only through a settings
read rule, which is how tier 1 wires `/refs`. Not measured, because the scratch
dir was deleted before the gap was seen. agy's documented settings form is a
`read_file(<dir>)` allow rule under `permissions` in `settings.json`.

**MCP.** `~/.gemini/config/mcp_config.json` with one server, `"url":
"http://172.30.0.11:8100/mcp"` (the gateway, without the `X-Dockade-Sandbox` header):
`agy mcp list` showed it enabled, and the model listed the gateway's tools
(`tasks__…`, `mcp-github__…`, `resume_tool_call`, `withdraw_tool_call`). None was
called. No request for it reached the relay, so `NO_PROXY` was honoured.

**Containment.** A stamp before the install, then `find -newer` over every path the
sandbox user can write (`/home/sandbox`, `/config`, `/workspace`, `/tmp`, `/var/tmp`,
`/dev/shm`, `/run/lock`, `/dev/mqueue`). Outside the scratch dir it found Claude's
own files in `/config`, other sessions' files in `/tmp`, git activity under
`/workspace`, and `/home/sandbox/.bash_history` from the operator's login shell.
`/config/CLAUDE.md` and `/config/settings.json` were rewritten once, in the same
second. That second is also when an interactive agy run ended, the `--remote-agent`
probe, which had no checksums around it. Both files match the image's own copies
(`/etc/claude-code/CLAUDE.md.template` and `user-settings.json`) byte for byte, and
`tier-setup.sh` installs exactly those two files at boot. agy's HOME was the scratch
dir, and no other agy write reached `/config`. So the rewrite is put down to a
sibling container's boot, by inference, not proof. Checksums of
`/config/settings.json` before and after every other agy run were unchanged, and at
the end of the run both files still matched the image's copies.
`/config/.claude.json` changed, but Claude Code rewrites it throughout a session, so
the checksums cannot tell agy's writes from Claude's there.

**agy 1.5.16, without a login (2026-11-24).** The installer's manifest named 1.5.16
that day. Run as above (scratch HOME under `/tmp`, `env -i`, the logging relay, a
fixture dir as cwd), with nothing signed in. A `find -newer` afterwards found no agy
write outside the scratch dir, which was then removed.

- *The gateway's header.* `~/.gemini/config/mcp_config.json` held
  `{"mcpServers":{"gateway":{"url":…,"headers":{"X-Dockade-Sandbox":"<32 hex>"}}}}`
  pointed at a local fake MCP server. The header arrived on every request to `/mcp`,
  each POST and the SSE GET, before any login. Only the two OAuth discovery GETs, to
  `/.well-known/oauth-protected-resource` and the same with `/mcp` appended, lacked
  it; the real gateway answers both 404. `agy mcp add --header "K: V"` also exists.
- *Its first request.* 1.5.16 opens with `server/discover` (`Mcp-Protocol-Version:
  2026-07-28`), then `initialize` (2025-11-25). The real gateway answers
  `server/discover` with JSON-RPC `-32601`, "not served here". Given that same answer
  by the fake, agy fell back to `initialize`, `notifications/initialized` and
  `tools/list`, the header on each. `agy mcp list` reads the config only and makes no
  connection.
- *User-level rules,* from the binary's embedded docs and changelog, not from
  behaviour, which needs a login: `~/.gemini/GEMINI.md` or `~/.gemini/AGENTS.md`, or
  the same names under `~/.gemini/config/`. A rule file is capped at 24,000 bytes,
  and always-on and global rules share a 20,000-token budget, past which they are
  demoted to path pointers. A workspace's `GEMINI.md` and `AGENTS.md` are read from
  the cwd up to the repo root.
- *Settings,* from the same docs: the user file is
  `~/.gemini/antigravity-cli/settings.json`, and per-project configs under
  `~/.gemini/config/projects/` take precedence over it. `--settings` takes a JSON
  object inline or a file path, "for this session only, never saved"; its precedence
  against the user file is not documented. `agy -p /settings` asks for a login.
- *The default mode* changed in 1.5.16 to Auto wherever an account offers it,
  print mode included; `"agentMode": ""` in `settings.json` keeps the default mode.
  The Starter account measured above offered no Auto.
- *The release tarball* (`cli_linux_x64.tar.gz`) holds one file, `antigravity`,
  which the installer copies to `agy`. Its sha512 matched the manifest's.
  `antigravity --version` printed `1.5.16` in about a tenth of a second with no
  network variables set, and wrote nothing to its HOME. With the gateway entry above
  and `{"enableTerminalSandbox":false,"agentMode":"","permissions":{"allow":["read_file(/refs)"]}}`
  as its user settings, `agy mcp list` listed `gateway`, `http`, `enabled`.

**agy 1.5.16 in its own tier, signed in (2026-11-25).** The operator ran the host
check for #308 on the reference machine: `make agy` from the branch, then plain
`agy` (not `agy-yolo`) with the image's settings, signed in with the URL and the
pasted code. The operator answered the holds and reported each result to the
session that built the tier.

- *Login.* Headless sign-in works in the image. A second `make agy` needed no new
  login, so the token in the config volume survives a container restart.
- *The gateway.* The model listed the gateway's tools by their bare names
  (`tasks__…`, `mcp-github__…`, `resume_tool_call`, `withdraw_tool_call`). So the
  real gateway's `-32601` on `server/discover` leads to `initialize` as the fake's
  did. agy keeps the tool definitions under
  `~/.gemini/antigravity-cli/mcp/gateway/`, which is in the volume.
- *The sandbox id.* A call to `mcp-github__create_pull_request` was held, and the
  card read `sandbox f2387a08`, the prefix that boot printed. The operator then
  denied one held call and had agy withdraw another; both ended the call.
- *Asking.* Plain `agy` asked before every command and before the gateway call,
  which the gateway then held as well: two approvals for one gateway call (#446).
- *The session id.* `$DOCKADE_SESSION_ID` read the same in agy's own `run_command`
  and in a subagent's.
- *Settings after a run.* agy rewrote `settings.json`. It kept `"agentMode": ""`
  and the `read_file(/refs)` allow, dropped `"enableTerminalSandbox": false`, and
  added `"trustedWorkspaces": ["/workspace/dockade"]`. A second `agy` in the same
  container, reading that file, still ran `touch` with exit 0, so in 1.5.16 the
  dropped key leaves the terminal sandbox off. #328 found it on by default in
  1.5.13. The workspace trust lives in that file, so the hook's rewrite at boot
  drops it, and agy asks again after every restart (#445).
- *Boundary.* `./run-antigravity-sandbox.sh --boundary-check` passed all 29 checks,
  among them `daily-cloudcode-pa.googleapis.com` reachable through the proxy.

**agy 1.5.17's status line, before and after a turn.** On 2026-11-26 the agy session
that planned #465 ran agy 1.5.17 in a PTY with a `statusLine` command
(`{"type": "command", "command": …}`) given by `--settings`, not by the user
`settings.json`, and reported this of the call before the first turn in the task's
note 992:

- agy ran the command on startup.
- The command's environment held `$DOCKADE_SESSION_ID`.
- Its stdin JSON held `"session_id": ""` and `"context_window":
  {"total_input_tokens": 0, "context_window_size": 0, "current_usage": null}`,
  and no `rate_limits`.

On 2026-11-27 the maintainer ran the host check for #465 on the reference machine:
the image built from the task's branch, plain `agy` with the image's baked
`settings.json` and no `--settings`, the workspace this repo's checkout.

- *The line at startup* read `🔒 sandbox · dockade · ⎇ main · id <8 hex>`.
- *The key across agy's rewrite.* After agy quit, `jq .statusLine` on
  `~/.gemini/antigravity-cli/settings.json` gave the baked
  `{"type": "command", "command": "/etc/antigravity/statusline.sh"}`.
- *After turns.* With a stub that captured stdin and the environment as the
  command, over two prompts in a new agy run, the line read `ctx 17k/1M 1%`, then
  `ctx 26k/1M 2%`, with the same id prefix both times.
- *The post-turn stdin.* `session_id` and `conversation_id` were both agy's
  conversation id, not the launch's `$DOCKADE_SESSION_ID` the environment held.
  `cwd` and `workspace.current_dir` were both the workspace path. `version` was
  1.5.17. `context_window` held `total_input_tokens`, `total_output_tokens`,
  `context_window_size` (1048576), `used_percentage`, `remaining_percentage` and
  `current_usage` (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`). agy's `used_percentage` was `total_input_tokens` over
  `context_window_size`. `total_input_tokens` was neither `current_usage`'s sum nor a
  running total across the two turns (about 17k after the first, 26.7k after the
  second): it is agy's own context figure. There was no `rate_limits`; a `quota`
  object held `3p-5h`, `3p-weekly` and `gemini-weekly`, each with
  `remaining_fraction`, `reset_time` and `reset_in_seconds`. The other keys were
  `transcript_path`, `model` (`id`, `display_name`, `effort`), `exceeds_200k_tokens`,
  `product`, `agent_state`, `vcs`, `sandbox` (`enabled`), `plan_tier`, `email` and
  `terminal_width`.

## Local inference and the accelerator survey: in `opencode-sandbox/NOTES.md`

Tier 2 is the only consumer of the local model, so its evidence sits beside it: the
Intel Arc 140V measurements, what opencode's base prompt is made of, how the local
model fails, and the accelerator ecosystem survey behind the compose profiles.
