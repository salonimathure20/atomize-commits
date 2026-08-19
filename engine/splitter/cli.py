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
from .llm import LLMError, provider_from_env
from .messages import generate as generate_messages
from .ordering import missing_references, order_groups
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


def _prepare(repo: Repo, use_history: bool, use_model: bool):
    """Group, order, and name the commits. Returns (plan, warnings).

    A plan entry is (message, content-key set, members).
    """
    warnings: list[str] = []
    _, profiles, groups, _ = _plan(repo, use_history)
    if not groups:
        return [], warnings

    by_id = {p.hunk_id: p for p in profiles}
    member_groups = [[by_id[hid] for hid in g] for g in groups]

    # 1. Order so that a commit never uses something a later commit creates.
    order, cycle_warnings = order_groups(member_groups)
    warnings.extend(cycle_warnings)
    member_groups = [member_groups[i] for i in order]

    for index, dangling in missing_references(member_groups):
        names = ", ".join(sorted(dangling)[:3])
        warnings.append(f"commit {index + 1} references {names} before it exists")

    # 2. Name them.
    provider = None
    if use_model:
        try:
            provider = provider_from_env()
        except LLMError as exc:
            warnings.append(str(exc))
    messages, message_warning = generate_messages(member_groups, provider)
    if message_warning:
        warnings.append(message_warning)

    from .patch import content_key
    plan = []
    for message, members in zip(messages, member_groups):
        wanted = frozenset().union(*(content_key(m.hunk) for m in members))
        plan.append((message, wanted, members))
    return plan, warnings


def cmd_apply(args) -> int:
    """Compute a split and commit every group, in dependency order.

    Doing this in a single run matters: after each commit the diff is
    regenerated and hunk ids change, so we re-resolve each remaining group by
    the text it changes rather than by id.
    """
    repo = Repo(args.repo)
    if not repo.index_is_clean():
        print("Index is not clean. Unstage first (git reset).", file=sys.stderr)
        return 2

    plan, warnings = _prepare(repo, not args.no_history, not args.no_model)
    if not plan:
        print("Nothing to commit.")
        return 0

    for warning in warnings:
        print(f"note: {warning}", file=sys.stderr)

    if args.dry_run:
        for n, (message, _, members) in enumerate(plan, start=1):
            print(f"\n[{n}] {message.header()}   ({message.generated_by})")
            if message.body:
                print(f"      {message.body}")
            for m in sorted(members, key=lambda x: x.path):
                print(f"      {m.label()}")
        return 0

    from .patch import match_by_content
    for n, (message, wanted, _) in enumerate(plan, start=1):
        files = load(repo)                      # fresh view after each commit
        selected = match_by_content(files, wanted)
        if not selected:
            print(f"[{n}] nothing left to match; skipping", file=sys.stderr)
            continue

        try:
            repo.apply_cached(build_patch(files, selected))
        except GitError as exc:
            print(f"[{n}] failed to stage:\n{exc}", file=sys.stderr)
            repo.reset_index()
            return 1

        sha = repo.commit(message.full())
        print(f"{sha}  {message.header()}")

    leftover = all_hunks(load(repo))
    if leftover:
        print(f"\n{len(leftover)} hunk(s) left unassigned; run `list` to see them.")
    return 0


def cmd_doctor(args) -> int:
    """Check whether the configured model is reachable, before you need it.

    Setup problems (unset key, Ollama not running, wrong model name) otherwise
    only surface as a quiet fallback to heuristic messages, which is easy to
    miss. This makes them loud.
    """
    import os

    choice = os.environ.get("SPLITTER_LLM", "").strip().lower() or "(unset)"
    print(f"SPLITTER_LLM = {choice}")

    try:
        provider = provider_from_env()
    except LLMError as exc:
        print(f"  config error: {exc}")
        return 1

    if provider is None:
        print("  no model configured — heuristic messages will be used")
        print("  set SPLITTER_LLM to ollama, gemini, anthropic, or openai")
        return 0

    model = getattr(provider, "model", "?")
    print(f"provider     = {provider.name}")
    print(f"model        = {model}")
    print("sending a test prompt...")

    try:
        reply = provider.complete(
            "Reply with exactly one word.",
            "Say the word: ready",
        )
    except LLMError as exc:
        message = str(exc)
        print(f"  FAILED: {message}")

        # Providers retire model names regularly, and the error usually names
        # the replacement. Surface that instead of making the user read JSON.
        if "404" in message or "not found" in message.lower():
            import re
            suggestion = re.search(r"models/([\w.\-]+) for the latest", message)
            print(f"  the model name {model!r} looks retired")
            if suggestion:
                print(f"  the provider suggests: {suggestion.group(1)}")
                print(f"  fix with:  export SPLITTER_MODEL={suggestion.group(1)}")
            else:
                print("  set a current one with:  export SPLITTER_MODEL=<name>")
        elif "401" in message or "403" in message or "API_KEY" in message:
            print("  the key looks wrong or expired; create a new one")
        elif provider.name == "ollama":
            print("  is ollama running? try:  ollama serve")
            print(f"  is the model pulled? try:  ollama pull {model}")
        return 1

    snippet = " ".join(reply.split())[:60]
    print(f"  reply: {snippet!r}")
    print("  model is reachable")
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
    p_apply.add_argument("--no-model", action="store_true",
                         help="skip the language model, use heuristic messages")
    p_apply.set_defaults(func=cmd_apply)

    p_doctor = sub.add_parser("doctor", help="check the model configuration")
    p_doctor.set_defaults(func=cmd_doctor)

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
