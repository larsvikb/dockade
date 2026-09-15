# You are in a sandbox

This is dockade's tier-1 container: a capability-limited environment with one
governed route out. Everything below is enforced by the container and the
firewall rather than by settings, so none of it can be worked around from in
here. It is written down so you spend no turns discovering it.

- **Egress goes through the governed proxy or not at all.** Allowlisted hosts
  work normally. An unknown host is not refused outright — it is **held for a
  human decision**, so a request can hang until someone approves it or a timeout
  fires. Prefer `WebFetch` over raw sockets, and expect a first request to a new
  host to be slow rather than to fail.
- **MCP tools are governed per tool, and one may be held for a human.** The tools
  under `mcp__dockade__` are brokered by a gateway that asks policy before every
  call. Three answers: it runs, it is refused, or it is **held for approval** — and
  a held call comes back immediately as a *result* saying so, carrying an id. That
  is not a failure and not a hang: do other work, then finish it with
  `resume_tool_call` and the same id. Calling the tool again instead raises a
  **second** question for the same person. A tool whose description begins
  "Approval required" is one of these, so you can plan around it rather than
  discover it. A refusal says retrying will not help, and means it.
- **No `git push`, no SSH.** External names do not resolve outside the proxy, so
  `git push`, `ssh` and `scp` fail at DNS resolution. Branch and commit here;
  pushing is the human's step. There is no governed git path yet.
- **`gh` is not installed**, deliberately — it will not run without a token, and
  no write-capable credential lives in this container. Use `WebFetch` for public
  GitHub reads.
- **No Docker.** `make lint`, `make consistency` and `make test` all work here.
  `make verify-build`, `make check-boundary` and anything driving `docker
  compose` do not — they need the host, so ask rather than working around them.
- **`/workspace` is the human's checkout, live.** Commits, branch switches and
  edits appear outside the container immediately; switching branches moves their
  working tree too. Say so when you do it.
- **The control plane is unreachable by design.** It is on a network this
  container has no route to. Read its source to reason about it; do not try to
  query it.
- **You are not root and cannot become root.** No sudo, capabilities dropped.
  A permission error here is the design working, not a problem to route around.
- **`rm` is aliased to `-I`.** One prompt for a recursive delete or three or more
  files, none for a single file — so ordinary removes work, and a bulk one from a
  non-interactive caller does nothing while **still exiting 0**. Use `rm -f` or
  `command rm` when deleting many at once, and confirm a destructive step by its
  result rather than by its exit status.
