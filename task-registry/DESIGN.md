# Task registry — who writes what

A work list that agent sessions and the operator share: an agent proposes and shapes
tasks, the operator approves them, and each write is recorded with its author. The
store is `taskdb.py`, `tasks.py`, `agent_moves.py` and `operator_moves.py`, tested by
`tests/test_task_registry_tasks.py`.

## Two writers, two modules

**The registry carries one agent's text to the next.** A task's body is written by one
session and acted on by another, so a session that read something hostile could steer
every later one through it. The operator stands on that path: an agent proposes, and a
task reaches the agents who implement it only once the operator has approved it.

**The split is in what a surface calls, not in a check.** `agent_moves.py` holds what
an agent may do; `operator_moves.py` holds what only the operator may do, and every
write there is authored by `taskdb.OPERATOR`. A surface for agents must call only
`agent_moves` and the reads in `tasks`. Not importing `operator_moves` is not enough on
its own: the plain writes in `tasks` check nothing, so a surface that called them
directly could make any move, however its arguments are shaped.

## Who an agent is

**An agent names itself: its kind, a hyphen and its own session id**
(`claude-<uuid>`). The kind keeps the registry agent-agnostic; each agent's
instructions are where it learns its id. For Claude Code it is the session id, which
also names the session's transcript, so a write leads back to what the session said
(`NOTES.md` → "Claude Code puts its session id in every Bash call's environment").

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
| `proposed` | Being specified: the operator and an agent shape the body until it is as clear as the implementation needs |
| `open` | Approved by the operator; the spec is frozen to agents |
| `landing` | The work is done and waits for the operator to land it; who pushes may change, the merge is the operator's |
| `deferred` | Parked: needs a decision, an environment, or the operator |
| `closed` | Landed or dropped |

**The spec is settled in `proposed` and frozen at `open`.** Any agent may edit a
proposed task's title, body, severity, kind and blockers. From `open` on, only the
operator may, so what an implementer reads is what the operator approved. The operator
moves any task to any state.

**One revision covers content and state.** Every change bumps it, and every write to an
existing task names the revision it read; a stale one is refused with the task as it
is now. For the operator this is what makes triage mean something: moving a task to
`open` approves the spec as the operator read it, not as an agent edited it a moment
later. Same idea as the control plane's rule writes, which name the rule as the page
showed it.

## Parent and blockers

The work arrives as an objective, a design, a breakdown and a spec per task, and two
optional fields carry that shape. **`parent`**: an objective is a task whose body is the
design while it is proposed; the breakdown is its children. **`blocked_by`**: ordering
between tasks; a task is blocked until every blocker is `closed`, since `landing` is
not landed.

Both stay inside one repo, and neither may loop: a re-parent that would make a task its
own ancestor is refused, and so is a blocker that already waits on the task. Blockers
freeze for agents at `open` with the rest of the spec, since the order is part of the
plan the operator approves. Strict for now; loosening a rule later is cheaper than
tightening one under data that broke it.

## Every write leaves a history row

`taskdb.record` writes the author, the action and the values written, in the write's
own transaction, so a write and its record land together or not at all. For a spec it
is also the revision log: every body an agent proposed, under its author.
