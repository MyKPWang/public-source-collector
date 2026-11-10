from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL


def sqlite_url(path: Path) -> str:
    return str(URL.create("sqlite+pysqlite", database=str(path.expanduser().resolve())))


def create_sqlite_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(sqlite_url(path), future=True)

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def upgrade_database(path: Path) -> None:
    project_root = Path(__file__).resolve().parents[4]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    command.upgrade(config, "head")
