"""Pins for a JWKS cache that does not exist yet (services/uzora/auth.py).

Today's ``auth.fetch_jwks`` performs a fresh, uncached, blocking
``httpx.get`` (module-level, ``timeout=10``, ``follow_redirects=False``) on
*every* call, and ``main.py``'s ``GET /whoami`` -- despite being an ``async``
route -- calls it synchronously on every request. There is no cache, no
``auth._clock``, no ``jwks_for_kid``, and no ``signing_key``.

This file pins the cache/coalescing/cooldown/non-blocking behavior a fix
must have:

- A. repeated calls inside a 600s TTL cost zero further GETs; the TTL
  boundary itself costs exactly one.
- B. a discovery document without ``keys`` still costs exactly two GETs
  cold, then zero further inside the TTL.
- C/D/E. an unseen ``kid`` triggers exactly one refetch, then a *60-second*
  "unknown kid" cooldown suppresses further refetches for *any* unseen kid
  (not just the one that missed) until the full window elapses -- checked
  at age 0, age 59 (still suppressed, with a different unseen kid), and
  age 60 (exactly one refetch). Each of these also proves the underlying
  document was actually stored (a previously-resolved kid stays a 200 with
  zero further GETs) rather than merely that *some* cooldown flag got set.
- F/G. the synchronous fetch must not block the event loop: ``/health``
  must be servable while a fetch is in flight, and two concurrent cold
  ``/whoami`` calls for *different* kids must coalesce into a single
  shared fetch, with the kid missing from that shared document costing
  exactly one -- not zero, not two more -- follow-up fetch.
- H-series. a stored JWK only counts as "present" when ``use`` is ``sig``
  (or omitted) and ``alg`` is ``RS256`` (or omitted); a same-kid entry that
  fails that filter must never be treated as a cache hit.
- I. a failed fetch is never cached, never replaces a previously-stored
  document, and never arms the unseen-kid cooldown.

Tests in the H-series and I are pins on behavior a fix must preserve, but
most of them also exercise cache internals (a prior successful fetch must
still be servable with zero further GETs) that do not exist today, so they
are not required to pass on today's code -- see the per-test comments for
which of these are today's already-correct pins (H) and which pin future
cache correctness (H2, H3, and half of I).

Harness notes (binding; see the task's plan-critic rules):
- Multi-request, single-event-loop-per-request tests use
  ``with TestClient(main.app) as client:`` so the client's lock/loop binds
  once. Concurrency tests (F, G) use only ``asyncio.run`` + an
  ``httpx.AsyncClient`` over ``httpx.ASGITransport`` on one loop -- the two
  styles are never mixed in one test.
- Every blocking spy under F/G calls ``release.wait(timeout=1.0)`` (never a
  bare wait) with ``release.set()`` in a ``finally``, so a hang is
  impossible even when the code under test is exactly as blocking as
  today's.
- ``auth.httpx.get`` (module-level) is patched after ``load_uzora()``, never
  the bare global ``httpx.get`` reference held before loading -- and never
  in a way that touches ``httpx.Client``/``AsyncClient`` instance methods,
  which is what the ASGI test clients use internally.
- ``jwt`` and ``cryptography`` are never imported at module top (this file
  must still be collectible without them installed); ``httpx`` for
  ``ASGITransport``/``AsyncClient``/``ConnectError`` is imported inside the
  tests that need it.
- ``_generate_rsa_key``, ``make_token``, and ``mock_jwks`` are imported from
  ``test_uzora_jwt``; ``_load``/``load_uzora``/the env fixture are a local
  copy (test_uzora_jwt.py itself is never edited).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_uzora_jwt import _AUDIENCE, _ISSUER, _generate_rsa_key, make_token, mock_jwks

_UZORA_DIR = Path(__file__).resolve().parents[2] / "services" / "uzora"

_KID_A = "kid-a"
_KID_B = "kid-b"
_KID_C = "kid-c"
_KID_D = "kid-d"
_KID_X = "kid-x"


def _load(name: str, filename: str) -> ModuleType:
    path = _UZORA_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"could not build spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_uzora() -> tuple[ModuleType, ModuleType]:
    """(Re)load services/uzora/auth.py then main.py fresh from disk, same
    approach as test_uzora_jwt.load_uzora -- copied locally rather than
    imported so this file has no coupling to that module's internals."""
    if str(_UZORA_DIR) not in sys.path:
        sys.path.insert(0, str(_UZORA_DIR))
    auth = _load("auth", "auth.py")
    main = _load("main", "main.py")
    return auth, main


@pytest.fixture
def uzora_env(monkeypatch: pytest.MonkeyPatch):
    """Configure a complete Authentik settings triple and clean up loaded
    modules afterward, matching test_uzora_jwt.uzora_env's shape."""
    monkeypatch.setenv("AUTHENTIK_URL", "http://authentik:9000")
    monkeypatch.setenv("AUTHENTIK_ISSUER", _ISSUER)
    monkeypatch.setenv("AUTHENTIK_AUDIENCE", _AUDIENCE)
    yield monkeypatch
    sys.modules.pop("auth", None)
    sys.modules.pop("main", None)


class _FakeClock:
    """Deterministic stand-in for the future ``auth._clock`` seam.

    Starts at a large, non-zero value so a future cache implementation that
    treats ``0``/``None`` as "unset" is never confused by this clock's
    starting point.
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeJsonResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


def _install_httpx_get_queue(
    monkeypatch: pytest.MonkeyPatch, auth: ModuleType, queue: list[Any]
) -> list[str]:
    """Patch ``auth.httpx.get`` to pop responses off ``queue`` in call order.

    Each queued item is either a JWKS/discovery ``dict`` (served as the JSON
    body of a 200) or an ``Exception`` instance (raised instead). Once the
    queue is drained, the most recently served item is repeated rather than
    raising -- a "this call must cost zero further GETs" assertion is meant
    to be caught by the returned call-count list, not by the spy crashing
    when today's uncached code reaches for one more fixture than a fixed
    implementation would have needed.
    """
    calls: list[str] = []
    last: dict[str, Any] = {"item": None}

    def _fake_get(url: str, *args: Any, **kwargs: Any) -> _FakeJsonResponse:
        calls.append(url)
        if queue:
            item = queue.pop(0)
            last["item"] = item
        else:
            item = last["item"]
        if isinstance(item, BaseException):
            raise item
        return _FakeJsonResponse(item)

    monkeypatch.setattr(auth.httpx, "get", _fake_get)
    return calls


def _merged_jwks(*documents: dict[str, Any]) -> dict[str, Any]:
    keys: list[Any] = []
    for document in documents:
        keys.extend(document["keys"])
    return {"keys": keys}


def _apply_clock(monkeypatch: pytest.MonkeyPatch, auth: ModuleType) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(auth, "_clock", clock, raising=False)
    return clock


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# A. TTL: zero further GETs inside the window, exactly one once it elapses.
# ---------------------------------------------------------------------------


def test_whoami_repeated_within_ttl_does_not_call_httpx_get(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key, kid=_KID_A)
    token = make_token(key, kid=_KID_A)
    auth, main = load_uzora()
    clock = _apply_clock(monkeypatch, auth)
    calls = _install_httpx_get_queue(monkeypatch, auth, [jwks])

    with TestClient(main.app) as client:
        first = client.get("/whoami", headers=_bearer(token))
        assert first.status_code == 200
        assert len(calls) == 1

        clock.advance(599)
        second = client.get("/whoami", headers=_bearer(token))
        assert second.status_code == 200
        assert len(calls) == 1, "expected zero further httpx.get calls within the TTL"

        clock.advance(1)
        third = client.get("/whoami", headers=_bearer(token))
        assert third.status_code == 200
        assert len(calls) == 2, "expected exactly one further httpx.get once the TTL elapsed"


# ---------------------------------------------------------------------------
# B. Cold discovery-without-keys costs two GETs, then zero inside the TTL.
# ---------------------------------------------------------------------------


def test_whoami_cold_jwks_uri_discovery_counts_two_gets_then_zero(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key, kid=_KID_A)
    token = make_token(key, kid=_KID_A)
    auth, main = load_uzora()
    _apply_clock(monkeypatch, auth)
    discovery_doc = {"jwks_uri": "https://auth.example.com/application/o/locus/jwks/"}
    calls = _install_httpx_get_queue(monkeypatch, auth, [discovery_doc, jwks])

    with TestClient(main.app) as client:
        first = client.get("/whoami", headers=_bearer(token))
        assert first.status_code == 200
        assert len(calls) == 2, "discovery without keys should cost exactly two GETs"

        second = client.get("/whoami", headers=_bearer(token))
        assert second.status_code == 200
        assert len(calls) == 2, "expected zero further httpx.get calls within the TTL"


# ---------------------------------------------------------------------------
# C. Unseen kid -> exactly one refetch; a *60s* cooldown then suppresses
#    refetches for *any* unseen kid, checked at age 0, 59, and 60.
# ---------------------------------------------------------------------------


def test_missing_kid_refetches_once_then_cooldown_401s(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_a = _generate_rsa_key()
    key_b = _generate_rsa_key()
    key_c = _generate_rsa_key()
    key_d = _generate_rsa_key()
    doc_a = mock_jwks(key_a, kid=_KID_A)
    doc_ab = _merged_jwks(doc_a, mock_jwks(key_b, kid=_KID_B))
    doc_d = mock_jwks(key_d, kid=_KID_D)

    token_a = make_token(key_a, kid=_KID_A)
    token_b = make_token(key_b, kid=_KID_B)
    token_c = make_token(key_c, kid=_KID_C)
    token_d = make_token(key_d, kid=_KID_D)

    auth, main = load_uzora()
    clock = _apply_clock(monkeypatch, auth)
    queue: list[Any] = [doc_a]
    calls = _install_httpx_get_queue(monkeypatch, auth, queue)

    with TestClient(main.app) as client:
        # Prime the cache with kid A.
        primed = client.get("/whoami", headers=_bearer(token_a))
        assert primed.status_code == 200
        assert len(calls) == 1

        # kid B is missing from the cached document -> exactly one refetch,
        # which finds it (no cooldown armed).
        queue.append(doc_ab)
        calls_before = len(calls)
        resp_b = client.get("/whoami", headers=_bearer(token_b))
        assert resp_b.status_code == 200
        assert len(calls) == calls_before + 1, "expected exactly one refetch for an unseen kid"

        # Repeating kid B inside the TTL must hit the (now-refreshed) stored
        # document -- zero further GETs. This proves the refresh actually
        # replaced the stored document rather than just answering once.
        calls_before = len(calls)
        resp_b_again = client.get("/whoami", headers=_bearer(token_b))
        assert resp_b_again.status_code == 200
        assert len(calls) == calls_before, "expected zero further httpx.get calls for a cached kid"

        # kid B's refresh above arms the 60s cooldown regardless of whether
        # B was found by it -- so at age 0 since that refresh, kid C (absent
        # from the document B's refresh fetched) must not trigger a refetch
        # of its own; it is 401 purely from the cooldown, with zero GETs.
        calls_before = len(calls)
        resp_c = client.get("/whoami", headers=_bearer(token_c))
        assert resp_c.status_code == 401
        assert len(calls) == calls_before, (
            "expected zero further httpx.get calls during cooldown (age 0)"
        )

        # Age 59: a *different* unseen kid must still be suppressed -- the
        # cooldown is not scoped to kid C alone, and 59s is not yet 60s.
        clock.advance(59)
        calls_before = len(calls)
        resp_d_cold = client.get("/whoami", headers=_bearer(token_d))
        assert resp_d_cold.status_code == 401
        assert len(calls) == calls_before, (
            "expected zero further httpx.get calls during cooldown (age 59)"
        )

        # Age 60: the cooldown has fully elapsed and a refetch happens.
        clock.advance(1)
        queue.append(doc_d)
        calls_before = len(calls)
        resp_d_warm = client.get("/whoami", headers=_bearer(token_d))
        assert resp_d_warm.status_code == 200
        assert len(calls) == calls_before + 1, "expected exactly one refetch once cooldown elapsed"


# ---------------------------------------------------------------------------
# D. Cold unknown kid still arms the cooldown for the next unknown kid, but
#    the fetched document itself must have been stored.
# ---------------------------------------------------------------------------


def test_cold_unknown_kid_arms_cooldown(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_x = _generate_rsa_key()
    key_1 = _generate_rsa_key()
    key_2 = _generate_rsa_key()
    key_3 = _generate_rsa_key()
    doc_x = mock_jwks(key_x, kid=_KID_X)
    doc_3 = mock_jwks(key_3, kid="kid-unknown-3")
    token_x = make_token(key_x, kid=_KID_X)
    token_1 = make_token(key_1, kid="kid-unknown-1")
    token_2 = make_token(key_2, kid="kid-unknown-2")
    token_3 = make_token(key_3, kid="kid-unknown-3")

    auth, main = load_uzora()
    clock = _apply_clock(monkeypatch, auth)
    queue: list[Any] = [doc_x]
    calls = _install_httpx_get_queue(monkeypatch, auth, queue)

    with TestClient(main.app) as client:
        first = client.get("/whoami", headers=_bearer(token_1))
        assert first.status_code == 401
        assert len(calls) == 1

        # The cold fetch's document (which contains kid-x) must have been
        # stored despite kid-unknown-1 being absent from it -- proves this
        # is a real cache, not just a cooldown flag.
        calls_before = len(calls)
        resp_x = client.get("/whoami", headers=_bearer(token_x))
        assert resp_x.status_code == 200
        assert len(calls) == calls_before, "expected zero further httpx.get calls for a cached kid"

        # Age 0 since cooldown armed: a second unknown kid must not refetch.
        calls_before = len(calls)
        second = client.get("/whoami", headers=_bearer(token_2))
        assert second.status_code == 401
        assert len(calls) == calls_before, (
            "expected zero further httpx.get calls for a second unknown kid (age 0)"
        )

        # Age 59: cooldown is still active for a third, different unknown kid.
        clock.advance(59)
        calls_before = len(calls)
        third_cold = client.get("/whoami", headers=_bearer(token_3))
        assert third_cold.status_code == 401
        assert len(calls) == calls_before, (
            "expected zero further httpx.get calls for a third unknown kid (age 59)"
        )

        # Age 60: the cooldown has fully elapsed and a refetch happens.
        clock.advance(1)
        queue.append(doc_3)
        calls_before = len(calls)
        third_warm = client.get("/whoami", headers=_bearer(token_3))
        assert third_warm.status_code == 200
        assert len(calls) == calls_before + 1, "expected exactly one refetch once cooldown elapsed"


# ---------------------------------------------------------------------------
# E. A fresh cache miss on one kid still arms the cooldown for another,
#    across the same age 0 / age 59 / age 60 checkpoints.
# ---------------------------------------------------------------------------


def test_fresh_cache_missing_kid_refresh_still_absent_arms_cooldown(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_a = _generate_rsa_key()
    key_b = _generate_rsa_key()
    key_c = _generate_rsa_key()
    key_d = _generate_rsa_key()
    doc_a = mock_jwks(key_a, kid=_KID_A)
    doc_d = mock_jwks(key_d, kid=_KID_D)
    token_a = make_token(key_a, kid=_KID_A)
    token_b = make_token(key_b, kid=_KID_B)
    token_c = make_token(key_c, kid=_KID_C)
    token_d = make_token(key_d, kid=_KID_D)

    auth, main = load_uzora()
    clock = _apply_clock(monkeypatch, auth)
    queue: list[Any] = [doc_a]
    calls = _install_httpx_get_queue(monkeypatch, auth, queue)

    with TestClient(main.app) as client:
        primed = client.get("/whoami", headers=_bearer(token_a))
        assert primed.status_code == 200
        assert len(calls) == 1

        queue.append(doc_a)
        calls_before = len(calls)
        resp_b = client.get("/whoami", headers=_bearer(token_b))
        assert resp_b.status_code == 401
        assert len(calls) == calls_before + 1, "expected exactly one refetch for an unseen kid"

        # Age 0 since cooldown armed: a different unseen kid must not refetch.
        calls_before = len(calls)
        resp_c = client.get("/whoami", headers=_bearer(token_c))
        assert resp_c.status_code == 401
        assert len(calls) == calls_before, (
            "expected zero further httpx.get calls for a second unseen kid (age 0)"
        )

        # Age 59: still within the cooldown window for yet another unseen kid.
        clock.advance(59)
        calls_before = len(calls)
        resp_d_cold = client.get("/whoami", headers=_bearer(token_d))
        assert resp_d_cold.status_code == 401
        assert len(calls) == calls_before, (
            "expected zero further httpx.get calls for a third unseen kid (age 59)"
        )

        # Age 60: the cooldown has fully elapsed and a refetch happens.
        clock.advance(1)
        queue.append(doc_d)
        calls_before = len(calls)
        resp_d_warm = client.get("/whoami", headers=_bearer(token_d))
        assert resp_d_warm.status_code == 200
        assert len(calls) == calls_before + 1, "expected exactly one refetch once cooldown elapsed"


# ---------------------------------------------------------------------------
# F. The event loop must stay free while a fetch is in flight.
# ---------------------------------------------------------------------------


def test_health_returns_while_jwks_get_blocks_on_same_loop(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    key = _generate_rsa_key()
    jwks = mock_jwks(key, kid=_KID_A)
    token = make_token(key, kid=_KID_A)

    auth, main = load_uzora()
    _apply_clock(monkeypatch, auth)

    started = threading.Event()
    release = threading.Event()

    def _fake_get(url: str, *args: Any, **kwargs: Any) -> _FakeJsonResponse:
        started.set()
        release.wait(timeout=1.0)
        return _FakeJsonResponse(jwks)

    monkeypatch.setattr(auth.httpx, "get", _fake_get)

    async def _run() -> None:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://uzora") as client:
            whoami_task = asyncio.create_task(client.get("/whoami", headers=_bearer(token)))
            try:
                await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=2)
                assert not release.is_set()
                assert not whoami_task.done(), (
                    "the /whoami request already finished before /health could be observed "
                    "concurrently -- today's synchronous fetch_jwks call blocks the event loop"
                )
                health_response = await asyncio.wait_for(client.get("/health"), timeout=2)
                assert health_response.status_code == 200
                assert not release.is_set()
                assert not whoami_task.done()
            finally:
                release.set()
            whoami_response = await asyncio.wait_for(whoami_task, timeout=2)
            assert whoami_response.status_code == 200

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# G. Two concurrent cold /whoami calls for *different* kids must coalesce
#    into one shared fetch; the kid missing from that shared document then
#    costs exactly one further fetch -- not zero (would mean cooldown wrongly
#    armed by kid A's success), not two (would mean no coalescing happened).
# ---------------------------------------------------------------------------


def test_overlapping_cold_whoami_shares_one_fetch(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    key_a = _generate_rsa_key()
    key_b = _generate_rsa_key()
    # The in-flight document contains kid A but not kid B.
    doc_a_only = mock_jwks(key_a, kid=_KID_A)
    # Served for kid B's own follow-up fetch once it discovers it is absent
    # from the shared document.
    doc_b = mock_jwks(key_b, kid=_KID_B)

    token_a = make_token(key_a, kid=_KID_A)
    token_b = make_token(key_b, kid=_KID_B)

    auth, main = load_uzora()
    _apply_clock(monkeypatch, auth)

    started = threading.Event()
    release = threading.Event()
    queue: list[Any] = [doc_a_only]
    count = {"n": 0}

    def _fake_get(url: str, *args: Any, **kwargs: Any) -> _FakeJsonResponse:
        count["n"] += 1
        started.set()
        release.wait(timeout=1.0)
        item = queue.pop(0) if queue else doc_a_only
        return _FakeJsonResponse(item)

    monkeypatch.setattr(auth.httpx, "get", _fake_get)

    async def _run() -> None:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://uzora") as client:
            task_a = asyncio.create_task(client.get("/whoami", headers=_bearer(token_a)))
            task_b = asyncio.create_task(client.get("/whoami", headers=_bearer(token_b)))
            try:
                await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=2)
                await asyncio.sleep(0.05)
                assert count["n"] == 1, "expected the two concurrent requests to share one fetch"
                assert not task_a.done()
                assert not task_b.done()
                assert not release.is_set()
            finally:
                # kid B's own follow-up fetch (if the implementation performs
                # one) must find a document containing kid B once it runs.
                queue.append(doc_b)
                release.set()

            response_a = await asyncio.wait_for(task_a, timeout=2)
            response_b = await asyncio.wait_for(task_b, timeout=2)
            assert response_a.status_code == 200
            assert response_b.status_code == 200
            assert count["n"] == 2, (
                "kid B's absence from the shared fetch must cost exactly one further "
                "httpx.get -- not zero (cooldown wrongly armed by kid A's success) and "
                "not more than one"
            )

            # Repeating kid B immediately (now cached) must not cost another GET.
            response_b_again = await asyncio.wait_for(
                client.get("/whoami", headers=_bearer(token_b)), timeout=2
            )
            assert response_b_again.status_code == 200
            assert count["n"] == 2

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# H. Pin (passes today): a JWK advertising use=enc is never accepted on a
#    cache miss.
# ---------------------------------------------------------------------------


def test_signing_key_use_enc_is_not_accepted(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key, kid=_KID_A)
    jwks["keys"][0]["use"] = "enc"
    token = make_token(key, kid=_KID_A)

    auth, main = load_uzora()
    monkeypatch.setattr(auth, "fetch_jwks", lambda *args, **kwargs: jwks)
    client = TestClient(main.app)

    response = client.get("/whoami", headers=_bearer(token))

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# H2. A *stored* jwk with use=enc must never count as a cache hit for its
#     kid -- presence checking is not kid-only. Not required to pass today
#     (no cache exists to hit or miss), but must hold once one does.
# ---------------------------------------------------------------------------


def test_stored_jwk_use_enc_forces_refetch_after_cooldown(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key, kid=_KID_A)
    jwks["keys"][0]["use"] = "enc"
    token = make_token(key, kid=_KID_A)

    auth, main = load_uzora()
    clock = _apply_clock(monkeypatch, auth)
    queue: list[Any] = [jwks]
    calls = _install_httpx_get_queue(monkeypatch, auth, queue)

    with TestClient(main.app) as client:
        first = client.get("/whoami", headers=_bearer(token))
        assert first.status_code == 401
        assert len(calls) == 1

        # Cooldown has fully elapsed, but the stored document (with the
        # enc-flagged key) is still well inside the 600s TTL.
        clock.advance(60)
        queue.append(jwks)
        calls_before = len(calls)
        second = client.get("/whoami", headers=_bearer(token))
        assert second.status_code == 401
        assert len(calls) == calls_before + 1, (
            "a use=enc key must never count as a cache hit for its kid -- a cache that "
            "treats 'same kid' as present will not perform this httpx.get"
        )


def test_stored_jwk_wrong_alg_forces_refetch_after_cooldown(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key, kid=_KID_A)
    jwks["keys"][0]["alg"] = "HS256"
    token = make_token(key, kid=_KID_A)

    auth, main = load_uzora()
    clock = _apply_clock(monkeypatch, auth)
    queue: list[Any] = [jwks]
    calls = _install_httpx_get_queue(monkeypatch, auth, queue)

    with TestClient(main.app) as client:
        first = client.get("/whoami", headers=_bearer(token))
        assert first.status_code == 401
        assert len(calls) == 1

        clock.advance(60)
        queue.append(jwks)
        calls_before = len(calls)
        second = client.get("/whoami", headers=_bearer(token))
        assert second.status_code == 401
        assert len(calls) == calls_before + 1, (
            "an alg!=RS256 key must never count as a cache hit for its kid -- a cache "
            "that treats 'same kid' as present will not perform this httpx.get"
        )


def test_stored_jwk_missing_use_and_alg_defaults_to_present(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authentik may omit ``use``/``alg`` entirely; ``mock_jwks`` always sets
    them, so this test deletes both to pin the same "sig"/"RS256" defaults
    main.py's cache-miss filter already applies."""
    key = _generate_rsa_key()
    jwks = mock_jwks(key, kid=_KID_A)
    del jwks["keys"][0]["use"]
    del jwks["keys"][0]["alg"]
    token = make_token(key, kid=_KID_A)

    auth, main = load_uzora()
    _apply_clock(monkeypatch, auth)
    calls = _install_httpx_get_queue(monkeypatch, auth, [jwks])

    with TestClient(main.app) as client:
        first = client.get("/whoami", headers=_bearer(token))
        assert first.status_code == 200
        assert len(calls) == 1

        calls_before = len(calls)
        second = client.get("/whoami", headers=_bearer(token))
        assert second.status_code == 200
        assert len(calls) == calls_before, (
            "expected zero further httpx.get calls -- a jwk with defaulted use/alg "
            "fields must count as present, same as one with them set explicitly"
        )


# ---------------------------------------------------------------------------
# I. A failed fetch is never cached, never replaces a previously-stored
#    document, and never arms the unseen-kid cooldown.
# ---------------------------------------------------------------------------


def test_jwks_fetch_error_is_not_cached(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    key_a = _generate_rsa_key()
    key_missing_1 = _generate_rsa_key()
    key_missing_2 = _generate_rsa_key()
    doc_a = mock_jwks(key_a, kid=_KID_A)
    token_a = make_token(key_a, kid=_KID_A)
    token_missing_1 = make_token(key_missing_1, kid="kid-missing-1")
    token_missing_2 = make_token(key_missing_2, kid="kid-missing-2")

    auth, main = load_uzora()
    queue: list[Any] = [doc_a]
    calls = _install_httpx_get_queue(monkeypatch, auth, queue)

    with TestClient(main.app) as client:
        # Prime the cache with kid A (a real, passing pin today: this
        # request has no cache to rely on, so it always succeeds).
        primed = client.get("/whoami", headers=_bearer(token_a))
        assert primed.status_code == 200
        assert len(calls) == 1

        # A missing kid's refresh raises -- must surface as 503, and must
        # not be stored anywhere or arm any cooldown.
        queue.append(httpx.ConnectError("authentik down"))
        calls_before = len(calls)
        errored = client.get("/whoami", headers=_bearer(token_missing_1))
        assert errored.status_code == 503
        assert len(calls) == calls_before + 1

        # kid A must still be served from the earlier, untouched stored
        # document -- no new httpx.get. This is the half that does not
        # exist yet: today's code has no cache to preserve, so it always
        # performs a fresh GET here.
        queue.append(doc_a)
        calls_before = len(calls)
        resp_a_again = client.get("/whoami", headers=_bearer(token_a))
        assert resp_a_again.status_code == 200
        assert len(calls) == calls_before, (
            "a failed refresh must not replace the previously stored document"
        )

        # A different missing kid must still trigger exactly one new fetch --
        # the earlier error must not have armed a cooldown. This document may
        # still lack that kid; only the GET count is asserted here.
        queue.append(doc_a)
        calls_before = len(calls)
        resp_missing_2 = client.get("/whoami", headers=_bearer(token_missing_2))
        assert resp_missing_2.status_code == 401
        assert len(calls) == calls_before + 1, (
            "expected exactly one httpx.get -- a prior fetch error must not arm cooldown"
        )
