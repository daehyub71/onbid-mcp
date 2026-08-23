# onbid-mcp

[한국어 README](README_KO.md)

An MCP server that exposes Korean public auction (공매) property data from
[온비드 (KAMCO)](https://www.onbid.co.kr) OpenAPI to LLM clients such as Claude Code.

> **Status: in progress.** Milestones M0–M3 are complete — collection, normalization, and
> Supabase loading run end to end against live data (6,902 Seoul listings loaded on
> 2026-08-23). Geocoding (M4), the query layer (M5), and the MCP tools themselves (M6) are
> not built yet.

## Why this exists

온비드 publishes auction listings through an OpenAPI, but the raw feed is awkward to reason
about: prices arrive as free text, addresses are missing from the list endpoint, ended
listings simply vanish from responses, and the daily request quota is small enough that a
naive crawler never finishes. This project absorbs those quirks once, in a batch pipeline,
so that an LLM can ask plain questions like "which properties in 강남구 have failed to sell
more than three times?"

## What is built

| Layer | Package | State |
|---|---|---|
| Collection | `core/onbid`, `core/codes` | ✅ Rate limiting, retry classification, pagination, code trees |
| Normalization | `core/normalizer` | ✅ Addresses, amounts, datetimes, PNU, status derivation |
| Loading | `core/store`, `migrations/` | ✅ Composite-key upsert, change history, tombstones, batch metadata |
| Orchestration | `core/pipeline` | ✅ Listing / round / code batches with explicit commit boundaries |
| Geocoding | `core/geocoder` | ⬜ M4 |
| Query + stats | `core/stats`, `api/` | ⬜ M5 |
| MCP tools | `onbid_mcp/` | ⬜ M6 |

## Design notes worth knowing

These are the decisions that took measurement rather than reading the API guide.

**Ended listings are marked, never deleted.** 온비드 only returns in-progress items, so a
listing that disappears cannot be distinguished from one that never existed. Rows are marked
`종료추정` instead, and three independent conditions must hold before that judgement runs:
full-scan mode, matching collection scope, and a complete scan. Getting the scope wrong would
have flipped 6,594 healthy rows in a measured trial.

**The primary key is composite.** `cltrMngNo` alone is not unique — one management number
carries up to 10 `pbctCdtnNo` values. Detail and bid-info lookups require both.

**Ratios are computed, not read.** 온비드 ships ratio fields, but their measured fill rate is
0%. `min_bid_rate` is derived from the amounts, and it legitimately exceeds 1.0 (measured max
150.2%, in 9.8% of rows), so it is never clamped.

**Round history rolls by last attempt time.** The bid-info endpoint allows 1,000 calls a day
against 1,088 eligible items. Rather than carrying a resume token — the progress position is
a *set*, not a scalar — targets are ordered by when they were last attempted, which makes the
rotation stateless and self-healing.

**Change diffing happens before the upsert.** Comparing afterwards silently yields zero
differences, so the two steps are bundled into one call that cannot be ordered wrongly.

## Requirements

- Python 3.11+
- A 공공데이터포털 service key with the 온비드 APIs enabled
- A Supabase (PostgreSQL) project
- A Kakao Local REST API key (needed from M4 onward)

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # then fill in your keys
python scripts/migrate.py     # create tables (safe to re-run)
```

## Usage

```bash
python scripts/run_batch.py                    # codes → listings → rounds
python scripts/run_batch.py --sgg 강남구 --rounds-budget 0
python scripts/run_batch.py --mode delta --since 20260820
python scripts/run_batch.py --dry-run
```

## Development

```bash
ruff check .
mypy core/ onbid_mcp/ api/ tests/ scripts/
pytest -q            # 450 tests, no network
pytest -m db -q      # 164 tests against Supabase, inside rolled-back transactions
pytest -m live -q    # real API calls, excluded by default
```

Database tests run against the real schema inside transactions that are always rolled back,
so they leave no trace. Tests never hit the network unless explicitly marked `live`.

## Documentation

Specification-driven; the documents are the source of truth and are written in Korean.

- [docs/SPEC.md](docs/SPEC.md) — requirements, data model, MCP tool contracts, open questions
- [docs/PLAN.md](docs/PLAN.md) — architecture, milestones, test strategy, risks
- [docs/TASKS.md](docs/TASKS.md) — progress dashboard and troubleshooting log
- [docs/API_FINDINGS.md](docs/API_FINDINGS.md) — measured API behavior; **takes precedence
  over the official guides**, which were wrong in several places

## Security

Keys live in `.env` only. The 온비드 API requires the service key as a query parameter, and
httpx logs full request URLs at INFO level, so `core/onbid/client.py` lowers the `httpx`
logger to WARNING at import time — otherwise enabling logging would leak the key. All
`onbid_*` tables have RLS enabled with no policies and revoked grants; access is
`service_role` only, verified by measurement (HTTP 401 for anon on all tables).

## License

Not yet chosen. The 온비드 API guide documents are intentionally excluded from this
repository; the response structures used here are recorded in `docs/API_FINDINGS.md` from
live measurement.
