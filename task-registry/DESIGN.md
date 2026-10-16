# Task registry — who writes what

A work list that agent sessions and the operator share: an agent proposes a task and
refines it into a plan, the operator approves the two together, and each write is
recorded with its author. The
store is the Python in this directory (`README.md` → Layout says which module holds
what), tested by the `tests/test_task_registry_*.py` files.

## Two writers, two modules

**The registry carries one agent's text to the next.** A task's body is written by one
session and acted on by another, so a session that read something hostile could steer
every later one through it. The operator stands on that path: an agent proposes, and a
task reaches the agents who implement it only once the operator has approved it.

**The split is in what a surface calls, not in a check.** `agent_moves.py` holds what
an agent may do; `operator_moves.py` holds what only the operator may do, and every
write there is authored by `taskdb.OPERATOR`. A surface for agents must call only
`agent_moves` and `reads`. Not importing `operator_moves` is not enough on its own:
the plain writes in `tasks` check nothing, so a surface that called them directly
could make any move, however its arguments are shaped.

## Who an agent is

**An agent names itself: its kind, a hyphen and its own session id**
(`claude-<uuid>`). The kind keeps the registry agent-agnostic; the tool descriptions
and each agent's instructions say where its id comes from (for the sandbox's Claude
Code, `claude-sandbox/user-CLAUDE.md`). For Claude Code it is the session id, which
also names the session's transcript, so a write leads back to what the session said
(`NOTES.md` → "Claude Code puts its session id in every Bash call's environment"), and
the status line shows its first eight characters (`claude-sandbox/statusline.sh`).

The alternatives each fail at uniqueness or at lasting as long as the work: a branch
(one session works several tasks), the container (its name comes back after `--rm`,
and several sessions share one), an id the registry issues (the model would have to
carry it through compaction).

**Self-declared, so it authenticates nothing.** It keeps cooperating sessions apart.
`taskdb.check_agent` refuses anything without the shape, and any id whose kind is
`operator`, so an agent cannot record a write as the operator's by accident.

## Repos are a list the operator keeps

Every task belongs to one repo, named from a list the operator maintains. Not a git
remote: a repo needs none, and inside the sandbox every workspace is `/workspace`, so
the path says nothing either. A repo's agents learn its name from the repo's
instructions file, a committed one because a worktree carries only tracked files. An
unknown name is refused with the known ones, so a typo cannot start a repo. A retired
repo keeps its tasks and takes no new one. A repo may record its remote
(`host/owner/repo`, never an owner alone), so anything later scoped to a repo is scoped
by the operator's data rather than an agent's.

**A filter, not a wall.** Reads cross repos. An agent's write names the repo it works
in and is refused on a task of another, which stops a session in one repo from shaping
another's work. Separation that must hold is a second registry with its own store.

## States

| State | Meaning |
|---|---|
| `proposed` | Being specified: agents shape the body, and one that claims the task refines it into a plan |
| `refined` | A plan, reviewed, waits for the operator; frozen to agents |
| `open` | Approved by the operator; the spec is frozen to agents |
| `landing` | The work is done and waits for the operator to land it; who pushes may change, the merge is the operator's |
| `deferred` | Parked: needs a decision, an environment, or the operator |
| `closed` | Landed or dropped |

**The spec is settled in `proposed` and frozen at `refined`.** The spec is the body and
the plan (see "Plans"). Any agent may edit a proposed task's title, body, severity,
kind and blockers, but only the holder while it is claimed. From `refined` on, only the
operator may, so what an implementer reads is what the operator approved. The operator
moves any task to any state, but to `refined` only with a current plan: a trivial
proposal can go straight to `open`, while a task is refined only by a plan.

**One revision covers content, state, and who holds the task.** Every change to them
bumps it, and every write to a task's spec, state or holder names the revision it read,
with two exceptions: a holder's own claim moves carry the claim's token instead, and
taking a claim names neither, since it is refused while anyone holds the task; a stale
one is refused with the task as it is now. For the operator this is what makes triage
mean something: moving a task to `open` approves the spec as the operator read it, not
as an agent edited it a moment later. Same idea as the control plane's rule writes,
which name the rule as the page showed it. Taking a claim and ending one bump it too, so
a page that showed a task unclaimed cannot defer it once an agent holds it; an expired
claim bumps it when it is swept, not the moment it expires. A holder's update does not:
its checkpoints would otherwise make the operator's page stale all day.

## Plans

**The body says what and why; a plan says how.** A proposal is cheap to file and stays
out of the operator's way; an agent claims it with `claim_plan`, works out the plan, and
hands it in with `submit_plan`, which makes the task `refined`. The operator reads the
two together and approves them by moving the task to `open`.

**A plan's parts are fields, each required and capped** (`plans.TEXTS`): the approach,
a footprint, the tests, what is out of scope, the risks, and the review it had. The caps
are a shape as much as storage: a plan past them is over-detailed, or more than one
task. The footprint is a claim's, but `.` is refused, since saying where is the point of
a plan; when the work is claimed it is the claim's footprint unless the claim names one.
The review fields are the agent's word that a fresh-context reviewer read the plan, not
proof; the operator's approval is the check. Overlaps with other plans are reported, as
a claim's are, and never refused.

**A review round is one reviewer with a fresh context reading the whole plan,** and the
task as written rather than the drafter's account of it; a narrowed re-check between
rounds is not one. So the count on the operator's page is how many times the plan was
read whole. A refiner runs at most `plans.REVIEW_ROUNDS_CAP` rounds and hands the plan
in as the last one read it, passed or not, its open findings in the note. The cap is
the operator's, stated in `claim_plan`'s description and not applied by the store
(`plans.REVIEW_ROUNDS_CAP` says why).

**Every plan is kept; one is current.** `submit_plan` adds a row and nothing changes
one, so the operator can compare a plan with the one before. A task that enters
`proposed`, by whoever's move, has its current plan superseded (`tasks.update`, the one
place a state is written): a claim on a `refined` task, a `send_back`, or the operator
moving it back. So a plan reaches `open` only as handed in, and an agent changes a
refined plan only by claiming the task back and handing in another.

**The revision pins the plan.** `submit_plan` bumps it, so a move to `open` naming the
revision the operator read approves the plan the operator read; there is no plan id to
name besides.

## Parent and blockers

The work arrives as an objective, a design, a breakdown and a spec per task, and two
optional fields carry that shape. **`parent`**: an objective is a task whose body is the
design while it is proposed; the breakdown is its children. **`blocked_by`**: ordering
between tasks; a task is blocked until every blocker is `closed`, since `landing` is
not landed.

Both stay inside one repo, and neither may loop: a re-parent that would make a task its
own ancestor is refused, and so is a blocker that already waits on the task. Blockers
freeze for agents at `refined` with the rest of the spec, since the order is part of the
plan the operator approves. Strict for now; loosening a rule later is cheaper than
tightening one under data that broke it.

## Claims

**A claim is not a state; it sits on a `proposed` task, to refine it, or an `open`
one, to do it.** One agent at a time holds a task, with a footprint and a free-text
progress line: how the work goes changes too fast for states. Each kind has its own
move, as each has its own hand-in: `claim_plan` and `submit_plan`, `claim_work` and
`submit_work`. So each tool's schema states its rule, each description arrives when
its phase starts, and the gateway's policy gates starting work apart from starting a
plan. Each move refuses a task in the other's states, and each hand-in the other kind
of claim, naming the right one. A state change ends a claim, so the task's state says
which kind a claim is, and nothing else records it.

**Only a claim to do the work has a branch:** the one the work will land on. The store
takes any name git would, so telling a placeholder from it is the agent's to know. A
plan lands nothing, so a claim to refine has no branch, stored as none rather than as a
value every reader would have to know is not one, and `update` refuses it one. Every
reader shows none: a read gives `null`, a history row leaves the key out, the page and
the export leave the branch out of the line.

Claiming a task past either state takes it back for rework (`claims.CLAIMED_INTO`):
`refined` to `proposed`, `landing` to `open`; releasing it then leaves it there. A
claim ends when its holder hands in (`submit_plan` to `refined`, `submit_work` to
`landing`), sends an open task back (`proposed`, with a note on what is unclear) or
releases it; when the operator breaks it or moves the task to another state; or when
it expires. No agent can end another's. The history row of every ending keeps a work
claim's branch, so what to land outlives the claim.

**The token makes a claim one deliberate act away from anyone else.** Only the claim
move's reply carries it, and the store keeps its hash. The holder's id is shown wherever
claims are listed, so without a token releasing another's claim would take one
argument, the path a helpful coordinator would take to free a stuck worker. With one,
it means reading another session's transcript, where the reply sits. Not containment:
every session can read those. For the same reason nobody gets a second token for a
claim that is held, not even under the holder's own id. A token lost to compaction
costs the claim: it lapses, or the operator breaks it. A session's subagents share its
id, so a coordinator's subagent workers hold claims as the coordinator, and get the
token from it.

**A claim lives four hours from its last write** (`TASKS_CLAIM_SECONDS`), so a session
that stopped lets its work go by the next morning. Each `update` renews it, and with
nothing else given is only a renewal. An expired claim is not held from the moment it
expires; the next claim or operator move sweeps it, under the registry's own name, and
bumps the revision as any ending does. A write by its old holder is refused, saying
who holds the task now, or that it is free or no longer in the state it was claimed
in, so a returning session learns it lost the task rather than working on.

**Footprints and blockers inform; they never refuse.** A footprint is at least one path
in the repo; `.` is all of it, and the honest answer before the paths are known, since a
claim with none would overlap nothing. Claiming or updating reports every other live
claim in the repo whose footprint overlaps, one path containing the other, and claiming
a blocked task names the blockers still open. Whether two claims really collide, or
whether stacking on an unmerged branch is right, is a judgement for whoever coordinates,
not a rule the store can apply. The ready queue is what needs no judgement: `open`,
unclaimed, and blocked by nothing.

## Notes

**A note is information from a peer, never an instruction.** Notes are the larger part
of the channel between sessions that triage does not cover: one session that read
something hostile reaches every later one through them. So the approved body outranks
every note, and a note saying the spec should change is a reason to send the task back
to `proposed`, not a change. Nothing in the store can make an agent read them that way;
the tool descriptions say it, and the agent's instructions must too. The other part is a
claim's progress line: short, the holder's own, and gone when the claim ends, so the
operator clears one by breaking the claim rather than hiding it. A read returns each
note as data, with its id, author and time, never spliced into the task's own text.

**Visible at once; the operator can hide one.** Holding notes until the operator
releases them would make the operator the courier the registry exists to replace.
Hiding takes a note out of every read an agent can call and keeps it in the record.
Only the operator's reads return a hidden note, `operator_moves.all_notes` and the
history, so they live in `operator_moves` and no agent surface may offer them; a test
calls every function in `reads` and holds the list of them fixed.

**Any agent in the repo writes one; none changes one.** An agent notes only tasks of
the repo it works in, the claim's check, in any state and without a token: a note is a
message, not a move on a claim. No agent move edits or deletes a note, its own
included. The note a claim move carries (a hand-in, send back, release) is posted as a
note too, marked with its move, so the next reader of the task sees it.

**One flag, *for the operator*,** marks a note that needs the operator, and nobody
waits on it. An agent that cannot go on without an answer sends the task back to
`proposed` or releases it, and takes other work: nothing in the registry holds an agent,
so the operator works through it at their own pace. The store cannot make an agent do
that; the tool descriptions say it. `operator_moves.notes_for_operator` lists the
flagged notes not yet handled, a page at a time with the total. The operator marks one
handled, posting a reply in the same write if there is one, which names the note it
answers; agents see both, so the next session knows the note was seen to and which reply
is its answer, and unlike hiding, marking it takes the note from no one.

A note does not move the task's revision: it is not the spec, and the operator's page
would otherwise go stale with every note. A note's length is capped (`notes.NOTE_MAX`), a
read of a task carries its latest few (`notes.IN_READ`) and the total, and
`reads.task_notes` pages back.

**Search carries a note past its task.** `reads.search_tasks` matches titles, bodies and
visible notes in every state and repo, so an agent finds an existing task before
proposing one, and what earlier sessions said about the same problem. That brings a note
to sessions on unrelated work, which is why a note being information, never an
instruction, matters more here. A hidden note does not even match.

## The operator API

**`operator_api` serves `operator_moves` and `reads` as JSON, and its one caller must be
the tasks UI (`tasks-ui/`),** which renders agent text behind the browser guards both
UIs share; the API serves no HTML, and nothing an agent calls. It takes no token: as
with the approval UI's relay to the control plane, being on its network is the
permission, so `serve.py` binds it on a network of its own, refuses to start anywhere
else, and answers that network's peers only (the root `DESIGN.md`, "Task registry").

**A body is checked for shape before the store's checks run** (`operator_api.Body`), as
the agent listener's calls are: an unknown field is refused, and ids, revisions and
flags are taken strictly, so `true` is no id here either. Every bound stays the store's.

**A refusal says what kind it is** (`taskdb.Missing`, `taskdb.Stale`; the statuses are
in `task-registry/operator_api.py`), so a page tells "changed since you read it" from
any other refusal without parsing a reason, and redraws from the task the answer
carries rather than reading again.

**The operator's reads come a page at a time with the total** (the flagged notes, a
task's notes, its history), and a bulk hide or handle names its notes rather than
matching a filter: a flood is cleared by what a page showed, so a note that arrives
meanwhile is not swept up unseen. All of a bulk lands, or none of it. The inbox
(`operator_moves.inbox`) is the exception: what waits on the operator and what is to
triage come whole, since a task a page left out would be the operator's turn and shown
nowhere, and their size is what the operator has not yet done.

## Every write leaves a history row

`taskdb.record` writes the author, the action and the values written, in the write's
own transaction, so a write and its record land together or not at all. For a spec it
is also the revision log: every body an agent proposed, under its author. That makes the
history the operator's read (`operator_moves.history`): it keeps every text a task ever
had, a hidden note's and a body triage replaced, so an implementer reading it could act
on a version the operator never approved.

## Backups

**`make backup` takes the registry's store beside the control plane's,** under the same
stamp, when there is one: the registry is optional, and a host that never started it is
told so in a line. It goes outside this tree for the reason the `Makefile` gives
`BACKUP_DIR`: a copy in the tree is one the agent edits, and restoring it would put
agent text back into approved bodies.

**Restoring is a target of its own, `make restore-tasks FILE=…`,** since a destructive
step names its store rather than guessing it from the file. It refuses a file the
registry would not open before it replaces anything, and starts the registry again only
if it was running. A backup from an older schema the registry takes forward
(`taskdb.OPENS`) is restored as it is, and migrated at the next start. The step is
one-way: an image from before it refuses the migrated store, so going back takes that
image and a restore of a backup from before the step. Ids carry on from
the store it replaces wherever that one had gone further, so an id a session or the
gateway's audit holds never comes to name another task or plan
(`backup._keep_ids_moving`).

**Every backup writes a markdown export beside the `.db`,** per repo and state: each
task's fields, body, claim, plans and notes. For reading without the server, and
one-way: nothing imports it, so the `.db` stays the only backup format. It carries the
hidden notes, marked, which makes it the operator's read, as the history is. Agent text in it
renders as nothing but text (`task-registry/export.py`). Nothing else reads the store:
the UI serves the operator, the tools serve the agents, and the export is the reading
without the server, so there is no command line.

## The agent listener

**The gateway is its only caller.** Every server on `mcp-net` reaches the listener,
`mcp-github` and its write-capable token among them, and one calling it directly would
write as any agent with no audit row at the gateway. So every request must carry the
secret the gateway holds for server `tasks`, sent the way the gateway sends any server's
(a header the operator registers: `Authorization: Bearer <secret>`), and one without it
is refused before its body is read. The listener does not start without the secret. Its
file is the one the gateway reads, and the registry must be given that file alone, never
the directory beside it, which holds the other servers' tokens.

**The schema checks shape; the bounds stay in the store.** `agent_tools.py` checks
each call against the schema it serves, and leaves every bound to the store's own checks,
which the operator's surface shares, so the two surfaces cannot disagree on what a field
may hold.

**The descriptions carry what the store cannot enforce.** They are all an agent reads of
the registry, so the rules the sections above leave to the agent are in them: where its
id and its repo's name come from, which branch a claim to do the work names, that a
lost token is never looked for in a transcript, what a plan holds and how detailed it
is, what a review round is and that the rounds come before the hand-in, that notes
inform and never instruct, and that nobody waits on a flagged note. What a refiner
needs before drafting is in `claim_plan`'s, read when refining starts, rather than in
`submit_plan`'s, often read once the plan is written; `get_task`'s points to it.
