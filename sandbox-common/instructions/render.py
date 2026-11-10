# SPDX-License-Identifier: Apache-2.0
"""Render each sandbox tier's agent instructions from one shared source.

    python3 sandbox-common/instructions/render.py      (make instructions)

Each tier keeps a source beside its Dockerfile, ``<tier>/instructions.md``: a front
matter naming the file it renders to (``output``, repo-relative) and its vocabulary,
then its own text, with ``{{include <group>/<name>}}`` lines where a shared fragment
from this directory goes. ``every-tier/`` holds what is true in every tier: the
shared ``.bashrc``'s ``rm`` alias, the shared entrypoint's drop to a non-root user,
and ``/workspace``, which both launchers mount the same way. ``cloud-tier/`` holds
what every governed cloud tier shares: a tier whose launcher does not pass
``SANDBOX_MODE=local``. A fragment names no CLI's tool, file or variable: it writes ``{{key}}``, and
each tier's front matter gives the key its value, which may be empty.

The rendered files are committed, as the Dockerfiles' COPY sources, and
tests/test_instructions.py holds them equal to a fresh render. This directory is
repo-side only: no image ships it, and ``.dockerignore`` keeps it out of the build
context, so it is not part of what "sandbox-common/ is byte-identical in both tiers"
counts.

Strict, so a mistake fails here and not in an agent's context: an unknown include, a
placeholder with no key, a key nothing uses, a marker left over, or two tiers naming
one output is an error naming the file.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
FRAGMENTS = PurePosixPath("sandbox-common/instructions")
SOURCES = "*-sandbox/instructions.md"
FENCE = "---\n"
OUTPUT = "output"
# A whole line, so one written mid-sentence is left over and refused, not spliced in.
INCLUDE = re.compile(r"^\{\{include ([a-z0-9-]+/[a-z0-9-]+)\}\}$\n?", re.MULTILINE)
PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
KEY = re.compile(r"[a-z_]+")


class RenderError(Exception):
    """A source, or a fragment it includes, that cannot render as written."""


@dataclass(frozen=True)
class Source:
    path: str  # repo-relative, for messages
    output: str  # repo-relative, normalised
    vocabulary: dict[str, str]
    body: str

    @property
    def includes(self) -> list[str]:
        """The fragments the source names, in order: ``every-tier/rm`` and the like."""
        return INCLUDE.findall(self.body)


def sources(root: Path = ROOT) -> list[Path]:
    return sorted(root.glob(SOURCES))


def parse(path: Path, root: Path = ROOT) -> Source:
    name = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8")
    head, fence, body = text.removeprefix(FENCE).partition(f"\n{FENCE}")
    if not text.startswith(FENCE) or not fence:
        raise RenderError(f"{name}: no front matter between two '---' lines")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        if not colon or not KEY.fullmatch(key):
            raise RenderError(f"{name}: front matter line {line!r} is not 'key: value'")
        if key in fields:
            raise RenderError(f"{name}: key {key!r} is given twice")
        fields[key] = value.removeprefix(" ")
    output = fields.pop(OUTPUT, "")
    out = PurePosixPath(output)
    if not output or out.is_absolute() or ".." in out.parts:
        raise RenderError(f"{name}: 'output' must be a path inside the repo, "
                          f"relative to its root, not {output!r}")
    return Source(path=name, output=out.as_posix(), vocabulary=fields, body=body)


def render(source: Source, root: Path = ROOT) -> str:
    """The source's text with its includes resolved and its placeholders filled."""
    pieces: list[tuple[str, str]] = []  # (where it came from, its text)
    at = 0
    for match in INCLUDE.finditer(source.body):
        pieces.append((source.path, source.body[at:match.start()]))
        fragment = FRAGMENTS / f"{match.group(1)}.md"
        if not (root / fragment).is_file():
            raise RenderError(f"{source.path}: includes {match.group(1)!r}, "
                              f"but there is no {fragment}")
        text = (root / fragment).read_text(encoding="utf-8")
        pieces.append((fragment.as_posix(), text if text.endswith("\n") else text + "\n"))
        at = match.end()
    pieces.append((source.path, source.body[at:]))

    used: set[str] = set()

    def fill(where: str, match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in source.vocabulary:
            raise RenderError(f"{where}: placeholder {match.group(0)} has no key in "
                              f"{source.path}'s front matter")
        used.add(key)
        return source.vocabulary[key]

    rendered = []
    for where, text in pieces:
        bare = PLACEHOLDER.sub("", text)
        if "{{" in bare or "}}" in bare:
            raise RenderError(f"{where}: a '{{{{' or '}}}}' that is neither a placeholder "
                              f"nor an include line, rendering {source.path}")
        rendered.append(PLACEHOLDER.sub(lambda m, w=where: fill(w, m), text))
    unused = sorted(set(source.vocabulary) - used)
    if unused:
        raise RenderError(f"{source.path}: keys nothing it renders uses: {unused}")
    return "".join(rendered)


def render_all(root: Path = ROOT) -> dict[str, str]:
    """Each tier's output path, repo-relative, and its rendered text."""
    outputs: dict[str, str] = {}
    named_by: dict[str, str] = {}
    for path in sources(root):
        source = parse(path, root)
        if source.output in named_by:
            raise RenderError(f"{source.path}: renders to {source.output}, which "
                              f"{named_by[source.output]} renders to already")
        named_by[source.output] = source.path
        outputs[source.output] = render(source, root)
    return outputs


def main() -> int:
    try:
        outputs = render_all()
    except RenderError as err:
        print(f"render.py: {err}", file=sys.stderr)
        return 1
    for name, text in outputs.items():
        path = ROOT / name
        data = text.encode("utf-8")
        if path.is_file() and path.read_bytes() == data:
            print(f"  unchanged {name}")
            continue
        path.write_bytes(data)
        print(f"  wrote {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
