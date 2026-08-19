"""Data models: the atoms our system reasons about.

A Hunk is one contiguous block of changed lines inside one file. It is the
smallest unit we are willing to move between commits. A FileDiff is all the
hunks for a single file, plus the diff header git needs in order to apply them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class Hunk:
    """One @@ block from a unified diff."""

    path: str          # path as it appears on the "+++ b/" side
    old_start: int     # first line number on the HEAD side
    old_count: int     # how many HEAD-side lines this hunk covers
    new_start: int     # first line number on the working-tree side
    new_count: int     # how many working-tree lines this hunk produces
    lines: list[str]   # raw diff body lines, each keeping its ' ', '+', '-' prefix
    order: int         # position of this hunk within its file, 0-based

    @property
    def hunk_id(self) -> str:
        """Stable, human-typable id.

        We hash the content rather than using a counter so that an id stays
        valid even if an earlier hunk in the same file gets committed first.
        """
        digest = hashlib.sha1("\n".join(self.lines).encode()).hexdigest()[:6]
        return f"{self.path}#{digest}"

    @property
    def added_lines(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln.startswith("+")]

    @property
    def removed_lines(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln.startswith("-")]

    @property
    def stat(self) -> tuple[int, int]:
        """(additions, deletions) — handy for summaries."""
        return len(self.added_lines), len(self.removed_lines)

    @property
    def first_changed_line(self) -> int:
        """New-side line number of the first real change.

        A hunk starts with context lines, so `new_start` can point at code
        that belongs to the *previous* function. Asking "which function am I
        in?" at new_start therefore gives the wrong answer. Use this instead.
        """
        line = self.new_start
        for raw in self.lines:
            if raw.startswith(("+", "-")):
                return line
            line += 1
        return self.new_start

    def header(self, new_start: int | None = None) -> str:
        """Rebuild the @@ line, optionally overriding the new-side start.

        The override exists because when we apply only *some* hunks, the
        working-tree line numbers shift. See patch.py.
        """
        start = self.new_start if new_start is None else new_start
        old = f"{self.old_start}" if self.old_count == 1 else f"{self.old_start},{self.old_count}"
        new = f"{start}" if self.new_count == 1 else f"{start},{self.new_count}"
        return f"@@ -{old} +{new} @@"

    def preview(self, max_lines: int = 6) -> str:
        body = [ln for ln in self.lines if ln[:1] in "+-"]
        shown = body[:max_lines]
        more = len(body) - len(shown)
        text = "\n".join("    " + ln for ln in shown)
        if more > 0:
            text += f"\n    ... {more} more changed line(s)"
        return text


@dataclass
class FileDiff:
    """All hunks belonging to one file, plus the header git needs."""

    path: str
    header_lines: list[str]           # "diff --git ...", "index ...", "--- ", "+++ "
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False
    is_rename: bool = False
    raw: str = ""                     # full original text, used for atomic files

    @property
    def is_atomic(self) -> bool:
        """True when the file cannot be split hunk-by-hunk.

        Binary blobs have no line structure, and renames carry metadata that
        must travel as one unit. These files go into a commit whole or not
        at all.
        """
        return self.is_binary or self.is_rename
