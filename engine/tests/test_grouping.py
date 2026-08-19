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

from splitter.features import build_features
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
    profiles = build_features(str(repo), files)
    routes = [p for p in profiles if p.path.endswith("routes.py") and p.symbol]
    login = [p for p in routes if p.symbol == "login_route"]
    checkout = [p for p in routes if p.symbol == "checkout_route"]
    assert login and checkout

    score, _ = score_pair(login[0], checkout[0])
    assert score < 0.35, "unrelated routes in one file must not be glued together"


def test_definition_and_its_user_score_high(repo: Path):
    tangle(repo)
    _, files = load(repo)
    profiles = build_features(str(repo), files)
    auth = [p for p in profiles if p.path.endswith("auth.py")][0]
    login = [p for p in profiles if p.symbol == "login_route"][0]
    score, reasons = score_pair(auth, login)
    assert score >= 0.35
    assert any("verify_token" in r for r in reasons)


# --- the headline test -----------------------------------------------------

def test_tangled_file_splits_by_feature(repo: Path):
    tangle(repo)
    _, files = load(repo)
    profiles = build_features(str(repo), files)
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
    profiles = build_features(str(repo), files)
    graph, _ = build_graph(profiles, {})
    groups = cluster(graph)
    by_id = {p.hunk_id: p for p in profiles}

    for group in groups:
        text = "\n".join(
            ln for h in group for ln in by_id[h].hunk.added_lines
        )
        if "import verify_token" in text:
            assert "charge_card" not in text
