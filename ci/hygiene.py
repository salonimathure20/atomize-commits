#!/usr/bin/env python3
"""Check a pull request's commit hygiene, and say so on the PR.

This is the service's client. It runs inside GitHub Actions: the workflow
starts the container, then this script sends the pull request's diff to
POST /analyze and turns the answer into a review comment.

Why a service rather than importing the engine directly? Because the container
is how CI gets a working engine without installing Python packages, pinning
versions, or caching a virtualenv. The image either runs or it does not.

Nothing here fails the build by default. A tool that blocks merges over style
gets switched off within a week; one that leaves a useful comment gets read.
Pass --fail-over N if you want it to be strict.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

# Lets us find and replace our own comment instead of posting a new one on
# every push. Invisible in rendered markdown.
MARKER = "<!-- commit-splitter-report -->"

MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 1_500_000


# --- gathering -------------------------------------------------------------

def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def diff_text(base: str, head: str) -> str:
    """The changes this pull request introduces.

    Three dots, not two: `base...head` compares against the merge base, so
    commits that landed on the base branch after the PR opened are excluded.
    Using two dots would blame the author for other people's work.
    """
    return run_git("--no-pager", "diff", "--no-color", "--no-ext-diff",
                   "-U3", f"{base}...{head}")


def changed_paths(base: str, head: str) -> list[str]:
    output = run_git("diff", "--name-only", f"{base}...{head}")
    return [p for p in output.splitlines() if p.strip()]


def read_files(paths: list[str]) -> dict[str, str]:
    """Current contents of the changed files, for symbol detection.

    Skips anything large or unreadable. Missing files simply make the analysis
    coarser; they do not break it.
    """
    contents: dict[str, str] = {}
    total = 0
    for path in paths:
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        total += len(text)
        if total > MAX_TOTAL_BYTES:
            break
        contents[path] = text
    return contents


# --- the service -----------------------------------------------------------

def analyze_via_service(url: str, diff: str, files: dict[str, str],
                        messages: bool = True, timeout: int = 120) -> dict:
    """POST the diff to the analysis service."""
    payload = json.dumps({
        "diff": diff,
        "files": files,
        "messages": messages,
    }).encode()

    request = urllib.request.Request(
        url.rstrip("/") + "/analyze",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"service returned {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"could not reach the service at {url}: {exc}") from exc


# --- reporting -------------------------------------------------------------

def should_report(result: dict, min_commits: int = 2) -> bool:
    """Only speak up when there is something worth saying.

    A pull request that is already one coherent change gets no comment. Praise
    on every PR is noise, and noise is how a bot gets muted.
    """
    return len(result.get("commits", [])) >= min_commits


def render_comment(result: dict) -> str:
    """Turn an analysis into a pull request comment."""
    commits = result.get("commits", [])
    lines = [
        MARKER,
        f"### This pull request contains {len(commits)} unrelated changes",
        "",
        "Smaller commits make history easier to search, revert, and review. "
        "Here is one way to split it:",
        "",
    ]

    for index, commit in enumerate(commits, start=1):
        header = commit.get("message", {}).get("header", "(no message)")
        files = commit.get("files", [])
        adds = commit.get("additions", 0)
        dels = commit.get("deletions", 0)

        lines.append(f"**{index}. `{header}`**")
        lines.append("")
        lines.append(f"<sub>{len(files)} file(s) &middot; +{adds} &minus;{dels}</sub>")
        lines.append("")

        for path in files[:8]:
            symbols = sorted({
                h["symbol"] for h in commit.get("hunks", [])
                if h.get("path") == path and h.get("symbol")
            })
            suffix = f" — {', '.join(symbols[:4])}" if symbols else ""
            lines.append(f"- `{path}`{suffix}")
        if len(files) > 8:
            lines.append(f"- <sub>and {len(files) - 8} more</sub>")

        reasons = commit.get("reasons", [])
        if reasons:
            lines.append("")
            lines.append(f"<sub>Grouped because: {reasons[0]}</sub>")
        lines.append("")

    warnings = result.get("warnings", [])
    if warnings:
        lines.append("<details><summary>Notes</summary>")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append(
        "<sub>This is a suggestion, not a blocker. Generated by "
        "[commit-splitter](https://github.com/) — run it locally with the "
        "VS Code extension to split changes before you commit.</sub>"
    )
    return "\n".join(lines)


# --- github ----------------------------------------------------------------

def github_request(url: str, token: str, method: str = "GET",
                   body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "commit-splitter",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode()
    return json.loads(payload) if payload else None


def find_existing_comment(repo: str, pr: int, token: str) -> int | None:
    """Our previous comment on this PR, if there is one."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr}/comments?per_page=100"
    for comment in github_request(url, token) or []:
        if MARKER in (comment.get("body") or ""):
            return comment["id"]
    return None


def upsert_comment(repo: str, pr: int, token: str, body: str) -> str:
    """Edit our comment if it exists, otherwise post a new one.

    Pushing to a PR should not leave a trail of near-identical bot comments.
    """
    existing = find_existing_comment(repo, pr, token)
    if existing:
        github_request(
            f"https://api.github.com/repos/{repo}/issues/comments/{existing}",
            token, method="PATCH", body={"body": body},
        )
        return "updated"

    github_request(
        f"https://api.github.com/repos/{repo}/issues/{pr}/comments",
        token, method="POST", body={"body": body},
    )
    return "created"


def delete_comment(repo: str, pr: int, token: str) -> bool:
    """Remove a stale comment once the PR has been tidied up."""
    existing = find_existing_comment(repo, pr, token)
    if not existing:
        return False
    github_request(
        f"https://api.github.com/repos/{repo}/issues/comments/{existing}",
        token, method="DELETE",
    )
    return True


# --- entry point -----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default=os.environ.get("SERVICE_URL", "http://localhost:8000"))
    parser.add_argument("--base", default=os.environ.get("BASE_SHA", "origin/main"))
    parser.add_argument("--head", default=os.environ.get("HEAD_SHA", "HEAD"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr", type=int, default=int(os.environ.get("PR_NUMBER", 0) or 0))
    parser.add_argument("--min-commits", type=int, default=2,
                        help="stay quiet unless the PR splits into at least this many")
    parser.add_argument("--fail-over", type=int, default=0,
                        help="exit non-zero when the PR splits into more than N commits (0 disables)")
    parser.add_argument("--no-messages", action="store_true",
                        help="skip the language model and use heuristic messages")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the comment instead of posting it")
    args = parser.parse_args(argv)

    diff = diff_text(args.base, args.head)
    if not diff.strip():
        print("No changes to analyse.")
        return 0

    files = read_files(changed_paths(args.base, args.head))
    print(f"Analysing {len(files)} file(s) against {args.service}")

    try:
        result = analyze_via_service(args.service, diff, files,
                                     messages=not args.no_messages)
    except RuntimeError as exc:
        # A broken analysis must not break someone's build.
        print(f"::warning::commit hygiene check skipped: {exc}")
        return 0

    count = len(result.get("commits", []))
    print(f"The service proposes {count} commit(s)")

    # A step summary always shows up in the Actions run, even on forks where
    # the token cannot write comments.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    body = render_comment(result) if count else ""
    if summary_path and body:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(body + "\n")

    # --dry-run suppresses only the posting. The verdict is still reported, so
    # you can check strict mode locally before enabling it in CI.
    if args.dry_run:
        print(body or "(nothing to report)")
    else:
        token = os.environ.get("GITHUB_TOKEN", "")
        if args.repo and args.pr and token:
            if should_report(result, args.min_commits):
                action = upsert_comment(args.repo, args.pr, token, body)
                print(f"comment {action}")
            elif delete_comment(args.repo, args.pr, token):
                print("previous comment removed; this PR is now one coherent change")
        else:
            print("no PR context or token; skipping the comment")

    if args.fail_over and count > args.fail_over:
        print(f"::error::this PR contains {count} unrelated changes (limit {args.fail_over})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
