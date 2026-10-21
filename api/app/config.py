from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./bridgeops.db"
    redis_url: str = "redis://localhost:6379/0"
    upload_dir: str = "./data/uploads"
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

    # Auto-accept thresholds. Values moved 0.85 -> 0.90 after the Gemini sweep (see README)
    mapping_threshold: float = 0.80
    normalize_threshold: float = 0.90
    duplicate_threshold: float = 0.85


@lru_cache
def get_settings() -> Settings:
    return Settings()
