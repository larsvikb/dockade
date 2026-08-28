# You are in a no-egress sandbox

This is dockade's tier-2 container. The limits below are enforced by the container
and the firewall rather than by configuration, so they cannot be worked around
from in here.

- **There is no network.** The only host you can reach is the model server that is
  answering you. Fetching a URL, `git clone`, `git push`, `pip install`,
  `npm install` and `apt-get` all fail, and they fail *slowly* — after a timeout.
  Do not retry them.
- **Everything you need is already installed**: python3, git, jq, ripgrep, vim.
  Nothing can be added at runtime, so solve the task with what is here.
- **No git remote.** Commit locally; pushing is the human's step.
- **`/workspace` is the human's checkout, live.** Your edits and branch switches
  appear outside this container immediately. Say so when you switch branches.
- **`rm` is aliased to `-I`.** One prompt for a recursive delete or three or more
  files, none for a single file. A non-interactive caller cannot answer that prompt,
  so a bulk remove does nothing and still exits 0. Use `rm -f` when deleting many at
  once, and confirm a destructive step by its result rather than its exit status.
- **You are not root and cannot become root.** No sudo. A permission error here is
  the design working, not a problem to route around.
