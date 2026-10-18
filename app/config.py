"""Settings loaded from environment variables (and an optional local .env file).

Nothing personal or secret has a default: credentials, allowed sign-in emails and the
public URL all come from the environment (see .env.example). Integration credentials are
checked with `Settings.require` when the feature that needs them is enabled.

`APP_MODE` selects `personal` (default: your own database, Google sign-in) or `demo`
(synthetic data, ADR-005 §6.1). Demo mode is localhost-only: an https or tunnel
`PUBLIC_BASE_URL` and `TEST_AUTH=1` are refused, Google settings and `DATABASE_PATH` are
not used, and `SESSION_SECRET` may be left empty (a random per-process secret is generated).
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.urls import UrlError, validate_http_url

LOCAL_HOSTS = {"localhost", "127.0.0.1"}
_EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


AppMode = Literal["personal", "demo"]
GOOGLE_SETTINGS = ("google_client_id", "google_client_secret", "allowed_emails")
"""Settings that demo mode ignores (it has no Google sign-in)."""


class Settings(BaseModel):
    app_mode: AppMode = "personal"
    openai_api_key: str = ""
    openai_model: str = ""
    ai_enabled_flag: bool = True  # AI_ENABLED; see `ai_enabled` for the effective switch
    ai_max_tokens_per_question: int = Field(default=40000, ge=5000, le=200000)  # AI_MAX_TOKENS_PER_QUESTION
    ai_questions_per_hour: int = Field(default=20, ge=1, le=200)  # AI_QUESTIONS_PER_HOUR
    google_client_id: str = ""
    google_client_secret: str = ""
    session_secret: str = ""  # required in personal mode; optional in demo mode
    allowed_emails: frozenset[str] = frozenset()
    public_base_url: str = "http://localhost:8000"
    database_path: Path = Path("data/app.db")
    test_auth: bool = False

    @field_validator("app_mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower() or "personal"
            if value not in {"personal", "demo"}:
                raise ValueError("APP_MODE must be personal or demo")
        return value

    @property
    def is_demo(self) -> bool:
        return self.app_mode == "demo"

    @property
    def ai_enabled(self) -> bool:
        """AI features may run only when AI_ENABLED=1 and both OPENAI_API_KEY and OPENAI_MODEL are set."""
        return self.ai_enabled_flag and bool(self.openai_api_key.strip()) and bool(self.openai_model.strip())

    def ignored_in_demo(self) -> list[str]:
        """Names of the Google settings that are set but unused because the app runs in demo mode."""
        return [name.upper() for name in GOOGLE_SETTINGS if self.is_demo and getattr(self, name)]

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def _split_emails(cls, value: object) -> object:
        if isinstance(value, str):
            emails = frozenset(e.strip().casefold() for e in value.split(",") if e.strip())
            bad = [e for e in emails if not _EMAIL.match(e)]
            if bad:
                raise ValueError(f"{len(bad)} entr{'y is' if len(bad) == 1 else 'ies are'} not a valid email address")
            return emails
        return value

    @field_validator("public_base_url")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if "@" in parsed.netloc:
            raise ValueError("must not contain a username or password")
        if parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS and not parsed.path.strip("/"):
            return value.rstrip("/")
        try:
            validate_http_url(value)
        except UrlError as exc:
            raise ValueError(f"must be http://localhost[:port] or an https URL ({exc})") from None
        if parsed.scheme != "https" or parsed.path.strip("/"):
            raise ValueError("a public URL must be https:// with no path")
        return value.rstrip("/")

    def require(self, *fields: str) -> None:
        """Raise SettingsError naming every listed setting that is empty (values never shown)."""
        missing = [f.upper() for f in fields if not getattr(self, f)]
        if missing:
            raise SettingsError(f"Missing required configuration: {', '.join(missing)} (see .env.example)")

    @model_validator(mode="after")
    def _test_auth_only_on_localhost(self) -> Settings:
        if self.test_auth and urlparse(self.public_base_url).hostname not in LOCAL_HOSTS:
            raise ValueError("TEST_AUTH is only allowed when PUBLIC_BASE_URL is localhost")
        return self

    @model_validator(mode="after")
    def _mode_rules(self) -> Settings:
        if not self.is_demo:
            if not self.session_secret:
                raise ValueError("SESSION_SECRET is required (see .env.example)")
            return self
        # Demo mode must never be reachable from outside this machine (ADR-005 §6.1, §6.3).
        parsed = urlparse(self.public_base_url)
        if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
            raise ValueError("in demo mode PUBLIC_BASE_URL must be http://localhost[:port] or "
                             "http://127.0.0.1[:port]; https and tunnel URLs are refused")
        if self.test_auth:
            raise ValueError("TEST_AUTH is not allowed in demo mode")
        if not self.session_secret:
            self.session_secret = secrets.token_hex(32)  # ephemeral: sessions end when the process stops
        return self

    @field_validator("database_path")
    @classmethod
    def _non_empty_path(cls, value: Path) -> Path:
        if not str(value).strip() or str(value) == ".":
            raise ValueError("DATABASE_PATH must be a file path")
        return value

    @field_validator("session_secret")
    @classmethod
    def _not_placeholder(cls, value: str) -> str:
        if not value:
            return value  # required or generated per mode (_mode_rules)
        if len(value) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        if "replace" in value.casefold() or len(set(value)) < 8:
            raise ValueError("SESSION_SECRET looks like a placeholder; generate a random one "
                             "(or leave it empty in demo mode)")
        return value


def read_env_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines; comments and blank lines are ignored."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(env_file: Path = Path(".env")) -> Settings:
    """Real environment variables take precedence over the .env file."""
    merged = read_env_file(env_file) | {k: v for k, v in os.environ.items()}
    fields = Settings.model_fields
    data: dict[str, object] = {n: merged[n.upper()] for n in fields if n.upper() in merged}
    data.pop("ai_enabled_flag", None)  # only the documented AI_ENABLED name is read
    for env_name, field, default in (("TEST_AUTH", "test_auth", False), ("AI_ENABLED", "ai_enabled_flag", True)):
        if env_name in merged:
            flag = merged[env_name].strip().lower()
            if flag not in {"", "0", "1", "true", "false"}:
                raise SettingsError(f"Invalid configuration: {field}: {env_name} must be 0 or 1")
            data[field] = default if flag == "" else flag in {"1", "true"}
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        # Report field names and reasons only: pydantic's default message echoes input values,
        # which here can be secrets.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}" for err in exc.errors()
        )
        raise SettingsError(f"Invalid configuration: {problems}") from None


class SettingsError(RuntimeError):
    """Configuration is missing or invalid; message never contains configured values."""
