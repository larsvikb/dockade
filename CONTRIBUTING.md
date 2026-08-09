<!-- SPDX-License-Identifier: Apache-2.0 -->
# Contributing

Contributions are welcome — fixes, features, documentation, and disagreement with
decisions already made. Fork, branch, run `make check`, open a pull request.

Two things to read first, because they will save you the most time:

- **[`SECURITY.md`](SECURITY.md) if you found a way across the boundary.** That is a
  private report, not a pull request. It also lists the weaknesses that are already
  known — accepted or still open — which is worth a look before you spend time on one.
- **[`CLAUDE.md`](CLAUDE.md) → *Invariants — never violate*.** This is a security
  boundary, so a handful of properties are not up for trade against convenience. A
  change that crosses one gets declined however good the rest of it is, and the list
  is short enough to read in a minute.

Everything else here is mechanics.

## Setting up

You need **Docker** and **GNU make**. That is enough to run the project.

For the full gate you also want `shellcheck`, `hadolint`, `ruff`, `yamllint`, `node`
and `python3`. You do not have to install them: every linter stage **skips** when its
tool is absent, so a partial toolchain still runs the checks it can. CI installs all
of them and refuses to skip (see below).

```bash
make help          # every target, with a one-line description
make check         # the gate: linters + consistency guards + unit tests + image builds
make up            # bring up the shared infra (egress proxy, control plane, UI)
make claude        # launch a tier-1 sandbox in the current directory
```

## The gate

**`make check` is the whole verdict.** CI runs the same targets rather than
reimplementing them, so a red run reproduces locally — with one difference that
matters:

```bash
make check-strict   # the same targets CI runs, with a MISSING TOOL a failure not a skip
```

Use `check-strict` before opening a pull request if you have the full toolchain. The
strictness exists because a runner without `hadolint` would print `SKIP hadolint` and
pass green, verifying less than the badge claims with nothing saying so.

Two stages are worth knowing about in advance, because they fail for reasons that are
not about your code being wrong:

- **`consistency`** — repo-spanning guards that have no other home: the firewall
  allowlist must be a subset of the control-plane policy seed, the inference server's
  context window must exceed the client's by a measured margin, the launchers must be
  executable *in the git index* (not just on disk — a bind mount with
  `core.fileMode=false` hides that, and it has shipped a broken clone before), and the
  sandbox must have no path to the control plane.
- **`verify-build`** — builds all five images. Skipped automatically when Docker is
  unavailable, so a green local run does not always mean CI will agree.

## Tests

`tests/` is **dependency-free on purpose**: plain `unittest`, no pip installs, no
fixtures library, no test runner. `fastapi` and `pydantic` are stubbed
(`tests/_loader.py`) so the control plane imports without them. Please keep it that
way — the suite runs anywhere `python3` does, including inside the sandbox image,
and that property is worth more than the convenience of any given library.

**A new test should fail if you break the thing it names.** Obvious, and easy to get
wrong: the habit here is to check it rather than assume it — break the behaviour
deliberately, watch the suite go red, restore. Several tests exist *because* that
found a gap, including a payload that changed shape while the whole suite stayed green
and a dismiss button that could silently send the wrong number.

Two things learned the hard way if you script it. Restore from a **copy of the working
tree**, never `git checkout --` — that reverts to `HEAD` and eats uncommitted work.
And confirm each mutation actually applied; one that silently fails to match its
pattern reports a reassuring "caught" while testing nothing.

Behaviour that lives in `start()` in `control-plane-ui/app.js` runs only in a browser
and is deliberately not unit-tested. Where a mistake there would be silent, the
convention is a **source-level guard** — a test that reads `app.js` and asserts the
call site looks right. There are several to copy from.

## Commits

Subject line in the imperative, describing what the change does to the system:
`Collapse duplicate holds onto one card`, not `fixed grouping`.

The body is where this repo differs from most. **It carries the reasoning** — why the
change is right, what was considered and rejected, and, for a bug fix, how the bug was
found. Commit messages here are frequently long, and that is deliberate: a commit is
dated and immutable, so it is the one place a narrative cannot rot. `git log` is the
best explanation of several decisions in this codebase.

There is no `Fixes #123` requirement and no commit-message linter. Read a few recent
commits and match the register.

**Where prose belongs** is a decision with four other answers besides the commit
message — code comment, `DESIGN.md`, `NOTES.md`, `CLAUDE.md` — and picking wrong is
how documents rot. [`CLAUDE.md`](CLAUDE.md) → *Where writing goes* has the table and
the test to apply; it is not restated here, because a second copy would be the exact
failure it warns about.

## Pull requests

- **Squash-merged.** Your branch's history does not need to be clean; the merged
  commit message does. Write the pull-request description as though it were that
  commit message, because it becomes one.
- **`make check` green.** If a check is wrong rather than your change, say so in the
  description and fix the check in the same PR.
- **Touching the boundary?** Run `make check-boundary` before and after. It stands the
  infra up, launches a throwaway tier-1 sandbox and runs `boundary-check.sh` inside it,
  exiting non-zero on any violation — a regression baseline, not a demo. It leaves your
  volumes and any running agent session alone. CI runs the same target, so a boundary
  regression fails the PR. Use `make boundary` instead when you already have a sandbox
  running, or to check tier 2 (whose probes need the local model server up).
- **New source file?** Add an `SPDX-License-Identifier: Apache-2.0` header in the
  first three lines, so licence scanners never have to parse `LICENSE`.
  `make consistency` enforces this for the file types where it applies — scripts,
  Python, JS, HTML, Dockerfiles, the compose file, the Makefile and the workflow. Not for JSON,
  which has no comment syntax, nor for markdown, linter configs or the baked
  dotfiles, which are settings and data rather than works.
- **Adding a recurring workflow?** Make it a skill or a `make` target rather than a
  README paragraph. The paved road is supposed to be paved.

## Licensing, and no CLA

Apache-2.0, and **there is no CLA and no DCO to sign.** Section 5 of the licence
already places anything you deliberately submit under the same terms, so a separate
agreement would add friction without protecting anyone. You keep your copyright.

## AI-assisted contributions

Welcome, and used heavily here — every commit on `main` so far carries a
`Co-Authored-By` trailer for the model that helped write it. It would be strange for a
project *about* running coding agents to be squeamish about it.

Two expectations, neither of them onerous:

- **Say so**, with a `Co-Authored-By` trailer or a line in the pull request. Not for
  purity; a reviewer reads a generated diff differently, and being told is faster than
  guessing.
- **Understand what you are submitting.** You are the author of the pull request and
  the person who will be asked why a decision was made. "The model wrote it" is not an
  answer to a review comment, and on a security boundary it is a bad one.

## Maintenance reality

One maintainer, unfunded, working on this in free time. Reviews are best-effort and
may be slow; nothing is being ignored on purpose. A nudge on a quiet thread is fine.

There are **no releases, no tags and no published images** — fixes land on `main` and
everyone pulls. So there is nothing to backport and no version to target: branch from
`main`, and that is the only branch that means anything.
