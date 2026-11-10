# SPDX-License-Identifier: Apache-2.0
"""Each tier's agent instructions are rendered from one source, and committed.

``sandbox-common/instructions/render.py`` (``make instructions``) renders every
``*-sandbox/instructions.md`` into the file its image COPYs. Those outputs are tracked,
so two copies of the text exist and one can be edited alone: the drift tests fail
then. Tier 1's output carries no "generated from" header, because a byte-identical
render was the proof that moving its text into a source changed nothing; the failure
message is what points an editor at the source instead.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from _loader import ROOT, load_instructions_renderer

render = load_instructions_renderer()

FRAGMENTS = ROOT / render.FRAGMENTS
EVERY_TIER, CLOUD_TIER = "every-tier", "cloud-tier"
#: The mode the firewall and the boundary check key on: a launcher passing it starts a
#: tier with no egress, no gateway and no registry.
LOCAL = "SANDBOX_MODE=local"


def _fragments(group: str) -> set[str]:
    return {f"{group}/{p.stem}" for p in (FRAGMENTS / group).glob("*.md")}


class DriftTests(unittest.TestCase):

    def test_each_output_is_a_fresh_render_of_its_source(self):
        self.assertEqual(set(render.render_all()),
                         {"claude-sandbox/user-CLAUDE.md", "opencode-sandbox/user-AGENTS.md"})
        for path in render.sources():
            source = render.parse(path)
            with self.subTest(source=source.path):
                self.assertEqual(
                    (ROOT / source.output).read_bytes(), render.render(source).encode(),
                    f"{source.output} is rendered: edit {source.path}, or the "
                    f"{render.FRAGMENTS}/ fragment it includes, then run make instructions")

    def test_each_output_is_what_its_image_installs(self):
        for path in render.sources():
            source = render.parse(path)
            with self.subTest(source=source.path):
                dockerfile = (path.parent / "Dockerfile").read_text()
                self.assertRegex(dockerfile, rf"(?m)^COPY {re.escape(source.output)} ")


class CoverageTests(unittest.TestCase):
    """Every tier takes the shared facts from the fragments rather than restating them,
    and every fragment is somebody's. A new tier's launcher is held here from the
    moment it exists."""

    def setUp(self):
        self.launchers = sorted(ROOT.glob("run-*-sandbox.sh"))
        self.assertTrue(self.launchers)
        self.assertTrue(_fragments(EVERY_TIER))
        self.assertTrue(_fragments(CLOUD_TIER))

    def _includes(self, tier: str) -> set[str]:
        return set(render.parse(ROOT / tier / "instructions.md").includes)

    def test_every_tier_has_a_source(self):
        for launcher in self.launchers:
            tier = launcher.name.removeprefix("run-").removesuffix(".sh")
            with self.subTest(tier=tier):
                self.assertTrue((ROOT / tier / "instructions.md").is_file(),
                                f"{launcher.name} starts a tier with no {tier}/instructions.md")

    def test_each_tier_includes_the_fragments_its_mode_shares(self):
        for launcher in self.launchers:
            tier = launcher.name.removeprefix("run-").removesuffix(".sh")
            local = LOCAL in launcher.read_text()
            includes = self._includes(tier)
            with self.subTest(tier=tier, local=local):
                self.assertEqual(_fragments(EVERY_TIER) - includes, set())
                if local:
                    self.assertEqual(_fragments(CLOUD_TIER) & includes, set())
                else:
                    self.assertEqual(_fragments(CLOUD_TIER) - includes, set())
        # Both kinds exist today, so neither branch above is vacuous.
        self.assertEqual({LOCAL in lnch.read_text() for lnch in self.launchers},
                         {True, False})

    def test_every_fragment_is_included_by_some_tier(self):
        included = set().union(*(set(render.parse(p).includes) for p in render.sources()))
        self.assertEqual(_fragments(EVERY_TIER) | _fragments(CLOUD_TIER), included)


class VocabularyTests(unittest.TestCase):
    """No fragment hard-codes a name tier 1 fills from its front matter. A deny-list of
    the names tier 1 fills today, not of every Claude Code name: a new one is added
    here when tier 1 gains a key for it."""

    DENIED = ("mcp__", "WebFetch", "Bash", "CLAUDE_CODE_", "CLAUDE.md", "subagent")

    def test_each_denied_name_is_one_tier_one_fills(self):
        values = render.parse(ROOT / "claude-sandbox" / "instructions.md").vocabulary.values()
        for name in self.DENIED:
            with self.subTest(name=name):
                self.assertTrue(any(name in value for value in values))

    def test_no_fragment_names_one(self):
        fragments = sorted(FRAGMENTS.glob("*/*.md"))
        self.assertTrue(fragments)
        for path in fragments:
            text = path.read_text()
            for name in self.DENIED:
                with self.subTest(fragment=path.name, name=name):
                    self.assertNotIn(name, text,
                                     f"{path.relative_to(ROOT)} names {name}: make it a "
                                     "placeholder each tier's source fills")


class StrictnessTests(unittest.TestCase):
    """The renderer refuses what would otherwise reach an agent half-rendered."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fragment("every-tier/fact", "- **A fact**{{clause}}.\n")

    def fragment(self, name: str, text: str) -> None:
        path = self.root / render.FRAGMENTS / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def source(self, tier: str, text: str) -> None:
        (self.root / tier).mkdir(exist_ok=True)
        (self.root / tier / "instructions.md").write_text(text)

    def refused(self, pattern: str) -> None:
        with self.assertRaisesRegex(render.RenderError, pattern):
            render.render_all(self.root)

    def test_it_renders_an_empty_value(self):
        self.source("a-sandbox", "---\noutput: a/out.md\nclause:\n---\n# A\n\n"
                                 "{{include every-tier/fact}}\n")
        self.assertEqual(render.render_all(self.root), {"a/out.md": "# A\n\n- **A fact**.\n"})

    def test_an_unknown_include(self):
        self.source("a-sandbox", "---\noutput: out.md\n---\n{{include every-tier/nope}}\n")
        self.refused(r"^a-sandbox/instructions\.md: includes 'every-tier/nope'")

    def test_a_placeholder_with_no_key(self):
        self.source("a-sandbox", "---\noutput: out.md\n---\n{{include every-tier/fact}}\n")
        self.refused(r"^sandbox-common/instructions/every-tier/fact\.md: placeholder "
                     r"\{\{clause\}\} has no key")

    def test_a_key_nothing_uses(self):
        self.source("a-sandbox", "---\noutput: out.md\nclause: x\nspare: y\n---\n"
                                 "{{include every-tier/fact}}\n")
        self.refused(r"^a-sandbox/instructions\.md: keys nothing it renders uses: \['spare'\]")

    def test_a_leftover_marker(self):
        for body in ("{{ clause }}\n", "text {{include every-tier/fact}}\n", "a }} b\n"):
            with self.subTest(body=body):
                self.source("a-sandbox", f"---\noutput: out.md\nclause: x\n---\n{body}")
                self.refused(r"^a-sandbox/instructions\.md: a '\{\{' or '\}\}'")

    def test_two_tiers_naming_one_output(self):
        for tier, output in (("a-sandbox", "out.md"), ("b-sandbox", "./out.md")):
            self.source(tier, f"---\noutput: {output}\nclause:\n---\n"
                              "{{include every-tier/fact}}\n")
        self.refused(r"^b-sandbox/instructions\.md: renders to out\.md, which "
                     r"a-sandbox/instructions\.md renders to already")

    def test_a_fragment_with_no_final_newline_still_ends_its_line(self):
        self.fragment("every-tier/bare", "- **Bare**")
        self.source("a-sandbox", "---\noutput: out.md\n---\n{{include every-tier/bare}}\n"
                                 "- **Next**\n")
        self.assertEqual(render.render_all(self.root), {"out.md": "- **Bare**\n- **Next**\n"})

    def test_front_matter_that_is_not_key_value_lines(self):
        cases = {"no front matter": ("# A\n", r"no front matter"),
                 "a fence not at the top": ("x: y\n---\n# A\n", r"no front matter"),
                 "no closing fence": ("---\noutput: out.md\n# A\n", r"no front matter"),
                 "a key given twice": ("---\noutput: out.md\noutput: b.md\n---\n# A\n",
                                       r"key 'output' is given twice"),
                 "a line with no colon": ("---\noutput: out.md\nstray\n---\n# A\n",
                                          r"front matter line 'stray' is not 'key: value'")}
        for case, (text, message) in cases.items():
            with self.subTest(case=case):
                self.source("a-sandbox", text)
                self.refused(rf"^a-sandbox/instructions\.md: {message}")

    def test_an_output_outside_the_repo(self):
        for output in ("", "/etc/out.md", "../out.md"):
            with self.subTest(output=output):
                self.source("a-sandbox", f"---\noutput: {output}\n---\n# A\n")
                self.refused(r"^a-sandbox/instructions\.md: 'output' must be a path")


if __name__ == "__main__":
    unittest.main()
