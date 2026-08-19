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

from .features import build_features
from .gitrepo import GitError, Repo
from .grouping import build_graph, cluster, co_change_matrix, explain_group
from .parser import all_hunks, parse_diff
from .subhunks import split_all
from .patch import build_patch, resolve_ids, selection_stat


def load(repo: Repo, include_untracked: bool = True, fine: bool = True):
    """Read the working tree into FileDiff objects.

    `fine` splits oversized hunks at function boundaries so that two
    features added next to each other can land in different commits.
    """
    if include_untracked:
        repo.intent_to_add(repo.untracked())
    files = parse_diff(repo.working_diff())
    return split_all(files) if fine else files


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


def cmd_group(args) -> int:
    """Propose a split. This is Phase 2: still no AI, just structure."""
    repo = Repo(args.repo)
    files = load(repo)
    profiles = build_features(str(repo.root), files)

    if not profiles:
        print("Nothing to group.")
        return 0

    history = {} if args.no_history else co_change_matrix(repo)
    graph, edges = build_graph(profiles, history)
    groups = cluster(graph)

    by_id = {p.hunk_id: p for p in profiles}

    for n, group in enumerate(groups, start=1):
        members = [by_id[hid] for hid in group]
        adds = sum(m.hunk.stat[0] for m in members)
        dels = sum(m.hunk.stat[1] for m in members)
        touched = sorted({m.path for m in members})

        print(f"\nGroup {n}  —  {len(members)} hunk(s), "
              f"{len(touched)} file(s), +{adds} -{dels}")
        for reason in explain_group(group, edges):
            print(f"  why: {reason}")
        for m in sorted(members, key=lambda x: (x.path, x.hunk.new_start)):
            print(f"    {m.hunk.hunk_id}  {m.label()}")

        short = " ".join(f"-H {hid.split('#')[1]}" for hid in group)
        print(f"    commit with:  splitter commit -m \"...\" {short}")

    atomic = [fd.path for fd in files if fd.is_atomic]
    if atomic:
        print(f"\nCannot be split (binary or rename): {', '.join(atomic)}")

    print(f"\n{len(groups)} proposed commit(s) from {len(profiles)} hunk(s).")
    return 0


def _plan(repo: Repo, use_history: bool):
    """Compute the proposed split once. Returns (groups, profiles-by-id, edges)."""
    files = load(repo)
    profiles = build_features(str(repo.root), files)
    history = co_change_matrix(repo) if use_history else {}
    graph, edges = build_graph(profiles, history)
    return files, profiles, cluster(graph), edges


def _heuristic_message(members) -> str:
    """A placeholder commit message until Phase 3 brings in the model.

    Conventional Commits shape: type(scope): summary. We guess the scope from
    the shared directory or filename and the type from whether the group looks
    like new code, tests, or edits.
    """
    paths = {m.path for m in members}
    dirs = {m.directory for m in members}
    scope = next(iter(dirs)).split("/")[-1] if len(dirs) == 1 else "core"
    if scope in (".", ""):
        scope = "core"

    only_tests = all(m.is_test for m in members)
    adds = sum(m.hunk.stat[0] for m in members)
    dels = sum(m.hunk.stat[1] for m in members)

    kind = "test" if only_tests else ("feat" if dels == 0 else "refactor")
    names = sorted({s for m in members for s in m.defines})
    if names:
        subject = f"add {', '.join(names[:3])}"
    else:
        subject = f"update {', '.join(sorted(paths)[:2])}"
    return f"{kind}({scope}): {subject}"


def cmd_apply(args) -> int:
    """Compute a split and commit every group, in one pass.

    Doing this in a single run matters: after each commit the diff is
    regenerated and hunk ids change, so we re-resolve each remaining group by
    the text it changes rather than by id.
    """
    repo = Repo(args.repo)
    if not repo.index_is_clean():
        print("Index is not clean. Unstage first (git reset).", file=sys.stderr)
        return 2

    _, profiles, groups, _ = _plan(repo, not args.no_history)
    if not groups:
        print("Nothing to commit.")
        return 0

    by_id = {p.hunk_id: p for p in profiles}
    from .patch import content_key, match_by_content

    plan = []
    for group in groups:
        members = [by_id[hid] for hid in group]
        wanted = frozenset().union(*(content_key(m.hunk) for m in members))
        plan.append((_heuristic_message(members), wanted, members))

    if args.dry_run:
        for message, _, members in plan:
            print(f"\n{message}")
            for m in sorted(members, key=lambda x: x.path):
                print(f"    {m.label()}")
        return 0

    for n, (message, wanted, members) in enumerate(plan, start=1):
        files = load(repo)                      # fresh view after each commit
        selected = match_by_content(files, wanted)
        if not selected:
            print(f"[{n}] nothing left to match; skipping", file=sys.stderr)
            continue

        patch = build_patch(files, selected)
        try:
            repo.apply_cached(patch)
        except GitError as exc:
            print(f"[{n}] failed to stage:\n{exc}", file=sys.stderr)
            repo.reset_index()
            return 1

        sha = repo.commit(message)
        print(f"{sha}  {message}")

    leftover = all_hunks(load(repo))
    if leftover:
        print(f"\n{len(leftover)} hunk(s) left unassigned; run `list` to see them.")
    return 0


def cmd_why(args) -> int:
    """Show the raw pairwise scores. Use this when tuning the weights."""
    repo = Repo(args.repo)
    files = load(repo)
    profiles = build_features(str(repo.root), files)
    history = {} if args.no_history else co_change_matrix(repo)
    _, edges = build_graph(profiles, history)

    if not edges:
        print("No pair scored above the merge threshold.")
        return 0

    for edge in sorted(edges, key=lambda e: -e.weight):
        print(f"{edge.weight:.2f}  {edge.a}  <->  {edge.b}")
        for reason in edge.reasons:
            print(f"       {reason}")
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

    p_group = sub.add_parser("group", help="propose how to split the changes")
    p_group.add_argument("--no-history", action="store_true",
                         help="ignore git log co-change mining")
    p_group.set_defaults(func=cmd_group)

    p_apply = sub.add_parser("apply", help="split and commit everything in one pass")
    p_apply.add_argument("--no-history", action="store_true")
    p_apply.add_argument("--dry-run", action="store_true",
                         help="show the plan without committing")
    p_apply.set_defaults(func=cmd_apply)

    p_why = sub.add_parser("why", help="show pairwise relatedness scores")
    p_why.add_argument("--no-history", action="store_true")
    p_why.set_defaults(func=cmd_why)

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
