---
output: claude-sandbox/user-CLAUDE.md
gateway_prefix: mcp__gateway__
web_fetch_tool: WebFetch
shell_tool: Bash
agent_kind: claude
session_id_var: $CLAUDE_CODE_SESSION_ID
repo_instructions: CLAUDE.md
id_shared_clause: , and your subagents share it
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
{{include cloud-tier/control-plane}}
{{include every-tier/not-root}}
{{include every-tier/rm}}
