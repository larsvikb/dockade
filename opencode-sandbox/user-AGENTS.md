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
- **`/workspace/<folder-name>` is the human's checkout, live.** It is mounted under
  its host folder's name, and the session starts in it. Commits, branch switches and
  edits appear outside the container immediately; switching branches moves their
  working tree too. Say so when you do it. Anything written beside it, in
  `/workspace` itself, is lost when the container exits.
- **`/refs/<name>`, when present, is reference material.** Host directories the
  human mounted read-only for you to read. Changes belong in the checkout.
- **You are not root and cannot become root.** No sudo, capabilities dropped.
  A permission error here is the design working, not a problem to route around.
