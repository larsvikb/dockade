---
name: review-before-push
description: Before telling the maintainer a dockade commit is ready to push, have the reviewer agent check it with a fresh context, then verify each finding, fix or decline it, and record the review. Use after committing, before the hand-off.
---

A review earns its time on what an author cannot see in their own work: claims that
outrun the evidence, Verify steps that do not show what they say, the same defect
elsewhere, docs the change made false. The `reviewer` agent brings a blank context.
This skill brings it what that context lacks, and deals with what comes back.

## 1. Commit first

The reviewer reads commits, in a clone of its own. Everything under review is
committed on the branch, and the commit body is the PR description, reviewed with the
diff.

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
- your premises, marked as premises, so it can challenge them;
- your doubts: the questions you could not settle.

Launch the `reviewer` agent with it, in the background, and keep working: it reads
the commit in its own clone, so your checkout may move.

## 4. Triage what comes back

- Verify each finding yourself before acting on it. Reviewers are wrong too.
- Fix it, or decline it with a reason. Unpushed, amend; pushed, add a commit to the
  branch.
- A fix larger than a line or two gets its own mutation check. A rewrite gets a second
  review.

## 5. Record it

- Save the report, with your verdict on each finding (fixed, or declined and why), as
  `review-<branch>.md` in the worktree root, untracked. The doc guards read only what
  git tracks, so check it by staging it: `git add -N` it, run
  `(cd tests && python3 -m unittest test_docs)`, then `git rm -q --cached` it.
- Tell the maintainer the verdict and the findings in plain words, before the push.
- Add one line to `local-review-log.md` — the PR, the reviewer and how long it took,
  the findings in the diff by severity and what became of them, anything outside the
  diff — and move what is left over into the sections of `local-todo.md`. Both are
  the maintainer's untracked files in the main checkout, and a worktree session cannot
  write them: write an anchored script that makes the edits, dry-run it on a copy,
  and hand it to the maintainer to run from the main checkout.
- When the log shows a pattern that changes the reviewer's brief or this skill, the
  commit that changes it states the evidence in its message: the log is untracked, and
  the reason for a committed change must not be. A finding about reviews that would
  hold in another repo is evidence for `NOTES.md` instead.
- Delete the report once the branch is pushed.
