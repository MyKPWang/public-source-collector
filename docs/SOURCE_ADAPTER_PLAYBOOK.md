# Public Source Adapter Playbook

This guide defines how a new public information source is admitted. The collector is a list-metadata system: the normal path collects only title, URL, publication time, cover image and list summary. It does not open article pages.

## Admission Contract

Every source must have all of the following before it is enabled in `catalog.toml`:

1. A human-verified public list page or official RSS feed, with the allowed host and list URL recorded in the catalog.
2. A source-owned adapter: `rss`, `json`, `browser`, or `browser_network`. Generic HTML guessing is not an acceptable production adapter.
3. A sample fixture containing normalized list cards and a test that asserts title, URL, summary and publication-time behavior.
4. Explicit failure markers and a stable reason code for blocked pages, invalid responses, missing browser-network payloads, and required-field failures.
5. An interval appropriate to the source. A source is never retried in a tight loop; a failed endpoint is recorded and the remaining sources continue.

`browser_network` has one additional boundary: the JSON response must be observed while a real browser opens the configured public list page. The collector must not turn that observation into a separate, bare HTTP API caller.

## Adapter Choice

| Situation | Preferred adapter | Why |
| --- | --- | --- |
| Official RSS or Atom feed | `rss` | Lowest maintenance cost; publication time and summary are usually present. |
| Stable, documented public list JSON | `json` | Lightweight only when verified as a public list endpoint. |
| Server-rendered or browser-rendered listing page | `browser` | The source-specific selector contract reads visible list cards. |
| Listing page loads its own public JSON while rendering | `browser_network` | The browser establishes the same page context before the response is parsed. |

## What We Borrow From Scrapling

Scrapling is useful as a design reference, not a required runtime dependency. We adopt its healthy ideas: source fixtures, replayable parsing, selector resilience, and per-site extraction ownership. We do **not** adopt stealth, anti-bot evasion, CAPTCHA bypass, proxy rotation, or browser fingerprint workarounds. A source that needs those mechanisms is not an eligible production source.

The next incremental improvement is a fixture runner under `tests/fixtures/public_sources/`: each adapter receives captured, non-sensitive list-page or list-JSON input and must produce the expected normalized `ListingItem` records. This catches site markup changes before a catalog entry is enabled or changed.

## What We Borrow From Scrapy

Scrapy is not justified for the current small, once-daily catalog. Its useful operational ideas are already represented here: source-level intervals, isolated failures, reason-code taxonomy, item validation, retry restraint, and run statistics. Consider a Scrapy worker only when the catalog grows past roughly 100 independently scheduled sources, requires durable multi-page queues, or needs distributed scheduling. Until then, adding it would create a second crawler runtime without improving the list-only contract.

## Review Checklist

- The source can be opened manually without login, account-specific state, or anti-bot workarounds.
- The adapter never navigates to an article URL in normal collection.
- Normalized URL removes tracking parameters and deduplicates correctly.
- Publication time includes a timezone or has a documented timezone fallback.
- Empty title, URL, digest, or publication time is visible as a rejected item and affects endpoint health.
- At least one successful fixture test and one expected-failure test exist.
- `sources diagnose --run-id <id>` shows enough context to identify the failing source, endpoint, reason code, duration and counts.
