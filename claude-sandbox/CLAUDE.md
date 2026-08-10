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
- **`rm`, `mv` and `cp` are aliased to `-i`.** A non-interactive caller cannot
  answer the prompt, so the command does nothing and **still exits 0** —
  `rm f && echo done` prints `done` with `f` untouched. Use `rm -f` or
  `command rm`, and confirm a destructive step by its result rather than by its
  exit status.
