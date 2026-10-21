from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./bridgeops.db"
    redis_url: str = "redis://localhost:6379/0"
    max_upload_mb: int = 25
    auto_migrate: bool = True  # run Alembic migrations on startup; turn off when a deploy step runs them

    decision_provider: str = "rules"  # "rules" | "gemini" | "jev"
    typesafe_api_key: str = ""
    typesafe_api_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_model: str = "jev-latest"
    jev_timeout_s: float = 10.0

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_timeout_s: float = 30.0

    cors_origins: str = "http://localhost:5173"   # comma-separated; the web UI's dev server

    # Sign-in. ADMIN_* creates the first account on startup when there are no users yet.
    admin_email: str = ""
    admin_password: str = ""
    session_hours: int = 12
    cookie_secure: bool = False   # true when served over HTTPS

    hubspot_token: str = ""
    hubspot_base_url: str = "https://api.hubapi.com"
    hubspot_requests_per_10s: int = 90  # private apps on Free/Starter allow 100 per 10 s; keep headroom

    # Auto-accept thresholds. Values moved 0.85 -> 0.90 after the Gemini sweep (see README)
    mapping_threshold: float = 0.80
    normalize_threshold: float = 0.90
    duplicate_threshold: float = 0.85

    # Built web UI to serve from the API (set in the Docker image); empty = API only
    web_dist: str = ""

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, url: str) -> str:
        """Hosts hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the driver named."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
