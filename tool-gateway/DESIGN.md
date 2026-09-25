# MCP gateway — what the agent is shown, and what runs

The decisions the gateway takes for itself: which tools the agent is shown and under
what name, what must be true before a call runs, what a refusal looks like, and the rule
a tool of the gateway's own must meet. All of it is code in this directory —
`surface.py` and `execute.py` above all — and its tests, `tests/test_tool_surface.py`
and `tests/test_tool_execution.py`. What the gateway is to the rest of the system stays
in the root, and this file does not repeat it:

- where it sits — one container per server on `mcp-net`, its three legs, and a bridge of
  its own to the control plane — `DESIGN.md` → "MCP gateway — governed tool capability";
- whose credential it holds, and how a secret reaches it, `DESIGN.md` → "A credential
  should live as far from the agent as its server allows";
- what it asks the control plane, and what it may cache, `DESIGN.md` → "The gateway
  pulls";
- how a held call comes back — the pending result, resumption by id, and executing on
  resumption rather than on approval — `DESIGN.md` → "An `ask` answers immediately";
- how the sandbox is told it exists, `DESIGN.md` → "Telling the sandbox it exists".

## What is shown, and what runs

**Two axes, not one.** A rule here governs two separable things: whether a tool's
schema is **presented**, and whether a call is **executed**. Withholding a schema is
ergonomics — it keeps the agent from planning around a capability it cannot have.
Execution is the boundary, and `deny` must be enforced there *regardless of
presentation*, because a tool name can arrive from anywhere: a transcript, a
`CLAUDE.md`, text injected into the agent's context by an earlier tool result. Same
shape as settings-versus-capability everywhere else in this design.

**The two axes landed one at a time, presentation first.** The order followed from the
axes themselves: presentation is ergonomics and can be wrong without granting anything,
while execution is the boundary — so the half that cannot grant shipped first and was
probed by hand (`make gateway-tools`, which dials the listener from a throwaway
container on `sandbox-net`, the agent's own position). `tool-gateway/surface.py` decides
what is shown, `tool-gateway/execute.py` decides what runs, and
`tool-gateway/protocol.py` holds the wire with no I/O at all. The sandbox was pointed at
it last, deliberately: a tool surface offered before it works misleads the agent about
its own capability, which is the same objection that rules out a runtime probe in the
launcher.

**Nothing runs before the control plane has answered, and that is structural rather
than asserted.** There is exactly one function in the gateway that dials a server, it
takes no policy argument, and it is reached from exactly two places — an `allow` from
`/tool/authorize`, and a successful claim on an ask a human approved. A function that
both decided and ran would have two reasons to be called and one of them would
eventually be wrong. The failure direction is the one that matters: an unreachable
control plane refuses the call rather than running it, so an outage cannot become a
default-allow wearing a disguise.

**Every refusal reaches the agent as a RESULT, not a protocol error**, generalising what
`DESIGN.md` already required of the pending answer. An agent can act on a result — read
the reason, do something else, come back — where an error is something it records as a
failed call, carrying nothing it can use. The corollary is that the text has to carry
the distinction the agent needs: a `deny` says retrying will not help, a pending ask
says come back with the same id, and a `denied`/`expired`/`spent` approval says the
matter is final. An agent that cannot tell a refusal from a delay retries one forever,
and that is a property of the wording rather than of the protocol.

## What the tool list carries

**Flattening several servers into one namespace, reversibly.** The agent talks to one
MCP server, so tools arrive from several backing servers into a single list and a name
has to carry the server: `tool-gateway/surface.py` exposes `<server>__<tool>`. What
makes that safe is a property spanning three files rather than a convention — a server
name is a DNS label (`discovery.check_name`), so it cannot contain `_`, which makes the
*first* `__` in an exposed name always the join no matter what the tool name contains.
The decode is therefore exact, and that exactness is the whole of it: a call arriving
under a flattened name must resolve to precisely the `(server, tool)` policy is keyed
on, or the rule that was read and the call that runs are for different tools.

**A server's own annotations stop at the gateway.** `readOnlyHint` reaches the
operator's picker, where a human reads it as a claim, and it is not forwarded to the
agent. This is `DESIGN.md`'s "server-supplied and therefore untrusted" rule, applied to
the one place it would otherwise leak: a client that treats the hint as grounds to skip
its own prompt would be taking a third party's word for how dangerous a call is. Nothing
is lost, because the hint decides nothing on either side of the boundary.

**Gateway-native tools are a category, and need their own rule.** Resume is proxied
from no MCP server; it is the gateway's own, permanently `allow`, and therefore outside
the per-tool policy governing everything else on the surface. The rule that keeps the
category honest: **a native tool must not cause an ungoverned side effect.** Resume
sits precisely on that line, because it does cause one — admissible only because the
effect is bound to an id a human explicitly approved, with arguments they read. The
next native tool will not inherit that property, which is why the criterion is written
here rather than left to be inferred from this one being safe.
