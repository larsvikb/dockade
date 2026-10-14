# Tasks UI — the operator's page onto the task registry

The page the operator reads the work list on. Every block here is about code in this
directory and its tests. Why it is a container of its own, on another origin and with no
leg on `control-net`, is the root `DESIGN.md`'s "Task registry"; who writes what in the
registry, and its states and notes, is `task-registry/DESIGN.md`. The browser guards in
front of it are the approval UI's, from `ui-common/guards.py`, and what each closes is
`control-plane-ui/DESIGN.md`, "The browser boundary".

## The boundary

The relay's table is the operator's whole say in the registry, since its operator
listener takes no token; `_RELAY_ROUTES` in `app.py` says what may be on it, and a test
holds it and the page's fetches equal in both directions. The bind and the peer check
are the approval UI's (`app.py`'s docstring); why each matters beside the registry is
the root `DESIGN.md`'s "Task registry".

## The page

**Not running, a third state.** The registry can stop on its own (`make tasks-up` starts
it with this page), so its absence is a state the page shows, not a failure. The page
asks `/running`, which this container answers itself, before it calls the registry at
all: a relayed call to a stopped registry would be a failed request in the console on
every load. Not running is one line on the page, and the poll asks `/running` instead of
the registry, quietly, so a page left open picks it up again when it answers. A registry
that stops mid-session is found by the next poll, whose 502 (`_registry_unreachable`, a
status the registry never sends) turns the polling the same way.

**The inbox: whose turn it is, worked out.** The page opens on what is the operator's
turn rather than on the backlog, as Gerrit's attention set does. `operator_moves.inbox`
works it out from the store on every read, so there is no read or unread state to keep
or to drift from the tasks, and a task leaves the inbox when what put it there changes.
One row per task, in the highest section any of its reasons puts it, with every reason
and since when (`operator_moves.SECTIONS`). Only what the operator alone can act on is
in it, a flagged note, work to land, a plan to triage, so a fresh claim or an open task
is not; what goes wrong slowly, a claim that ran out or a task gone quiet, sits lower and
a few at a time, so it shows without burying the rest. What waits and what is to triage
come whole, never a page of them. A deferred task is out of it: deferring is the
operator's own decision to take it up later, so it is nobody's turn until then. Every
task is a view away, under All tasks.

**The task is read beside the list, and the hash names it.** Opening a task, closing it
or changing view sets the URL's hash (`parseHash`, `hashFor`), and the page follows the
hash, so a reload, a link and the browser's back agree with what was clicked. The task
it names is read once the registry answers, never before.

**The count is in the title, and quiet.** The page's title carries the number of tasks
waiting on the operator, the inbox's first section, ahead of its name, as the approval
page's carries its pending count, so a background tab shows it in the tab strip. Never
`hot`, and nothing else alerts: no agent is held on the operator
(`task-registry/DESIGN.md`, "Notes").

**Agent prose is spelled where it hides, and only there.** A task's title, body,
notes and claim are prose, so they keep their line breaks and tabs and show visible
non-ASCII as written; a dash or an accent is ordinary there, and spelling it would
bury the one that matters. A character that renders as nothing or reorders the text
around it (`Cf`, `Cc` but the line break and tab, `Zl`, `Zp`, and Unicode's
default-ignorable code points, variation selectors among them) is spelled `\uXXXX`
where it sits, with a note counting them (`spellHidden` in `tasks-ui/tasks.js`),
because an approved body is what every later session reads, and the operator must see
what is in it rather than what the browser draws. Between the approval page's two rules,
the payload's, which notes non-ASCII, and the pin's, which spells it all
(`control-plane-ui/DESIGN.md`), for that reason.

**The plan sits under the body, part by part.** Approving a refined task approves its
body and plan together (`task-registry/DESIGN.md`, "Plans"), so the detail shows both,
the plan in the order `submit_plan` takes its parts, each as prose spelled like the
body. A missing or superseded plan is read against the task's state (`planNotice`):
only a proposed task is refined by an agent, so only there does the page say a plan is
coming; past it, the operator moved the task on without one. A superseded plan is never
something to approve, so it carries a warning. Earlier plans load on request, newest
first, so what a plan replaced can be read beside it.

**Its tests.** `tests/test_tasks_ui.py` holds the server: the relay's table, `/running`,
the files it serves, and that it starts behind the shared guards under its own name,
allowlist, address and network. `tests/test_tasks_ui_js.py` holds the page: every
structural check the approval page has (`PageChecks`: markup and script, the module
list against the Dockerfile and the imports, fetches against the relay, free names),
`tasks.js`'s pure half under node, and, from source, how the page gates the registry and
draws agent text; `tests/test_task_registry_inbox.py` holds what the inbox counts.
`make ui-diff UI_DIFF_PAGE=tasks` runs `tests/ui-harness/tasks-scenario.js` on the base
and on the branch.
