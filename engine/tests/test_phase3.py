"""Phase 3 tests: ordering, message quality, and model handling.

No network is used anywhere here. ScriptedProvider stands in for a real model,
which means these tests run in CI for free and stay deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from splitter.features import HunkFeatures
from splitter.llm import LLMError, ScriptedProvider, extract_json, redact
from splitter.messages import generate, heuristic_message, validate
from splitter.models import Hunk
from splitter.ordering import dependency_edges, missing_references, order_groups


def fake(path: str, defines=(), words=(), symbol="thing") -> HunkFeatures:
    hunk = Hunk(path=path, old_start=1, old_count=0, new_start=1,
                new_count=1, lines=["+x = 1"], order=0)
    return HunkFeatures(
        hunk=hunk,
        directory=path.rsplit("/", 1)[0] if "/" in path else ".",
        symbols=[symbol],
        defines=set(defines),
        words=set(words) | set(defines),
        is_test=path.startswith("test") or "/test" in path,
    )


# --- ordering --------------------------------------------------------------

def test_definition_group_sorts_before_its_user():
    creator = [fake("app/payments.py", defines=["charge_card"])]
    consumer = [fake("app/routes.py", words=["charge_card"])]

    order, warnings = order_groups([consumer, creator])   # deliberately backwards
    assert order.index(1) < order.index(0), "the group defining a name must go first"
    assert not warnings


def test_independent_groups_keep_stable_order():
    a = [fake("app/auth.py", defines=["verify_token"])]
    b = [fake("app/mail.py", defines=["send_mail"])]
    order, warnings = order_groups([a, b])
    assert order == [0, 1]
    assert not warnings


def test_mutual_dependency_is_broken_not_fatal():
    a = [fake("a.py", defines=["alpha"], words=["beta"])]
    b = [fake("b.py", defines=["beta"], words=["alpha"])]
    order, warnings = order_groups([a, b])
    assert len(order) == 2
    assert warnings and "depend on each other" in warnings[0]


def test_dependency_edges_ignore_self_definition():
    """A group that defines and uses a name owes nothing to anyone."""
    a = [fake("a.py", defines=["shared"])]
    b = [fake("b.py", defines=["shared"], words=["shared"])]
    assert dependency_edges([a, b]) == []


def test_missing_reference_is_reported():
    consumer = [fake("app/routes.py", words=["charge_card"])]
    creator = [fake("app/payments.py", defines=["charge_card"])]
    problems = missing_references([consumer, creator])   # wrong order on purpose
    assert problems and "charge_card" in problems[0][1]


# --- message validation ----------------------------------------------------

def test_validate_accepts_a_good_message():
    msg = validate({"type": "feat", "scope": "auth", "subject": "add jwt login"})
    assert msg.header() == "feat(auth): add jwt login"


def test_validate_rejects_unknown_type():
    assert validate({"type": "banana", "subject": "do a thing"}) is None


def test_validate_rejects_empty_subject():
    assert validate({"type": "feat", "subject": "   "}) is None


def test_validate_normalises_capital_and_period():
    msg = validate({"type": "fix", "scope": None, "subject": "Correct the offset."})
    assert msg.subject == "correct the offset"
    assert msg.header() == "fix: correct the offset"


def test_validate_truncates_long_subjects():
    long = "add " + "very " * 40 + "long thing"
    msg = validate({"type": "feat", "subject": long})
    assert len(msg.header()) <= 72


def test_body_is_wrapped():
    msg = validate({"type": "feat", "subject": "add x", "body": "why " * 60})
    assert all(len(line) <= 72 for line in msg.full().split("\n"))


# --- json extraction -------------------------------------------------------

def test_extract_json_survives_code_fences():
    assert extract_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]


def test_extract_json_survives_chatty_preamble():
    reply = 'Sure! Here you go:\n[{"index": 0, "type": "feat"}]\nHope that helps.'
    assert extract_json(reply)[0]["index"] == 0


def test_extract_json_raises_on_garbage():
    try:
        extract_json("I would rather not.")
    except LLMError:
        return
    raise AssertionError("expected LLMError")


# --- secret handling -------------------------------------------------------

def test_redact_masks_query_string_keys():
    url = "https://api.example.com/v1/x?key=AQ.Ab8RN6JlSECRETVALUE1234"
    assert "SECRET" not in redact(url)
    assert redact(url).endswith("key=***")


def test_redact_masks_bare_tokens():
    assert "mockmock" not in redact("failed with AIzaSyDmockmockmockmockmock")
    assert "abc123" not in redact("failed with sk-proj-abc123def456ghi789")


def test_redact_leaves_ordinary_text_alone():
    assert redact("could not reach localhost:11434") == "could not reach localhost:11434"


def test_gemini_key_is_never_put_in_the_url():
    """Query strings leak into shell history, proxy logs, and error messages."""
    import inspect
    from splitter.llm import GeminiProvider
    source = inspect.getsource(GeminiProvider.complete)
    assert "key={self.api_key}" not in source
    assert "x-goog-api-key" in source


# --- generation ------------------------------------------------------------

def test_model_messages_are_used_when_valid():
    groups = [[fake("app/auth.py", defines=["verify_token"])]]
    provider = ScriptedProvider([
        '[{"index": 0, "type": "feat", "scope": "auth", '
        '"subject": "add token verification", "body": null}]'
    ])
    messages, warning = generate(groups, provider)
    assert messages[0].header() == "feat(auth): add token verification"
    assert messages[0].generated_by == "model"
    assert warning is None


def test_falls_back_when_model_returns_garbage():
    groups = [[fake("app/auth.py", defines=["verify_token"])]]
    messages, warning = generate(groups, ScriptedProvider(["not json at all"]))
    assert messages[0].generated_by == "heuristic"
    assert warning and "heuristic" in warning


def test_falls_back_when_model_invents_a_bad_type():
    groups = [[fake("app/auth.py", defines=["verify_token"])]]
    provider = ScriptedProvider(['[{"index": 0, "type": "nonsense", "subject": "x"}]'])
    messages, warning = generate(groups, provider)
    assert messages[0].generated_by == "heuristic"


def test_out_of_range_index_is_ignored():
    groups = [[fake("app/auth.py", defines=["verify_token"])]]
    provider = ScriptedProvider(['[{"index": 7, "type": "feat", "subject": "x"}]'])
    messages, _ = generate(groups, provider)
    assert messages[0].generated_by == "heuristic"


def test_no_provider_means_no_network_call():
    groups = [[fake("app/auth.py", defines=["verify_token"])]]
    messages, warning = generate(groups, None)
    assert messages[0].generated_by == "heuristic"
    assert warning is None


def test_prompt_contains_the_code_but_stays_small():
    groups = [[fake("app/auth.py", defines=["verify_token"], words=["jwt"])]]
    provider = ScriptedProvider(['[{"index":0,"type":"feat","subject":"add auth"}]'])
    generate(groups, provider)
    _, user_prompt = provider.calls[0]
    assert "app/auth.py" in user_prompt
    assert "verify_token" in user_prompt
    assert len(user_prompt) < 4000


# --- heuristic fallback ----------------------------------------------------

def test_heuristic_marks_test_only_groups():
    msg = heuristic_message([fake("tests/test_auth.py", defines=["test_login"])])
    assert msg.type == "test"


def test_heuristic_always_produces_valid_output():
    msg = heuristic_message([fake("app/x.py")])
    assert msg.type in ("feat", "fix", "refactor", "test", "chore")
    assert msg.subject
    assert len(msg.header()) <= 72
