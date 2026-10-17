"""Programmatic Alembic entry point used by API, worker, and maintenance jobs."""

from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import settings


def run_migrations(_engine=None) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("alembic_migrations")))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")
