# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for offline token counting."""

from __future__ import annotations

import pytest

from plinth import tokens


def test_count_empty_string():
    assert tokens.count("") == 0


def test_count_simple_string():
    # "hello world" tokenises to a small, fixed number of tokens. We
    # don't pin the exact count to avoid breaking on tiktoken updates,
    # but we do assert a sensible bound.
    n = tokens.count("hello world")
    assert 1 <= n <= 5


def test_count_is_deterministic():
    s = "The quick brown fox jumps over the lazy dog."
    assert tokens.count(s) == tokens.count(s)


def test_count_scales_with_length():
    short = tokens.count("hello")
    long = tokens.count("hello " * 100)
    assert long > short * 50  # ~100x growth


def test_count_known_value_for_tokenizer_sanity():
    # cl100k_base tokenises common ASCII words into single tokens. We
    # take "tokenization" — six unicode chars but should be 2 tokens
    # ("token", "ization") in cl100k_base.
    assert tokens.count("tokenization") == 2


def test_estimate_cost_zero_when_no_tokens():
    assert tokens.estimate_cost(0, 0) == 0.0


def test_estimate_cost_input_only():
    # 1M input tokens at $3/M → $3.
    assert tokens.estimate_cost(1_000_000, 0) == pytest.approx(3.0)


def test_estimate_cost_input_and_output():
    # 1M input + 1M output → $3 + $15 = $18.
    assert tokens.estimate_cost(1_000_000, 1_000_000) == pytest.approx(18.0)


def test_estimate_cost_small_quantities():
    # 1k prompt + 500 completion.
    cost = tokens.estimate_cost(1000, 500)
    assert cost == pytest.approx(1000 * 3 / 1_000_000 + 500 * 15 / 1_000_000)


def test_estimate_cost_rejects_negatives():
    with pytest.raises(ValueError):
        tokens.estimate_cost(-1, 0)
    with pytest.raises(ValueError):
        tokens.estimate_cost(0, -1)
