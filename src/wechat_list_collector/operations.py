from __future__ import annotations

import os
import sqlite3
from pathlib import Path


class OperationError(RuntimeError):
    pass


def backup_database(source: Path, destination: Path) -> None:
    if not source.exists():
        raise OperationError("source database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(temporary, 0o600)
    temporary.replace(destination)


def restore_database(source: Path, destination: Path) -> None:
    if not source.exists():
        raise OperationError("backup database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".restore.tmp")
    if temporary.exists():
        temporary.unlink()
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(temporary, 0o600)
    temporary.replace(destination)


def integrity_check(database: Path) -> str:
    if not database.exists():
        raise OperationError("database does not exist")
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else "unknown"
