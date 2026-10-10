"""Application settings, loaded from the project-root .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
WEBAPP_DIR = PROJECT_ROOT / "webapp"

EXPIRATIONS = [30, 60, 120, 180, 300, 600, 900, 1500, 1800, 3600, 7200, 10800, 14400]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PRISM AI"

    # Telegram
    telegram_bot_token: SecretStr | None = None
    allowed_telegram_user_ids: str = ""  # comma separated; empty = anyone who opens your bot
    public_url: str = ""  # https URL of the Mini App; empty = start a cloudflared quick tunnel
    port: int = 8000
    dev_allow_browser: bool = False  # allow opening without Telegram (local testing only)

    # Gemini
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    ai_top_candidates: int = 3
    ai_candles: int = 128
    ai_min_confidence: float = 0.6
    ai_timeout_seconds: float = 60.0

    # Market data (Pocket Option, read-only)
    po_ssid: SecretStr | None = None
    po_require_demo: bool = True
    po_time_offset_seconds: int | None = None  # None = auto-detect
    po_stale_after_seconds: float = 10.0
    po_probe_asset: str = "EURUSD_otc"

    # Scanner
    scan_max_pairs: int = 20
    scan_min_payout: int = 0
    scan_include_otc: bool = True
    scan_include_regular: bool = True

    # Signal flow
    signal_countdown_seconds: int = 20
    signal_min_interval_seconds: float = 5.0
    reveal_max_move_atr: float = 1.5
    settle_max_quote_staleness_seconds: float = 10.0

    db_path: Path = DATA_DIR / "signals.db"
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
        return value.strip() if isinstance(value, str) else value

    @property
    def allowed_user_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_telegram_user_ids.replace(" ", "").split(",") if x}


@lru_cache
def get_settings() -> Settings:
    return Settings()
