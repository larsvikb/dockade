- **MCP tools are governed per tool, and one may be held for a human.** The tools
  under `{{gateway_prefix}}` are brokered by a gateway that asks policy before every
  call. Three answers: it runs, it is refused, or it is **held for approval** — and
  a held call comes back immediately as a *result* saying so, carrying an id. That
  is not a failure and not a hang: finish it with `resume_tool_call` and the same id.
  **Re-calling it while the answer is still pending is free** — nothing runs, the id
  stays good. Nothing can push the answer to you, so you choose how to collect it:
  pass `wait_seconds` and the call blocks until the human answers (capped, and well
  inside any timeout), or do other work and come back with a zero wait. Prefer the
  wait when you have nothing else to do and the zero wait when you have. Either way
  give up after a few tries: the held result says when its question expires, and a
  human who has stepped away is not helped by spinning. Calling the TOOL again is
  the different thing: unless its arguments are identical and the first is still
  pending, it raises a **second** question for the same person. If you no longer
  want a held call, `withdraw_tool_call` with the same id takes the question back —
  use it the moment your plan changes. A tool whose description begins
  "Approval required" is one of these, so you can plan around it rather than
  discover it. A refusal says retrying will not help, and means it.
