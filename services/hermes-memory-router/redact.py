"""Credential redaction for Hermes reasoning traces.

Pure module — stdlib ``re`` and ``math`` only.  No network I/O, no Neo4j,
no Qdrant, no third-party imports.
"""

from __future__ import annotations

import math
import re

REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# Patterns (fixed tuple order — never reorder; rule indices referenced below)
# ---------------------------------------------------------------------------

# 1. PEM private key block: non-greedy body, dotall, BEGIN…END pair required.
_PAT_PEM = re.compile(
    r"-----BEGIN [\w ]*PRIVATE KEY-----.*?-----END [\w ]*PRIVATE KEY-----",
    re.DOTALL,
)

# 2. AWS access key id: AKIA + exactly 16 uppercase alphanumerics.
#    17+ chars after AKIA is a total miss (the whole token stays).
_PAT_AKIA = re.compile(r"AKIA[A-Z0-9]{16}(?![A-Z0-9])")

# 3. GitHub classic PAT: ghp_ + exactly 36 alphanumerics.
#    37+ alphanumerics after ghp_ is a total miss.
_PAT_GHP = re.compile(r"ghp_[A-Za-z0-9]{36}(?![A-Za-z0-9])")

# 4. GitHub fine-grained PAT: github_pat_ + exactly 82 of [A-Za-z0-9_].
_PAT_FINE_GRAINED = re.compile(r"github_pat_[A-Za-z0-9_]{82}(?![A-Za-z0-9_])")

# 5. Slack token: xox[baprs]- + at least 10 of [A-Za-z0-9-].  No upper cap.
#    Greedy — consumes the whole run.
_PAT_SLACK = re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")

# 6. Anthropic key: sk-ant- + 24–200 of [A-Za-z0-9_-].
#    Over-long (201+) is a total miss.
_PAT_ANTHROPIC = re.compile(r"sk-ant-[A-Za-z0-9_-]{24,200}(?![A-Za-z0-9_-])")

# 7. OpenAI project key: sk-proj- + at least 20 of [A-Za-z0-9_-].  No upper
#    cap.
_PAT_OPENAI_PROJECT = re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}")

# 8. OpenAI classic key: sk- (not ant- or proj-) + 20–200 alphanumeric only.
#    Hyphen stops the class, so it does not consume sk-ant- or sk-proj-.
_PAT_OPENAI_CLASSIC = re.compile(
    r"sk-(?!ant-)(?!proj-)[A-Za-z0-9]{20,200}(?![A-Za-z0-9])"
)

# 9. JWT: eyJ + three base64url segments of >= 10 chars, two dots between.
_PAT_JWT = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)

# 10. Bearer token (case-insensitive "Bearer") + whitespace + token of
#     [A-Za-z0-9._~+/-] length >= 16.  "=" stops the token class.
#     The word Bearer is inside the replaced span.  No upper cap.
_PAT_BEARER = re.compile(r"(?i:Bearer)\s+[A-Za-z0-9._~+/\-]{16,}")

# 11. Assignment: keyword + optional whitespace + "=" or ":" + value.
#     Only the VALUE group (group 1) is added to the span list; the keyword
#     and separator survive.  Value must be >= 8 chars.
#     Quoted values allow internal spaces; bare values stop at whitespace.
_PAT_ASSIGNMENT = re.compile(
    r"(?i:api[_-]key|token|password|secret)"
    r"\s*[=:]"
    r"("
    r'(?:"[^"]*"|\'[^\']*\')'  # quoted value — single or double quotes
    r"|"
    r"\S+"  # bare value — non-whitespace run
    r")"
)

# Entropy gate: a run of [A-Za-z0-9+/=_-] of length >= 32 whose Shannon
# entropy is >= 3.5 bits, but ONLY when the immediate left context is one of:
#   • assignment keywords (api_key / api-key / token / password / secret),
#     optional whitespace, then "=" or ":", then optional opening quote; OR
#   • "Authorization:" with an optional "Bearer" (any case).
# Group 1 captures the high-entropy run; the context prefix is not captured.
_PAT_ENTROPY_GATE = re.compile(
    r"(?:(?i:api[_-]key|token|password|secret)\s*[=:]\s*[\"']?"
    r"|Authorization:\s*(?:(?i:Bearer)\s+)?)"
    r"([A-Za-z0-9+/=_\-]{32,})"
)

# Patterns whose entire match becomes the redacted span (rules 1–10).
_DIRECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    _PAT_PEM,             # 1
    _PAT_AKIA,            # 2
    _PAT_GHP,             # 3
    _PAT_FINE_GRAINED,    # 4
    _PAT_SLACK,           # 5
    _PAT_ANTHROPIC,       # 6
    _PAT_OPENAI_PROJECT,  # 7
    _PAT_OPENAI_CLASSIC,  # 8
    _PAT_JWT,             # 9
    _PAT_BEARER,          # 10
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shannon_entropy(s: str) -> float:
    """Return the Shannon entropy of *s* in bits (using math.log2).

    All characters in *s* are assumed non-empty (the caller guarantees
    ``len(s) >= 32`` before invoking this).
    """
    n = len(s)
    counts: dict[str, int] = {}
    for c in s:
        counts[c] = counts.get(c, 0) + 1
    return -sum((count / n) * math.log2(count / n) for count in counts.values())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def redact_reasoning(text: str) -> str:
    """Strip known credential shapes from *text* before it is persisted.

    Policy
    ------
    Every recognised span is replaced with the literal string ``[REDACTED]``.
    No hash or digest of the secret is substituted — the replacement is always
    the same static marker so that no information about the original value can
    be derived from the output.

    All patterns are evaluated against the *original* string.  Overlapping or
    adjacent spans are merged into a single ``[REDACTED]`` by extending the
    current end when the next span starts at or before it.  The rebuilt string
    is produced in one pass with no further evaluation.

    This function is deterministic and idempotent: calling it twice on the
    same input produces the same output as calling it once, and calling it
    twice on any input produces the same result each time.

    Example — protruding overlap
    ----------------------------
    ``Authorization: Bearer AbcdEfghIjklMnop=QrstUvwxyz0123456789ABCD``

    The Bearer pattern consumes ``Bearer AbcdEfghIjklMnop`` (stopping at
    ``=``).  The entropy gate fires on the full run
    ``AbcdEfghIjklMnop=QrstUvwxyz0123456789ABCD`` (which includes ``=``).
    The two spans overlap; the merge extends to cover the entropy run, so
    ``QrstUvwxyz0123456789ABCD`` is not left behind.

    Known misses (documented; not claimed as features)
    --------------------------------------------------
    * AWS secret keys that lack the ``AKIA`` prefix **and** have no recognised
      assignment keyword (``api_key`` / ``token`` / ``password`` / ``secret``)
      immediately to the left.
    * AWS access key prefixes other than ``AKIA``.
    * Unclosed PEM blocks: a ``-----BEGIN … PRIVATE KEY-----`` header with no
      matching ``-----END … PRIVATE KEY-----`` is not redacted.
    * Secrets split across lines or separated by whitespace.
    * Assignment values shorter than 8 characters
      (e.g. ``password=hunter2`` is a miss; ``password=hunter22`` is caught).
    * Secrets in ``strategy_key``, ``task_type``, or ``task_id`` fields —
      this function only processes the string it is given.
    * Unicode homoglyphs substituted for ASCII characters (e.g. a Cyrillic
      ``а`` in place of the Latin ``a`` in ``api_key``).
    * GitHub classic tokens of 37 or more alphanumerics after ``ghp_`` (the
      whole over-long token stays — the correct-length prefix is not stripped).
    * Anthropic keys with 201 or more characters after ``sk-ant-`` (total
      miss, same rationale as the GitHub over-long case).
    * High-entropy runs in ordinary prose with no assignment or
      ``Authorization:`` context are left alone (e.g. a plain SHA-256 hex
      digest in a log message).
    """
    if not text:
        return text

    spans: list[tuple[int, int]] = []

    # Rules 1–10: whole match → span.
    for pat in _DIRECT_PATTERNS:
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))

    # Rule 11: only the value sub-span is redacted; keyword + separator stay.
    for m in _PAT_ASSIGNMENT.finditer(text):
        value = m.group(1)
        if len(value) >= 8:
            spans.append((m.start(1), m.end(1)))

    # Entropy gate: run must be >= 32 chars and entropy >= 3.5 bits.
    for m in _PAT_ENTROPY_GATE.finditer(text):
        run = m.group(1)
        if len(run) >= 32 and _shannon_entropy(run) >= 3.5:
            spans.append((m.start(1), m.end(1)))

    if not spans:
        return text

    # Sort: start ascending, length descending (longer span wins for same
    # start), then by insertion order (rule index) as final tiebreaker — the
    # Python sort is stable, so equal (start, length) pairs retain the order
    # they were appended above.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))

    # Merge: when the next span starts at or before the current end, extend.
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        cur_start, cur_end = merged[-1]
        if start <= cur_end:
            merged[-1] = (cur_start, max(cur_end, end))
        else:
            merged.append((start, end))

    # Rebuild in a single pass.
    parts: list[str] = []
    prev = 0
    for start, end in merged:
        parts.append(text[prev:start])
        parts.append(REDACTED)
        prev = end
    parts.append(text[prev:])
    return "".join(parts)
