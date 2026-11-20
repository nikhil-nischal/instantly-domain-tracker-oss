"""Typed configuration loaded from .env."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application settings — immutable-ish, typed access to env vars."""

    # API
    INSTANTLY_API_KEY: str = ""
    INSTANTLY_BASE_URL: str = "https://api.instantly.ai"

    # Database
    DATABASE_PATH: str = ""

    # Burn thresholds
    BURN_REPLY_RATE_THRESHOLD: float = 1.0
    AT_RISK_REPLY_RATE_THRESHOLD: float = 2.0
    BURN_BOUNCE_RATE_THRESHOLD: float = 8.0
    AT_RISK_BOUNCE_RATE_THRESHOLD: float = 5.0

    # Analysis
    LOOKBACK_DAYS: int = 30
    MIN_VOLUME_THRESHOLD: int = 50

    # Relative detection
    RELATIVE_RATE_MULTIPLIER: float = 0.3
    RELATIVE_MAX_REPLY_RATE: float = 3.0

    # Provider-specific burn thresholds
    # Gmail/Google Workspace
    GOOGLE_BURN_REPLY_RATE: float = 1.0
    GOOGLE_AT_RISK_REPLY_RATE: float = 2.0
    GOOGLE_BURN_BOUNCE_RATE: float = 8.0
    GOOGLE_AT_RISK_BOUNCE_RATE: float = 5.0

    # Outlook / Microsoft 365
    OUTLOOK_BURN_REPLY_RATE: float = 0.5
    OUTLOOK_AT_RISK_REPLY_RATE: float = 1.0
    OUTLOOK_BURN_BOUNCE_RATE: float = 10.0
    OUTLOOK_AT_RISK_BOUNCE_RATE: float = 7.0

    # Backfill
    BACKFILL_START_DATE: str = "2026-01-01"

    def __init__(self):
        self._load()

    def _load(self):
        self.INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY", "")
        self.INSTANTLY_BASE_URL = os.getenv(
            "INSTANTLY_BASE_URL", "https://api.instantly.ai"
        ).rstrip("/")

        # DATABASE_PATH: if set explicitly (env var), it overrides multi-user resolution.
        # If empty (default), get_connection() resolves per-user paths automatically
        # under data/users/<slug>/tracker.db — the correct multi-user behavior.
        db_path = os.getenv("DATABASE_PATH", "")
        self.DATABASE_PATH = db_path

        def _float(key: str, default: float) -> float:
            raw = os.getenv(key)
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        def _int(key: str, default: int) -> int:
            raw = os.getenv(key)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        self.BURN_REPLY_RATE_THRESHOLD = _float(
            "BURN_REPLY_RATE_THRESHOLD", 1.0
        )
        self.AT_RISK_REPLY_RATE_THRESHOLD = _float(
            "AT_RISK_REPLY_RATE_THRESHOLD", 2.0
        )
        self.BURN_BOUNCE_RATE_THRESHOLD = _float(
            "BURN_BOUNCE_RATE_THRESHOLD", 8.0
        )
        self.AT_RISK_BOUNCE_RATE_THRESHOLD = _float(
            "AT_RISK_BOUNCE_RATE_THRESHOLD", 5.0
        )
        self.LOOKBACK_DAYS = _int("LOOKBACK_DAYS", 30)
        self.MIN_VOLUME_THRESHOLD = _int("MIN_VOLUME_THRESHOLD", 50)
        self.RELATIVE_RATE_MULTIPLIER = _float(
            "RELATIVE_RATE_MULTIPLIER", 0.3
        )
        self.RELATIVE_MAX_REPLY_RATE = _float(
            "RELATIVE_MAX_REPLY_RATE", 3.0
        )

        # Provider thresholds
        self.GOOGLE_BURN_REPLY_RATE = _float("GOOGLE_BURN_REPLY_RATE", 1.0)
        self.GOOGLE_AT_RISK_REPLY_RATE = _float("GOOGLE_AT_RISK_REPLY_RATE", 2.0)
        self.GOOGLE_BURN_BOUNCE_RATE = _float("GOOGLE_BURN_BOUNCE_RATE", 8.0)
        self.GOOGLE_AT_RISK_BOUNCE_RATE = _float("GOOGLE_AT_RISK_BOUNCE_RATE", 5.0)
        self.OUTLOOK_BURN_REPLY_RATE = _float("OUTLOOK_BURN_REPLY_RATE", 0.5)
        self.OUTLOOK_AT_RISK_REPLY_RATE = _float("OUTLOOK_AT_RISK_REPLY_RATE", 1.0)
        self.OUTLOOK_BURN_BOUNCE_RATE = _float("OUTLOOK_BURN_BOUNCE_RATE", 10.0)
        self.OUTLOOK_AT_RISK_BOUNCE_RATE = _float("OUTLOOK_AT_RISK_BOUNCE_RATE", 7.0)

        self.BACKFILL_START_DATE = os.getenv(
            "BACKFILL_START_DATE", "2026-01-01"
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.INSTANTLY_API_KEY)


settings = Settings()
