"""Uzora config/settings contract (services/uzora/auth.py + main.py).

Assumed contract these tests hold the implementation to (amended plan,
plan-critic C1/C5/C7 — "fail closed at request time", not "fail import"):

- ``auth.py`` reads ``AUTHENTIK_URL`` / ``AUTHENTIK_ISSUER`` /
  ``AUTHENTIK_AUDIENCE`` from ``os.environ`` at *import* time into module-level
  attributes ``auth.AUTHENTIK_URL``, ``auth.AUTHENTIK_ISSUER``,
  ``auth.AUTHENTIK_AUDIENCE`` (each ``str | None``, empty string treated the
  same as unset).
- ``auth.py`` exposes ``settings_ready() -> bool``, returning ``True`` only
  when all three of the above are set to a non-empty value.
- Importing ``auth.py`` and ``main.py`` must succeed even when none of the
  three env vars are set — nothing may be fetched from Authentik at import
  time, and a missing/empty setting must not raise during import or module
  load. Fail-closed behavior lives at request time in main.py's route
  handlers (covered in test_uzora_http.py / test_uzora_jwt.py), not at
  import time.

Modules are loaded fresh via importlib.util.spec_from_file_location (per
tests/unit/test_strategy_identity.py's pattern) so that os.environ changes
made inside each test are picked up, instead of a cached top-level import.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_UZORA_DIR = Path(__file__).resolve().parents[2] / "services" / "uzora"


def _load(name: str, filename: str) -> ModuleType:
    path = _UZORA_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"could not build spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_auth() -> ModuleType:
    """(Re)load services/uzora/auth.py fresh from disk."""
    if str(_UZORA_DIR) not in sys.path:
        sys.path.insert(0, str(_UZORA_DIR))
    return _load("auth", "auth.py")


def load_uzora() -> tuple[ModuleType, ModuleType]:
    """(Re)load auth.py then main.py fresh from disk.

    Both are re-registered in sys.modules under their bare names ("auth",
    "main") so main.py's `import auth` resolves to the module we just loaded,
    and so calling this again after mutating os.environ picks up fresh
    settings rather than a cached import.
    """
    auth = load_auth()
    main = _load("main", "main.py")
    return auth, main


@pytest.fixture
def uzora_env(monkeypatch: pytest.MonkeyPatch):
    """Start each test with no AUTHENTIK_* env vars set, and unregister the
    dynamically-loaded modules afterward so tests don't leak state."""
    for key in ("AUTHENTIK_URL", "AUTHENTIK_ISSUER", "AUTHENTIK_AUDIENCE"):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch
    sys.modules.pop("auth", None)
    sys.modules.pop("main", None)


def test_load_auth_missing_env_does_not_raise(uzora_env: pytest.MonkeyPatch) -> None:
    """Importing auth.py with no AUTHENTIK_* env vars set must not raise."""
    auth = load_auth()
    assert auth.AUTHENTIK_URL in (None, "")
    assert auth.AUTHENTIK_ISSUER in (None, "")
    assert auth.AUTHENTIK_AUDIENCE in (None, "")


def test_load_main_missing_env_does_not_raise(uzora_env: pytest.MonkeyPatch) -> None:
    """Importing main.py (which imports auth.py) with no AUTHENTIK_* env vars
    set must not raise and must still expose a FastAPI app."""
    _, main = load_uzora()
    assert main.app is not None


def test_load_auth_all_env_empty_string_does_not_raise(uzora_env: pytest.MonkeyPatch) -> None:
    """A process where the orchestrator (e.g. Railway) sets every
    AUTHENTIK_* var to the literal empty string -- rather than leaving it
    unset -- must import auth.py without raising. `delenv` alone does not
    cover this: an empty string is truthy for `"FOO" in os.environ` checks
    but falsy as a setting value, and a naive `os.environ["AUTHENTIK_URL"]`
    read (vs. `.get(..., "")`) behaves identically whether unset or empty."""
    uzora_env.setenv("AUTHENTIK_URL", "")
    uzora_env.setenv("AUTHENTIK_ISSUER", "")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "")

    auth = load_auth()

    assert auth.AUTHENTIK_URL in (None, "")
    assert auth.AUTHENTIK_ISSUER in (None, "")
    assert auth.AUTHENTIK_AUDIENCE in (None, "")
    assert auth.settings_ready() is False


def test_load_main_all_env_empty_string_does_not_raise(uzora_env: pytest.MonkeyPatch) -> None:
    """Same empty-string-not-unset process start, but through main.py's
    import chain (which imports auth.py)."""
    uzora_env.setenv("AUTHENTIK_URL", "")
    uzora_env.setenv("AUTHENTIK_ISSUER", "")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "")

    _, main = load_uzora()

    assert main.app is not None


def test_settings_ready_all_env_missing_returns_false(uzora_env: pytest.MonkeyPatch) -> None:
    auth = load_auth()
    assert auth.settings_ready() is False


def test_settings_ready_missing_url_returns_false(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    auth = load_auth()
    assert auth.settings_ready() is False


def test_settings_ready_missing_issuer_returns_false(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    auth = load_auth()
    assert auth.settings_ready() is False


def test_settings_ready_missing_audience_returns_false(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    auth = load_auth()
    assert auth.settings_ready() is False


def test_settings_ready_empty_string_issuer_returns_false(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_ISSUER", "")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    auth = load_auth()
    assert auth.settings_ready() is False


def test_settings_ready_empty_string_url_returns_false(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "")
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    auth = load_auth()
    assert auth.settings_ready() is False


def test_settings_ready_empty_string_audience_returns_false(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "")
    auth = load_auth()
    assert auth.settings_ready() is False


def test_settings_ready_all_present_returns_true(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    auth = load_auth()
    assert auth.settings_ready() is True


def test_settings_reload_picks_up_changed_env(uzora_env: pytest.MonkeyPatch) -> None:
    """Settings must not be cached across a reload — changing os.environ and
    reloading auth.py must be reflected in the new module object."""
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    first = load_auth()
    assert first.AUTHENTIK_URL == "http://authentik:9000"

    uzora_env.setenv("AUTHENTIK_URL", "http://authentik-2:9000")
    second = load_auth()
    assert second.AUTHENTIK_URL == "http://authentik-2:9000"
