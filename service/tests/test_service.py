"""Service tests, FastAPI version.

Nothing here touches a network or a container. TestClient drives the app
in-process, so these run in CI in about a second.

One behaviour changed in the port: a malformed request body now returns 422
instead of 400. That is FastAPI's convention for a body that failed schema
validation, and it distinguishes "your JSON was the wrong shape" from "your
request was too big" (413), which is more information than the Flask version
gave. No client depended on the old code — the extension talks to the CLI, not
to this service.
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

from fastapi.testclient import TestClient  # noqa: E402

from splitter.core import analyze  # noqa: E402
from splitter.features import mapping_source  # noqa: E402
from splitter.gitrepo import Repo  # noqa: E402
from splitter.parser import parse_diff  # noqa: E402
from splitter.patch import build_patch  # noqa: E402
from splitter.subhunks import split_all  # noqa: E402

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


@pytest.fixture
def client():
    from app import app
    return TestClient(app)


# --- the regression this project keeps rediscovering -----------------------

def test_one_hunk_holding_two_separate_edits_is_split(tangled):
    """A short file merges distant edits into a single hunk.

    Splitting used to bail whenever a hunk contained more than one block of
    added lines, which silently collapsed both features into one commit.
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
    assert analyze(diff).commits


def test_analyze_on_empty_diff_returns_nothing():
    result = analyze("")
    assert result.commits == []
    assert result.warnings == []


def test_analysis_serialises_to_json(tangled):
    import json
    _, diff, contents = tangled
    payload = analyze(diff, source=mapping_source(contents)).to_dict()
    json.dumps(payload)
    commit = payload["commits"][0]
    assert commit["message"]["header"]
    assert commit["files"]
    assert commit["hunks"][0]["path"]


def test_content_keys_survive_reparsing(tangled):
    """Ids change once a commit lands; content keys are what we match on."""
    _, diff, contents = tangled
    result = analyze(diff, source=mapping_source(contents))
    first, second = result.commits[0].content_keys(), result.commits[1].content_keys()
    assert first and second
    assert not (first & second), "commits must not claim the same changes"


# --- probes ----------------------------------------------------------------

def test_health_is_cheap_and_always_ok(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"]


def test_ready_reports_model_state(client):
    body = client.get("/ready").json()
    assert body["status"] == "ok"
    assert "configured" in body["model"]


# --- analyze ---------------------------------------------------------------

def test_analyze_endpoint_splits_a_real_diff(client, tangled):
    _, diff, contents = tangled
    response = client.post("/analyze", json={
        "diff": diff, "files": contents, "messages": False,
    })
    assert response.status_code == 200
    body = response.json()
    assert len(body["commits"]) == 2
    assert all(c["message"]["header"] for c in body["commits"])
    assert all(c["content_key"] for c in body["commits"])


def test_analyze_works_without_file_contents(client, tangled):
    _, diff, _ = tangled
    response = client.post("/analyze", json={"diff": diff, "messages": False})
    assert response.status_code == 200
    assert response.json()["commits"]


def test_analyze_accepts_co_change_pairs(client, tangled):
    _, diff, contents = tangled
    response = client.post("/analyze", json={
        "diff": diff,
        "files": contents,
        "co_change": [["app/auth.py", "app/routes.py", 0.9]],
        "messages": False,
    })
    assert response.status_code == 200


# --- validation, now handled by the schema ---------------------------------

def test_missing_diff_is_rejected(client):
    assert client.post("/analyze", json={}).status_code == 422


def test_blank_diff_is_rejected(client):
    assert client.post("/analyze", json={"diff": "   "}).status_code == 422


def test_wrong_type_for_files_is_rejected(client, tangled):
    _, diff, _ = tangled
    response = client.post("/analyze", json={"diff": diff, "files": "not-a-dict"})
    assert response.status_code == 422


def test_malformed_co_change_is_rejected(client, tangled):
    _, diff, _ = tangled
    response = client.post("/analyze", json={
        "diff": diff, "co_change": [["only-one-path"]],
    })
    assert response.status_code == 422


def test_unknown_field_is_rejected(client, tangled):
    """extra='forbid' turns a typo into an error instead of silence."""
    _, diff, _ = tangled
    response = client.post("/analyze", json={"diff": diff, "mesages": False})
    assert response.status_code == 422


def test_oversized_diff_is_rejected_with_413(client):
    """Size limits stay distinct from schema errors."""
    import app as service
    original = service.MAX_DIFF_BYTES
    service.MAX_DIFF_BYTES = 10
    try:
        response = client.post("/analyze", json={"diff": "diff --git a/x b/x\n+x\n"})
        assert response.status_code == 413
        assert "error" in response.json()
    finally:
        service.MAX_DIFF_BYTES = original


def test_too_many_files_is_rejected_with_413(client, tangled):
    _, diff, _ = tangled
    import app as service
    original = service.MAX_FILES
    service.MAX_FILES = 1
    try:
        response = client.post("/analyze", json={
            "diff": diff, "files": {"a.py": "x", "b.py": "y"},
        })
        assert response.status_code == 413
    finally:
        service.MAX_FILES = original


def test_unknown_route_returns_json_not_html(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


# --- generated documentation ------------------------------------------------

def test_openapi_document_is_generated(client):
    """The schemas are the API docs. If this breaks, /docs is wrong too."""
    spec = client.get("/openapi.json").json()
    assert "/analyze" in spec["paths"]
    assert "/health" in spec["paths"]
    assert spec["info"]["title"] == "Commit Splitter"


def test_analyze_is_documented_with_its_error_codes(client):
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/analyze"]["post"]["responses"]
    for code in ("200", "413", "422", "500"):
        assert code in responses


def test_docs_page_is_served(client):
    assert client.get("/docs").status_code == 200
