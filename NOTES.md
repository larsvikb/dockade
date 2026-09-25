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

## An MCP tool call has three timers, and the one that bites defaults to ~28 hours

From the Claude Code MCP documentation (`https://code.claude.com/docs/en/mcp`), read
while deciding whether a gateway `ask` may block the agent. **Documented, not
measured** — the defaults below have not been probed in-container, and the 28-hour
figure especially is worth pinning before anything depends on it.

| Timer | Scope | Default |
| --- | --- | --- |
| `MCP_TIMEOUT` | server **startup** | not stated in the docs |
| `MCP_TOOL_TIMEOUT` | per tool call, when no per-server `timeout` is set | ~28 hours |
| per-server `timeout` (ms, in the server's config entry) | per tool call | unset |
| first-response-byte, HTTP/SSE/WS servers only | per request | 60 s, raised to match `timeout` / `MCP_TOOL_TIMEOUT` when either is ≥ 60 s |

Three consequences, in the order they surprised:

- The per-server `timeout` is a **hard wall-clock limit**, and **progress
  notifications do not extend it**. MCP's own keepalive mechanism is therefore
  unavailable as a way to hold a call open past the limit — the option that looks
  obvious from the protocol does not exist in this client.
- The first-byte timer is the one an HTTP server hits first, and it is only 60 s
  *until* a longer `timeout` is configured, at which point it rises to match. So a
  server that intends to answer slowly must set `timeout`; there is nothing to set on
  the first-byte timer directly.
- Unset means ~28 hours, not "a sensible minute or two". Any design that blocks the
  caller and relies on the default to bound it hangs the agent for a day rather than
  failing — the failure mode arrives in someone else's config, not the author's.

**Re-read 2026-09-23, and two of the three conclusions above needed amending.** The
page had gained a fourth timer and a client feature; still documented rather than
measured, and the probe is still worth doing.

| Timer | Scope | Default |
| --- | --- | --- |
| idle — no response *and no progress notification* | per tool call, HTTP/SSE/WS/connector | **5 min** (30 min stdio; IDE and in-process exempt), `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, v2.1.187+ |

- **Progress notifications are useless against the wall clock and REQUIRED against the
  idle timer.** The bullet above is right that they do not extend the per-server
  `timeout`; it now reads as "keepalive is unavailable", and that is wrong. A call
  that intends to sit quietly for more than five minutes needs them.
- **The first-byte comparison excludes the 28-hour default**, which the bullet above
  misses: the timer is the greatest of 60 s, the server's `timeout`, and `MCP_TIMEOUT`,
  "and the 28-hour default of an unset `MCP_TOOL_TIMEOUT` doesn't enter that
  comparison". So an unconfigured HTTP server gets exactly 60 s and a value below 60
  cannot shorten it. **60 s is therefore the floor a portable server has to fit
  inside** — it is the one number no configuration can lower.
- **One field settles all three.** A per-server `timeout` of at least 1000 ms sets the
  wall clock, raises the first-byte timer to match, and (v2.1.203+) acts as a floor on
  the idle timeout.
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
both sandbox tiers ~**1.2 GB**, the services **142–255 MB**. Both tiers are large for
the same reason, and it is the toolchain they share from `sandbox-common` rather than
anything about what either tier is allowed to do.

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

## Local inference and the accelerator survey: in `opencode-sandbox/NOTES.md`

Tier 2 is the only consumer of the local model, so its evidence sits beside it: the
Intel Arc 140V measurements, what opencode's base prompt is made of, how the local
model fails, and the accelerator ecosystem survey behind the compose profiles.
