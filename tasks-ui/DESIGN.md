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
writes are the ones the page makes: a new task; on a task, a move, an approve with its
answers, breaking a claim, a spec edit, a reparent and a note; on
notes, hiding or showing one, handling one with or without a reply, and handling the
flagged notes a page showed; on the repo list, adding a repo, renaming one, setting or
removing its remote, and retiring one or bringing it back. A task or note is named in
the path by bounded digits, a repo by the registry's own pattern for a name, held equal
to `tasks.REPO_NAME_RE` by a test, since the relay cannot import it; a new repo's name
rides in the body. The bind and the peer check are the approval UI's
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
again and decides from it (`checkWrite` in `moves.js`): a move, approve or break whose
revision did not move has not landed yet and cannot land twice, since the revision would
refuse a second; an approve whose revision moved has landed only if its plan now has
exactly the answers sent, since another write could have moved the task to the state it
predicted; a note, which names no revision, has landed only if an operator note with its
text is newer than any the page held, and is otherwise "not found yet", never "safe to
send again", since it may still be committing. A move on a note has landed if its own
history entry, naming the note with the value sent, is newer than any the page held; the
newest page of the timeline carries it wherever the note sits. Absent, a hide, show or
handle is "not seen", harmless to send again since it sets the same flag, and a reply is
"not found yet", as a note is; when even the oldest entry on a full page is newer than
those held, its absence says nothing, and it is not checked. A bulk handle, all or none,
is decided by the flagged list read from just past the highest note it named, so notes
flagged since cannot push the named ones off the page: one still listed means none
landed, and sending again only marks them. A reparent is decided as an edit is, by its
revision and then the parent it sent. A create names no revision and is not idempotent,
and the page's list is filtered while ids are global, so the newest id it holds says
nothing; instead it reads its repo's newest id in every state, inside the gate, before
the send (`readBound`), and sends nothing when that read fails. Ids are never reused and
a task never changes repo, so on a lost answer a task past that bound with the title and
body sent, read in full, is the one it made (`createdTask`); none on a page that reaches
the bound is "not found yet", a full page all past it "could not check", and neither is
"safe to send again". A note's task is drawn, so its timeline can be read again by hand;
a lost create has nothing drawn, so the write is kept as sent, with its bound, and "look
again" reruns the check on it, never on the draft as edited since, which would miss the
task that landed. Found, the task opens, a draft edited since stays, and the line says
the task was created as sent. Until then the create reads "send anyway — may make a
second task", and only one that lands or is lost again replaces the kept write: a
refused one leaves it, since the first may still land. A reload loses both, as it loses
a note's draft. Repos carry no revision, so a lost repo write is settled by the repo list
read again (`checkRepoWrite` in `repos.js`), by the named repo's entry alone, both names
for a rename, so a repo added elsewhere meanwhile changes nothing: as sent is found; as
drawn is "not seen", safe to send again, since a landed add or rename refuses a second
and a remote or a retire sets a value; anything else "changed"; and a read that failed
"could not check".

**A note names no revision.** A move and a break name the revision the page showed, so
a task that changed since is refused, and the page draws the task the refusal carries
rather than reading again (`task-registry/DESIGN.md`, "The operator API"). A note does
not: the revision covers a task's spec, state and holder, and a note is none of them
(`task-registry/DESIGN.md`, "One revision covers content, state, and who holds the
task"). Nor does a move on a note: a hide or a handle changes none of them either, so
two tabs replying to one note post two replies, which the timeline shows. The
lost-answer check above is what keeps one from being sent twice.

**The moves are in the detail, below the plan, one at a time.** Only the open task
carries them, never an inbox row, so a move is made on what was read.
The moves on a note sit under it in the timeline, on every page of it, and a reparent,
the parent typed as an id as blockers are, under the moves. Three kinds of write are
outside the detail: the flagged list's bulk handle (below), which names the notes a page
drew rather than a task; a new task, from a form above All tasks, in an active repo and
any state but refined, kept across views until it lands or is discarded, its outcome a
line of its own under it and the task, once it lands, opened through the hash; and the
repo list's, from the Repos view, a view of their own since the list is the operator's
and no task's. Each repo's row keeps what is typed in it across draws until its write
lands, and nothing redraws the rows while the registry answers but a read the operator
caused, entering Repos or a repo write's own. A read answered after a newer one was
drawn draws nothing, though a write is still settled from its own read, and a rename's
follow (below) is made by whichever read draws first; a poll reads the list only when it
finds the registry back, while the view is hidden. A refusal's reason is spelled as
agent text is. A rename says, before it is pressed and once it lands, that the repo's
instructions file must name the new name before its agents can write again
(`operator_moves.rename_repo`), and a rename of the repo All tasks shows, once the list
shows it landed, takes All tasks' select with it. Approve and send back are each a
control of their own, as a refined task's two answers. Approve says when what it
approves hides a character. One write is out at a time, page-wide: a repo write goes
through the same gate (`beginRepoWrite` in `tasks.js`), and every control is off until
its outcome is drawn, the flagged list's, the create form's and the Repos view's too,
and the poll never redraws the detail, so no button moves under the pointer between two
clicks. Opening a task from another replaces what is under the pointer, so the second
click of a double-click is neither a move nor an open of a related task. No move asks to
be confirmed: its label, which names the outcome before it is pressed, is the
confirmation, as approve's names the revision and plan, a reparent's the parent and
revision, a create's the repo and state, since a task can be closed but never removed,
and each repo write's what it does to which repo. Most moves are recorded and undone by
another move, but not all: a new task or repo stays, since neither is ever removed,
though a task can be closed and a repo renamed or retired; a note or a reply can be
hidden and never removed; an approve's answers stand; a break, or any state move of a
claimed task, ends the live claim, which no operator move gives back
(`operator_moves.move`); and a move to proposed, a send back or from any other state,
supersedes the current plan, an approved one too, and no move makes a superseded plan
current again (`tasks.update`). A move on a note touches neither the task's spec nor its
revision, so whatever holds the task's moves leaves it free, as it leaves adding a note.

**One scheme of labels.** Every action control on the task page says what it does, and
is marked primary or caution, by one rule (`moveLabel` and the functions beside it): a main
line names the action and its outcome in the operator's words, and a smaller second
line, where needed, the mechanism and its cost, such as the revision and plan an approve
names. Each word means one thing: "close the task" moves it to closed, and the way to
the list is the strip's control. A move that takes away what no move gives back is
bordered in the warning colour, which hover keeps, and says what in its second line: a
move to proposed while a plan is current, and every move that ends a live claim. A write
that adds a record that stays is told by its words instead, since the record is its
point: approve's outcome is its main line, and a note's add says it can be hidden, never
removed. A claimed task's moves open with one line naming the holder. At most one
control is filled as primary (`primaryMove`): approve where the operator can approve,
reopen on a refined task whose plan was answered, and closing a landing task. Proposed,
open, deferred and closed tasks have none, since leaving them is a choice among several.
A move to open names the plan it opens, and reads "reopen" only for one approved, by
its answers or by the `approved_at` the registry derives from its approve
(`planApproved`), a plan with no decisions too; otherwise "open with plan N", or "open
without a plan", so opening a deferred plan nobody approved never reads as approving
it.

**A plan's decisions sit above it, answered with the approve.** A decision is the
refiner's question for the operator (`task-registry/DESIGN.md`, "Decisions"), and it
matters more to the operator than the plan, so the decisions of the plan drawn come
between the body and the plan, each option's label and meaning whole and spelled as the
body is, since this is agent text inside a control the operator acts on. Its inputs
carry positions, never agent text. Choices start on the options the plan assumes, so a
plan the operator accepts as written takes one click, and the label's outcome is what
is read first. A single-choice decision takes an option or an "other" text, never both,
so a qualified answer is an "other" and sends the task back. `approveOutcome` decides
open, sent back or incomplete by `plans.outcome`'s rule, a blank "other" by
`tasks.check_text`'s, and a test holds both to the registry's over every choice two
decisions can hold. While an answer is incomplete no approve is built at all, only a
line naming what is missing, so nothing can send a partial answer or show a label for
an outcome the choices do not reach. Approve has a row of its own, two lines high, above
the other moves, so relabelling it, or send back as "send back without answering" once
a choice differs from the assumed ones, moves neither: send back's second line is wider
than either of its main lines. Only a refined task whose current
plan has no answers draws choices; any other plan's decisions are text, with every
answer the task has had.

**Choices are kept by task and plan; another plan says so.** A redraw of the same plan,
such as a stale revision's refusal, keeps the choices made on it. A draw that brings
another plan than the one this task was last drawn with says so above the decisions,
whatever the choices were, and shows any "other" text it reset: without it, a refusal or
"read it again" that brought a new plan while every choice was assumed would leave one click
approving a plan nobody read. The first choice in the new plan spends that line, which
keeps its space so nothing moves under the pointer, and a landed approve drops it. Open
is offered from no state while the current plan has decisions unanswered, since the
registry refuses it, and from refined only as a reopen of a plan approved before, since
approving is `approve`, which refuses that plan. The inbox's count for a refined task is
what its plan carries, answered decisions included (`operator_moves.inbox`), so the page
says the count and never that decisions wait.

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
history. A bulk's outcome stays over the list until the next bulk or another view; back
from All tasks or Repos, the rows are drawn from a fresh read.

**The list or one task, on the whole page, and the hash names which.** Long bodies,
plans with decisions, timelines and the spec form do not fit a pane beside the list, so
at every width the page shows the list or the open task, never both, and the task takes
the window's width. The list has three views, the Inbox, All tasks and Repos, which the
hash names too. A click on a row opens its task, but on the row's own controls, a
flagged note's box or its cell, or a drag that selected text; the `#id` button is the
keyboard's way. Back is a control in a strip kept atop the task at any scroll, Esc
outside a text field, or the browser's back, and the list comes back as it was left: it
stays in the DOM, hidden, with its filters, search and page, and its row last read
marked. Opening a task, going back or changing view sets the URL's hash (`parseHash`,
`hashFor`), and the page follows the hash, so a reload, a link and the browser's back
agree with what was clicked. A task's parent, children and blockers open through the
hash too, so the browser's back returns to the task they were opened from. The task it
names is read once the registry answers, never before. The page places every scroll
itself (`enterTask` in `tasks.js` says how), since a browser restoring the list's offset
on back would apply it to a task first; so a reload of a scrolled list opens at its top,
the price of placing scroll by hand. A hash naming no task the registry could have, or
one it has not got, shows the list with a line saying so, under the list's URL, so back
and a reload pass over it. The back control sets the list's hash rather than going back
in history, since the entry before a linked or reloaded task is not the list; so the
browser's back after it reopens the task, as after following any link.

**The count is in the title, and quiet.** The page's title carries the number of tasks
waiting on the operator, the inbox's first section, ahead of its name, as the approval
page's carries its pending count, so a background tab shows it in the tab strip. Never
`hot`, and nothing else alerts: no agent is held on the operator
(`task-registry/DESIGN.md`, "Notes").

**Agent prose is spelled where it hides, and only there.** A task's title, body,
notes and claim are prose, so they keep their line breaks and tabs and show visible
non-ASCII as written. The body, the plan, the decisions and the notes are drawn as
markdown too (below). A dash or an accent is ordinary in prose, and spelling it would
bury the one that matters. A character that renders as nothing or reorders the text
around it (`Cf`, `Cc` but the line break and tab, `Zl`, `Zp`, and Unicode's
default-ignorable code points, variation selectors among them) is spelled `\uXXXX`
where it sits, with a note counting them (`spellHidden` in `tasks-ui/tasks.js`),
because an approved body is what every later session reads, and the operator must see
what is in it rather than what the browser draws. Between the approval page's two rules,
the payload's, which notes non-ASCII, and the pin's, which spells it all
(`control-plane-ui/DESIGN.md`), for that reason. The spec editor's mirror alone draws it
unspelled, and why is below, with the editor.

**Agent markdown is drawn, never hidden.** Agents write markdown, so the detail draws
the body, the plan's parts but its footprint, each decision's question, why and option
meanings, an answer's question, an option's label inline only, and the notes in the
timeline as a subset: ATX headings, paragraphs, bullet and numbered items, emphasis,
code spans and fenced blocks (`markdown.js`). Only that subset, because the operator
approves what they read: a link or an image can show one thing and point at another, or
fetch a URL; raw HTML and its comments can hide text or draw markup; so these and every
other construct are drawn as typed, never dropped, and the page builds each element
itself from text (`markdownBlock` in `tasks.js`), every run spelled as above. What the
drawn form leaves out is only ASCII delimiters, a heading's hashes, a bullet, emphasis
and backticks, a fence's run: line ends, indentation and a numbered item's number stay,
no list draws a browser marker, a bullet item shows a disc where its bullet was, and
emphasis opens and closes only beside space or punctuation, stricter than CommonMark: a
run that could both open and close does neither, and none opens before a `.` or a `/` or
closes after a `/`, so a quoted `"*"`, a path or a glob outside backticks stays as
written. What a session reads is the source, and a copy from the drawn form lacks the
delimiters, so one switch atop the detail shows every such field as typed and back; each
field holds both forms and a class on the detail picks one, so the switch redraws
nothing. The title, the footprint, a claim's progress, history values, the operator's
own "other" text, the spec form's preview, a plan's diff view and the flagged list stay
as typed, as does the export (`task-registry/export.py`). The tokenizer is lossless and
draws nothing, so the spec editor's highlighter shares it, and linear, since an
agent's text is its input; it nests spans `NEST_MAX` deep at most, since the browser's
layout costs the square of the depth.

**The plan sits under the body, part by part.** Approving a refined task approves its
body and plan together (`task-registry/DESIGN.md`, "Plans"), so the detail shows both,
the plan in the order `submit_plan` takes its parts, each as prose spelled like the
body, its parts but the footprint as markdown. A missing or superseded plan is read
against the task's state (`planNotice`):
only a proposed task is refined by an agent, so only there does the page say a plan is
coming; past it, the operator moved the task on without one. A superseded plan is never
something to approve, so it carries a warning. Every plan the task had is read with it,
and the earlier ones are drawn whole on request, newest first, so what a plan replaced
can be read beside it.

**A spec edit sends what changed, as it will be stored.** Editing the spec is how the
operator records a decision with authority, since a note never outranks the body, so it
is offered in every state. The form takes the place of the lines that show the spec, its
values set as properties, and sends only the fields the operator changed, compared with
the read it was drawn from as each control hands a value back (`asControl` in
`moves.js`): a textarea turns CR LF into LF, a text input drops line breaks, and a select
reads a value it has no option for as none. Compared raw, an untouched body stored with
CR LF would be sent and rewritten. Severity or kind set to none goes in `clear`; blockers
go whole, and only when the set differs, since `edit` replaces them. Under the title and
body, what will be stored is shown spelled, with how many characters of the stored text
the control changed. The draft is kept by task across draws, and a draw past the read it
was made from, whatever brought it (a refusal's task, the read after a write, "read
it again"), moves it onto that read (`rebaseDraft`): the fields the operator changed keep the
draft's values and the rest take the newer read's, so a save never reverts another
writer's change to a field left alone; a field both changed keeps the operator's, after a
line names it. Only a landed edit, discard, or leaving the task for the list or another
task drops it. Leaving a draft with changes, by any change of the hash, asks once first,
and closing or reloading the page asks too. Declined, the task stays as it was, its
scroll included, and the move is undone in history, never written over; each entry the
page follows carries its index, which is how the undo knows where to go (`undoLeave`).
Entries made before the page stamped them, in a tab left open on an older page and
reloaded after the upgrade, can be undone in the wrong direction once; no draft is lost
by it, so nothing handles it. That confirm guards text not yet sent, not a write, so
"no move asks to be confirmed" above stands; a draft with no changes drops unasked,
since nothing is lost. While the draft has changes, approve, send back, every state
move and breaking the claim are held, since each would act on a spec other than the
one on screen; a note, the moves on notes and a reparent stay free, a reparent since
it changes no spec field, so the draft only rebases onto the read after it. An edit
leaves a claim as it is (`operator_moves.edit`), and the save says the claim stays. A
refined task's form says the edit amends the spec its plan was written against, and
once the task's revision is past the plan's, `planNotice` says approving approves the
task as it now is, with that plan, or for a plan approved before, that reopening opens
it so: for a refined task only, since an approved task's revision always passes its
plan's.

**A long body is edited in a field as long as it, coloured as it is typed.** The operator
rewrites specs of thousands of characters here, so the body's textarea grows to its text
and the page scrolls, not the field: a mirror of the text, in flow in the same grid cell,
sizes it (`bodyField` in `tasks.js`, and its rule in `index.html`). The same mirror
colours the markdown: the textarea stays the editor, so undo, input methods, spellcheck
and selection are the browser's, and draws only its caret and selection over the
mirror's runs, which `highlightRuns` (`markdown.js`) reads off the detail's own
tokenizer, so the editor colours nothing the detail would not draw. A mirror, not an
editor library, since the page loads no third-party code. The mirror is the one place
agent text is drawn unspelled: to line up with the textarea it must hold the characters
the textarea holds, so it builds its runs as text, never markup, and the body's preview
spells them (`drawMirror`). Since the page scrolls to the caret, while the form is open
the page keeps it out from under the strip and notice line stuck atop it, by a scroll
padding as tall as both, which the controls in that band take back, so focusing one
scrolls nothing. Save and discard sit right under the body, not pinned to the window;
to put them there without placing save above a field it sends, the body is the form's
last field, and its spelled preview comes after them, so the line beside them counts
what that preview has to show (`storedCount`) and keeps one line's height, so filling it
moves neither button.

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
the open task (`refreshOpenTask`) and compares it with the one drawn (`taskUpdate`): a
newer revision is a change, and a newer note alone is one too, though no revision moves
for a note, because a note saying "don't approve this" that arrives while the operator
reads is worth a line. Either fills one line atop the detail whose space is kept whether
it says anything or not, so filling it shifts nothing; it stays in view under the strip
as the page scrolls, and "read it again" reads the task through the hash, keeping the
scroll. A refused move's "the task changed" notice is drawn in the same line, since the
task it draws is the refusal's and the timeline is the one held; every other outcome
stays under the moves, beside the controls it is about. A redraw of the read already
drawn, as opening or discarding an edit makes, keeps what the line says. When the poll
may fill or replace the line, and why it never empties it, is `refreshOpenTask`'s
comment. A move from a stale view still meets the revision's refusal.

**Its tests.** `tests/test_tasks_ui.py` holds the server: the relay's table, the guard
in front of its writes, which errors are not running and which an unknown outcome,
`/running`, the files it serves, and that it starts behind the shared guards under its
own name, allowlist, address and network. `tests/test_tasks_ui_js.py` holds the page:
every structural check the approval page has (`PageChecks`: markup and script, the
module list against the Dockerfile and the imports, fetches against the relay, free
names), the writes' methods against the relay, the pure halves of `tasks.js`,
`moves.js`, `repos.js`, `diff.js` and `markdown.js` under node, the approve's rule and
body against the registry's own (`ApproveRuleTests`), the edit's, the create's, the
reparent's and the repo writes' bodies on a scratch store (`EditRuleTests`,
`CreateRuleTests`, `ReparentRuleTests`, `RepoRuleTests`),
and, from source, how the page gates the registry,
sends its writes, draws agent text and decisions, and fills the line atop the detail;
`tests/test_task_registry_inbox.py` holds what the inbox counts, and
`tests/test_task_registry_operator.py` that each note is on the history row that
posted it. `make ui-test` runs `tests/ui-harness/tasks.test.js` in a browser against
expected values, beside the approval page's: opening a task, each way back, a task URL
loaded or reloaded, a hash naming no task, leaving with an edit open, agent
markdown drawn and shown as typed, and the spec editor: its field's height against its
mirror's, the mirror drawn as text, the caret kept out from under the bands atop the
page, and the save row. Unlike the approval page's, these read layout boxes
and scroll offsets, since where the list and the task are is what they hold; still never
pixels.
`make ui-diff UI_DIFF_PAGE=tasks` runs `tests/ui-harness/tasks-scenario.js` on the base
and on the branch.
