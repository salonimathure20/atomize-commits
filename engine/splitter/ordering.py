"""Put the proposed commits in an order that could actually build.

THE PROBLEM
-----------
Grouping tells us *what* belongs together but says nothing about *when*. In
the demo repo, the auth commit contained `from .payments import charge_card`
while the payments commit created `charge_card`. Correct grouping, broken
history: checking out the auth commit alone gives you an ImportError.

THE FIX
-------
If group A introduces a name that group B uses, A has to land first. Those
"A before B" rules form a directed graph, and a topological sort turns them
into a running order.

Cycles happen — two features can genuinely reference each other. We do not
fail on them; we break the weakest link and carry on, then report it, because
a slightly wrong order is far more useful than a refusal.
"""

from __future__ import annotations

from .features import HunkFeatures


def _group_names(members: list[HunkFeatures]) -> tuple[set[str], set[str]]:
    """(names this group introduces, names this group merely mentions)."""
    defines: set[str] = set()
    uses: set[str] = set()
    for m in members:
        defines |= m.defines
        uses |= m.words
    return defines, uses - defines


def dependency_edges(groups: list[list[HunkFeatures]]) -> list[tuple[int, int, int]]:
    """Return (before, after, strength) triples between group indices."""
    profiles = [_group_names(g) for g in groups]
    edges: list[tuple[int, int, int]] = []

    for i, (defines_i, _) in enumerate(profiles):
        for j, (defines_j, uses_j) in enumerate(profiles):
            if i == j:
                continue
            shared = defines_i & uses_j
            shared -= defines_j          # j makes its own copy; no dependency
            if shared:
                edges.append((i, j, len(shared)))
    return edges


def order_groups(groups: list[list[HunkFeatures]]) -> tuple[list[int], list[str]]:
    """Return (order of group indices, warnings).

    Uses Kahn's algorithm: repeatedly take any group with nothing left waiting
    on it. Ties are broken by original position so results stay reproducible.
    """
    import networkx as nx

    n = len(groups)
    warnings: list[str] = []
    graph = nx.DiGraph()
    graph.add_nodes_from(range(n))

    for before, after, strength in dependency_edges(groups):
        graph.add_edge(before, after, strength=strength)

    while True:
        try:
            cycle = nx.find_cycle(graph, orientation="original")
        except nx.NetworkXNoCycle:
            break
        weakest = min(cycle, key=lambda e: graph[e[0]][e[1]]["strength"])
        graph.remove_edge(weakest[0], weakest[1])
        warnings.append(
            f"commits {weakest[0] + 1} and {weakest[1] + 1} depend on each "
            "other; order between them is a guess"
        )

    order = list(nx.lexicographical_topological_sort(graph))
    return order, warnings


def missing_references(
    ordered: list[list[HunkFeatures]],
) -> list[tuple[int, set[str]]]:
    """Names a commit uses that no earlier commit has defined yet.

    A rough proxy for "would this commit build?" It only sees names introduced
    by the changes themselves, not names that already existed in the repo, so
    treat it as a hint. The real answer comes from running the build, which is
    the containerised verification job in Phase 6.
    """
    seen: set[str] = set()
    problems: list[tuple[int, set[str]]] = []

    for n, members in enumerate(ordered):
        defines, uses = _group_names(members)
        all_defined_later = set()
        for later in ordered[n + 1:]:
            d, _ = _group_names(later)
            all_defined_later |= d

        dangling = (uses & all_defined_later) - seen
        if dangling:
            problems.append((n, dangling))
        seen |= defines

    return problems
