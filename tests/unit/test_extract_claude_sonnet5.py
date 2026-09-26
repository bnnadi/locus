"""extract_claude must join text-typed content blocks and disable thinking sampling."""

import json
import os
import re
import sys
import types
from pathlib import Path

os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "ci-not-a-real-password")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")

_ROUTER_DIR = str(Path(__file__).resolve().parents[2] / "services" / "hermes-memory-router")
if _ROUTER_DIR not in sys.path:
    sys.path.insert(0, _ROUTER_DIR)

import main  # noqa: E402  (must follow env setup and sys.path insert)


def _trace() -> main.ReasoningTraceIn:
    return main.ReasoningTraceIn(
        trace_id="trace-1",
        task_id="task-1",
        task_type="code_review",
        raw_reasoning="did a thing",
        outcome="success",
    )


def test_extract_claude_thinking_block_before_text_parses_strategy_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-live")

    thinking_block = types.SimpleNamespace(type="thinking", thinking='{"title": "from-thinking"}')
    text_block_1 = types.SimpleNamespace(
        type="text",
        text='{"title": "from-text", "description": "d", "conditions": {"k": "v"}, "steps": ["on',
    )
    text_block_2 = types.SimpleNamespace(type="text", text='e"], "success_rate": 0.25}')

    fake_response = types.SimpleNamespace(content=[thinking_block, text_block_1, text_block_2])

    create_calls = []

    class _FakeMessages:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            return fake_response

    class _FakeAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = _FakeMessages()

    monkeypatch.setattr(main, "Anthropic", _FakeAnthropic)

    result = main.extract_claude(_trace())

    assert result == {
        "title": "from-text",
        "description": "d",
        "conditions": {"k": "v"},
        "steps": ["one"],
        "success_rate": 0.25,
    }


def test_extract_claude_request_omits_sampling_parameters(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-live")

    text_block = types.SimpleNamespace(
        type="text",
        text=json.dumps(
            {
                "title": "t",
                "description": "d",
                "conditions": {},
                "steps": ["s"],
                "success_rate": 1.0,
            }
        ),
    )
    fake_response = types.SimpleNamespace(content=[text_block])

    create_calls = []

    class _FakeMessages:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            return fake_response

    class _FakeAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = _FakeMessages()

    monkeypatch.setattr(main, "Anthropic", _FakeAnthropic)

    main.extract_claude(_trace())

    assert len(create_calls) == 1
    kwargs = create_calls[0]
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "top_k" not in kwargs
    assert kwargs["thinking"] == {"type": "disabled"}
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["max_tokens"] == 500


def test_extract_claude_sdk_pin_accepts_thinking():
    requirements_path = (
        Path(__file__).resolve().parents[2] / "services" / "hermes-memory-router" / "requirements.txt"
    )
    requirements_text = requirements_path.read_text()

    match = re.search(r"^anthropic==(\d+)\.(\d+)\.(\d+)$", requirements_text, re.MULTILINE)
    assert match is not None, "anthropic pin not found in requirements.txt"

    major, minor, patch = (int(part) for part in match.groups())

    assert (major, minor, patch) >= (0, 47, 0)
    assert major < 1
