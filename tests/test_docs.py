# SPDX-License-Identifier: Apache-2.0
"""Guards over claims the documentation makes about the repo.

Same shape as ``test_topology.py`` — two ends with no compiler between them — but
the drifting end here is prose. Every check below exists because the same failure
already happened: `mcp-net` shipped, and the `## Networks` section of ``DESIGN.md``
kept describing five networks and a triple-homed proxy for as long as nobody
re-read it. Nothing broke, which is the point. A stale document is not a failing
test, it is a document that teaches the wrong topology to whoever reads it next,
including the agent that reads ``CLAUDE.md`` on every boot.

Scoped deliberately to claims with a machine-checkable counterpart. Prose that
only a human can judge is not in here and should not be — a guard that needs
constant appeasement gets deleted, and takes the useful ones with it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "mcp-servers.yml")

#: Absolute path, so the command below is not resolved through PATH.
_GIT = shutil.which("git")
#: Same arrangement as ``test_control_plane_ui_js.py``: without git these guards
#: cannot tell a tracked doc from an untracked scratch file, so they skip — which
#: is right on a machine without git and a silent hole in CI, where
#: DOCKADE_REQUIRE_TOOLS turns the skip into the failure it should be.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))

#: Number words as they appear in this repo's prose. Only the ones actually used —
#: a longer table would imply the checks below cover claims they do not.
COUNT_WORDS = {"single": 1, "dual": 2, "double": 2, "triple": 3, "quadruple": 4,
               "quintuple": 5, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _tracked() -> list[str]:
    """Files git knows about. Read from the INDEX rather than the filesystem for two
    reasons: an untracked scratch file (`local-todo.md` is gitignored and present)
    must not fail the gate, and a new doc must be covered the moment it is staged.
    """
    if not _GIT:
        return []
    out = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
        [_GIT, "-C", str(ROOT), "ls-files"],
        capture_output=True, text=True, check=False)
    return out.stdout.split() if out.returncode == 0 else []


def _docs() -> list[str]:
    return [f for f in _tracked() if f.endswith(".md")]


def _lead_ins(text: str) -> set[str]:
    """What a doc's sections go by: its headings, and the bold or italic phrase a
    paragraph or bullet opens with. Each opening is read across the next two lines as
    well, since a bold lead-in in a hard-wrapped doc wraps as often as not."""
    lines = text.splitlines()
    found = set()
    for i, line in enumerate(lines):
        if m := re.match(r"#+\s+(.*)$", line):
            found.add(m.group(1))
            continue
        opening = " ".join(lines[i:i + 3])
        m = (re.match(r"\s*(?:[-*]\s+)?\*\*(.+?)\*\*", opening)
             or re.match(r"\s*\*([^*]{4,200})\*", opening))
        if m:
            found.add(m.group(1))
    return found


def _compose_text() -> str:
    return "\n".join((ROOT / f).read_text() for f in COMPOSE_FILES)


def _declared_networks() -> set[str]:
    """Top-level ``networks:`` keys — 2-space indent, which distinguishes a network
    DECLARATION from a service's attachment to one (6 spaces)."""
    text = (ROOT / "docker-compose.yml").read_text()
    block = re.search(r"^networks:$(.*?)^[a-z]", text, re.M | re.S)
    if not block:
        raise AssertionError("docker-compose.yml has no top-level networks: block")
    return set(re.findall(r"^  ([a-z0-9-]+):", block.group(1), re.M))


def _service_legs() -> dict[str, set[str]]:
    """service -> the networks it attaches to, across both compose files."""
    legs: dict[str, set[str]] = {}
    svc = None
    for line in _compose_text().splitlines():
        if m := re.match(r"^  ([a-z0-9-]+):\s*$", line):
            svc, in_nets = m.group(1), False
            legs[svc] = set()
        elif svc and re.match(r"^    networks:\s*$", line):
            in_nets = True
        elif svc and re.match(r"^    \S", line):
            in_nets = False
        elif svc and in_nets and (m := re.match(r"^      ([a-z0-9-]+):", line)):
            legs[svc].add(m.group(1))
    # Networks are declared with the same 2-space indent as services, so the walk
    # above also picks up the networks: block. Keep only keys with legs or a build.
    return {s: n for s, n in legs.items() if n}


class _NeedsGit(unittest.TestCase):
    """Base for the guards that read the git index, so the skip is declared once.

    ``NetworkRosterTests`` deliberately does not inherit it: that one reads
    ``DESIGN.md`` and ``docker-compose.yml`` by name and needs no index at all."""

    @classmethod
    def setUpClass(cls) -> None:
        if _GIT:
            return
        if _STRICT:
            raise AssertionError(
                "git is not installed and DOCKADE_REQUIRE_TOOLS is set — refusing to "
                "report success for the doc guards, which read the index to tell a "
                "tracked doc from a gitignored scratch file. Install git, or drop "
                "strict mode to skip them knowingly.")
        raise unittest.SkipTest("git is not installed — cannot read the index")


class NetworkRosterTests(unittest.TestCase):
    """`DESIGN.md` → `## Networks` is the canonical description of the topology,
    and the one place a reader looks to learn what exists. A network that ships
    without a bullet there is invisible to everyone who has not read compose."""

    SECTION: ClassVar[str] = ""

    @classmethod
    def setUpClass(cls) -> None:
        text = (ROOT / "DESIGN.md").read_text()
        m = re.search(r"^## Networks$(.*?)^## ", text, re.M | re.S)
        if not m:
            raise AssertionError("DESIGN.md has no '## Networks' section")
        cls.SECTION = m.group(1)

    def test_every_compose_network_is_documented(self):
        # The bug this file was written for. `mcp-net` had existed since the MCP
        # catalogue landed and appeared nowhere in the roster.
        #
        # Matched against the BULLET SUBJECTS, not the section text: a network named
        # in passing in some other bullet is not documentation of it, and accepting
        # that is how this check would come to pass while the roster stayed short.
        # Verified by removing the mcp-net bullet — a section-wide search still
        # found the name, because the paragraph below the list mentions it.
        rostered = set(re.findall(r"^- `([a-z0-9-]+)`", self.SECTION, re.M))
        for net in sorted(_declared_networks()):
            with self.subTest(network=net):
                self.assertIn(net, rostered,
                              f"{net} is declared in docker-compose.yml but has no "
                              f"bullet in DESIGN.md's '## Networks' roster (rostered: "
                              f"{sorted(rostered)})")

    def test_the_roster_documents_no_network_that_does_not_exist(self):
        # The other direction, which is worse for a reader: a network described in
        # detail that they will not find, and cannot tell was removed.
        declared = _declared_networks()
        for net in set(re.findall(r"`([a-z0-9-]+-net)`", self.SECTION)):
            with self.subTest(network=net):
                self.assertIn(net, declared,
                              f"DESIGN.md's network roster describes {net}, which no "
                              f"compose file declares")

    def test_any_unqualified_count_of_networks_is_the_number_declared(self):
        # "all five networks are implemented" was true when written and wrong two
        # commits later, silently — and a reader has no way to tell which.
        #
        # An UNQUALIFIED count means "all of them", so it is derivable and checked.
        # A qualified one names a subset ("the two internal control nets"), which is
        # not derivable from a total and is legitimate prose; any word between the
        # number and the noun exempts the claim. The fix for a failure here is
        # usually to drop the number, not to update it: CLAUDE.md's rule is that
        # numbers in prose rot, and this section can say "every network above".
        total = len(_declared_networks())
        pattern = rf"\b({'|'.join(COUNT_WORDS)})\s+((?:\w+\s+){{0,2}}?)(?:nets?|networks?)\b"
        for m in re.finditer(pattern, self.SECTION, re.I):
            if m.group(2):
                continue            # qualified — names a subset, not the roster
            with self.subTest(claim=m.group(0)):
                self.assertEqual(
                    COUNT_WORDS[m.group(1).lower()], total,
                    f"DESIGN.md's network roster says {m.group(0)!r} but "
                    f"{total} networks are declared")


class HomingClaimTests(_NeedsGit):
    """A ``**quadruple-homed** (a-net + b-net + ...)`` claim is prose with a
    machine-checkable payload: the parenthesised list is a service's attachment
    set, so both the list and the word counting it can be verified.

    Worth a guard because the number is the part that rots and the part nobody
    re-derives while reading. `triple-homed` survived the proxy gaining a fourth
    leg, in the same paragraph as the network roster that was also stale."""

    def test_every_homing_claim_matches_a_real_service(self):
        found = 0
        legs = _service_legs()
        for doc in _docs():
            text = (ROOT / doc).read_text()
            for m in re.finditer(r"\*\*(\w+)-homed\*\*\s*\(([^)]*)\)", text):
                word, listed = m.group(1).lower(), m.group(2)
                # ``strip()`` and not ``strip(" `")``: these docs are hard-wrapped, so
                # a list of four networks WRAPS, and a member that carried a leading
                # newline used to match no service's attachment set — the guard
                # failing on correct prose, which is how a guard becomes something to
                # appease rather than read.
                nets = {n.strip().strip("`") for n in listed.split("+")}
                line = text[:m.start()].count("\n") + 1
                with self.subTest(doc=doc, line=line, claim=m.group(0)[:60]):
                    self.assertIn(word, COUNT_WORDS,
                                  f"unknown count word {word!r} — add it to "
                                  f"COUNT_WORDS or reword the claim")
                    self.assertEqual(
                        COUNT_WORDS[word], len(nets),
                        f"{doc}:{line} says {word}-homed but lists {len(nets)} "
                        f"networks: {sorted(nets)}")
                    owners = [s for s, legs_of in legs.items() if legs_of == nets]
                    self.assertTrue(
                        owners,
                        f"{doc}:{line} lists {sorted(nets)}, which is no service's "
                        f"actual attachment set. Closest: " + "; ".join(
                            f"{s}={sorted(legs_of)}"
                            for s, legs_of in sorted(legs.items())
                            if legs_of & nets))
                found += 1
        # Fail closed on a vacuous pass, like the globbed guards in `make
        # consistency`: this asserts a documented convention, so if the phrasing
        # changes shape the guard must say so rather than silently pass on nothing.
        self.assertGreater(found, 0,
                           "no '**N-homed** (...)' claim found in any doc — the "
                           "phrasing changed and this guard now checks nothing")


class DocPathReferenceTests(_NeedsGit):
    """A backticked repo path in a doc must exist.

    Restricted to paths that START with a tracked top-level directory, which is
    what keeps this quiet enough to survive: the docs are full of backticked
    tokens that look like paths and are not — CIDRs (`172.16/12`), MIME types
    (`text/event-stream`), MCP methods (`tools/call`), and files inside OTHER
    projects (`mitmproxy/tls.py`, `pkg/http/middleware/token.go`). Bare basenames
    like `addon.py` are also excluded: referring to a file by name is normal in
    prose here, and resolving them by search would guess.
    """

    def test_every_referenced_repo_path_exists(self):
        tracked = set(_tracked())
        topdirs = {t.split("/")[0] for t in tracked if "/" in t}
        checked = 0
        for doc in _docs():
            text = (ROOT / doc).read_text()
            for i, line in enumerate(text.splitlines(), 1):
                for tok in re.findall(r"`([^`\s]+)`", line):
                    path = tok.rstrip(".,:;)").rstrip("/")
                    if path.split("/")[0] not in topdirs or "*" in path:
                        continue
                    checked += 1
                    with self.subTest(doc=doc, line=i, path=path):
                        self.assertTrue(
                            path in tracked or (ROOT / path).exists(),
                            f"{doc}:{i} references `{path}`, which does not exist "
                            f"(renamed or removed without updating the doc)")
        self.assertGreater(checked, 0, "no repo paths found in any doc — the glob "
                                       "or the docs changed shape")


class InstructionFileTests(_NeedsGit):
    """Every tracked ``CLAUDE.md`` or ``AGENTS.md`` is one somebody meant as instructions
    for working in the repo. Claude Code loads the root ``CLAUDE.md`` at launch and a
    subdirectory's when a session reads a file beside it, and ``AGENTS.md`` is the same
    file for other coding agents, scoped to the directory that holds it. So the NAME
    alone makes a file instructions. Both sandboxes' baked user-scope files carried it,
    and told an agent working beside them, on the host too, that it had no `git push`,
    no Docker, or no network.

    Adding one is fine; this makes it a decision rather than an accident."""

    NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")
    INTENDED: ClassVar[set] = {"CLAUDE.md", "control-plane/CLAUDE.md"}

    def test_every_instruction_file_is_meant_as_instructions(self):
        found = {f for f in _tracked() if f.rsplit("/", 1)[-1] in self.NAMES}
        self.assertEqual(found, self.INTENDED,
                         "a file with one of these names is loaded as instructions by "
                         "path; rename one that is not, or list one that is here")


class MarkdownAnchorTests(_NeedsGit):
    """Every in-document link resolves to a heading in that document.

    `DESIGN.md` carries a hand-maintained `## Contents` over ~30 sections, so
    renaming a heading breaks a link nothing else would notice — the document
    still renders, the link just goes nowhere."""

    @staticmethod
    def _slug(heading: str) -> str:
        """GitHub's heading slug, to the extent this repo's headings exercise it:
        drop formatting and punctuation, then EACH remaining space becomes one
        hyphen — which is why an em-dashed heading slugs to a DOUBLE hyphen. The
        naive `\\s+ -> -` collapse reports every such link as broken."""
        s = re.sub(r"[^\w\s-]", "", heading.strip().lower().replace("`", ""))
        return s.replace(" ", "-")

    def test_every_internal_link_resolves(self):
        checked = 0
        for doc in _docs():
            text = (ROOT / doc).read_text()
            headings = {self._slug(m.group(1))
                        for m in re.finditer(r"^#+\s+(.*)$", text, re.M)}
            for i, line in enumerate(text.splitlines(), 1):
                for anchor in re.findall(r"\]\(#([^)]+)\)", line):
                    checked += 1
                    with self.subTest(doc=doc, line=i, anchor=anchor):
                        self.assertIn(anchor, headings,
                                      f"{doc}:{i} links to #{anchor}, which matches "
                                      f"no heading in {doc}")
        self.assertGreater(checked, 0, "no internal links found — DESIGN.md's "
                                       "Contents section changed shape")


class CrossReferenceTests(_NeedsGit):
    """A `see "Some Heading"` reference resolves to something in the docs.

    `DESIGN.md` navigates by prose reference rather than by link — 30-odd sections,
    cross-referenced by name — and a renamed section leaves every reference to it
    pointing at nothing, with no broken link for a reader to notice.

    The one this caught had dropped the quotes: *(not built yet — see Build
    status)*, naming a heading that CLAUDE.md records as deliberately renamed to
    `## Status`, two paragraphs above a sentence explaining the rename. Unquoted
    references cannot be checked without flagging ordinary prose ("see the CSP note
    above"), so the convention is to quote them, and this holds the quoted ones.
    """

    #: Bold and italic lead-ins count as targets, not just headings: the docs
    #: legitimately reference `*Design note — why three control nets*` and
    #: `**HTTPS inspection depth**`, neither of which is a heading.
    TARGET_PATTERNS = (r"^#+\s+(.*)$", r"^\s*[-*]?\s*\*\*(.+?)\*\*",
                       r"^\s*\*([^*]{4,90})\*")

    @staticmethod
    def _norm(s: str) -> str:
        """Collapse whitespace, because a reference that WRAPS ACROSS LINES is the
        common case in a hard-wrapped document — comparing raw text reports every
        one of them as dangling."""
        return re.sub(r"\s+", " ", s.strip().lower().replace("`", "").replace("*", ""))

    def test_every_quoted_cross_reference_resolves(self):
        targets: set[str] = set()
        for doc in _docs():
            text = (ROOT / doc).read_text()
            for pattern in self.TARGET_PATTERNS:
                targets |= {self._norm(m.group(1))
                            for m in re.finditer(pattern, text, re.M)}
        checked = 0
        for doc in _docs():
            text = (ROOT / doc).read_text()
            for m in re.finditer(r'\b(?:see|under|in)\s+"([^"]{4,90})"', text, re.S):
                ref = self._norm(m.group(1))
                checked += 1
                line = text[:m.start()].count("\n") + 1
                with self.subTest(doc=doc, line=line, ref=ref):
                    self.assertTrue(
                        any(ref == t or ref in t for t in targets),
                        f'{doc}:{line} references "{ref}", which is no heading or '
                        f"bold/italic lead-in in any doc — renamed or removed")
        self.assertGreater(checked, 0, "no quoted cross-references found — the "
                                       "convention changed and this checks nothing")


class SectionCitationTests(_NeedsGit):
    """A section cited by FILE, as ``DESIGN.md, "X"`` or ``"X" in NOTES.md``, is in the
    file it names, whether a code comment cites it or another doc does.

    ``CrossReferenceTests`` resolves a bare ``see "X"`` against every doc at once, which
    suits a reference that names no file and is too lax for one that does. The first
    run found `docker-compose.yml` sending readers to DESIGN.md for "Operational
    constraints", a heading that was in NOTES.md. Resolved in the NAMED file, because a
    reference is only as good as the file a reader opens, and as text rather than as a
    heading: a comment may quote a phrase from inside a section. The path is from the
    repo root, so `DESIGN.md` inside `opencode-sandbox/` still means the root one.

    Text, unless the phrase is the name of a section somewhere, and then it must name
    one in the file cited. A section that moves to another file leaves its name behind
    in the pointer that replaces it, so the text still matches. Twice, when a section
    moved to a component's `DESIGN.md`, that hid a citation still naming the root."""

    #: `DESIGN.md, "X"`, `NOTES.md "X"`, `DESIGN.md → "X"` (the name may be backticked),
    #: and `"X" in DESIGN.md`. The separator is required, so a file name inside a string
    #: literal is not a citation.
    NAMED_FIRST = re.compile(
        r'\b([\w./-]*[A-Z][\w./-]*\.md)`?(?:,\s*|:\s*|\s+|\s*→\s*)"([^"]{4,120})"')
    QUOTE_FIRST = re.compile(r'"([^"]{4,120})"\s+in\s+`?([\w./-]*[A-Z][\w./-]*\.md)\b')

    @staticmethod
    def _flat(text: str) -> str:
        """One line, comment markers dropped, so a quote wrapped across lines rejoins."""
        return re.sub(r"[ \t]*\n[ \t]*(?:#:?|//|\*|--)?[ \t]*", " ", text)

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.lower().replace("*", "").replace("`", "")).strip()

    def _citations(self):
        """(citing file, doc it names, phrase it quotes), for every tracked file."""
        for name in _tracked():
            path = ROOT / name
            if not path.is_file():
                continue
            try:
                text = self._flat(path.read_text())
            except UnicodeDecodeError:
                continue
            for m in self.NAMED_FIRST.finditer(text):
                yield name, *m.groups()
            for m in self.QUOTE_FIRST.finditer(text):
                yield name, *m.groups()[::-1]

    def test_every_cited_section_is_in_the_file_it_names(self):
        checked = 0
        for name, doc, ref in self._citations():
            checked += 1
            with self.subTest(file=name, doc=doc, ref=ref):
                target = ROOT / doc
                self.assertTrue(target.is_file(), f"{name} cites {doc}, which "
                                                  f"does not exist")
                self.assertTrue(self._norm(ref) in self._norm(target.read_text()),
                                f'{name} cites {doc} "{ref}", which is not in it')
        self.assertGreater(checked, 0, "no citations found — the syntax changed and "
                                       "this checks nothing")

    def test_a_cited_section_name_is_one_in_the_file_it_names(self):
        names = {doc: {self._norm(n) for n in _lead_ins((ROOT / doc).read_text())}
                 for doc in _docs()}
        checked = 0
        for name, doc, ref in self._citations():
            phrase = self._norm(ref)
            homes = sorted(d for d, ns in names.items() if any(phrase in n for n in ns))
            if not homes:
                continue  # a phrase from inside a section, which the text check covers
            checked += 1
            with self.subTest(file=name, doc=doc, ref=ref):
                self.assertIn(doc, homes,
                              f'{name} cites {doc} "{ref}", which names a section in '
                              f"{', '.join(homes)} but none in {doc} — moved without "
                              f"the citation?")
        self.assertGreater(checked, 0, "no citation names a section — the lead-in "
                                       "patterns changed and this checks nothing")


class CitedArtifactTests(_NeedsGit):
    """Test names and make targets cited in prose exist.

    Both are load-bearing citations rather than decoration: the docs answer "how do
    you know?" by naming the test, and "how do I run it?" by naming the target. A
    rename breaks the answer silently, and these are the two identifier kinds that
    can be resolved with no false positives — a `test_*` name can only live in
    ``tests/``, and a target can only be defined in the Makefile.

    Deliberately NOT extended to every backticked identifier. That reader finds
    ~18 symbols in no tracked source, and all of them are correct: upstream Claude
    Code env vars, mitmproxy and github-mcp-server internals, wire field names
    from the gateway's third-party peers, and identifiers named precisely to say they are
    NOT used (`ANTHROPIC_API_KEY`, `env_file`, a rejected `elapsed_seconds` field).
    Separating those from a real rename needs an allowlist of exceptions, and a
    guard with a growing exception list is one that gets appeased rather than read.
    """

    def test_every_cited_test_name_exists(self):
        tests = "\n".join(
            (ROOT / f).read_text() for f in _tracked()
            if f.startswith("tests/") and f.endswith(".py"))
        checked = 0
        for doc in _docs():
            text = (ROOT / doc).read_text()
            for m in re.finditer(r"`(test_[a-z0-9_]+|[A-Z][A-Za-z0-9]*Tests)`", text):
                name = m.group(1)
                checked += 1
                with self.subTest(doc=doc, name=name):
                    self.assertRegex(
                        tests, rf"\b{re.escape(name)}\b",
                        f"{doc} cites {name}, which no file under tests/ defines")
        self.assertGreater(checked, 0, "no test names cited in any doc")

    def test_every_cited_make_target_exists(self):
        makefile = (ROOT / "Makefile").read_text()
        defined = set(re.findall(r"^([a-z][a-z0-9-]*):", makefile, re.M))
        self.assertIn("check", defined, "the Makefile target reader found nothing "
                                        "recognisable — did the file change shape?")
        checked = 0
        for doc in _docs():
            text = (ROOT / doc).read_text()
            # Backticked only. Bare "make" is an ordinary English verb, and scanning
            # for it reports "make it", "make the" and "make good" as targets.
            for m in re.finditer(r"`make ([a-z][a-z0-9-]*)`", text):
                target = m.group(1)
                checked += 1
                with self.subTest(doc=doc, target=target):
                    self.assertIn(target, defined,
                                  f"{doc} tells the reader to run `make {target}`, "
                                  f"which the Makefile does not define")
        self.assertGreater(checked, 0, "no `make <target>` citations found in docs")


class ShippedComponentTests(_NeedsGit):
    """A component the Status table says is built is not described as unbuilt.

    The failure this guards against already happened: the MCP gateway shipped over a
    dozen PRs, and eleven passages across four documents kept saying "planned", "not
    built yet" and "no agent is pointed at it yet" — SECURITY.md among them, which
    declared the live tool-execution path unreportable. The Status table in DESIGN.md
    is the one place that tracks sequence, so it is the source of truth here; a
    phrase is only stale relative to it.

    Deliberately narrow: the phrases are the ones actually written, not a theory of
    how staleness is spelled. A new component gets its own row here when it ships and
    its docs are swept, not before."""

    #: (Status-row fragment, stale phrases) — the row is matched on the `What`
    #: column, and its state must read `**done**` for the phrases to be forbidden.
    SHIPPED: ClassVar = {
        "MCP gateway — per-tool allow/deny/ask": (
            r"gateway\b[^.\n]{0,40}\((planned|not built)",
            r"until the gateway exists",
            r"before the gateway exists",
            r"no agent (is )?point(s|ed) at it yet",
            r"[Nn]ot yet built[^.]{0,80}\bMCP gateway",
        ),
    }

    def _status_rows(self) -> dict[str, str]:
        design = (ROOT / "DESIGN.md").read_text()
        status = design.split("\n## Status\n", 1)[1].split("\n## ", 1)[0]
        rows = {}
        for line in status.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 3:
                rows[cells[1]] = cells[2]
        self.assertTrue(rows, "no Status rows parsed — did the table change shape?")
        return rows

    def test_a_done_row_is_not_contradicted_by_the_prose(self):
        rows = self._status_rows()
        # Anything a reader or the agent is handed: the docs, the compose files, and
        # the comments in the code that describe the topology.
        files = _docs() + [f for f in _tracked()
                           if f.endswith((".yml", ".py")) and not f.startswith("tests/")]
        for fragment, phrases in self.SHIPPED.items():
            state = next((s for what, s in rows.items() if fragment in what), None)
            self.assertIsNotNone(state, f"no Status row contains {fragment!r}")
            if "**done**" not in state:
                continue
            for f in files:
                text = (ROOT / f).read_text()
                for phrase in phrases:
                    with self.subTest(file=f, phrase=phrase):
                        self.assertIsNone(
                            re.search(phrase, text),
                            f"{f} still describes a shipped component as unbuilt "
                            f"(Status says {state!r}): /{phrase}/")


if __name__ == "__main__":
    unittest.main()
