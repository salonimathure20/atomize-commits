"""A thin, boring wrapper around the git CLI.

We shell out rather than use a library. It keeps dependencies at zero and it
means every operation is one you could have typed yourself, which matters a
lot when you are debugging a patch that will not apply.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class Repo:
    def __init__(self, root: str | Path = "."):
        self.root = Path(root).resolve()

    def run(self, *args: str, stdin: str | None = None, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.root,
            input=stdin,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed:\n{proc.stderr.strip()}")
        return proc.stdout

    # --- reads -----------------------------------------------------------

    def working_diff(self) -> str:
        """Unstaged changes to tracked files, in a stable machine format."""
        return self.run(
            "--no-pager", "diff", "--no-color", "--no-ext-diff",
            "--find-renames", "-U3",
        )

    def untracked(self) -> list[str]:
        out = self.run("ls-files", "--others", "--exclude-standard")
        return [p for p in out.splitlines() if p.strip()]

    def index_is_clean(self) -> bool:
        """True when nothing is staged. We require this before committing."""
        proc = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=self.root, capture_output=True, text=True,
        )
        return proc.returncode == 0

    def head_exists(self) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=self.root, capture_output=True, text=True,
        )
        return proc.returncode == 0

    # --- writes ----------------------------------------------------------

    def intent_to_add(self, paths: list[str]) -> None:
        """Register untracked files so they appear in `git diff` as additions.

        This is the trick that lets brand-new files participate in splitting
        without actually staging their content.
        """
        if paths:
            self.run("add", "--intent-to-add", "--", *paths)

    def apply_cached(self, patch: str, check_only: bool = False) -> None:
        args = ["apply", "--cached", "--unidiff-zero", "--whitespace=nowarn"]
        if check_only:
            args.append("--check")
        self.run(*args, stdin=patch)

    def commit(self, message: str) -> str:
        self.run("commit", "-m", message)
        return self.run("rev-parse", "--short", "HEAD").strip()

    def reset_index(self) -> None:
        """Unstage everything, leaving the working tree alone."""
        if self.head_exists():
            self.run("reset", "--quiet", "HEAD", "--")
        else:
            self.run("rm", "--cached", "-r", "--quiet", ".", check=False)
