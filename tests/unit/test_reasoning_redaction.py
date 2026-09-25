"""Reasoning traces must have credentials stripped before persistence.

Loads redact.py the same way test_strategy_identity.py loads identity.py:
via importlib from a file path, never through main.py (main.py requires
Neo4j/Qdrant env vars at import time and this module has nothing to do
with that boundary).
"""

import importlib.util
from pathlib import Path

_ROUTER_DIR = Path(__file__).resolve().parents[2] / "services" / "hermes-memory-router"
_REDACT = _ROUTER_DIR / "redact.py"
_IDENTITY = _ROUTER_DIR / "identity.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Module-level load: redact.py does not exist yet on this branch, so this
# raises FileNotFoundError at collection time. That is the intended failure
# for every test below -- none of them are expected to run until the
# redactor is implemented.
redact = _load(_REDACT, "redact")
identity = _load(_IDENTITY, "identity")


REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# Fixtures sized to the documented boundaries. Do not shrink these below the
# stated minimums -- e.g. password=hunter2 (7 chars) is a documented miss,
# not a success case.
# ---------------------------------------------------------------------------

BEARER_TOKEN = "AbcdEfghIjklMnopQrst0123"  # 24 chars, class [A-Za-z0-9._~+/-]
AKIA_KEY = "AKIAIOSFODNN7EXAMPLE"  # AKIA + 16 uppercase alphanumerics
# Unique tail of the AKIA fixture -- "XAMPLE" appears nowhere else in this
# file, so a buggy redactor that only strips the AKIA prefix and a short
# run (leaving this suffix behind) is caught instead of hidden by the fact
# that the *full* literal string is technically no longer contiguous.
AKIA_SUFFIX = AKIA_KEY[-6:]
GHP_36 = "a" * 36
GHP_TOKEN = f"ghp_{GHP_36}"
GHP_37 = "b" * 37
GHP_OVERLONG = f"ghp_{GHP_37}"
# Unique (non-repeating) tail so a partial-prefix match that leaves the rest
# of the run in place can't hide behind "the full literal string is absent".
FINE_GRAINED_TAIL = "Nq7Zk2"
FINE_GRAINED_82 = ("c" * 76) + FINE_GRAINED_TAIL  # 82 total, [A-Za-z0-9_]
FINE_GRAINED_TOKEN = f"github_pat_{FINE_GRAINED_82}"
SLACK_TAIL = "Zz9Q1w"
SLACK_TOKEN = f"xoxb-{'A' * 54}{SLACK_TAIL}"  # 60 chars after the prefix
# Unique tail, not a run of identical characters -- a redactor that matches
# only `sk-ant` (stopping at the second hyphen, à la OpenAI's `sk-` class)
# would leave "-" + this tail behind, and a repeated-char tail could look
# absent by coincidence even when characters from it remain.
ANTHROPIC_TAIL = "Zx9Qa7"
ANTHROPIC_KEY = f"sk-ant-{'d' * 18}{ANTHROPIC_TAIL}"  # 24 chars total (the minimum)
# Same reasoning as ANTHROPIC_TAIL: unique, not a repeated run.
OPENAI_PROJECT_TAIL = "Qw3nZp"
OPENAI_PROJECT_KEY = f"sk-proj-{'e' * 14}{OPENAI_PROJECT_TAIL}"  # >= 20 total
JWT = (
    "eyJhbGciOiJIUzI1NiJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)

PEM_CLOSED = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAK6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6P\n"
    "-----END RSA PRIVATE KEY-----"
)

# High-entropy token used both for the protruding-overlap test and for the
# "same token, no keyword context" negative test. Length >= 32, entropy
# clearly above the 3.5 gate (mixed case + digits, no repeats).
HIGH_ENTROPY_TOKEN = "QrstUvwxyz0123456789ABCDefghIJKL"  # 33 chars


def test_bearer_token_is_redacted():
    text = f"Authorization: Bearer {BEARER_TOKEN}"
    out = redact.redact_reasoning(text)
    assert BEARER_TOKEN not in out
    assert REDACTED in out


def test_aws_access_key_is_redacted():
    text = f"exported AWS_ACCESS_KEY_ID={AKIA_KEY} to the shell"
    out = redact.redact_reasoning(text)
    # AKIA_KEY not in out is necessary but not sufficient: a redactor that
    # only strips a short prefix would already make the full literal
    # string non-contiguous while leaving most of the key behind. The
    # unique suffix pins down that the whole token, not just its head, is
    # gone.
    assert AKIA_KEY not in out
    assert AKIA_SUFFIX not in out
    assert REDACTED in out


def test_github_classic_pat_is_redacted():
    text = f"used {GHP_TOKEN} to clone the repo"
    out = redact.redact_reasoning(text)
    assert GHP_TOKEN not in out
    assert REDACTED in out


def test_github_fine_grained_pat_is_redacted():
    text = f"rotated to {FINE_GRAINED_TOKEN} for CI"
    out = redact.redact_reasoning(text)
    # Same rationale as the AKIA suffix check above: FINE_GRAINED_TOKEN not
    # in out alone would pass even if most of the 82-char run survived,
    # because the fixture is mostly one repeated character. The unique
    # tail can't hide behind that.
    assert FINE_GRAINED_TOKEN not in out
    assert FINE_GRAINED_TAIL not in out
    assert REDACTED in out


def test_anthropic_key_is_redacted():
    text = f"set ANTHROPIC_API_KEY={ANTHROPIC_KEY} before retrying"
    out = redact.redact_reasoning(text)
    # ANTHROPIC_KEY not in out is a weak check on its own: a redactor that
    # only implements `sk-` + alphanumerics matches "sk-ant" and stops at
    # the next hyphen, so the literal full-key string is technically no
    # longer contiguous while "-" + almost the entire tail remains. The
    # unique tail assertion catches that.
    assert ANTHROPIC_KEY not in out
    assert ANTHROPIC_TAIL not in out
    assert REDACTED in out


def test_closed_pem_block_is_redacted():
    text = f"dumped the key:\n{PEM_CLOSED}\nthen continued"
    out = redact.redact_reasoning(text)
    assert "MIIBOgIBAAJBAK6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6P" not in out
    assert REDACTED in out


def test_jwt_is_redacted():
    text = f"the session token was {JWT}"
    out = redact.redact_reasoning(text)
    assert JWT not in out
    assert REDACTED in out


def test_slack_token_over_old_cap_is_fully_consumed():
    # A regex with an upper bound of 48 chars after the prefix would leave
    # this tail behind. The contract has no upper cap for Slack tokens.
    text = f"posted via {SLACK_TOKEN} webhook"
    out = redact.redact_reasoning(text)
    assert SLACK_TAIL not in out
    assert SLACK_TOKEN not in out


def test_openai_project_key_tail_is_redacted():
    text = f"OPENAI_API_KEY={OPENAI_PROJECT_KEY} in the env file"
    out = redact.redact_reasoning(text)
    assert OPENAI_PROJECT_TAIL not in out
    assert REDACTED in out


def test_overlong_github_classic_token_is_a_total_miss():
    # 37+ alphanumerics after ghp_ must not match at all -- the whole
    # over-long token stays, not a prefix-replaced 36-char slice.
    text = f"leaked {GHP_OVERLONG} in a log line"
    out = redact.redact_reasoning(text)
    assert GHP_OVERLONG in out


def test_anthropic_key_is_not_left_behind_by_the_openai_classic_pattern():
    # sk-ant- keys contain a hyphen, which the bare `sk-` + alphanumeric
    # class does not admit. A redactor that only implements OpenAI's
    # `sk-` pattern would match just "sk-ant" (stopping at the next
    # hyphen) and leave "-" + almost the entire tail behind -- which
    # would make the *full* literal ANTHROPIC_KEY string "absent" by
    # coincidence of non-contiguity, not because the secret is gone. The
    # unique-tail assertion is the one that actually catches that.
    text = f"ANTHROPIC_API_KEY={ANTHROPIC_KEY} was reused"
    out = redact.redact_reasoning(text)
    assert ANTHROPIC_KEY not in out
    assert ANTHROPIC_TAIL not in out


def test_protruding_overlap_does_not_leave_a_tail():
    # Bearer's token class stops at `=`. The entropy-gated run starting
    # right after "Bearer " extends across that `=` and the rest of the
    # high-entropy tail. The merge must extend to cover the whole span,
    # not drop the overlap and leave the tail exposed.
    secret_head = "AbcdEfghIjklMnop"
    secret_tail = "QrstUvwxyz0123456789ABCD"
    text = f"Authorization: Bearer {secret_head}={secret_tail}"
    out = redact.redact_reasoning(text)
    assert secret_tail not in out
    assert secret_head not in out


def test_assignment_value_is_redacted_but_keyword_and_separator_survive():
    text = "api_key=hunter22 was set in the config"
    out = redact.redact_reasoning(text)
    assert "hunter22" not in out
    assert "api_key=" in out
    assert REDACTED in out


def test_quoted_assignment_with_internal_space_is_redacted():
    text = 'password="correct horse" appears in the audit log'
    out = redact.redact_reasoning(text)
    # "correct horse" not in out alone would pass even if only "correct"
    # got replaced (e.g. a bare-run matcher that ignores the quotes and
    # stops at the first space), leaving `password=[REDACTED] horse"...`
    # behind. Checking "horse" specifically catches that half-redaction.
    assert "correct" not in out
    assert "horse" not in out
    assert "password=" in out
    assert REDACTED in out


def test_unclosed_pem_block_does_not_swallow_trailing_prose():
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAK6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6PYt6P\n"
        "this prose must survive the redactor"
    )
    out = redact.redact_reasoning(text)
    assert "this prose must survive the redactor" in out


def test_benign_prose_with_plain_sha256_hex_is_unchanged():
    # The real, 64-character SHA-256 digest of the empty string. Not 63
    # chars (a truncated fixture would let a shorter matcher "cover" it by
    # accident) and not shaped like any secret pattern above -- it's pure
    # lowercase hex, so it can't collide with AKIA/sk-/Bearer prefixes.
    sha256_hex = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert len(sha256_hex) == 64
    text = f"the build hash was {sha256_hex} and matched the manifest"
    out = redact.redact_reasoning(text)
    assert out == text


def test_high_entropy_token_without_keyword_context_is_unchanged():
    # Same class of token that trips the entropy gate under Bearer/keyword
    # context, but here it appears in ordinary prose with no assignment
    # and no Authorization header. It must be left alone.
    text = f"the cache key happened to look like {HIGH_ENTROPY_TOKEN} today"
    out = redact.redact_reasoning(text)
    assert out == text


def test_authorization_header_without_the_word_bearer_still_redacts_entropy_run():
    text = f"Authorization: {HIGH_ENTROPY_TOKEN}"
    out = redact.redact_reasoning(text)
    assert HIGH_ENTROPY_TOKEN not in out


def test_empty_string_round_trips():
    assert redact.redact_reasoning("") == ""


def test_redaction_is_deterministic():
    text = f"Authorization: Bearer {BEARER_TOKEN}"
    assert redact.redact_reasoning(text) == redact.redact_reasoning(text)


def test_redaction_is_idempotent():
    text = f"Authorization: Bearer {BEARER_TOKEN}"
    once = redact.redact_reasoning(text)
    twice = redact.redact_reasoning(once)
    assert once == twice


def test_replacement_is_a_literal_marker_not_a_digest():
    text = f"Authorization: Bearer {BEARER_TOKEN}"
    out = redact.redact_reasoning(text)
    assert REDACTED in out
    # A digest of the secret is not an acceptable stand-in for the literal
    # marker -- guard against a hash-based "redaction" that just replaces
    # one derivable secret with another.
    assert BEARER_TOKEN.lower() not in out.lower()


def test_strategy_identity_is_stable_across_a_redacted_bearer_token():
    other_token = "ZyxwVutsRqpoNmlk9876"  # different token, same valid shape
    reasoning_a = f"used the API. Authorization: Bearer {BEARER_TOKEN}. it worked."
    reasoning_b = f"used the API. Authorization: Bearer {other_token}. it worked."

    redacted_a = redact.redact_reasoning(reasoning_a)
    redacted_b = redact.redact_reasoning(reasoning_b)

    assert BEARER_TOKEN not in redacted_a
    assert other_token not in redacted_b

    id_a = identity.strategy_id_for(task_type="code_review", raw_reasoning=redacted_a)
    id_b = identity.strategy_id_for(task_type="code_review", raw_reasoning=redacted_b)

    assert id_a == id_b
