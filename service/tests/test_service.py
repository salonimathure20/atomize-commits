"""Phase 4 tests: the git-free core and the HTTP service.

Nothing here touches a network or a container. The service is exercised with
Flask's test client, so these run in CI in under a second.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "service"))

os.environ.setdefault("SPLITTER_LLM", "none")

from splitter.core import analyze
from splitter.features import mapping_source
from splitter.gitrepo import Repo
from splitter.parser import parse_diff
from splitter.patch import build_patch
from splitter.subhunks import split_all

BASE = 'from flask import Flask\n\napp = Flask(__name__)\n\n\ndef health():\n    return {"ok": True}\n'

TANGLED = (
    "from flask import Flask\n\n"
    "from .auth import verify_token\n"
    "from .payments import charge_card\n\n"
    'app = Flask(__name__)\n\n\ndef health():\n    return {"ok": True}\n\n\n'
    "def login_route(request):\n"
    '    return verify_token(request.headers.get("Authorization"))\n\n\n'
    "def checkout_route(request):\n"
    '    return charge_card(request.json["card"], request.json["amount"])\n'
)

AUTH = 'import jwt\n\nAUTH_SECRET = "dev"\n\n\ndef verify_token(h):\n    return jwt.decode(h, AUTH_SECRET)\n'
PAY = "import stripe\n\n\ndef charge_card(card, amount):\n    return stripe.Charge.create(card=card, amount=amount)\n"


@pytest.fixture
def tangled(tmp_path: Path):
    """A short file where two features land close enough to share a hunk."""
    for cmd in (["git", "init", "-q", "."],
                ["git", "config", "user.email", "t@t.co"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "routes.py").write_text(BASE)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "app" / "routes.py").write_text(TANGLED)
    (tmp_path / "app" / "auth.py").write_text(AUTH)
    (tmp_path / "app" / "payments.py").write_text(PAY)
    subprocess.run(["git", "add", "-N", "."], cwd=tmp_path, check=True, capture_output=True)

    diff = subprocess.run(
        ["git", "--no-pager", "diff", "--no-color", "-U3"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    contents = {
        "app/routes.py": TANGLED,
        "app/auth.py": AUTH,
        "app/payments.py": PAY,
    }
    return tmp_path, diff, contents


# --- the regression this phase uncovered -----------------------------------

def test_one_hunk_holding_two_separate_edits_is_split(tangled):
    """A short file merges distant edits into a single hunk.

    The import block and the new routes are forty lines apart in a long file
    but share one hunk in a short one. Splitting used to bail whenever a hunk
    contained more than one block of added lines, which silently collapsed
    both features into a single commit.
    """
    _, diff, contents = tangled
    result = analyze(diff, source=mapping_source(contents))

    assert len(result.commits) == 2, "the two features must not be merged"

    defined = [{n for m in c.members for n in m.defines} for c in result.commits]
    auth = next(d for d in defined if "login_route" in d)
    pay = next(d for d in defined if "checkout_route" in d)
    assert "verify_token" in auth
    assert "charge_card" in pay
    assert auth is not pay


def test_split_pieces_still_apply_as_a_patch(tangled):
    repo_path, diff, _ = tangled
    files = split_all(parse_diff(diff))
    ids = {h.hunk_id for fd in files for h in fd.hunks}
    Repo(repo_path).apply_cached(build_patch(files, ids), check_only=True)


# --- the core without git --------------------------------------------------

def test_analyze_works_with_no_file_contents(tangled):
    """Symbol detection degrades, but the analysis must not fail."""
    _, diff, _ = tangled
    result = analyze(diff)
    assert result.commits


def test_analyze_on_empty_diff_returns_nothing():
    result = analyze("")
    assert result.commits == []
    assert result.warnings == []


def test_analysis_serialises_to_json(tangled):
    import json
    _, diff, contents = tangled
    payload = analyze(diff, source=mapping_source(contents)).to_dict()
    json.dumps(payload)          # must not raise
    commit = payload["commits"][0]
    assert commit["message"]["header"]
    assert commit["files"]
    assert commit["hunks"][0]["path"]


def test_content_keys_survive_reparsing(tangled):
    """Ids change once a commit lands; content keys are what we match on."""
    _, diff, contents = tangled
    result = analyze(diff, source=mapping_source(contents))
    assert all(c.content_keys() for c in result.commits)
    first, second = result.commits[0].content_keys(), result.commits[1].content_keys()
    assert not (first & second), "commits must not claim the same changes"


# --- the service -----------------------------------------------------------

@pytest.fixture
def client():
    from service.app import app
    app.config["TESTING"] = True
    return app.test_client()


def test_health_is_cheap_and_always_ok(client):
    body = client.get("/health").get_json()
    assert body["status"] == "ok"
    assert body["version"]


def test_ready_reports_model_state(client):
    body = client.get("/ready").get_json()
    assert body["status"] == "ok"
    assert "configured" in body["model"]


def test_analyze_endpoint_splits_a_real_diff(client, tangled):
    _, diff, contents = tangled
    response = client.post("/analyze", json={
        "diff": diff, "files": contents, "messages": False,
    })
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["commits"]) == 2
    assert all(c["message"]["header"] for c in body["commits"])


def test_analyze_rejects_a_missing_diff(client):
    assert client.post("/analyze", json={}).status_code == 400


def test_analyze_rejects_a_non_object_body(client):
    assert client.post("/analyze", json=["nope"]).status_code == 400


def test_analyze_rejects_bad_files_type(client, tangled):
    _, diff, _ = tangled
    response = client.post("/analyze", json={"diff": diff, "files": "not-a-dict"})
    assert response.status_code == 400


def test_analyze_rejects_an_oversized_diff(client):
    import service.app as service
    huge = "diff --git a/x b/x\n" + ("+x\n" * 10)
    original = service.MAX_DIFF_BYTES
    service.MAX_DIFF_BYTES = 10
    try:
        assert client.post("/analyze", json={"diff": huge}).status_code == 413
    finally:
        service.MAX_DIFF_BYTES = original


def test_analyze_works_without_file_contents(client, tangled):
    _, diff, _ = tangled
    response = client.post("/analyze", json={"diff": diff, "messages": False})
    assert response.status_code == 200
    assert response.get_json()["commits"]


def test_unknown_route_returns_json_not_html(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.get_json()["error"]
