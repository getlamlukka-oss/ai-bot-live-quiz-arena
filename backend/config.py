"""
Centralized environment configuration loader for AI Bot Live Quiz Arena.

All credentials, JWT secrets, Supabase keys, and Sentry DSNs are loaded exclusively
from environment variables / `.env` files — never hardcoded in source code.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DEFAULT_LOCAL_CACHE_PATH = DATA_DIR / "quiz_questions.json"

# Load `.env` once at import time without overriding explicit OS environment variables
# (this preserves Docker / CI / pytest monkeypatch overrides cleanly).
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable application settings resolved from environment variables."""

    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8080
    cors_origins: List[str] = field(default_factory=lambda: ["*"])

    # Authentication credentials (must be provided via .env / environment)
    app_username: str = ""
    app_password: str = ""

    # JWT settings
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 120

    # Phase 1: ROV Academy + Garena data acquisition settings
    rov_academy_url: str = "https://academy.rov.in.th"
    garena_url: str = "https://www.garena.co.th"
    rov_login_url: str = (
        "https://100055.connect.garena.com/oauth/login"
        "?response_type=code"
        "&client_id=100055"
        "&redirect_uri=https%3A%2F%2Facademy.rov.in.th%2Fauth%2Fcallback%2F"
        "&locale=th-TH"
        "&platform=1"
    )
    rov_username: str = ""
    rov_password: str = ""
    playwright_headless: bool = False

    # Supabase configuration
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_table: str = "quiz_questions"

    # Sentry monitoring
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 1.0

    # AI Bot thinking latency range (seconds)
    bot_min_think_seconds: float = 2.0
    bot_max_think_seconds: float = 4.0

    # External connection retry policy
    retry_max_attempts: int = 3
    retry_min_wait_seconds: float = 1.0
    retry_max_wait_seconds: float = 4.0

    # Local fallback dataset path
    local_cache_path: Path = DEFAULT_LOCAL_CACHE_PATH

    @property
    def has_supabase_config(self) -> bool:
        """True when a real Supabase URL and API key are both configured."""
        url = self.supabase_url.strip()
        key = self.supabase_key.strip()
        if not url or not key:
            return False
        if "your-project-ref" in url or "your_supabase" in key:
            return False
        return url.startswith("http://") or url.startswith("https://")

    @property
    def has_sentry_config(self) -> bool:
        """True when a valid Sentry DSN is configured."""
        dsn = self.sentry_dsn.strip()
        return bool(dsn and (dsn.startswith("https://") or dsn.startswith("http://")))


def load_settings() -> Settings:
    """Build a fresh Settings object from current environment variables."""
    cors_raw = os.getenv("CORS_ORIGINS", "*").strip()
    cors_origins = [origin.strip() for origin in cors_raw.split(",") if origin.strip()] or ["*"]

    # Ephemeral fallback secret prevents crash during local tooling if JWT_SECRET_KEY
    # was omitted, while production/Docker deployments must supply a stable secret.
    jwt_secret = os.getenv("JWT_SECRET_KEY", "").strip() or secrets.token_hex(32)

    min_think = max(0.0, _get_float("BOT_MIN_THINK_SECONDS", 2.0))
    max_think = max(min_think, _get_float("BOT_MAX_THINK_SECONDS", 4.0))

    return Settings(
        app_env=os.getenv("APP_ENV", "development").strip() or "development",
        host=os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=_get_int("PORT", 8080),
        cors_origins=cors_origins,
        app_username=os.getenv("APP_USERNAME", "").strip(),
        app_password=os.getenv("APP_PASSWORD", "").strip(),
        jwt_secret_key=jwt_secret,
        jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256").strip() or "HS256",
        jwt_expire_minutes=_get_int("JWT_EXPIRE_MINUTES", 120),
        rov_academy_url=os.getenv("ROV_ACADEMY_URL", "https://academy.rov.in.th").strip().rstrip("/"),
        garena_url=os.getenv("GARENA_URL", "https://www.garena.co.th").strip().rstrip("/"),
        rov_login_url=os.getenv(
            "ROV_LOGIN_URL",
            "https://100055.connect.garena.com/oauth/login"
            "?response_type=code&client_id=100055"
            "&redirect_uri=https%3A%2F%2Facademy.rov.in.th%2Fauth%2Fcallback%2F"
            "&locale=th-TH&platform=1",
        ).strip(),
        rov_username=os.getenv("ROV_USERNAME", "").strip(),
        rov_password=os.getenv("ROV_PASSWORD", "").strip(),
        playwright_headless=_get_bool("PLAYWRIGHT_HEADLESS", False),
        supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        supabase_key=os.getenv("SUPABASE_KEY", "").strip(),
        supabase_table=os.getenv("SUPABASE_TABLE", "quiz_questions").strip() or "quiz_questions",
        sentry_dsn=os.getenv("SENTRY_DSN", "").strip(),
        sentry_traces_sample_rate=_get_float("SENTRY_TRACES_SAMPLE_RATE", 1.0),
        bot_min_think_seconds=min_think,
        bot_max_think_seconds=max_think,
        retry_max_attempts=max(1, _get_int("RETRY_MAX_ATTEMPTS", 3)),
        retry_min_wait_seconds=max(0.0, _get_float("RETRY_MIN_WAIT_SECONDS", 1.0)),
        retry_max_wait_seconds=max(0.0, _get_float("RETRY_MAX_WAIT_SECONDS", 4.0)),
        local_cache_path=Path(os.getenv("LOCAL_CACHE_PATH", str(DEFAULT_LOCAL_CACHE_PATH))),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton settings accessor (call `get_settings.cache_clear()` in tests)."""
    return load_settings()
