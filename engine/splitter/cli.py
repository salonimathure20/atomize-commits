"""Command line interface for Phase 1.

    python -m splitter list              show every hunk with its id
    python -m splitter verify            prove we can round-trip the diff
    python -m splitter commit -m "msg" -H a1b2c3 -H d4e5f6

Phase 1 has no intelligence at all. You pick the hunks. That is on purpose:
the grouping engine in Phase 2 is only worth building once this layer is
provably correct.
"""

from __future__ import annotations

import argparse
import sys

from .gitrepo import GitError, Repo
from .parser import all_hunks, parse_diff
from .patch import build_patch, resolve_ids, selection_stat


def load(repo: Repo, include_untracked: bool = True):
    if include_untracked:
        repo.intent_to_add(repo.untracked())
    return parse_diff(repo.working_diff())


def cmd_list(args) -> int:
    repo = Repo(args.repo)
    files = load(repo)
    hunks = all_hunks(files)
    if not hunks and not files:
        print("No changes in the working tree.")
        return 0

    for fd in files:
        if fd.is_atomic:
            kind = "binary" if fd.is_binary else "rename"
            print(f"\n{fd.path}  [{kind} — cannot be split]")
            continue
        print(f"\n{fd.path}")
        for hunk in fd.hunks:
            adds, dels = hunk.stat
            print(f"  {hunk.hunk_id}  +{adds} -{dels}  @ line {hunk.new_start}")
            if args.verbose:
                print(hunk.preview())

    print(f"\n{len(hunks)} hunk(s) across {len(files)} file(s).")
    return 0


def cmd_verify(args) -> int:
    """Round-trip check: rebuild a patch of ALL hunks and ask git to test it.

    If this fails, the parser lost information and nothing downstream can be
    trusted. Run it against big messy repos before moving on.
    """
    repo = Repo(args.repo)
    if not repo.index_is_clean():
        print("Index is not clean. Stage nothing before verifying.", file=sys.stderr)
        return 2

    files = load(repo)
    everything = {h.hunk_id for h in all_hunks(files)}
    everything |= {fd.path for fd in files if fd.is_atomic}
    patch = build_patch(files, everything)

    if not patch:
        print("Nothing to verify.")
        return 0

    try:
        repo.apply_cached(patch, check_only=True)
    except GitError as exc:
        print("ROUND-TRIP FAILED\n", exc, file=sys.stderr)
        return 1

    print(f"Round-trip OK — {len(everything)} unit(s) rebuilt and accepted by git.")
    return 0


def cmd_commit(args) -> int:
    repo = Repo(args.repo)
    if not repo.index_is_clean():
        print("Index is not clean. Unstage first (git reset).", file=sys.stderr)
        return 2

    files = load(repo)
    try:
        selected = resolve_ids(files, args.hunk)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 2

    patch = build_patch(files, selected)
    if not patch:
        print("Selection produced an empty patch.", file=sys.stderr)
        return 2

    if args.dry_run:
        print(patch)
        return 0

    try:
        repo.apply_cached(patch)
    except GitError as exc:
        print("Could not stage the selection:\n", exc, file=sys.stderr)
        repo.reset_index()
        return 1

    sha = repo.commit(args.message)
    n_files, adds, dels = selection_stat(files, selected)
    print(f"{sha}  {args.message}")
    print(f"  {n_files} file(s), +{adds} -{dels}")
    print("  Remaining changes are still in your working tree; run `list` again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="splitter")
    parser.add_argument("--repo", default=".", help="path to the git repository")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show all hunks with their ids")
    p_list.add_argument("-v", "--verbose", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_verify = sub.add_parser("verify", help="check the parser round-trips")
    p_verify.set_defaults(func=cmd_verify)

    p_commit = sub.add_parser("commit", help="commit a chosen subset of hunks")
    p_commit.add_argument("-m", "--message", required=True)
    p_commit.add_argument("-H", "--hunk", action="append", required=True,
                          help="hunk id, short hash, or file path (repeatable)")
    p_commit.add_argument("--dry-run", action="store_true",
                          help="print the patch instead of applying it")
    p_commit.set_defaults(func=cmd_commit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
