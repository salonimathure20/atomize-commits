"""Tests for the pull request hygiene check.

The service is stubbed with a real HTTP server on a local port rather than
mocked, so the client's request building, JSON handling, and error paths are
genuinely exercised. No network, no container, no GitHub.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hygiene


SAMPLE = {
    "commits": [
        {
            "content_key": ["import stripe"],
            "message": {"header": "feat(payments): add stripe charging",
                        "full": "feat(payments): add stripe charging"},
            "files": ["app/payments.py", "app/routes.py"],
            "additions": 12,
            "deletions": 0,
            "reasons": ["uses charge_card defined by the other"],
            "hunks": [
                {"id": "a#1", "path": "app/payments.py", "symbol": "charge_card",
                 "start_line": 1, "additions": 5, "deletions": 0},
                {"id": "a#2", "path": "app/routes.py", "symbol": "checkout_route",
                 "start_line": 20, "additions": 7, "deletions": 0},
            ],
        },
        {
            "content_key": ["import jwt"],
            "message": {"header": "feat(auth): add token verification",
                        "full": "feat(auth): add token verification"},
            "files": ["app/auth.py"],
            "additions": 7,
            "deletions": 0,
            "reasons": [],
            "hunks": [
                {"id": "b#1", "path": "app/auth.py", "symbol": "verify_token",
                 "start_line": 1, "additions": 7, "deletions": 0},
            ],
        },
    ],
    "warnings": ["commit 2 references charge_card before it exists"],
    "unsplittable": [],
}


class StubHandler(BaseHTTPRequestHandler):
    status = 200
    captured: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        StubHandler.captured = json.loads(raw)

        self.send_response(StubHandler.status)
        self.send_header("Content-Type", "application/json")
        body = (json.dumps(SAMPLE) if StubHandler.status == 200
                else json.dumps({"error": "nope"})).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass          # keep test output clean


@pytest.fixture
def stub():
    StubHandler.status = 200
    StubHandler.captured = {}
    server = HTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", StubHandler
    server.shutdown()
    server.server_close()


# --- talking to the service -------------------------------------------------

def test_client_gets_a_result_back(stub):
    url, _ = stub
    result = hygiene.analyze_via_service(url, "diff --git a/x b/x\n", {})
    assert len(result["commits"]) == 2


def test_client_sends_the_diff_and_files(stub):
    url, handler = stub
    hygiene.analyze_via_service(url, "THE-DIFF", {"a.py": "code"})
    assert handler.captured["diff"] == "THE-DIFF"
    assert handler.captured["files"] == {"a.py": "code"}


def test_messages_flag_is_forwarded(stub):
    url, handler = stub
    hygiene.analyze_via_service(url, "d", {}, messages=False)
    assert handler.captured["messages"] is False


def test_service_error_becomes_a_readable_message(stub):
    url, handler = stub
    handler.status = 500
    with pytest.raises(RuntimeError, match="service returned 500"):
        hygiene.analyze_via_service(url, "d", {})


def test_unreachable_service_is_a_readable_message():
    with pytest.raises(RuntimeError, match="could not reach"):
        hygiene.analyze_via_service("http://127.0.0.1:1", "d", {}, timeout=2)


# --- deciding whether to speak ---------------------------------------------

def test_a_single_coherent_change_gets_no_comment():
    assert hygiene.should_report({"commits": [SAMPLE["commits"][0]]}) is False


def test_two_unrelated_changes_get_a_comment():
    assert hygiene.should_report(SAMPLE) is True


def test_threshold_is_configurable():
    assert hygiene.should_report(SAMPLE, min_commits=3) is False


def test_empty_result_gets_no_comment():
    assert hygiene.should_report({"commits": []}) is False


# --- the comment -----------------------------------------------------------

def test_comment_carries_the_marker_so_it_can_be_updated():
    assert hygiene.render_comment(SAMPLE).startswith(hygiene.MARKER)


def test_comment_states_the_number_of_changes():
    assert "contains 2 unrelated changes" in hygiene.render_comment(SAMPLE)


def test_comment_lists_every_proposed_message():
    body = hygiene.render_comment(SAMPLE)
    assert "feat(payments): add stripe charging" in body
    assert "feat(auth): add token verification" in body


def test_comment_names_the_functions_involved():
    body = hygiene.render_comment(SAMPLE)
    assert "checkout_route" in body
    assert "verify_token" in body


def test_comment_explains_the_grouping():
    assert "uses charge_card defined by the other" in hygiene.render_comment(SAMPLE)


def test_warnings_are_tucked_into_a_details_block():
    body = hygiene.render_comment(SAMPLE)
    assert "<details>" in body
    assert "before it exists" in body


def test_comment_says_it_is_not_a_blocker():
    assert "not a blocker" in hygiene.render_comment(SAMPLE)


def test_long_file_lists_are_truncated():
    plan = {"commits": [dict(SAMPLE["commits"][0],
                             files=[f"f{i}.py" for i in range(20)])],
            "warnings": [], "unsplittable": []}
    body = hygiene.render_comment(plan)
    assert "and 12 more" in body


def test_comment_survives_a_commit_with_no_message():
    plan = {"commits": [{"files": [], "hunks": []}], "warnings": [], "unsplittable": []}
    assert "(no message)" in hygiene.render_comment(plan)


# --- reading files ---------------------------------------------------------

def test_read_files_skips_missing_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real.py").write_text("x = 1\n")
    contents = hygiene.read_files(["real.py", "gone.py"])
    assert contents == {"real.py": "x = 1\n"}


def test_read_files_skips_oversized_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.py").write_text("x" * (hygiene.MAX_FILE_BYTES + 1))
    (tmp_path / "small.py").write_text("y = 2\n")
    contents = hygiene.read_files(["big.py", "small.py"])
    assert "big.py" not in contents
    assert "small.py" in contents


def test_read_files_skips_binaries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    assert hygiene.read_files(["blob.bin"]) == {}


# --- failure behaviour -----------------------------------------------------

def test_an_unreachable_service_does_not_fail_the_build(monkeypatch, tmp_path):
    """A broken analysis must never block someone's merge."""
    monkeypatch.setattr(hygiene, "diff_text", lambda base, head: "diff --git a/x b/x\n")
    monkeypatch.setattr(hygiene, "changed_paths", lambda base, head: [])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    exit_code = hygiene.main([
        "--service", "http://127.0.0.1:1", "--base", "a", "--head", "b", "--dry-run",
    ])
    assert exit_code == 0


def test_dry_run_prints_and_posts_nothing(stub, monkeypatch, capsys):
    url, _ = stub
    monkeypatch.setattr(hygiene, "diff_text", lambda base, head: "diff --git a/x b/x\n")
    monkeypatch.setattr(hygiene, "changed_paths", lambda base, head: [])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("dry run must not touch GitHub")

    monkeypatch.setattr(hygiene, "upsert_comment", explode)
    assert hygiene.main(["--service", url, "--dry-run"]) == 0
    assert "unrelated changes" in capsys.readouterr().out


def test_fail_over_can_make_the_check_strict(stub, monkeypatch):
    url, _ = stub
    monkeypatch.setattr(hygiene, "diff_text", lambda base, head: "diff --git a/x b/x\n")
    monkeypatch.setattr(hygiene, "changed_paths", lambda base, head: [])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert hygiene.main(["--service", url, "--dry-run", "--fail-over", "1"]) == 1


def test_no_changes_exits_quietly(monkeypatch):
    monkeypatch.setattr(hygiene, "diff_text", lambda base, head: "")
    assert hygiene.main(["--service", "http://unused"]) == 0
