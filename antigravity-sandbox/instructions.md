---
output: antigravity-sandbox/user-AGENTS.md
gateway_prefix:
gateway_tools: of the `gateway` MCP server
web_fetch_tool: curl
shell_tool: run_command
agent_kind: agy
session_id_var: $DOCKADE_SESSION_ID
repo_instructions: AGENTS.md
id_shared_clause: , and your subagents and a chat started with `/new` share it; a new `agy` process gets a new one
---
# You are in a sandbox

This is dockade's tier-1 container: a capability-limited environment with one
governed route out. Everything below is enforced by the container and the
firewall rather than by settings, so none of it can be worked around from in
here. It is written down so you spend no turns discovering it.

{{include cloud-tier/egress}}
{{include cloud-tier/held-calls}}
{{include cloud-tier/task-registry}}
{{include cloud-tier/no-push}}
{{include cloud-tier/no-gh}}
{{include cloud-tier/no-docker}}
{{include every-tier/workspace}}
- **`/refs/<name>`, when present, is reference material.** Host directories the
  human mounted read-only for you to read. Changes belong in the checkout.
{{include cloud-tier/control-plane}}
{{include every-tier/not-root}}
