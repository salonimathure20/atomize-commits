"""Rebuild a patch containing only the hunks we selected.

THE KEY IDEA
------------
We apply patches to the git *index*, not the working tree. Right before we
apply, the index is identical to HEAD. That gives us a useful guarantee:

  * The "-" side line numbers in every hunk are measured against HEAD, so
    they are correct no matter which hunks we skip.
  * The "+" side line numbers are measured against the fully-modified working
    tree, so they are WRONG as soon as we skip a hunk.

So we recompute the "+" side. We walk a file's hunks in original order and
keep a running offset that only advances for hunks we actually include:

    new_start = old_start + offset
    offset   += (new_count - old_count)   # only for included hunks

Get this wrong and git will either refuse the patch or, worse, apply it in the
wrong place. This function is the single most important piece of Phase 1.
"""

from __future__ import annotations

from .models import FileDiff, Hunk


def build_patch(files: list[FileDiff], selected_ids: set[str]) -> str:
    """Produce patch text applying only `selected_ids`, in file order."""
    chunks: list[str] = []

    for fd in files:
        if fd.is_atomic:
            # Binary and rename files travel whole. They are selected if any
            # of their hunks is, or if the file itself was named.
            if fd.path in selected_ids or any(h.hunk_id in selected_ids for h in fd.hunks):
                chunks.append(fd.raw.rstrip("\n"))
            continue

        chosen = [h for h in fd.hunks if h.hunk_id in selected_ids]
        if not chosen:
            continue

        body = ["\n".join(fd.header_lines)]
        offset = 0
        for hunk in fd.hunks:                    # original order matters
            if hunk.hunk_id not in selected_ids:
                continue
            body.append(hunk.header(new_start=hunk.old_start + offset))
            body.extend(hunk.lines)
            offset += hunk.new_count - hunk.old_count

        chunks.append("\n".join(body))

    if not chunks:
        return ""
    return "\n".join(chunks) + "\n"


def selection_stat(files: list[FileDiff], selected_ids: set[str]) -> tuple[int, int, int]:
    """(files touched, lines added, lines removed) for a selection."""
    touched, adds, dels = set(), 0, 0
    for fd in files:
        for hunk in fd.hunks:
            if hunk.hunk_id in selected_ids:
                touched.add(fd.path)
                a, d = hunk.stat
                adds += a
                dels += d
    return len(touched), adds, dels


def resolve_ids(files: list[FileDiff], tokens: list[str]) -> set[str]:
    """Let users type short prefixes instead of full ids.

    Accepts a full id (`src/auth.py#a1b2c3`), a bare hash (`a1b2c3`), or a
    file path meaning "every hunk in this file".
    """
    lookup: dict[str, list[Hunk]] = {}
    for fd in files:
        for hunk in fd.hunks:
            lookup.setdefault(hunk.hunk_id, []).append(hunk)
            lookup.setdefault(hunk.hunk_id.split("#")[1], []).append(hunk)
            lookup.setdefault(fd.path, []).append(hunk)

    resolved: set[str] = set()
    for token in tokens:
        matches = lookup.get(token)
        if not matches:
            raise KeyError(f"no hunk matches {token!r}")
        resolved.update(h.hunk_id for h in matches)
    return resolved


def content_key(hunk) -> frozenset[str]:
    """Identify a hunk by the text it changes, not by position.

    Hunk ids are hashes of the diff body, which includes context lines. Once
    you commit one group, the remaining diff is regenerated and context shifts,
    so ids change. Content keys survive that, which is what lets us execute a
    multi-commit plan in sequence.
    """
    lines = [ln[1:].strip() for ln in hunk.lines if ln[:1] in "+-"]
    return frozenset(ln for ln in lines if ln)


def match_by_content(files, wanted: frozenset[str]) -> set[str]:
    """Find current hunk ids whose changed text belongs to `wanted`."""
    out: set[str] = set()
    for fd in files:
        for hunk in fd.hunks:
            key = content_key(hunk)
            if key and key <= wanted:
                out.add(hunk.hunk_id)
    return out
