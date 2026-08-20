"""Graph algorithms, written out rather than imported.

WHY NOT NETWORKX
----------------
It was the obvious choice while this was a CLI. It stopped being obvious once
the engine had to ship inside a VS Code extension: an extension that says
"first install Python, then pip install networkx" is an extension nobody
keeps. Removing the dependency makes the engine pure standard library, so it
can be bundled and simply work.

Only two algorithms were ever used, and both are short:

  * community detection, to find which hunks belong together
  * topological sort, to order commits so nothing is used before it exists

Writing them out also makes them tunable. The clustering below is a weighted
label-propagation variant: cheap, deterministic, and easy to reason about
when a grouping comes out wrong.
"""

from __future__ import annotations

from collections import defaultdict


class Graph:
    """An undirected weighted graph. Just enough of one for our purposes."""

    def __init__(self):
        self._nodes: set[str] = set()
        self._edges: dict[str, dict[str, float]] = defaultdict(dict)

    def add_node(self, node: str) -> None:
        self._nodes.add(node)

    def add_edge(self, a: str, b: str, weight: float = 1.0) -> None:
        self._nodes.add(a)
        self._nodes.add(b)
        self._edges[a][b] = weight
        self._edges[b][a] = weight

    def nodes(self) -> list[str]:
        return sorted(self._nodes)

    def neighbors(self, node: str) -> dict[str, float]:
        return self._edges.get(node, {})

    def number_of_nodes(self) -> int:
        return len(self._nodes)

    def number_of_edges(self) -> int:
        return sum(len(v) for v in self._edges.values()) // 2


def modularity(graph: Graph, groups: list[list[str]]) -> float:
    """How much better than chance is this grouping?

    Q = (1/2m) * sum over pairs in the same group of [A_ij - k_i*k_j/(2m)]

    The first term is the weight actually between two nodes; the second is how
    much weight you would expect between them if edges were rewired at random,
    keeping each node's total. Q near 0 means the grouping is no better than
    chance. Real structure lands around 0.3 to 0.7.

    This is the number Louvain maximises, and having it separate means we can
    check that the optimiser actually improved things.
    """
    two_m = sum(sum(graph.neighbors(n).values()) for n in graph.nodes())
    if two_m == 0:
        return 0.0

    total = 0.0
    for members in groups:
        member_set = set(members)
        internal = 0.0
        degree = 0.0
        for node in members:
            for neighbor, weight in graph.neighbors(node).items():
                degree += weight
                if neighbor in member_set:
                    internal += weight
        total += internal / two_m - (degree / two_m) ** 2
    return total


def louvain(graph: Graph, max_passes: int = 10) -> list[list[str]]:
    """Community detection by modularity optimisation.

    Two phases, repeated until nothing improves:

      1. Local moving. Visit each node and move it to whichever neighbouring
         community gives the largest increase in modularity. Repeat until no
         node wants to move.
      2. Aggregation. Collapse each community into a single node, with edge
         weights summed and internal weight becoming a self-loop. Run phase 1
         again on the smaller graph.

    Collapsing is what makes it good: after the first pass it is optimising
    over groups rather than individual nodes, so it can escape the local
    decisions that trap simpler methods.

    Node order is sorted throughout, so the result is identical on every run.
    A tool that proposes a different split each time it runs is not one you
    would let touch your git history.
    """
    nodes = graph.nodes()
    if not nodes:
        return []

    # Work on integer-labelled nodes throughout, so that aggregation can reuse
    # the same code on every pass. `labels` remembers which original hunks sit
    # inside each collapsed node.
    current = Graph()
    index_of = {node: str(i) for i, node in enumerate(nodes)}
    for node in nodes:
        current.add_node(index_of[node])
    for node in nodes:
        for neighbor, weight in graph.neighbors(node).items():
            current.add_edge(index_of[node], index_of[neighbor], weight)

    labels = {i: [node] for i, node in enumerate(nodes)}

    for _ in range(max_passes):
        assignment = _local_moving(current)

        distinct = sorted(set(assignment.values()))
        if len(distinct) == current.number_of_nodes():
            break                       # nothing merged; we are done

        renumber = {old: new for new, old in enumerate(distinct)}
        assignment = {node: renumber[community] for node, community in assignment.items()}

        # Carry the new grouping back down to the original hunks.
        merged: dict[int, list[str]] = {}
        for node, community in assignment.items():
            merged.setdefault(community, []).extend(labels[int(node)])
        labels = merged

        current = _aggregate(current, assignment)

    groups = [sorted(members) for members in labels.values()]
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


def _local_moving(graph: Graph) -> dict[str, int]:
    """Phase one: move nodes greedily until modularity stops improving."""
    nodes = graph.nodes()
    degree = {n: sum(graph.neighbors(n).values()) for n in nodes}
    two_m = sum(degree.values())
    if two_m == 0:
        return {node: index for index, node in enumerate(nodes)}

    community = {node: index for index, node in enumerate(nodes)}
    total_degree = {index: degree[node] for index, node in enumerate(nodes)}

    for _ in range(50):
        moved = False
        for node in nodes:
            own = community[node]
            k = degree[node]

            # Weight from this node into each neighbouring community.
            into: dict[int, float] = {}
            for neighbor, weight in graph.neighbors(node).items():
                if neighbor != node:
                    into[community[neighbor]] = into.get(community[neighbor], 0.0) + weight

            # Take the node out before comparing, so its own community is
            # judged on the same terms as the alternatives.
            total_degree[own] -= k

            best_community = own
            best_gain = into.get(own, 0.0) - total_degree[own] * k / two_m
            for candidate, weight_in in sorted(into.items()):
                gain = weight_in - total_degree[candidate] * k / two_m
                if gain > best_gain + 1e-12:
                    best_gain = gain
                    best_community = candidate

            total_degree[best_community] += k
            if best_community != own:
                community[node] = best_community
                moved = True

        if not moved:
            break

    return community


def _aggregate(graph: Graph, assignment: dict[str, int]) -> Graph:
    """Phase two: collapse each community into a single node."""
    collapsed = Graph()
    for community in set(assignment.values()):
        collapsed.add_node(str(community))

    weights: dict[tuple[int, int], float] = {}
    for node in graph.nodes():
        for neighbor, weight in graph.neighbors(node).items():
            a, b = assignment[node], assignment[neighbor]
            key = (a, b) if a <= b else (b, a)
            # Each undirected edge is seen twice; halve to compensate.
            weights[key] = weights.get(key, 0.0) + weight / 2

    for (a, b), weight in weights.items():
        if a == b:
            collapsed.add_edge(str(a), str(a), weight)    # internal weight
        else:
            collapsed.add_edge(str(a), str(b), weight)
    return collapsed


def label_propagation(graph: Graph, max_rounds: int = 20) -> list[list[str]]:
    """Group nodes that are strongly connected to each other.

    Weighted label propagation: every node repeatedly joins whichever
    neighbouring group it is most strongly tied to.

    Kept as a fast baseline to compare Louvain against — see eval/. It is
    cheaper but purely local, so it can chain through weak links and merge
    communities that Louvain keeps apart.
    Ties break toward the smallest label, and nodes are visited in sorted
    order, so the result is identical on every run — which matters, because a
    tool that proposes a different split each time it runs is not trustworthy.

    Isolated nodes keep their own group, which is what we want: a hunk related
    to nothing else deserves its own commit.
    """
    nodes = graph.nodes()
    if not nodes:
        return []

    label = {node: node for node in nodes}

    for _ in range(max_rounds):
        changed = False
        for node in nodes:
            neighbors = graph.neighbors(node)
            if not neighbors:
                continue

            # Total edge weight from this node into each candidate group.
            pull: dict[str, float] = defaultdict(float)
            for neighbor, weight in neighbors.items():
                pull[label[neighbor]] += weight

            # Staying put is only worth it if nothing pulls harder.
            best = max(sorted(pull.items()), key=lambda item: item[1])
            if pull.get(label[node], 0.0) < best[1]:
                label[node] = best[0]
                changed = True

        if not changed:
            break

    grouped: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        grouped[label[node]].append(node)

    groups = [sorted(members) for members in grouped.values()]
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


ALGORITHMS = {
    "louvain": louvain,
    "label-propagation": label_propagation,
}


def communities(graph: Graph, algorithm: str = "louvain") -> list[list[str]]:
    """Find communities using the named algorithm."""
    try:
        return ALGORITHMS[algorithm](graph)
    except KeyError:
        raise ValueError(
            f"unknown algorithm {algorithm!r}; expected one of "
            + ", ".join(sorted(ALGORITHMS))
        ) from None


def topological_order(
    node_count: int,
    edges: list[tuple[int, int, int]],
) -> tuple[list[int], list[tuple[int, int]]]:
    """Order nodes so every edge points forwards.

    Kahn's algorithm: repeatedly take a node nothing is waiting on. Ties break
    toward the lower index so the order is stable.

    edges are (before, after, strength). Cycles are not an error here — two
    features can genuinely reference each other — so the weakest edge in a
    cycle is dropped and reported.

    Returns (order, dropped_edges).
    """
    import heapq

    remaining = {(a, b): strength for a, b, strength in edges}
    dropped: list[tuple[int, int]] = []

    while True:
        incoming = defaultdict(int)
        outgoing = defaultdict(list)
        for (a, b) in remaining:
            incoming[b] += 1
            outgoing[a].append(b)

        ready = [n for n in range(node_count) if incoming[n] == 0]
        heapq.heapify(ready)

        order: list[int] = []
        degree = dict(incoming)
        while ready:
            node = heapq.heappop(ready)
            order.append(node)
            for target in sorted(outgoing[node]):
                degree[target] -= 1
                if degree[target] == 0:
                    heapq.heappush(ready, target)

        if len(order) == node_count:
            return order, dropped

        # Something is cyclic. Drop the weakest edge among the nodes that
        # never became ready, then try again.
        stuck = set(range(node_count)) - set(order)
        candidates = {
            edge: strength
            for edge, strength in remaining.items()
            if edge[0] in stuck and edge[1] in stuck
        }
        if not candidates:
            # Should not happen, but never loop forever over a graph we
            # failed to understand.
            return order + sorted(stuck), dropped

        weakest = min(sorted(candidates), key=lambda e: candidates[e])
        del remaining[weakest]
        dropped.append(weakest)
