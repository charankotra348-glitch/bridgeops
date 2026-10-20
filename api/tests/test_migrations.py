from alembic import command
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, inspect

from app import migrate


def test_migrations_build_the_schema_and_match_the_models(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    migrate.upgrade(url)

    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"sources", "jobs", "mappings", "decisions", "audit_log"} <= tables

    try:  # fails if a model changed without a migration
        command.check(migrate.alembic_config(url))
    except CommandError as exc:
        raise AssertionError(f"models and migrations differ; autogenerate a revision: {exc}") from exc


def test_downgrade_to_base_and_back(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    migrate.upgrade(url)
    command.downgrade(migrate.alembic_config(url), "base")
    assert set(inspect(create_engine(url)).get_table_names()) <= {"alembic_version"}
    migrate.upgrade(url)
