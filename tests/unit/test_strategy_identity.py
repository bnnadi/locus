"""Identity must come from caller-controlled fields, not model output."""

import importlib.util
from pathlib import Path

_IDENTITY = Path(__file__).resolve().parents[2] / "services" / "hermes-memory-router" / "identity.py"


def _load():
    spec = importlib.util.spec_from_file_location("identity", _IDENTITY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


identity = _load()


def test_same_reasoning_same_id():
    kwargs = {
        "task_type": "code_review",
        "raw_reasoning": "Used early returns to cut cyclomatic complexity.",
    }
    assert identity.strategy_id_for(**kwargs) == identity.strategy_id_for(**kwargs)


def test_whitespace_and_case_do_not_fork_identity():
    a = identity.strategy_id_for(
        task_type="Code_Review",
        raw_reasoning="  Used   early returns.  ",
    )
    b = identity.strategy_id_for(
        task_type="code_review",
        raw_reasoning="Used early returns.",
    )
    assert a == b


def test_different_reasoning_different_id():
    a = identity.strategy_id_for(task_type="code_review", raw_reasoning="use a guard clause")
    b = identity.strategy_id_for(task_type="code_review", raw_reasoning="use a nested if")
    assert a != b


def test_title_is_not_part_of_identity():
    # Descriptive fields the model fills in must not move the key.
    base = identity.strategy_id_for(task_type="code_review", raw_reasoning="guard clause")
    # strategy_key is the only override, and it is caller-owned.
    keyed = identity.strategy_id_for(
        task_type="code_review",
        raw_reasoning="guard clause",
        strategy_key="caller-stable-key",
    )
    assert keyed != base
    assert keyed == identity.strategy_id_for(
        task_type="other",
        raw_reasoning="totally different",
        strategy_key="caller-stable-key",
    )


def test_ids_are_prefixed():
    sid = identity.strategy_id_for(task_type="t", raw_reasoning="r")
    assert sid.startswith("strategy_")
    assert len(sid) == len("strategy_") + 16


def test_zero_is_a_real_min_success_rate_threshold():
    assert identity.below_min_success_rate(-0.1, 0.0) is True
    assert identity.below_min_success_rate(0.0, 0.0) is False
    assert identity.below_min_success_rate(0.5, 0.0) is False


def test_omitted_min_success_rate_does_not_filter():
    assert identity.below_min_success_rate(0.0, None) is False
    assert identity.below_min_success_rate(None, None) is False
