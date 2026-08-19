"""Turn `git diff` text into structured FileDiff / Hunk objects.

This is deliberately hand-written rather than pulled from a library. Diff
parsing is the load-bearing part of the whole project, and you want to
understand every branch of it when something applies cleanly in testing but
not on a real repo.
"""

from __future__ import annotations

import re

from .models import FileDiff, Hunk

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$")


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a full `git diff` into one FileDiff per changed file."""
    if not text.strip():
        return []

    lines = text.split("\n")
    files: list[FileDiff] = []
    current: FileDiff | None = None
    raw_start = 0
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git "):
            if current is not None:
                current.raw = "\n".join(lines[raw_start:i])
                files.append(current)
            raw_start = i
            current = _start_file(line)
            current.header_lines.append(line)
            i += 1
            continue

        if current is None:
            i += 1
            continue

        if HUNK_HEADER.match(line):
            hunk, i = _read_hunk(lines, i, current)
            current.hunks.append(hunk)
            continue

        # Anything before the first @@ is header material.
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.is_binary = True
        if line.startswith("rename from") or line.startswith("rename to"):
            current.is_rename = True
        if line.startswith("+++ b/"):
            current.path = line[6:]
        elif line.startswith("+++ ") and line[4:] == "/dev/null":
            pass  # deletion; keep the a/ path already stored

        if not current.hunks:
            current.header_lines.append(line)
        i += 1

    if current is not None:
        current.raw = "\n".join(lines[raw_start:])
        files.append(current)

    return files


def _start_file(diff_git_line: str) -> FileDiff:
    match = DIFF_GIT.match(diff_git_line)
    # Paths containing spaces are ambiguous in this line; the +++ line below
    # corrects us in the common case.
    path = match.group(2) if match else diff_git_line
    return FileDiff(path=path, header_lines=[])


def _read_hunk(lines: list[str], i: int, fd: FileDiff) -> tuple[Hunk, int]:
    m = HUNK_HEADER.match(lines[i])
    old_start = int(m.group(1))
    old_count = int(m.group(2)) if m.group(2) is not None else 1
    new_start = int(m.group(3))
    new_count = int(m.group(4)) if m.group(4) is not None else 1

    i += 1
    body: list[str] = []
    while i < len(lines):
        ln = lines[i]
        if ln == "" and i == len(lines) - 1:
            break  # trailing newline artifact
        if ln[:1] in (" ", "+", "-", "\\"):
            body.append(ln)
            i += 1
            continue
        if ln == "":
            # git emits a bare empty line for an empty context line in some
            # configurations; treat it as context.
            body.append(" ")
            i += 1
            continue
        break

    hunk = Hunk(
        path=fd.path,
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        lines=body,
        order=len(fd.hunks),
    )
    return hunk, i


def all_hunks(files: list[FileDiff]) -> list[Hunk]:
    return [h for fd in files for h in fd.hunks]
