from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Credentials:
    token: str
    cookie: str


@dataclass(frozen=True)
class Feed:
    id: str
    name: str
    fakeid: str
    enabled: bool


@dataclass(frozen=True)
class Article:
    id: str
    feed_id: str
    title: str
    url: str
    digest: str
    cover_url: str
    publish_time: int


@dataclass(frozen=True)
class FetchResult:
    feed_id: str
    fetched: int
    inserted_or_updated: int
    status: str


@dataclass(frozen=True)
class ArticlePage:
    publish_record_count: int
    articles: tuple[Article, ...]
