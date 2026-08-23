# onbid-mcp

[한국어 README](README_KO.md)

An MCP server that lets an LLM query Korean public auction (공매) property data from
[온비드 (KAMCO)](https://www.onbid.co.kr).

Ask **"which properties in 강남구 have failed to sell more than three times?"** in Claude
Desktop and get answers from data you collected yourself — no subscription, no scraping.

> **Status.** Working end to end — the pipeline runs on a schedule, and the four tools are
> connected and answering in Claude Desktop against live data (6,902 Seoul listings, 99.9%
> geocoded). Remaining: final acceptance checks (M7) and a week of watching the scheduled
> batches. See [docs/TASKS.md](docs/TASKS.md) for the exact state.

---

## What you get

Four tools and four resources, over stdio:

| Tool | What it does |
|---|---|
| `search_auction_items` | Filter by region, usage, property type, private-contract eligibility, price, discount rate, failure count, deadline, status. Korean names work directly (`"강남구"`, `"아파트"`). Cursor paginated. |
| `get_auction_detail` | One property by management number, plus its sibling condition numbers and the original 온비드 link. |
| `get_auction_stats` | Distributions across six axes, plus winning-bid ratios. Aggregates only — never individual properties. |
| `get_address_geocode` | Address → coordinates, with a server-side daily cap. |

| Resource | What it holds |
|---|---|
| `onbid://codes/regions` | Districts and neighborhoods that **actually have listings** |
| `onbid://codes/usages` | The three-level usage category tree |
| `onbid://codes/property-types` | Property type codes |
| `onbid://dataset/status` | Batch timestamp, counts, geocoding rate — how fresh your data is |

Every response carries `meta` (source, `synced_at`, `is_realtime: false`, count, truncated,
notice) and `query_echo` (the filters actually applied, after defaults and clamping).

---

## Before you start

This server queries **your own database**, not a hosted service. You collect the data, so
you need your own credentials:

| What | Where | Notes |
|---|---|---|
| 온비드 service key | [공공데이터포털](https://www.data.go.kr) | Apply for the five 온비드 OpenAPIs. Approval is usually immediate for a development account. |
| Supabase project | [supabase.com](https://supabase.com) | Free tier is enough — the Seoul dataset is about 7,000 rows. Any PostgreSQL works. |
| Kakao REST API key | [Kakao Developers](https://developers.kakao.com) | Geocoding. Must be the **REST API key**, not the JavaScript one. |

Also: Python 3.11+ and Claude Desktop (or any MCP client that speaks stdio).

Scope is **Seoul, sale-type listings** by default. Widening it is a one-line filter change,
but the geocoding and quota numbers below assume Seoul.

---

## Setup

```bash
git clone https://github.com/daehyub71/onbid-mcp.git
cd onbid-mcp
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # fill in the three keys above
python scripts/migrate.py     # create tables (safe to re-run)
```

Then collect a first dataset. This takes about two minutes and stays well inside the daily
API quota:

```bash
python scripts/run_batch.py
```

You should see something like:

```
── 물건 ──
  ok · 수집 6902 · 적재 6902 · 이력 0 · tombstone 0
── 좌표 ──
  ok · 대상 500 · 좌표 500 (근사 0) · 실패 0 · 호출 133
```

Run it again with `--geocode-budget 1000` until `dataset/status` reports a geocoding rate
you're happy with — the cache absorbs most calls, so the whole 6,902 rows cost roughly 800
Kakao calls in total.

---

## Connecting Claude Desktop

Add the server to `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "onbid": {
      "command": "/absolute/path/to/onbid-mcp/venv/bin/python",
      "args": ["-m", "onbid_mcp.server"],
      "cwd": "/absolute/path/to/onbid-mcp",
      "env": {
        "PYTHONPATH": "/absolute/path/to/onbid-mcp",
        "SUPABASE_DATABASE_URL": "postgresql://...",
        "ONBID_SERVICE_KEY": "...",
        "KAKAO_REST_API_KEY": "..."
      }
    }
  }
}
```

Four things trip people up here:

- **`PYTHONPATH` is required — `cwd` alone is not enough.** Claude Desktop does not apply
  the `cwd` entry, so `python -m onbid_mcp.server` cannot find the package and the process
  dies instantly with `ModuleNotFoundError`. The app reports that as "Server disconnected",
  which looks like a connection problem rather than a path one.
- **Use the absolute path to the venv interpreter.** Claude Desktop does not inherit your
  shell `PATH`, so a bare `python` picks up a system interpreter with none of the dependencies.
- **Put the keys in `env`.** The app does not read the project's `.env` file.
- **Logs must never reach stdout.** stdout is the JSON-RPC channel; this server logs to
  stderr for exactly that reason. If you add prints, send them to stderr.

Restart Claude Desktop, then try:

> 강남구에서 3회 이상 유찰된 물건 중 최저가율 60% 이하인 것 보여줘

If it fails, read `~/Library/Logs/Claude/mcp-server-onbid.log` — the actual Python error is
in there, while the UI only says "Server disconnected".

To check the connection without Claude Desktop:

```bash
python scripts/mcp_smoke.py        # lists tools and calls one over stdio
```

---

## Keeping the data fresh

Two GitHub Actions workflows are included. Add `ONBID_SERVICE_KEY`,
`SUPABASE_DATABASE_URL`, and `KAKAO_REST_API_KEY` to your repository secrets and they run
on their own:

| Workflow | When (KST) | What |
|---|---|---|
| `onbid-daily` | Mon–Sat 04:00 | Changed listings + bid rounds + geocoding |
| `onbid-weekly` | Sun 04:00 | Code tables + **full scan** — the only run that can mark ended listings |

Cron is UTC-only, so 04:00 KST is 19:00 UTC the previous day, which shifts the weekday by
one. Check the last week with:

```bash
python scripts/batch_health.py
```

A skipped cron leaves no trace anywhere — GitHub only emails about runs that started and
failed — so this counts the days instead.

---

## Design notes worth knowing

These came from measurement, not from the API guide.

**Ended listings are marked, never deleted.** 온비드 returns in-progress items only, so a
listing that disappears is indistinguishable from one that never existed. Rows become
`종료추정` instead, and three conditions must all hold first: full-scan mode, matching
collection scope, and a completed scan. Getting the scope wrong flipped 6,594 healthy rows
in a measured trial.

**The primary key is composite.** `cltrMngNo` alone is not unique — one management number
carries up to ten `pbctCdtnNo` values, and the bid-info API returns the same round history
under each. Statistics deduplicate by `(management number, opening time, round)`; counting
rows made 13 real auction events look like 62.

**Ratios are computed, not read.** 온비드 ships ratio fields whose measured fill rate is 0%.
`min_bid_rate` is derived, and it legitimately exceeds 1.0 (measured max 150.2%, in 9.8% of
rows), so it is never clamped.

**Empty results are an error, not an empty list.** `no_result` tells the model to relax the
filters; an empty array would let it conclude "there are no such properties".

**The winning-bid statistics are biased, and that matters more than the numbers.** The only
completed auctions visible are ones that were won and then fell through — normally completed
sales never appear in the listing API. Every response carries that caveat.

---

## Development

```bash
ruff check .
mypy core/ onbid_mcp/ api/ tests/ scripts/
pytest -q            # 595 tests, no network
pytest -m db -q      # 361 tests against your database, inside rolled-back transactions
pytest -m live -q    # real API calls, excluded by default
```

Database tests run against the real schema inside transactions that always roll back, so
they leave no trace — verified by comparing table counts before and after. Pure tests pass
with a deliberately broken connection string.

There is also a local HTTP API (`api/main.py`) bound to loopback only, useful for poking at
the data with curl. It is not required for MCP use.

---

## Documentation

Specification-driven; the documents are the source of truth and are written in Korean.

- [docs/SPEC.md](docs/SPEC.md) — requirements, data model, MCP tool contracts, open questions
- [docs/PLAN.md](docs/PLAN.md) — architecture, milestones, test strategy, risks
- [docs/TASKS.md](docs/TASKS.md) — progress dashboard and troubleshooting log
- [docs/API_FINDINGS.md](docs/API_FINDINGS.md) — measured API behavior; **takes precedence
  over the official guides**, which were wrong in several places

---

## Security

Keys live in `.env` (local) or GitHub Secrets / the MCP config `env` block (deployed), never
in code. The 온비드 API requires the service key as a query parameter and httpx logs full
request URLs at INFO level, so the client lowers the `httpx` logger to WARNING at import
time — otherwise enabling logging would leak the key. The settings object masks its values
in `repr` for the same reason.

All `onbid_*` tables have RLS enabled with no policies and revoked grants; access is
`service_role` only, verified by measurement (HTTP 401 for anon on every table). The HTTP
API refuses to bind anywhere but loopback.

---

## Limits and non-goals

- **Lookup only.** No ranking, scoring, or recommendation — the tools return public data and
  leave judgement to you. This is deliberate: 공인중개사법 restricts listing-style display and
  advertisement of properties.
- No brokerage, no valuation, no legal or investment advice.
- Seoul, sale-type listings, in-progress by default.
- Winning-bid statistics come from a biased sample (see above).

## License

Not yet chosen. The 온비드 API guide documents are intentionally excluded from this
repository; the response structures used here are recorded in
[docs/API_FINDINGS.md](docs/API_FINDINGS.md) from live measurement.
