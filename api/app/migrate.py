"""Run Alembic migrations from code, so the API, the tests and `python -m app.migrate`
all bring the database to the same schema the same way."""

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import get_settings

API_DIR = Path(__file__).resolve().parent.parent


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    cfg.attributes["database_url"] = database_url or get_settings().database_url
    return cfg


def upgrade(database_url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


if __name__ == "__main__":
    upgrade()
