"""Score how related two hunks are, then find the tight bundles.

HOW THE SCORING WORKS
---------------------
For every pair of hunks we ask a handful of yes/no-ish questions and add up
points. Each question has a weight in WEIGHTS below, and every weight is
tunable — this is the dial board for the whole system.

Then we build a graph: hunks are dots, scores are the thickness of the string
between them. Clustering finds groups of dots that are heavily strung to each
other and barely strung to anything else. Those groups are your commits.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .features import HunkFeatures
from .graphs import Graph, communities

WEIGHTS = {
    "same_symbol": 0.60,     # both edit the same function -> almost certainly one commit
    "def_use": 0.55,         # one defines a name the other uses -> a real dependency
    "same_file": 0.30,       # weak on its own; overridden when symbols differ
    "same_file_diff_symbol": 0.08,   # deliberately tiny: this is the tangled case
    "test_pairing": 0.45,    # auth.py and test_auth.py
    "same_directory": 0.12,
    "word_overlap": 0.40,    # scaled by how much vocabulary they share
    "co_change": 0.20,       # these files are usually committed together
}

MERGE_THRESHOLD = 0.35       # below this we do not even draw a string


@dataclass
class Edge:
    a: str
    b: str
    weight: float
    reasons: list[str]


def jaccard(x: set[str], y: set[str]) -> float:
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def score_pair(
    p: HunkFeatures,
    q: HunkFeatures,
    co_change: dict[tuple[str, str], float] | None = None,
) -> tuple[float, list[str]]:
    """Return (score, human-readable reasons) for one pair of hunks."""
    score = 0.0
    reasons: list[str] = []

    # --- structural position -------------------------------------------
    if p.path == q.path:
        if p.symbol and q.symbol and p.symbol == q.symbol:
            score += WEIGHTS["same_symbol"]
            reasons.append(f"both inside {p.symbol}()")
        elif p.symbol and q.symbol and p.symbol != q.symbol:
            # Same file but different functions. This is the case that makes
            # naive tools fail, so we award almost nothing here and let the
            # other signals decide.
            score += WEIGHTS["same_file_diff_symbol"]
            reasons.append("same file, different functions")
        else:
            score += WEIGHTS["same_file"]
            reasons.append("same file")
    elif p.directory == q.directory:
        score += WEIGHTS["same_directory"]
        reasons.append(f"both in {p.directory}/")

    # --- dependency ------------------------------------------------------
    if p.defines & q.words:
        shared = sorted(p.defines & q.words)[:2]
        score += WEIGHTS["def_use"]
        reasons.append(f"uses {', '.join(shared)} defined by the other")
    elif q.defines & p.words:
        shared = sorted(q.defines & p.words)[:2]
        score += WEIGHTS["def_use"]
        reasons.append(f"defines {', '.join(shared)} used by the other")

    # --- code and its test ----------------------------------------------
    if p.is_test != q.is_test and p.base_name == q.base_name:
        score += WEIGHTS["test_pairing"]
        reasons.append("implementation and its test")

    # --- shared vocabulary ----------------------------------------------
    overlap = jaccard(p.words, q.words)
    if overlap > 0.05:
        score += WEIGHTS["word_overlap"] * overlap
        common = sorted(p.words & q.words, key=len, reverse=True)[:3]
        if common:
            reasons.append(f"shares {', '.join(common)}")

    # --- repository habits ------------------------------------------------
    if co_change and p.path != q.path:
        key = tuple(sorted((p.path, q.path)))
        strength = co_change.get(key, 0.0)
        if strength > 0:
            score += WEIGHTS["co_change"] * strength
            reasons.append("usually committed together")

    return min(score, 1.0), reasons


def build_graph(
    profiles: list[HunkFeatures],
    co_change: dict[tuple[str, str], float] | None = None,
):
    """Build a weighted graph. Returns (graph, list of Edge)."""
    graph = Graph()
    edges: list[Edge] = []

    for profile in profiles:
        graph.add_node(profile.hunk_id)

    for i, p in enumerate(profiles):
        for q in profiles[i + 1:]:
            weight, reasons = score_pair(p, q, co_change)
            if weight >= MERGE_THRESHOLD:
                graph.add_edge(p.hunk_id, q.hunk_id, weight=weight)
                edges.append(Edge(p.hunk_id, q.hunk_id, weight, reasons))

    return graph, edges


def cluster(graph, algorithm: str = "louvain") -> list[list[str]]:
    """Find the tight bundles.

    Louvain repeatedly moves nodes between groups, keeping any move that makes
    the groups more internally connected and less externally connected. It
    picks the number of groups by itself, which is what we want — we do not
    know in advance how many features got tangled together.
    """
    if graph.number_of_nodes() == 0:
        return []
    if graph.number_of_edges() == 0:
        return [[n] for n in graph.nodes()]
    return communities(graph, algorithm)


def explain_group(
    group: list[str],
    edges: list[Edge],
    limit: int = 3,
) -> list[str]:
    """Why did these hunks end up together? Strongest reasons first."""
    members = set(group)
    inside = [e for e in edges if e.a in members and e.b in members]
    inside.sort(key=lambda e: -e.weight)

    seen: set[str] = set()
    out: list[str] = []
    for edge in inside:
        for reason in edge.reasons:
            if reason not in seen:
                seen.add(reason)
                out.append(reason)
            if len(out) >= limit:
                return out
    return out


def co_change_matrix(repo, commits: int = 300) -> dict[tuple[str, str], float]:
    """Mine history for files that habitually change together.

    Reads the last N commits, counts how often each pair of files appears in
    the same commit, and normalises. Huge commits are skipped — a 40-file
    commit tells us nothing about which files are truly related.
    """
    out = repo.run("--no-pager", "log", f"-n{commits}",
                   "--format=%x00", "--name-only", check=False)

    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    file_counts: dict[str, int] = defaultdict(int)

    for block in out.split("\x00"):
        paths = sorted({ln.strip() for ln in block.split("\n") if ln.strip()})
        if not 2 <= len(paths) <= 12:
            continue
        for path in paths:
            file_counts[path] += 1
        for i, a in enumerate(paths):
            for b in paths[i + 1:]:
                pair_counts[(a, b)] += 1

    matrix: dict[tuple[str, str], float] = {}
    for (a, b), count in pair_counts.items():
        denom = min(file_counts[a], file_counts[b])
        if denom:
            matrix[(a, b)] = min(count / denom, 1.0)
    return matrix
