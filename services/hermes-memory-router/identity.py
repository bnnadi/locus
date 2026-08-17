"""Stable StrategyItem identity.

Primary keys must come from caller-controlled fields, never from model-generated
prose. Hashing an LLM title makes MERGE insert a new node on every ingest
instead of reinforcing counters. See the never-derive-identity-from-model-output
pattern.
"""

from __future__ import annotations

import hashlib
import re

_WHITESPACE = re.compile(r"\s+")


def normalize_reasoning(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip().lower())


def strategy_id_for(
    *,
    task_type: str,
    raw_reasoning: str,
    strategy_key: str | None = None,
) -> str:
    if strategy_key and strategy_key.strip():
        material = strategy_key.strip()
    else:
        material = f"{task_type.strip().lower()}\n{normalize_reasoning(raw_reasoning)}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"strategy_{digest}"


def below_min_success_rate(
    success_rate: float | None,
    min_success_rate: float | None,
) -> bool:
    """True when the strategy should be dropped for falling under the floor.

    A threshold of 0.0 is a real filter (keep rates >= 0). It must not be
    treated as "no filter" via a truthiness check.
    """
    if min_success_rate is None:
        return False
    rate = 0.0 if success_rate is None else success_rate
    return rate < min_success_rate
