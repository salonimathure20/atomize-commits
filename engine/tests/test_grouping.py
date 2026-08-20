"""Phase 2 tests: does the tool put the right hunks in the right piles?

The headline test is test_tangled_file_splits_by_feature, which is the exact
scenario this project exists for: two unrelated features edited into the same
file in the same sitting.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from splitter.features import build_features, disk_source
from splitter.gitrepo import Repo
from splitter.grouping import build_graph, cluster, score_pair
from splitter.parser import all_hunks, parse_diff
from splitter.subhunks import split_all
from splitter.symbols import enclosing_symbols


def git(repo: Path, *args: str):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True)


BASE_ROUTES = '''from flask import Flask

app = Flask(__name__)


def health():
    return {"ok": True}


def index():
    return {"service": "demo"}
'''

TANGLED_ROUTES = '''from flask import Flask

from .auth import verify_token
from .payments import charge_card

app = Flask(__name__)


def health():
    return {"ok": True}


def index():
    return {"service": "demo"}


def login_route(request):
    token = verify_token(request.headers.get("Authorization"))
    return {"token": token}


def checkout_route(request):
    receipt = charge_card(request.json["card"], request.json["amount"])
    return {"receipt": receipt}
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", ".")
    git(tmp_path, "config", "user.email", "t@t.co")
    git(tmp_path, "config", "user.name", "T")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "routes.py").write_text(BASE_ROUTES)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def tangle(repo: Path) -> None:
    """Two unrelated features, one of them landing inside an existing file."""
    app = repo / "app"
    (app / "routes.py").write_text(TANGLED_ROUTES)
    (app / "auth.py").write_text(
        'import jwt\n\nAUTH_SECRET = "dev"\n\n\n'
        "def verify_token(header):\n    return jwt.decode(header, AUTH_SECRET)\n"
    )
    (app / "payments.py").write_text(
        "import stripe\n\n\ndef charge_card(card, amount):\n"
        "    return stripe.Charge.create(card=card, amount=amount)\n"
    )
    (app / "test_payments.py").write_text(
        "from .payments import charge_card\n\n\n"
        'def test_charge_card():\n    assert charge_card("4242", 100)\n'
    )


def load(repo: Path):
    r = Repo(repo)
    r.intent_to_add(r.untracked())
    return r, split_all(parse_diff(r.working_diff()))


# --- symbol detection ------------------------------------------------------

def test_enclosing_symbol_found():
    lines = TANGLED_ROUTES.split("\n")
    inside_login = lines.index("    return {\"token\": token}") + 1
    assert enclosing_symbols(lines, inside_login) == ["login_route"]


def test_nested_symbols_are_outermost_first():
    src = "class PaymentService:\n    def charge(self):\n        return 1\n".split("\n")
    assert enclosing_symbols(src, 3) == ["PaymentService", "charge"]


# --- sub-hunk splitting ----------------------------------------------------

def test_adjacent_definitions_get_split(repo: Path):
    """Git gives us one hunk for both routes; we must cut it into two."""
    tangle(repo)
    r = Repo(repo)
    coarse = parse_diff(r.working_diff())
    routes_coarse = [fd for fd in coarse if fd.path.endswith("routes.py")][0]
    fine = split_all(parse_diff(r.working_diff()))
    routes_fine = [fd for fd in fine if fd.path.endswith("routes.py")][0]

    assert len(routes_fine.hunks) > len(routes_coarse.hunks)


def test_splitting_preserves_applyability(repo: Path):
    """Splitting must never produce a patch git will reject."""
    tangle(repo)
    r, files = load(repo)
    from splitter.patch import build_patch
    patch = build_patch(files, {h.hunk_id for h in all_hunks(files)})
    r.apply_cached(patch, check_only=True)


# --- scoring ---------------------------------------------------------------

def test_same_file_different_functions_scores_low(repo: Path):
    """The signal that rescues the tangled-file case."""
    tangle(repo)
    _, files = load(repo)
    profiles = build_features(disk_source(str(repo)), files)
    routes = [p for p in profiles if p.path.endswith("routes.py") and p.symbol]
    login = [p for p in routes if p.symbol == "login_route"]
    checkout = [p for p in routes if p.symbol == "checkout_route"]
    assert login and checkout

    score, _ = score_pair(login[0], checkout[0])
    assert score < 0.35, "unrelated routes in one file must not be glued together"


def test_definition_and_its_user_score_high(repo: Path):
    tangle(repo)
    _, files = load(repo)
    profiles = build_features(disk_source(str(repo)), files)
    auth = [p for p in profiles if p.path.endswith("auth.py")][0]
    login = [p for p in profiles if p.symbol == "login_route"][0]
    score, reasons = score_pair(auth, login)
    assert score >= 0.35
    assert any("verify_token" in r for r in reasons)


# --- the headline test -----------------------------------------------------

def test_tangled_file_splits_by_feature(repo: Path):
    tangle(repo)
    _, files = load(repo)
    profiles = build_features(disk_source(str(repo)), files)
    graph, _ = build_graph(profiles, {})
    groups = cluster(graph)

    by_id = {p.hunk_id: p for p in profiles}

    # Describe each group by every name it introduces, which is stabler than
    # the single display symbol.
    named = [{n for h in g for n in by_id[h].defines} for g in groups]

    def group_with(name):
        return next(g for g in named if name in g)

    assert len(groups) == 2, f"expected 2 commits, got {len(groups)}"

    auth_group = group_with("login_route")
    pay_group = group_with("checkout_route")

    assert auth_group is not pay_group, "the two features must not be merged"
    assert "verify_token" in auth_group
    assert "charge_card" in pay_group
    assert "charge_card" not in auth_group


def test_import_lines_follow_their_feature(repo: Path):
    """Each import must land with the feature that uses the name it brings in.

    This used to fail: `from .auth import ...` and `from .payments import ...`
    are adjacent lines, so git emitted them as one inseparable hunk. Import
    splitting in subhunks.py fixed it.
    """
    tangle(repo)
    _, files = load(repo)
    profiles = build_features(disk_source(str(repo)), files)
    graph, _ = build_graph(profiles, {})
    groups = cluster(graph)
    by_id = {p.hunk_id: p for p in profiles}

    for group in groups:
        text = "\n".join(
            ln for h in group for ln in by_id[h].hunk.added_lines
        )
        if "import verify_token" in text:
            assert "charge_card" not in text


# --- the graph algorithms themselves ---------------------------------------

def test_two_disconnected_clusters_are_found():
    from splitter.graphs import Graph, communities

    g = Graph()
    for a, b in [("a1", "a2"), ("a2", "a3"), ("a1", "a3")]:
        g.add_edge(a, b, 0.9)
    for a, b in [("b1", "b2"), ("b2", "b3"), ("b1", "b3")]:
        g.add_edge(a, b, 0.9)

    groups = communities(g)
    assert len(groups) == 2
    assert {"a1", "a2", "a3"} in [set(x) for x in groups]


def test_an_isolated_node_gets_its_own_group():
    """A hunk related to nothing deserves its own commit."""
    from splitter.graphs import Graph, communities

    g = Graph()
    g.add_edge("a1", "a2", 0.9)
    g.add_node("lonely")
    groups = communities(g)
    assert ["lonely"] in groups


def test_clustering_is_deterministic():
    """A tool that proposes a different split each run is not trustworthy."""
    from splitter.graphs import Graph, communities

    def build():
        g = Graph()
        for a, b, w in [("a", "b", 0.9), ("b", "c", 0.8), ("d", "e", 0.7)]:
            g.add_edge(a, b if b else b, w)
        return g

    g1, g2 = Graph(), Graph()
    for g in (g1, g2):
        for a, b, w in [("a", "b", 0.9), ("b", "c", 0.8), ("d", "e", 0.7)]:
            g.add_edge(a, b, w)
    assert communities(g1) == communities(g2)


def test_a_weak_bridge_does_not_merge_two_clusters():
    from splitter.graphs import Graph, communities

    g = Graph()
    for a, b in [("a1", "a2"), ("a2", "a3"), ("a1", "a3")]:
        g.add_edge(a, b, 0.95)
    for a, b in [("b1", "b2"), ("b2", "b3"), ("b1", "b3")]:
        g.add_edge(a, b, 0.95)
    g.add_edge("a1", "b1", 0.36)      # barely over the threshold

    assert len(communities(g)) == 2


def test_topological_order_respects_dependencies():
    from splitter.graphs import topological_order

    order, dropped = topological_order(3, [(2, 0, 1), (0, 1, 1)])
    assert order.index(2) < order.index(0) < order.index(1)
    assert dropped == []


def test_topological_order_is_stable_without_edges():
    from splitter.graphs import topological_order

    order, dropped = topological_order(4, [])
    assert order == [0, 1, 2, 3]
    assert dropped == []


def test_a_cycle_drops_its_weakest_edge_rather_than_failing():
    from splitter.graphs import topological_order

    order, dropped = topological_order(2, [(0, 1, 5), (1, 0, 1)])
    assert sorted(order) == [0, 1]
    assert dropped == [(1, 0)], "the weaker edge is the one to lose"


def test_louvain_beats_label_propagation_on_a_strong_bridge():
    """The reason Louvain is the default.

    Label propagation is purely local, so a single strong edge between two
    dense clusters drags them into one community. Louvain optimises a global
    modularity score and keeps them apart.
    """
    from splitter.graphs import Graph, label_propagation, louvain, modularity

    g = Graph()
    for a, b in [("a1", "a2"), ("a2", "a3"), ("a1", "a3")]:
        g.add_edge(a, b, 0.9)
    for a, b in [("b1", "b2"), ("b2", "b3"), ("b1", "b3")]:
        g.add_edge(a, b, 0.9)
    g.add_edge("a1", "b1", 0.9)

    assert len(louvain(g)) == 2
    assert len(label_propagation(g)) == 1
    assert modularity(g, louvain(g)) > modularity(g, label_propagation(g))


def test_modularity_of_a_pointless_grouping_is_about_zero():
    from splitter.graphs import Graph, modularity

    g = Graph()
    for a, b in [("a", "b"), ("b", "c"), ("c", "d")]:
        g.add_edge(a, b, 1.0)
    assert abs(modularity(g, [["a", "b", "c", "d"]])) < 0.01


def test_louvain_is_deterministic():
    from splitter.graphs import Graph, louvain

    def build():
        g = Graph()
        for a, b, w in [("a", "b", .9), ("b", "c", .8), ("c", "a", .7),
                        ("d", "e", .9), ("e", "f", .8), ("a", "d", .4)]:
            g.add_edge(a, b, w)
        return g

    assert louvain(build()) == louvain(build())


def test_unknown_algorithm_is_rejected():
    from splitter.graphs import Graph, communities

    g = Graph()
    g.add_edge("a", "b", 1.0)
    try:
        communities(g, "magic")
    except ValueError as exc:
        assert "magic" in str(exc)
    else:
        raise AssertionError("expected ValueError")
