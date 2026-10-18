from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, SettingsError, load_settings, read_env_file
from tests.conftest import TEST_SECRET


def test_env_file_parsing_ignores_comments_and_quotes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('# comment\n\nSESSION_SECRET="abc"\nBROKEN LINE\n', encoding="utf-8")
    assert read_env_file(env) == {"SESSION_SECRET": "abc"}


def test_allowed_emails_are_split_and_casefolded() -> None:
    s = Settings(session_secret=TEST_SECRET, allowed_emails=" A@example.com, b@example.com ,")  # type: ignore[arg-type]
    assert s.allowed_emails == frozenset({"a@example.com", "b@example.com"})


def test_short_session_secret_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(session_secret="short")


def test_test_auth_refused_on_public_url() -> None:
    with pytest.raises(ValidationError):
        Settings(session_secret=TEST_SECRET, test_auth=True, public_base_url="https://a.trycloudflare.com")


def test_test_auth_allowed_on_localhost() -> None:
    assert Settings(session_secret=TEST_SECRET, test_auth=True).test_auth


@pytest.mark.parametrize("secret", ["replace-me" * 4, "x" * 40, "REPLACE-with-random-value-please!!"])
def test_placeholder_or_trivial_secret_rejected(secret: str) -> None:
    with pytest.raises(ValidationError):
        Settings(session_secret=secret)


def test_invalid_settings_error_does_not_echo_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", "SECRETabcdefghij")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-appear")
    monkeypatch.setenv("TEST_AUTH", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://a.trycloudflare.com")
    with pytest.raises(SettingsError) as info:
        load_settings(tmp_path / "missing.env")
    message = str(info.value)
    assert "session_secret" in message
    assert "SECRETabcdefghij" not in message and "sk-should" not in message


def test_env_example_does_not_validate_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("SESSION_SECRET", "TEST_AUTH", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SettingsError):
        load_settings(Path(__file__).resolve().parents[2] / ".env.example")


def test_environment_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("SESSION_SECRET=" + "fedcba9876543210" * 2 + "\nTEST_AUTH=0\n", encoding="utf-8")
    monkeypatch.setenv("SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("TEST_AUTH", "1")
    s = load_settings(env)
    assert s.session_secret == TEST_SECRET
    assert s.test_auth is True


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000/", "https://abc-def.trycloudflare.com"])
def test_valid_public_base_urls(url: str) -> None:
    assert Settings(session_secret=TEST_SECRET, public_base_url=url).public_base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url", ["http://example.com", "https://example.com/app", "ftp://localhost", "https://user:pw@host.example.com", "x"]
)
def test_invalid_public_base_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(session_secret=TEST_SECRET, public_base_url=url)


def test_allowed_emails_must_be_valid() -> None:
    with pytest.raises(ValidationError) as info:
        Settings(session_secret=TEST_SECRET, allowed_emails="ok@example.com, not-an-email")  # type: ignore[arg-type]
    assert "not-an-email" not in str(info.value).split("input_value")[0]


def test_require_names_missing_settings_without_values() -> None:
    s = Settings(session_secret=TEST_SECRET, google_client_id="id.apps.example")
    with pytest.raises(SettingsError) as info:
        s.require("google_client_id", "google_client_secret", "openai_api_key")
    assert "GOOGLE_CLIENT_SECRET" in str(info.value) and "OPENAI_API_KEY" in str(info.value)
    assert "GOOGLE_CLIENT_ID" not in str(info.value)
    s.require("google_client_id")


def test_no_personal_or_secret_defaults() -> None:
    s = Settings(session_secret=TEST_SECRET)
    assert (s.openai_api_key, s.google_client_id, s.google_client_secret, s.allowed_emails) == ("", "", "", frozenset())
    assert s.test_auth is False


@pytest.mark.parametrize("url", ["http://user:pw@localhost:8000", "http://u@127.0.0.1"])
def test_local_base_url_with_credentials_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(session_secret=TEST_SECRET, public_base_url=url)


def test_empty_database_path_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(session_secret=TEST_SECRET, database_path="")  # type: ignore[arg-type]


@pytest.mark.parametrize("flag", ["yes", "on", "2", "maybe"])
def test_unrecognized_test_auth_value_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.setenv("SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("TEST_AUTH", flag)
    with pytest.raises(SettingsError, match="test_auth"):
        load_settings(tmp_path / "missing.env")


# ---------------------------------------------------------------- APP_MODE and the AI switch (B-14)
def test_mode_defaults_to_personal_and_personal_still_needs_a_secret() -> None:
    assert Settings(session_secret=TEST_SECRET).app_mode == "personal"
    with pytest.raises(ValidationError, match="SESSION_SECRET is required"):
        Settings()


@pytest.mark.parametrize("url", ["https://localhost:8000", "https://abc-def.trycloudflare.com",
                                 "https://demo.example.com", "http://example.com"])
def test_demo_mode_refuses_non_local_or_https_base_url(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(app_mode="demo", public_base_url=url)


@pytest.mark.parametrize("url", ["http://localhost", "http://localhost:8000", "http://127.0.0.1:9000/"])
def test_demo_mode_accepts_local_http_base_url(url: str) -> None:
    assert Settings(app_mode="demo", public_base_url=url).is_demo


def test_demo_mode_refuses_test_auth() -> None:
    with pytest.raises(ValidationError, match="TEST_AUTH is not allowed in demo mode"):
        Settings(app_mode="demo", test_auth=True)


def test_demo_mode_generates_an_ephemeral_secret_per_process() -> None:
    a, b = Settings(app_mode="demo"), Settings(app_mode="demo")
    assert len(a.session_secret) == 64 and a.session_secret != b.session_secret


def test_demo_mode_still_rejects_a_placeholder_secret() -> None:
    with pytest.raises(ValidationError, match="leave it empty in demo mode"):
        Settings(app_mode="demo", session_secret="replace-me" * 4)


def test_demo_mode_lists_ignored_google_settings_without_values() -> None:
    s = Settings(app_mode="demo", google_client_id="id.apps.example", allowed_emails="a@example.com")  # type: ignore[arg-type]
    assert s.ignored_in_demo() == ["GOOGLE_CLIENT_ID", "ALLOWED_EMAILS"]
    assert Settings(session_secret=TEST_SECRET, google_client_id="x").ignored_in_demo() == []


def test_unknown_app_mode_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "public")
    with pytest.raises(SettingsError, match="app_mode"):
        load_settings(tmp_path / "missing.env")


def test_demo_mode_loads_with_no_env_file_and_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("SESSION_SECRET", "TEST_AUTH", "PUBLIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "AI_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_MODE", " Demo ")
    s = load_settings(tmp_path / "missing.env")
    assert s.is_demo and s.public_base_url == "http://localhost:8000" and not s.ai_enabled


@pytest.mark.parametrize(("flag", "key", "model", "enabled"), [
    ("1", "sk-test", "m", True), ("", "sk-test", "m", True), ("0", "sk-test", "m", False),
    ("1", "", "m", False), ("1", "sk-test", "", False), ("1", "  ", "m", False),
])
def test_ai_enabled_needs_switch_key_and_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, key: str,
                                               model: str, enabled: bool) -> None:
    monkeypatch.setenv("SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("AI_ENABLED", flag)
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("OPENAI_MODEL", model)
    assert load_settings(tmp_path / "missing.env").ai_enabled is enabled


def test_invalid_ai_enabled_value_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("AI_ENABLED", "yes")
    with pytest.raises(SettingsError, match="AI_ENABLED"):
        load_settings(tmp_path / "missing.env")
