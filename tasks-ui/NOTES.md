# Notes — the tasks UI

The evidence behind `tasks-ui/DESIGN.md`. The split rule is the root `NOTES.md`'s: a
measurement or observation lives here, and the decision it produced lives in the
design.

## Inbox prior art

What other tools do about whose turn it is, behind the attention-set inbox in
`tasks-ui/DESIGN.md`. Fetched 2026-10-14, and the quotes came through a summariser,
so check one at its URL before citing it.

- **Gerrit's attention set is the model.** Whose turn it is is computed by rules: a
  reply removes you, a reply to you adds you, an outdated vote re-adds you, a job
  re-adds owners of quiet changes. "Your turn" heads the dashboard with a "Waiting"
  column, and a hovercard says why and when. Votes count only on the current patch
  set, with diffs between patch sets
  (gerrit-review.googlesource.com/Documentation/user-attention-set.html,
  user-review-ui.html).
- **Linear's Triage:** Accept 1, Decline 2, Duplicate 3, Snooze H ("until a time …
  or new activity, whichever comes first"). Its Inbox has a Priority tab, J/K, and Peek
  on Space (linear.app/docs/triage, /inbox, /peek).
- **GitHub's triage order** puts "Who am I blocking?" first (docs.github.com,
  customizing-a-workflow-for-triaging-your-notifications). The issue timeline mixes
  comments and events, and "Viewed" unticks when a file changes.
- **beadside** (github.com/thatmike1/beadside), the closest tool, opens on "an inbox
  of what you can clear in one action". It has a detail pane on the right,
  agent-flagged `needs-human` items in a pinned strip, and three stale items a day for
  a verdict.
- **Plane:** Intake (accept/decline/snooze/duplicate), with side peek by default.
  **Vibe Kanban:** board plus right panel, with "Needs Attention" marks; it is being
  sunset.

**The case for an inbox:** SRE's "every page should be actionable", and PagerDuty's
"an alert is something which requires a human to perform an action". The operator is
the bottleneck the agents wait on. Guards against the counter-arguments:
- only actionable items are in the inbox, so a fresh claim is not one;
- a lower "drifting" section lets slow problems show;
- "All tasks" is one key away;
- the count is quiet, and the empty state is clear;
- `deferred` is the parking place.

**Not taken:** stored notifications or read/unread state, drag to change state, opaque
auto-priority, and snooze as new stored state.
