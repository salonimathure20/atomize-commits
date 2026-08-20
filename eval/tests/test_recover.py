"""Tests for the evaluation harness.

The harness is what our quality claims rest on, so its scoring has to be
right. These check the metrics against cases where the answer is obvious by
inspection.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recover import adjusted_rand, compare, pair_set


def test_pairs_within_a_group_are_found():
    assert pair_set({"a": 0, "b": 0, "c": 1}) == {("a", "b")}


def test_a_perfect_split_scores_one():
    truth = {"a": 0, "b": 0, "c": 1, "d": 1}
    score = compare(truth, dict(truth))
    assert score.f1 == 1.0
    assert score.ari == 1.0


def test_labels_may_differ_as_long_as_the_grouping_matches():
    """Our 'commit 1' may be their 'commit 2'; only the partition matters."""
    truth = {"a": 0, "b": 0, "c": 1}
    predicted = {"a": 7, "b": 7, "c": 3}
    assert compare(truth, predicted).f1 == 1.0


def test_putting_everything_in_one_commit_has_perfect_recall():
    truth = {"a": 0, "b": 0, "c": 1, "d": 1}
    predicted = {"a": 0, "b": 0, "c": 0, "d": 0}
    score = compare(truth, predicted)
    assert score.recall == 1.0
    assert score.precision < 1.0, "it groups pairs that do not belong"


def test_splitting_every_hunk_apart_has_zero_recall():
    truth = {"a": 0, "b": 0, "c": 1, "d": 1}
    predicted = {"a": 0, "b": 1, "c": 2, "d": 3}
    score = compare(truth, predicted)
    assert score.recall == 0.0
    assert score.f1 == 0.0


def test_over_splitting_keeps_precision_but_loses_recall():
    """The failure mode our benchmark actually shows."""
    truth = {"a": 0, "b": 0, "c": 0, "d": 1, "e": 1, "f": 1}
    predicted = {"a": 0, "b": 0, "c": 1, "d": 2, "e": 2, "f": 3}
    score = compare(truth, predicted)
    assert score.precision == 1.0
    assert 0 < score.recall < 1


def test_random_grouping_scores_near_zero_on_ari():
    """This is why ARI is reported alongside F1.

    A grouping that is no better than chance still gets a respectable-looking
    F1, because most pairs genuinely belong apart. ARI corrects for that.
    """
    import random

    rng = random.Random(4)
    truth = {str(i): i % 3 for i in range(30)}
    predicted = {str(i): rng.randrange(3) for i in range(30)}

    score = compare(truth, predicted)
    assert abs(score.ari) < 0.2, "chance agreement must score near zero"
    assert score.f1 > 0.25, "yet F1 still looks respectable, which is the point"


def test_score_counts_groups_on_both_sides():
    score = compare({"a": 0, "b": 1, "c": 1}, {"a": 0, "b": 0, "c": 1})
    assert score.truth_groups == 2
    assert score.predicted_groups == 2
    assert score.hunks == 3


def test_a_single_hunk_cannot_be_scored():
    assert compare({"a": 0}, {"a": 0}).f1 == 0.0


def test_only_hunks_present_in_both_are_compared():
    score = compare({"a": 0, "b": 0, "z": 1}, {"a": 0, "b": 0})
    assert score.hunks == 2
    assert score.f1 == 1.0
