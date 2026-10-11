from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./bridgeops.db"
    redis_url: str = "redis://localhost:6379/0"
    upload_dir: str = "./data/uploads"
    max_upload_mb: int = 25

    decision_provider: str = "rules"  # "rules" | "jev"
    typesafe_api_key: str = ""
    typesafe_api_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_model: str = "jev-latest"
    jev_timeout_s: float = 10.0

    # Auto-accept thresholds (tuned later on the eval set)
    mapping_threshold: float = 0.80
    normalize_threshold: float = 0.85


@lru_cache
def get_settings() -> Settings:
    return Settings()
