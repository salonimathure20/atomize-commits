#!/usr/bin/env python3
"""Measure how well the splitter recovers real commit boundaries.

THE IDEA
--------
We need ground truth, and real repositories are full of it. Developers already
split their work into commits; those splits are what we are trying to
reproduce.

So: take N consecutive commits from a real repository, squash them into one
messy working tree, hand that to the splitter, and check whether it puts the
changes back the way the developer originally had them.

    commits c1, c2, c3  ->  squash  ->  splitter  ->  compare to c1, c2, c3

WHAT WE MEASURE
---------------
Comparing two groupings is not as simple as counting matching groups, because
the labels are arbitrary — our "commit 1" might be their "commit 3". The
standard fix is to look at *pairs* of items instead:

    For every pair of hunks, did the developer put them in the same commit?
    Did we?

That gives a confusion matrix and therefore precision, recall and F1:

    precision  of the pairs we grouped together, how many really belonged
    recall     of the pairs that really belonged together, how many we found
    F1         their harmonic mean

Precision and recall fail in opposite directions, which is why both matter.
Putting everything in one commit gives perfect recall and terrible precision.
Putting every hunk in its own commit does the reverse.

We also report the Adjusted Rand Index, which corrects for the fact that some
pairs match by chance. ARI of 0 means "no better than guessing"; 1 means exact.

Usage:
    python eval/recover.py --repo ~/src/flask --windows 40
    python eval/recover.py --repo ~/src/flask --algorithm label-propagation
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from splitter.core import analyze  # noqa: E402
from splitter.features import mapping_source  # noqa: E402


# --- talking to the repository under test ----------------------------------

def git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-pager", *args],
        cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:200])
    return result.stdout


def commit_list(repo: str, limit: int, branch: str) -> list[str]:
    """Newest first. Merges are skipped: their diffs are not one person's work."""
    output = git(repo, "log", "--no-merges", "--format=%H", f"-n{limit}", branch)
    return output.split()


def added_lines(repo: str, sha: str) -> set[str]:
    """The distinctive text a commit introduced.

    We fingerprint a commit by the lines it added, then match hunks in the
    squashed diff back to whichever commit added them. Blank lines and very
    short lines are dropped because they appear everywhere and would create
    false matches.
    """
    diff = git(repo, "show", "--format=", "--no-color", "-U0", sha)
    lines = set()
    for line in diff.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            text = line[1:].strip()
            if len(text) >= 8:
                lines.add(text)
    return lines


def files_at(repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    contents = {}
    for path in paths:
        try:
            contents[path] = git(repo, "show", f"{sha}:{path}")
        except RuntimeError:
            continue                    # deleted or renamed; skip
    return contents


# --- scoring ---------------------------------------------------------------

def pair_set(groups: dict[str, int]) -> set[tuple[str, str]]:
    """Every pair of items that share a group."""
    pairs = set()
    items = sorted(groups)
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if groups[a] == groups[b]:
                pairs.add((a, b))
    return pairs


@dataclass
class Score:
    precision: float
    recall: float
    f1: float
    ari: float
    truth_groups: int
    predicted_groups: int
    hunks: int


def compare(truth: dict[str, int], predicted: dict[str, int]) -> Score:
    """Pairwise precision, recall, F1 and Adjusted Rand Index."""
    shared = sorted(set(truth) & set(predicted))
    truth = {k: truth[k] for k in shared}
    predicted = {k: predicted[k] for k in shared}

    n = len(shared)
    if n < 2:
        return Score(0.0, 0.0, 0.0, 0.0, 0, 0, n)

    truth_pairs = pair_set(truth)
    predicted_pairs = pair_set(predicted)

    hit = len(truth_pairs & predicted_pairs)
    precision = hit / len(predicted_pairs) if predicted_pairs else 0.0
    recall = hit / len(truth_pairs) if truth_pairs else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    return Score(
        precision=precision,
        recall=recall,
        f1=f1,
        ari=adjusted_rand(truth, predicted),
        truth_groups=len(set(truth.values())),
        predicted_groups=len(set(predicted.values())),
        hunks=n,
    )


def adjusted_rand(truth: dict[str, int], predicted: dict[str, int]) -> float:
    """Rand index corrected for agreement that would happen by chance.

    Without the correction, two random groupings score surprisingly well
    simply because most pairs of items belong apart. ARI subtracts the
    expected score, so 0 really does mean "no better than guessing".
    """
    from collections import Counter

    def choose2(x: int) -> float:
        return x * (x - 1) / 2

    items = sorted(set(truth) & set(predicted))
    n = len(items)
    if n < 2:
        return 0.0

    joint = Counter((truth[i], predicted[i]) for i in items)
    rows = Counter(truth[i] for i in items)
    cols = Counter(predicted[i] for i in items)

    index = sum(choose2(v) for v in joint.values())
    row_sum = sum(choose2(v) for v in rows.values())
    col_sum = sum(choose2(v) for v in cols.values())
    expected = row_sum * col_sum / choose2(n)
    maximum = (row_sum + col_sum) / 2

    if maximum == expected:
        return 0.0
    return (index - expected) / (maximum - expected)


# --- one window ------------------------------------------------------------

@dataclass
class WindowResult:
    base: str
    tip: str
    score: Score | None = None
    skipped: str = ""
    detail: dict = field(default_factory=dict)


def evaluate_window(repo: str, shas: list[str], algorithm: str) -> WindowResult:
    """Squash `shas` into one diff and see whether we can undo the squash.

    shas are oldest-first. The parent of the oldest is our base.
    """
    oldest, newest = shas[0], shas[-1]
    base = git(repo, "rev-parse", f"{oldest}^").strip()
    result = WindowResult(base=base, tip=newest)

    diff = git(repo, "diff", "--no-color", "--no-ext-diff", "-U3", base, newest)
    if not diff.strip():
        result.skipped = "empty diff"
        return result

    # Fingerprint each original commit by the lines it added.
    fingerprints = {sha: added_lines(repo, sha) for sha in shas}

    paths = [p for p in git(repo, "diff", "--name-only", base, newest).split("\n") if p]
    contents = files_at(repo, newest, paths)

    analysis = analyze(diff, source=mapping_source(contents), algorithm=algorithm)
    if len(analysis.commits) == 0:
        result.skipped = "nothing to split"
        return result

    # Assign each hunk to whichever original commit added most of its lines.
    truth: dict[str, int] = {}
    predicted: dict[str, int] = {}
    unmatched = 0

    for index, commit in enumerate(analysis.commits):
        for member in commit.members:
            hunk_lines = {
                line.strip() for line in member.hunk.added_lines
                if len(line.strip()) >= 8
            }
            if not hunk_lines:
                continue

            overlaps = {
                sha: len(hunk_lines & fingerprint)
                for sha, fingerprint in fingerprints.items()
            }
            best = max(overlaps.items(), key=lambda kv: kv[1])
            if best[1] == 0:
                unmatched += 1
                continue                # cannot attribute; leave it out

            truth[member.hunk_id] = shas.index(best[0])
            predicted[member.hunk_id] = index

    if len(truth) < 2:
        result.skipped = "too few attributable hunks"
        return result

    result.score = compare(truth, predicted)
    result.detail = {"unmatched_hunks": unmatched}
    return result


# --- driver ----------------------------------------------------------------

def run(repo: str, windows: int, size: int, branch: str,
        algorithm: str, verbose: bool) -> dict:
    shas = commit_list(repo, windows * size + size, branch)
    if len(shas) < size + 1:
        raise SystemExit(f"{repo} does not have enough history")

    shas = list(reversed(shas))         # oldest first
    results: list[WindowResult] = []

    for start in range(0, len(shas) - size, size):
        window = shas[start:start + size]
        if len(window) < size:
            break
        try:
            result = evaluate_window(repo, window, algorithm)
        except RuntimeError as exc:
            result = WindowResult(base="", tip=window[-1], skipped=str(exc)[:60])
        results.append(result)

        if verbose:
            if result.score:
                s = result.score
                print(f"  {result.tip[:8]}  F1={s.f1:.2f}  ARI={s.ari:.2f}  "
                      f"{s.predicted_groups} predicted vs {s.truth_groups} actual")
            else:
                print(f"  {result.tip[:8]}  skipped: {result.skipped}")

        if len(results) >= windows:
            break

    scored = [r.score for r in results if r.score]
    if not scored:
        raise SystemExit("no windows could be evaluated")

    def mean(values):
        return statistics.mean(values) if values else 0.0

    return {
        "repo": str(Path(repo).resolve().name),
        "algorithm": algorithm,
        "window_size": size,
        "windows_evaluated": len(scored),
        "windows_skipped": len(results) - len(scored),
        "hunks": sum(s.hunks for s in scored),
        "precision": round(mean([s.precision for s in scored]), 3),
        "recall": round(mean([s.recall for s in scored]), 3),
        "f1": round(mean([s.f1 for s in scored]), 3),
        "ari": round(mean([s.ari for s in scored]), 3),
        "median_f1": round(statistics.median([s.f1 for s in scored]), 3),
        "exact_matches": sum(1 for s in scored if s.f1 == 1.0),
        "mean_predicted_groups": round(mean([s.predicted_groups for s in scored]), 2),
        "mean_actual_groups": round(mean([s.truth_groups for s in scored]), 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="path to a git repository")
    parser.add_argument("--windows", type=int, default=30,
                        help="how many groups of commits to test")
    parser.add_argument("--size", type=int, default=3,
                        help="commits squashed together per window")
    parser.add_argument("--branch", default="HEAD")
    parser.add_argument("--algorithm", default="louvain",
                        choices=["louvain", "label-propagation"])
    parser.add_argument("--compare", action="store_true",
                        help="run every algorithm and print a table")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    algorithms = ["louvain", "label-propagation"] if args.compare else [args.algorithm]
    rows = []
    for algorithm in algorithms:
        if args.verbose:
            print(f"\n{algorithm}:")
        rows.append(run(args.repo, args.windows, args.size,
                        args.branch, algorithm, args.verbose))

    if args.json:
        print(json.dumps(rows if len(rows) > 1 else rows[0], indent=2))
        return 0

    header = f"{'algorithm':<20}{'F1':>7}{'prec':>7}{'recall':>8}{'ARI':>7}{'exact':>7}"
    print(f"\n{rows[0]['repo']}  —  {rows[0]['windows_evaluated']} windows of "
          f"{rows[0]['window_size']} commits, {rows[0]['hunks']} hunks\n")
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['algorithm']:<20}{row['f1']:>7.3f}{row['precision']:>7.3f}"
              f"{row['recall']:>8.3f}{row['ari']:>7.3f}{row['exact_matches']:>7}")
    print(f"\nproposed {rows[0]['mean_predicted_groups']} commits on average, "
          f"actual was {rows[0]['mean_actual_groups']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
