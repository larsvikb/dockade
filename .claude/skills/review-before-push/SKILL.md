---
name: review-before-push
description: Before telling the maintainer a dockade commit is ready to push, have the `commit-reviewer` agent check it with a fresh context, then verify each finding, fix or decline it, and record the review. Use after committing, before the hand-off.
---

A review earns its time on what an author cannot see in their own work: claims that
outrun the evidence, host checks that would not show what they say, the same defect
elsewhere, docs the change made false. The `commit-reviewer` agent brings a blank
context. This skill brings it what that context lacks, and deals with what comes back.

## 1. Commit first

The reviewer reads commits, in a clone of its own. Everything under review is
committed on the branch, and the commit body is the PR description, reviewed with the
diff: `## What` the change is and why, and `## Checked`, what was tested and what that
showed, and what was not run. Never what the maintainer should run. It names its
task in words, as `Registry task N.`: a squash merge drops the branch, a subject's
`(#N)` reads as a PR number, which shares the task ids' range, and the review's record
sits on that task (step 6).

## 2. Pick the tier

- **Full** — schema and migrations, policy, containment (firewall, proxy, launcher,
  Dockerfiles, gateway, control-plane access), or anything SECURITY.md lists.
- **Standard** — application logic and UI behaviour.
- **Light** — docs, copy, comments.

When unsure, take the higher one. A commit that fixes a defect is at least Standard,
for the search for the same defect elsewhere. The reviewer knows which checks each
tier gets.

## 3. Write the brief

The reviewer knows only the repo and what you tell it. Give it:

- the commit (SHA and branch) and the tier;
- the evidence that is not in the repo — measurements, output the maintainer pasted,
  what was tested where and under which conditions — verbatim, not summarised;
- the host checks you will ask the maintainer for, if any, word for word as you will
  give them, and the script if there is one (step 5);
- your premises, marked as premises, so it can challenge them;
- your doubts: the questions you could not settle.

Launch the `commit-reviewer` agent with it, in the background, and keep working: it
reads the commit in its own clone, so your checkout may move.

## 4. Triage what comes back

- Verify each finding yourself before acting on it. Reviewers are wrong too.
- Fix it, or decline it with a reason. Unpushed, amend; pushed, add a commit to the
  branch.
- A fix larger than a line or two gets its own mutation check. A rewrite gets a second
  review.
- When a worker, the `implementer` agent, wrote the commit, send it the verified
  findings with your verdicts, since it holds the context to fix them, and check its
  fix by diff against the commit the reviewer read.
- When a finding is a hole in an earlier round's fix, ask whether the design is wrong
  before patching again. A fix that must hold for every ordering of a race wants a
  guard where the race is decided, such as a transaction or a conditional write, not
  one more check beside it.

## 5. Host checks, when one earns it

Run everything the container can run, and put it under Checked. Ask the maintainer
only for what needs the host and CI does not already prove — CI runs `make
verify-build` and `make check-boundary` on every PR — such as `make up`, the launcher,
a real browser against their stack, host networking; and only when the result could
change whether the PR merges. A check that could not, skip, and say under Checked
that it did not run. Plan the checks before the brief, so the reviewer sees them; put
them to the maintainer after triage, since a fix can change what the host must check.

- Put the check to the maintainer in the conversation, not in the commit.
- Where it is more than a command or two, write `host-check-<branch>.sh` in your
  worktree root, untracked, for the maintainer to run from the main checkout as
  `bash .claude/worktrees/<name>/host-check-<branch>.sh`. What it must work around
  is in NOTES.md, "A worktree made in the sandbox is unreadable to git on the host".
  So it:
  - resolves its log, `host-check-<branch>.log` beside it (`*.log` is ignored),
    against the main checkout before it changes directory, and names no `/workspace`
    path, which exists only in the sandbox;
  - checks the branch out with `git worktree add --detach` into a `mktemp -d`
    directory, and removes it with `git worktree remove --force` in a `trap` set
    first — never `git worktree prune`;
  - writes the commit it checked (`git rev-parse HEAD`) at the top of the log;
  - for anything compose, goes through `make`, which supplies `MCP_SECRETS`, and
    symlinks the main checkout's `.env` and `models/` into the worktree, since the
    Makefile passes no `--env-file`;
  - before it starts the control plane, runs `make backup`, because the store's volume
    is the live one; and ends by saying the live stack may now be the branch's, and
    what puts it back from the main checkout:
    - always `make up`, and `make tasks-up` if the check started the registry, since
      `make up` leaves it alone;
    - the store is kept, so its rows stay the record: the audit since the backup
      lists what the check changed, by script or by hand, to undo in the approval UI
      (a revoked lease or pin comes back only by a new grant), and the check's
      pending cards are denied rather than left;
    - only when the branch changed what a store holds — a `SCHEMA_VERSION` bump, or a
      value main would read differently — a restore, which drops that store's rows
      since the backup. Main's images first, since a restore runs in the built image
      and restarts without rebuilding: `make up`, which fails on the newer store but
      builds them, then `make restore FILE=…` with the backup's
      `dockade-control-<stamp>.db`; for the registry `make tasks-up` (failing the
      same way), then `make restore-tasks FILE=…` with its `dockade-tasks-<stamp>.db`,
      then `make tasks-up` again.

  It runs on the host with their privileges: keep it short enough to read, and doing
  nothing beyond the check.
- When the results come back: unpushed, amend Checked with what ran on the host and
  what it showed; pushed, update the PR description instead, which becomes the squash
  message, rather than making the maintainer force-push a text change.

## 6. Record it

- Tell the maintainer the verdict and the findings in plain words, before the push.
- Propose what is left over in the registry with `tasks__propose`, with a severity and
  a kind, searching and folding as its description says.
- Post the review's record on its task with `tasks__add_note`, not flagged for the
  operator, once triage and any host checks the review led to are done, from whichever
  checkout you are in: it takes no token and works in any state, so a commit reviewed
  after `submit_work` is recorded the same way, and the record stays apart from that
  hand-in. One record per review of a commit, every pass in it, within a note's 4000
  characters (the registry refuses a longer one; trim what is outside the diff first):
  - first line exactly `Review record: YYYY-MM-DD, <branch>`, the session's local date;
  - second line the tier and the reviewer's time per pass;
  - then the findings in the diff by severity and what became of each, the host checks
    and what they showed, and what was found outside the diff, by the task it went to.

  Write it as a past-tense account that names tasks, never as instructions: search
  carries notes to sessions on unrelated work. A commit covering several tasks carries
  the record on one and `Reviewed with task N` on the others, so a review counts once.
  Work with no task gets no record.
- Read records side by side with `tasks__search_tasks` for the marker with a date
  prefix, never bare, since a note quoting the template matches it: `Review record:
  YYYY-MM` for a month, or a day once `total` passes 50, as a search keeps the 50
  highest task ids. Note ids rise across tasks, so they give the reviews' order;
  `tasks__task_notes` with an id plus one as `before` returns that record as the last
  of up to 20 notes. A skipped record leaves no trace; the spot check is each
  task's `submit_work` note, which `tasks__get_task` marks by its move, against its
  record. Reviews before records are in the main checkout's `local-review-log.md`, a
  frozen archive whose header no longer applies.
- When the records show a pattern that changes the commit reviewer's brief or this
  skill, the commit that changes it states the evidence in its message: the records are
  not in git, and the reason for a committed change must be. A finding about reviews that
  would hold in another repo is evidence for `NOTES.md` instead.
