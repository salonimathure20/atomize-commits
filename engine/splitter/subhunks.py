"""Split a hunk that contains more than one new definition.

THE PROBLEM
-----------
Git decides hunk boundaries by proximity, not meaning. If you add a login
handler and a checkout handler next to each other, git hands you ONE hunk
containing both. No amount of clever grouping downstream can separate them,
because our smallest movable unit is already too big.

    @@ -9,3 +12,13 @@
     def index():
         return {"service": "demo"}
    +
    +def login_route(request):     <- feature A
    +    ...
    +
    +def checkout_route(request):  <- feature B
    +    ...

THE FIX
-------
When a run of purely-added lines contains two or more top-level definitions,
we cut it at those boundaries and emit one sub-hunk per definition. Each piece
becomes a zero-context insertion anchored at the same HEAD line, which git
applies happily (we already pass --unidiff-zero).

We only touch hunks that are purely additions. Mixed add/remove regions are
left alone, because reasoning about them is where correctness bugs live.

A hunk can hold several separate blocks of added lines with untouched code
between them — git merges nearby edits into one hunk. Each block is handled
independently, which matters: in a short file, an import block and a function
added forty lines apart can still arrive as a single hunk.
"""

from __future__ import annotations

import re

from .models import FileDiff, Hunk
from .symbols import DEFINITION_PATTERNS

MIN_RUN = 4          # do not bother splitting tiny runs of added code

# Import statements across the languages we care about.
IMPORT_LINE = re.compile(
    r"^\s*(?:import\s|from\s+\S+\s+import\s|#include\s|use\s+\S+;"
    r"|(?:const|let|var)\s+\{?[\w\s,}]+\}?\s*=\s*require\()"
)


def _definition_indent(line: str) -> int | None:
    """Indent of this line if it starts a definition, else None."""
    for pattern in DEFINITION_PATTERNS:
        m = pattern.match(line)
        if m:
            return len(m.group(1).expandtabs(4))
    return None


def _cut_points(added: list[str]) -> list[int]:
    """Indices in `added` where a new top-level definition begins.

    We take the shallowest indent found — that is the file's top level for
    this run — and cut only there, so a class keeps its methods together.
    """
    defs = [(i, _definition_indent(ln)) for i, ln in enumerate(added)]
    defs = [(i, ind) for i, ind in defs if ind is not None]
    if len(defs) < 2:
        return []

    top = min(ind for _, ind in defs)
    starts = [i for i, ind in defs if ind == top]
    if len(starts) < 2:
        return []

    # Pull any blank lines immediately above a definition into its own piece,
    # so each commit carries its own spacing.
    adjusted = []
    for start in starts:
        i = start
        while i > 0 and not added[i - 1].strip():
            i -= 1
        adjusted.append(i)

    adjusted[0] = 0
    return sorted(set(adjusted))


def _import_cut_points(added: list[str]) -> list[int]:
    """Cut an import block into one piece per import statement.

    Imports are the worst case for proximity-based hunking: two imports for
    two completely unrelated features sit on adjacent lines, so git hands
    them over as one inseparable unit. Once they are split, the ordinary
    scoring attaches each import to whichever feature actually uses the name
    it brings in.
    """
    starts = [i for i, ln in enumerate(added) if IMPORT_LINE.match(ln)]
    if len(starts) < 2:
        return []
    starts[0] = 0
    return sorted(set(starts))


def split_hunk(hunk: Hunk) -> list[Hunk]:
    """Return one or more hunks. Returns [hunk] when no split applies."""
    body = hunk.lines
    if any(ln.startswith("\\") for ln in body):
        return [hunk]                      # "no newline at eof" — leave alone

    # Walk the body, tracking line numbers on both sides, and find maximal
    # runs of consecutive '+' lines.
    old_line, new_line = hunk.old_start, hunk.new_start
    runs: list[tuple[int, int, int, int]] = []   # body_start, body_end, old_at, new_at
    run_start: int | None = None
    run_old = run_new = 0

    for i, line in enumerate(body):
        if line.startswith("+"):
            if run_start is None:
                run_start, run_old, run_new = i, old_line, new_line
            new_line += 1
        else:
            if run_start is not None:
                runs.append((run_start, i, run_old, run_new))
                run_start = None
            if line.startswith("-"):
                old_line += 1
            else:
                old_line += 1
                new_line += 1

    if run_start is not None:
        runs.append((run_start, len(body), run_old, run_new))

    # Deletions make position tracking subtle, so we leave those hunks whole.
    # Pure insertions are safe: every piece anchors at a known HEAD line.
    if any(ln.startswith("-") for ln in body) or not runs:
        return [hunk]

    pieces: list[Hunk] = []
    for start, end, old_at, new_at in runs:
        added = [ln[1:] for ln in body[start:end]]

        # Try definition boundaries, then import boundaries. Definitions need
        # a run of some size; imports do not, because two adjacent import
        # lines are already the problem.
        cuts = _cut_points(added) if len(added) >= MIN_RUN else []
        if len(cuts) < 2:
            cuts = _import_cut_points(added)
        if len(cuts) < 2:
            cuts = [0]                      # keep this run intact

        bounds = cuts + [len(added)]
        for n in range(len(cuts)):
            lo, hi = bounds[n], bounds[n + 1]
            segment = ["+" + ln for ln in added[lo:hi]]
            if not any(ln[1:].strip() for ln in segment):
                continue                    # blank-only piece
            pieces.append(
                Hunk(
                    path=hunk.path,
                    old_start=old_at,
                    old_count=0,           # pure insertion at this anchor
                    new_start=new_at + lo,
                    new_count=len(segment),
                    lines=segment,
                    order=hunk.order,
                )
            )

    return pieces if len(pieces) >= 2 else [hunk]


def split_file(fd: FileDiff) -> FileDiff:
    """Apply split_hunk across a file, renumbering the hunk order."""
    if fd.is_atomic:
        return fd
    fresh: list[Hunk] = []
    for hunk in fd.hunks:
        for piece in split_hunk(hunk):
            piece.order = len(fresh)
            fresh.append(piece)
    fd.hunks = fresh
    return fd


def split_all(files: list[FileDiff]) -> list[FileDiff]:
    return [split_file(fd) for fd in files]
