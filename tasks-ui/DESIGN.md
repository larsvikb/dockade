# Tasks UI — the operator's page onto the task registry

The page the operator reads the work list on. Every block here is about code in this
directory and its tests. Why it is a container of its own, on another origin and with no
leg on `control-net`, is the root `DESIGN.md`'s "Task registry"; who writes what in the
registry, and its states and notes, is `task-registry/DESIGN.md`. The browser guards in
front of it are the approval UI's, from `ui-common/guards.py`, and what each closes is
`control-plane-ui/DESIGN.md`, "The browser boundary". The evidence behind these
decisions, such as the prior art the inbox follows, is in `tasks-ui/NOTES.md`.

## The boundary

The relay's table is the operator's whole say in the registry, since its operator
listener takes no token; `_RELAY_ROUTES` in `app.py` says what may be on it, and a test
holds it and the page's fetches equal in both directions, the writes' method too. Its
writes are the ones the page makes: on a task, a move, breaking a claim and a note; on
notes, hiding or showing one, handling one with or without a reply, and handling the
flagged notes a page showed. The bind and the peer check are the approval UI's
(`app.py`'s docstring); why each matters beside the registry is the root `DESIGN.md`'s
"Task registry".

**Where an operator write came from: asserted by the relay, recorded by the registry.**
The relay drops a client's `x-dockade-actor` and sends the peer it saw, as the approval
UI's does to the control plane, and `operator_api.actor` records it, with what the
registry saw itself, in every operator write's history row (`task-registry/DESIGN.md`,
"Every write leaves a history row"). The registry's peer is this container, or the host,
which holds an address on the registry's network (`SECURITY.md`), so `via-ui=` is what
tells a write through the page from a POST made straight to the registry, by `docker
exec` say, from anyone who does not forge it; through the loopback publish it is the
publish's gateway, so it says "through the page", not which browser. `origin=`/`ua=` are
what betray a caller of the relay that is not a browser. Detection, not prevention, for
the reason the root `DESIGN.md`'s "Approval provenance" gives; the author stays
`operator`, since there is one operator.

## The page

**Not running, a third state.** The registry can stop on its own (`make tasks-up` starts
it with this page), so its absence is a state the page shows, not a failure. The page
asks `/running`, which this container answers itself, before it calls the registry at
all: a relayed call to a stopped registry would be a failed request in the console on
every load. Not running is one line on the page, and the poll asks `/running` instead of
the registry, quietly, so a page left open picks it up again when it answers. A registry
that stops mid-session is found by the next poll, whose 502 (`_registry_unreachable`, a
status the registry never sends) turns the polling the same way.

**A write whose answer was lost is read back, never sent again on a guess.** Not running
means nothing was sent, which holds for a read but not for a write: a timeout after a
write went out leaves it landed or not, and reported as not running, a retried note is
two notes. So the relay answers 502 only for the errors raised before a request is on
the wire (`_NOT_SENT`, listed so an error it does not name counts as sent), and a 504
for a write sent with no answer; a read keeps 502 for every error, since asking again
is safe. On a 504, a fetch that rejected, or an answer cut off, the page reads the task
again and decides from it (`checkWrite` in `moves.js`): a move or break whose revision
did not move has not landed yet and cannot land twice, since the revision would refuse a
second; a note, which names no revision, has landed only if an operator note with its
text is newer than any the page held, and is otherwise "not found yet", never "safe to
send again", since it may still be committing. A move on a note has landed if its own
history entry, naming the note with the value sent, is newer than any the page held; the
newest page of the timeline carries it wherever the note sits. Absent, a hide, show or
handle is "not seen", harmless to send again since it sets the same flag, and a reply is
"not found yet", as a note is; when even the oldest entry on a full page is newer than
those held, its absence says nothing, and it is not checked. A bulk handle, all or none,
is decided by the flagged list read from just past the highest note it named, so notes
flagged since cannot push the named ones off the page: one still listed means none
landed, and sending again only marks them.

**A note names no revision.** A move and a break name the revision the page showed, so
a task that changed since is refused, and the page draws the task the refusal carries
rather than reading again (`task-registry/DESIGN.md`, "The operator API"). A note does
not: the revision covers a task's spec, state and holder, and a note is none of them
(`task-registry/DESIGN.md`, "One revision covers content, state, and who holds the
task"). Nor does a move on a note: a hide or a handle changes none of them either, so
two tabs replying to one note post two replies, which the timeline shows. The
lost-answer check above is what keeps one from being sent twice.

**The moves are in the detail, below the plan, one at a time.** Only the task read
beside the list carries them, never an inbox row, so a move is made on what was read.
The moves on a note sit under it in the timeline, on every page of it. The one write
outside the detail is the flagged list's bulk handle (below), which names the notes a
page drew rather than a task. Approve and send back are each a control of their own, as
a refined task's two answers. Approve names the revision and plan it approves and says
when what it approves hides a character, and a move that ends a claim names its holder.
One write is out at a time, its controls off until its outcome is drawn, the flagged
list's too, and the poll never redraws the detail, so no button moves under the pointer
between two clicks. No move asks to be confirmed: each is recorded and undone by another
move, but a reply, which can be hidden and never removed. A move on a note touches
neither the task's spec nor its revision, so whatever holds the task's moves leaves it
free, as it leaves adding a note.

**The inbox: whose turn it is, worked out.** The page opens on what is the operator's
turn rather than on the backlog, as Gerrit's attention set does. `operator_moves.inbox`
works it out from the store on every read, so there is no read or unread state to keep
or to drift from the tasks, and a task leaves the inbox when what put it there changes.
One row per task, in the highest section any of its reasons puts it, with every reason
and since when (`operator_moves.SECTIONS`). Only what the operator alone can act on is
in it, a flagged note, work to land, a plan to triage, so a fresh claim or an open task
is not; what goes wrong slowly, a claim that ran out or a task gone quiet, sits lower and
a few at a time, so it shows without burying the rest. What waits and what is to triage
come whole, never a page of them. A deferred or closed task is out of it: deferring or
closing is the operator's own decision, so it is nobody's turn until reopened, and a
flagged note on it waits with it and comes back when it is. Every
task is a view away, under All tasks.

**The flagged notes are listed above the inbox, for handling together.** A flagged note
puts its task in the inbox with a count, unless the task is deferred or closed, but a
bulk handle names the notes a page showed, never a filter (`task-registry/DESIGN.md`,
"Notes"), and a count names none. So the inbox view opens on the notes themselves, the
newest page of the flagged list. This partly reverses #226, which replaced a "For you"
table of them with the inbox's rows and dropped the relayed read; those rows and the
title's count stay as it made them. A row is a box to select it, its task to open, who
wrote it and when, and the note spelled; "handle selected" sends the notes both selected
and drawn, in the order drawn. Hide, show and reply are on the note in the timeline, not
here. The list carries notes on a deferred or closed task too, each with its task's
state: such a note waits with its task and comes back to the inbox when it is reopened,
so handling it by accident would lose it for good. Its row says the state as of the
draw, "select all shown" leaves it out when the draw or the last good read says its task
is parked, and it is handled only when ticked on its own. A tick records whether its row
is marked parked, the only informed choice of such a note, so a note whose task was
parked since the draw, seen by a redraw or only by a poll, is never sent: a redraw
un-ticks it, and the bulk and its count leave it out, named beside the handle and in the
outcome. A task parked in another tab after the last poll and before the click is not
seen, so its note can be sent: it takes two tabs and a click within one poll interval,
and a registry guard that would close it is a follow-up task; the count line says how
many such notes the read holds, so its total and the inbox's can be told apart. Rows are
drawn only from a read the operator caused, or the first since the registry answered:
one after a move on notes or on a task, which can park or reopen a drawn note's task,
drawn while that write's controls are still off, one on coming back from All tasks, and
"show them". Every other read, the poll's, rewrites only a count line whose space is
kept: how many are not handled, how many are not shown, how many are gone since the draw
and how many are on a task deferred, closed or reopened since, elsewhere, with "show
them" when any is. The block sits above the sections the poll redraws, its rows in a box
of fixed height, so neither moves the other. A note handled or hidden elsewhere stays
drawn until a draw; handling it again only marks it again, a second entry in its
history. A bulk's outcome stays over the list until the next bulk or All tasks.

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
something to approve, so it carries a warning. Every plan the task had is read with it,
and the earlier ones are drawn whole on request, newest first, so what a plan replaced
can be read beside it.

**A revised plan is drawn as what changed.** A refiner's revision is mostly the plan it
replaced, so a plan with a predecessor (`planPredecessor`: the next lower id, not the one
the operator last read) opens on what changed from it, section by section in the same
order (`planDiff`), and the whole plan is one toggle away. A section is unchanged,
added, removed or changed; a changed one is its words, kept, added and removed
(`diff.js`), each run spelled as the body is, added and removed marked by their element
as well as their colour. Equality is on the raw text, so a change only a hidden
character makes shows, and one only whitespace makes is said in words, since its runs
may draw as nothing. Past an edit ceiling a section is shown rewritten, both sides
whole, because the search's cost grows with the edits and the text is an agent's. The
approve control counts hidden characters over the whole plan whichever view is open,
so an unchanged section that carries one is drawn spelled rather than as a line: what
the count warns of is on screen in either view.

**One timeline: the history, carrying its notes.** Each note is posted beside the
history row that names it, and the history read carries it there as it is now
(`task-registry/DESIGN.md`, "Every write leaves a history row"), so the detail shows
one record, oldest first and paged as the history is, with each note once among the
writes around it, its marks beside it and a hidden one set aside. A page-side merge of
the notes read and the history read could not page both by one cursor. Each entry's
other values are folded; the note a hide or a handle acts on stays in the open, to
match it with the entry that posted it.

**A task that changed while it is read says so, and moves nothing.** The poll reads
the task open beside the list (`refreshOpenTask`) and compares it with the one drawn
(`taskUpdate`): a newer revision is a change, and a newer note alone is one too,
though no revision moves for a note, because a note saying "don't approve this" that
arrives while the operator reads is worth a line. Either fills one line atop the detail
whose space is kept whether it says anything or not, so filling it shifts nothing; it
stays in view as the detail scrolls, and "show it" reads the task again through the
hash. A refused move's "the task changed" notice is drawn in the same line, since the
task it draws is the refusal's and the timeline is the one held; every other outcome
stays under the moves, beside the controls it is about. When the poll may fill or
replace the line, and why it never empties it, is `refreshOpenTask`'s comment. A move
from a stale view still meets the revision's refusal.

**Its tests.** `tests/test_tasks_ui.py` holds the server: the relay's table, the guard
in front of its writes, which errors are not running and which an unknown outcome,
`/running`, the files it serves, and that it starts behind the shared guards under its
own name, allowlist, address and network. `tests/test_tasks_ui_js.py` holds the page:
every structural check the approval page has (`PageChecks`: markup and script, the
module list against the Dockerfile and the imports, fetches against the relay, free
names), the writes' methods against the relay, the pure halves of `tasks.js`,
`moves.js` and `diff.js` under node, and, from source, how the page gates the
registry, sends its writes, draws agent text and fills the line atop the detail;
`tests/test_task_registry_inbox.py` holds what the inbox counts, and
`tests/test_task_registry_operator.py` that each note is on the history row that
posted it.
`make ui-diff UI_DIFF_PAGE=tasks` runs `tests/ui-harness/tasks-scenario.js` on the base
and on the branch.
