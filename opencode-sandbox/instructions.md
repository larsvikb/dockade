---
output: opencode-sandbox/user-AGENTS.md
---
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
{{include every-tier/workspace}}
- **`/refs/<name>`, when present, is reference material.** Host directories the
  human mounted read-only for you to read. Changes belong in `/workspace`.
{{include every-tier/rm}}
{{include every-tier/not-root}}
