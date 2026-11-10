# Public Source Collector

A local, list-only collector for a small set of public technology-news sources.
The active Pipeline path reads RSS/Atom listing metadata and never fetches
article full text or opens article links. The earlier WeChat implementation is
retained as legacy code and is no longer the production collection path.

> **Project status:** the active production path has versioned SQLite storage,
> catalog-driven RSS/Atom collection, endpoint-level audit records, Pipeline
> Read API v2, structured logs, backup/restore and a 23:50 systemd timer. The
> former WeChat queue, admin pages and QR authorization remain legacy code and
> are not part of the public-source deployment.

## Design documents

- [Public list ingestion design](docs/PUBLIC_LIST_INGESTION.md)
- [Architecture and locked decisions](ARCHITECTURE.md)
- [Implementation guide](docs/IMPLEMENTATION_GUIDE.md)
- [Data and API contracts](docs/DATA_AND_API_CONTRACTS.md)
- [Admin UI specification](docs/ADMIN_UI.md)
- [News daily console design](docs/NEWS_DAILY_CONSOLE_DESIGN.md)
- [Human and Agent usage guide](docs/USAGE_GUIDE.md)

Development is proceeding in the implementation-guide order. Keep current
capabilities separate from the target design when updating this README.

## Production Database Foundation

For a fresh production-rebuild database, run the versioned schema migration:

```bash
public-source-collector --config collector.toml db upgrade
```

The public list tables are additive: they do not require, read or modify a
previous WeChat credential/session database.

## Production Commands

```bash
# Register a versioned catalog, then fetch the listing endpoints once.
public-source-collector --config collector.toml sources sync
public-source-collector --config collector.toml sources collect --scheduled
# Target a recovery refresh at selected catalog sources.
public-source-collector --config collector.toml sources collect --source openai --source google-ai

# Start consumers of already collected data.
export PUBLIC_SOURCE_COLLECTOR_PIPELINE_TOKEN='long-random-value'
public-source-collector --config collector.toml api

# Operate SQLite safely at rest.
public-source-collector --config collector.toml sources health
public-source-collector --config collector.toml backup --to /secure/backups/collector.db
public-source-collector --config collector.toml integrity-check
```

The API listens on port `4100`; the Admin site defaults to loopback port `4101`.
Systemd unit and timer examples are in [deploy/systemd](deploy/systemd).

## Why this exists

The earlier WeChat route proved operationally dependent on an undocumented,
rate-limited internal API. The replacement prefers public, owned listing
endpoints and a strict metadata-only contract. It favors a small number of
complete operations over a broad feature set.

## First-time setup

```bash
cd public-source-collector
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp collector.example.toml collector.toml
public-source-collector --config collector.toml db upgrade
```

Set `PUBLIC_SOURCE_COLLECTOR_PIPELINE_TOKEN` before starting the read API. The
former `WECHAT_COLLECTOR_PIPELINE_TOKEN` is accepted temporarily for existing
installations. Copy
`public-sources.example.toml` to `public-sources.toml`, then explicitly enable
only endpoints whose list-level field contract has been validated. No WeChat
credential, QR login or browser is needed for the public-source Pipeline path.

The public-source collector makes one bounded listing request to each enabled
endpoint per run, with a persisted per-endpoint minimum interval (900 seconds
by default). Sources that require browser observation use an explicit adapter;
the default RSS/Atom path has no browser dependency. The supplied timer runs
at 23:50 with up to two minutes of harmless jitter.

For a report run, call `POST /api/v1/pipeline/refresh` first (all enabled
sources by default, or named `source_slugs`), then call
`GET /api/v1/pipeline/articles` with the report time window. The second call
reads only SQLite cache; Pipeline performs its own relevance filtering and
cross-source deduplication after it receives that initial list. See the
[Pipeline contract](docs/PUBLIC_LIST_INGESTION.md#pipeline-contract).

## Traceability

Every collection gets a UUID in `public_collection_runs`; each source endpoint
gets an independent `public_endpoint_runs` row. These records store request
outcome, accepted/rejected count, new/updated count and a stable reason code.
Expected terminal states are `completed`, `partial`, `failed`, or `skipped`.

Pipeline always queries the complete article cache by its report time range;
the `batch_id` only verifies that the requested refresh completed before that
query begins. A `running` batch older than
`public_sources.stale_run_seconds` is automatically marked failed before a
new collection can start. Catalog sync also disables sources and endpoints
removed from `public-sources.toml`.

The cache is indexed by `published_at + id` for the normal report window, and
by `source_id + published_at + id` for source-scoped pagination. The report
query is therefore a bounded index range scan, not a full database scan.

The same lifecycle is written as one JSON record per line to stderr and to the
rotating file configured by `[logging]` (by default `data/collector.log`). The
log includes run ID, source, endpoint, event, accepted/rejected/new/updated
count, duration and reason code. It deliberately excludes response bodies,
article content and request parameters. Each collection also writes a compact
health artifact to `data/health/`; configure `[health].webhook_url` to receive
an alert only when the artifact contains a failure or partial-result warning.
It also warns about empty list results, repeated empty results, stale sources
and large drops relative to recent list volume.

## Test

```bash
python -m unittest discover -s tests -v
```
