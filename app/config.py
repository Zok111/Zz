"""Application settings, loaded from the project-root .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Market data (Pocket Option, read-only)
    po_ssid: SecretStr | None = None
    po_require_demo: bool = True
    po_time_offset_seconds: int | None = None  # None = auto-detect
    po_stale_after_seconds: float = 10.0
    po_probe_asset: str = "EURUSD_otc"

    # Later phases
    telegram_bot_token: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None

    log_level: str = "INFO"

    @field_validator("po_time_offset_seconds", mode="before")
    @classmethod
    def _parse_offset(cls, value):
        if value is None or (isinstance(value, str) and value.strip().lower() in ("", "auto")):
            return None
        return int(value)

    @field_validator("po_ssid", "telegram_bot_token", "gemini_api_key", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("gemini_model", mode="before")
    @classmethod
    def _empty_str_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
