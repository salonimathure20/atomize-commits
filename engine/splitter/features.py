"""Give every hunk a profile card.

Before we can ask "are these two hunks related?", each hunk needs a small
description of itself: where it lives, what function it is inside, which words
it uses, and which names it introduces. That is all a HunkFeatures is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from typing import Callable

from .models import FileDiff, Hunk
from .symbols import defined_names, enclosing_symbols, identifiers, read_file

# A Source answers "give me the current lines of this file". Everything that
# needs file contents goes through one, so the analysis never has to know
# whether it is running next to a checkout or inside a stateless web service.
Source = Callable[[str], list[str]]


def disk_source(repo_root: str) -> Source:
    """Read files from a working tree. Used by the CLI."""
    return lambda path: read_file(repo_root, path)


def mapping_source(contents: dict[str, str]) -> Source:
    """Read files from a dict sent over the wire. Used by the service.

    Missing entries return an empty list rather than raising: symbol detection
    degrades to "unknown scope" instead of failing the whole request.
    """
    return lambda path: contents.get(path, "").split("\n")


def empty_source() -> Source:
    return lambda path: []

TEST_MARKERS = ("test_", "_test", ".test.", ".spec.", "/tests/", "/test/", "spec_")


def _first_defined(added: list[str]) -> str | None:
    """Name of the first definition in these added lines, in file order."""
    for line in added:
        names = defined_names([line])
        if names:
            return sorted(names)[0]
    return None


@dataclass
class HunkFeatures:
    hunk: Hunk
    directory: str
    symbols: list[str] = field(default_factory=list)   # outermost-first
    defines: set[str] = field(default_factory=set)
    words: set[str] = field(default_factory=set)
    is_test: bool = False

    @property
    def hunk_id(self) -> str:
        return self.hunk.hunk_id

    @property
    def path(self) -> str:
        return self.hunk.path

    @property
    def symbol(self) -> str | None:
        """The innermost enclosing definition, which is usually what we want."""
        return self.symbols[-1] if self.symbols else None

    @property
    def base_name(self) -> str:
        """Filename with test decorations stripped: test_auth.py -> auth."""
        stem = PurePosixPath(self.path).stem
        stem = re.sub(r"^(test_|spec_)", "", stem)
        stem = re.sub(r"(_test|\.test|\.spec|_spec)$", "", stem)
        return stem.lower()

    def label(self) -> str:
        where = f"{self.path}"
        if self.symbol:
            where += f" :: {self.symbol}"
        return where


def build_features(source: Source, files: list[FileDiff]) -> list[HunkFeatures]:
    """Turn parsed hunks into profile cards.

    `source` supplies file contents. Pass disk_source(root) locally or
    mapping_source(dict) when the contents arrived over HTTP.
    """
    profiles: list[HunkFeatures] = []

    for fd in files:
        if fd.is_atomic:
            continue
        lines = source(fd.path)
        lowered_path = fd.path.lower()

        for hunk in fd.hunks:
            added = hunk.added_lines
            removed = hunk.removed_lines
            introduced = defined_names(added)

            # If this hunk introduces a definition, that definition IS its
            # scope. Scanning upward would land us in the previous function,
            # because an inserted block starts with the blank lines above it.
            scope = enclosing_symbols(lines, hunk.first_changed_line)
            if introduced:
                first = _first_defined(added)
                if first and first not in scope:
                    scope = scope[:-1] + [first] if scope else [first]

            profiles.append(
                HunkFeatures(
                    hunk=hunk,
                    directory=str(PurePosixPath(fd.path).parent),
                    symbols=scope,
                    defines=introduced,
                    words=identifiers(added + removed),
                    is_test=any(marker in lowered_path for marker in TEST_MARKERS),
                )
            )

    return profiles
