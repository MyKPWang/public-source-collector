from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .errors import MissingCredentials
from .models import Article, Credentials, Feed


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                token TEXT NOT NULL,
                cookie TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feeds (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                fakeid TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                feed_id TEXT NOT NULL REFERENCES feeds(id),
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                digest TEXT NOT NULL,
                cover_url TEXT NOT NULL,
                publish_time INTEGER NOT NULL,
                first_seen_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_articles_feed_time ON articles(feed_id, publish_time DESC);
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_lock (
                name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collection_runs (
                id TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                status TEXT NOT NULL,
                feed_count INTEGER NOT NULL DEFAULT 0,
                article_count INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_collection_runs_started ON collection_runs(started_at DESC);
            """
        )
        self.connection.commit()

    def save_credentials(self, credentials: Credentials) -> None:
        now = int(time.time())
        self.connection.execute(
            """
            INSERT INTO credentials(id, token, cookie, updated_at) VALUES(1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET token = excluded.token, cookie = excluded.cookie, updated_at = excluded.updated_at
            """,
            (credentials.token, credentials.cookie, now),
        )
        self.connection.commit()

    def load_credentials(self) -> Credentials:
        row = self.connection.execute("SELECT token, cookie FROM credentials WHERE id = 1").fetchone()
        if row is None:
            raise MissingCredentials("no local WeChat credentials were imported")
        return Credentials(token=row["token"], cookie=row["cookie"])

    def add_feed(self, feed_id: str, name: str, fakeid: str) -> Feed:
        self.connection.execute(
            """
            INSERT INTO feeds(id, name, fakeid, enabled, created_at) VALUES(?, ?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, fakeid = excluded.fakeid, enabled = 1
            """,
            (feed_id, name, fakeid, int(time.time())),
        )
        self.connection.commit()
        return Feed(id=feed_id, name=name, fakeid=fakeid, enabled=True)

    def list_feeds(self, enabled_only: bool = False) -> list[Feed]:
        query = "SELECT id, name, fakeid, enabled FROM feeds"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY name COLLATE NOCASE"
        return [Feed(id=row["id"], name=row["name"], fakeid=row["fakeid"], enabled=bool(row["enabled"])) for row in self.connection.execute(query)]

    def get_feed(self, feed_id: str) -> Feed | None:
        row = self.connection.execute("SELECT id, name, fakeid, enabled FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if row is None:
            return None
        return Feed(id=row["id"], name=row["name"], fakeid=row["fakeid"], enabled=bool(row["enabled"]))

    def upsert_articles(self, articles: Iterable[Article]) -> int:
        now = int(time.time())
        count = 0
        for article in articles:
            self.connection.execute(
                """
                INSERT INTO articles(id, feed_id, title, url, digest, cover_url, publish_time, first_seen_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    feed_id = excluded.feed_id,
                    title = excluded.title,
                    url = excluded.url,
                    digest = excluded.digest,
                    cover_url = excluded.cover_url,
                    publish_time = excluded.publish_time,
                    updated_at = excluded.updated_at
                """,
                (article.id, article.feed_id, article.title, article.url, article.digest, article.cover_url, article.publish_time, now, now),
            )
            count += 1
        self.connection.commit()
        return count

    def get_state_int(self, key: str, default: int = 0) -> int:
        row = self.connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return int(row["value"]) if row else default

    def set_state_int(self, key: str, value: int) -> None:
        self.connection.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.connection.commit()

    def acquire_run_lock(self, owner: str, lease_seconds: int) -> bool:
        now = int(time.time())
        with self.connection:
            self.connection.execute("DELETE FROM run_lock WHERE expires_at <= ?", (now,))
            existing = self.connection.execute("SELECT owner FROM run_lock WHERE name = 'collection'").fetchone()
            if existing is not None:
                return False
            self.connection.execute(
                "INSERT INTO run_lock(name, owner, expires_at) VALUES('collection', ?, ?)",
                (owner, now + lease_seconds),
            )
        return True

    def release_run_lock(self, owner: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM run_lock WHERE name = 'collection' AND owner = ?", (owner,))

    def start_run(self, run_id: str, command: str, feed_count: int) -> None:
        self.connection.execute(
            """
            INSERT INTO collection_runs(id, command, started_at, status, feed_count)
            VALUES(?, ?, ?, 'running', ?)
            """,
            (run_id, command, int(time.time()), feed_count),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, status: str, article_count: int = 0, message: str = "") -> None:
        self.connection.execute(
            """
            UPDATE collection_runs
            SET finished_at = ?, status = ?, article_count = ?, message = ?
            WHERE id = ?
            """,
            (int(time.time()), status, article_count, message, run_id),
        )
        self.connection.commit()

    def list_runs(self, limit: int = 20) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT id, command, started_at, finished_at, status, feed_count, article_count, message
            FROM collection_runs
            ORDER BY started_at DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]
