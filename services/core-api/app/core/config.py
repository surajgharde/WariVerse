"""Typed application settings.  Everything configurable lives here."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
CrowdSource = Literal["live", "video", "sim"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- runtime ---------------------------------------------------------
    environment: Environment = "development"
    log_level: str = "INFO"
    service_name: str = "core-api"
    api_v1_prefix: str = "/api/v1"

    # --- datastores ------------------------------------------------------
    database_url: str = "postgresql+psycopg://wariverse:change-me-in-prod@localhost:5432/wariverse"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_size: int = 20
    db_max_overflow: int = 10

    # --- crypto ----------------------------------------------------------
    jwt_secret: str = "dev-only-jwt-secret-change-me-0000000000000000"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    phone_hash_secret: str = "dev-only-phone-hash-secret-change-me-000000"
    contact_encryption_key: str = ""
    # Root secret the per-day pass-signing keypair is derived from.  Rotating
    # this invalidates every QR already in a pilgrim's pocket — treat as
    # permanent for the duration of a Wari.
    qr_signing_secret: str = "dev-only-qr-signing-secret-change-me-00000"

    # --- otp -------------------------------------------------------------
    otp_ttl_seconds: int = 300
    otp_length: int = 6
    otp_max_per_hour: int = 3
    otp_max_attempts: int = 5
    otp_debug_echo: bool = False

    # --- rate limits (Section 9) ----------------------------------------
    rate_limit_pass_booking_per_day: int = 5
    rate_limit_sos_per_10min: int = 3

    # --- crowd -----------------------------------------------------------
    crowd_source: CrowdSource = "sim"
    ai_engine_url: str = "http://localhost:8100"
    ai_service_token: str = "dev-only-ai-service-token-change-me"
    # A reading older than this renders as stale in every UI (Section 4/M3).
    stale_reading_seconds: int = 90
    # Redis holds the latest zone snapshot so the map does not query Timescale
    # on every poll.  Five minutes is Section 4/M2 step 6; the TTL is also the
    # guarantee that a dead AI service stops answering rather than lying.
    density_cache_ttl_seconds: int = 300
    # No heartbeat for this long and the camera is marked offline, which drops
    # its zone's confidence rather than silently freezing the last reading.
    camera_offline_seconds: int = 120
    # One alert per zone per rule per cooldown.  A zone sitting at 5.2 p/m² for
    # ten minutes is one situation, not sixty alerts.
    alert_cooldown_seconds: int = 180
    # Ingest batches are capped so a wedged AI engine cannot post a 100 MB body.
    ingest_max_batch: int = 200

    # --- http ------------------------------------------------------------
    # NoDecode: the value arrives as a comma-separated string from .env, not as
    # JSON, so the validator below owns the parsing.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:5174", "http://localhost:5175"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def assert_production_safe(self) -> list[str]:
        """Return a list of settings that must not ship to production as-is."""
        problems: list[str] = []
        if "dev-only" in self.jwt_secret:
            problems.append("JWT_SECRET is still the development default")
        if "dev-only" in self.phone_hash_secret:
            problems.append("PHONE_HASH_SECRET is still the development default")
        if "dev-only" in self.ai_service_token:
            problems.append("AI_SERVICE_TOKEN is still the development default")
        if "dev-only" in self.qr_signing_secret:
            problems.append("QR_SIGNING_SECRET is still the development default — passes would be forgeable")
        if self.otp_debug_echo:
            problems.append("OTP_DEBUG_ECHO is on — OTPs would be returned over the API")
        if not self.contact_encryption_key:
            problems.append("CONTACT_ENCRYPTION_KEY is unset — contact table cannot be encrypted")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
