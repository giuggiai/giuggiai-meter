# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-25

From counting to control: the meter can now decide before a call, value subscriptions, measure cost per result and
forecast when a budget runs out. Still pure standard library; everything new is opt-in.

### Added
- **Budgets** (`budgets.json`, `$GIUGGIAI_METER_BUDGETS`): limits matched on project / source / provider / model, per day,
  week, month or total since a date, in any currency (`fx`). `gm.check(...)` returns a `Decision`; `gm.wrap(..., budgets=True)`
  switches to `degrade_to` over the limit and raises `gm.BudgetExceeded` before the call past `block_at` or on `block`.
  `fail: open | closed` decides what happens when the spend cannot be read. In-memory incremental totals: no writes.
- `GET /check` on the ingest server and `gm.check_remote(...)` for containers and other hosts.
- **Plans**: flat subscriptions in the budgets file (`plans`), or `billing="subscription:<id>"` / `"free"` when logging.
  Their calls count 0 against budgets; `giuggiai-meter plans` / `report.plans(...)` show list-price value vs fee.
- **Units of work and outcomes**: `with gm.unit(id, kind):`, `$GIUGGIAI_METER_UNIT`, `gm.bind(fn)` for threads,
  `gm.outcome(id, accepted, score)`; `giuggiai-meter units` / `report.units(...)` (cost per unit and per accepted unit).
- **Model advice**: `giuggiai-meter advise --kind K` / `advise.advise(...)` (min 20 results per model, max 5 points drop).
- **Forecast**: `giuggiai-meter status [--json]` / `forecast.status(...)`: pace over 7 days, run-out date, end-of-period
  projection; exit code 0 / 1 / 2. JSON schema in `docs/status.schema.json`.
- HTML report: Budgets, Forecast, Plans and Cost per result sections (`report --budgets FILE`; included in `demo`).
- `wrap(..., source=...)` labels the calls of a client; `log_usage(..., billing=, unit_id=, unit_kind=)`.
- `giuggiai-meter migrate` and `gm.migrate()`.
- Examples: `budgets.example.json`, `budget_guard.py`, `units_and_advice.py`, `plans_and_forecast.py`.
- Robustness: concurrent schema upgrades from several processes lose no rows (one write transaction, rows fall back to
  the v1 columns if the upgrade must wait); the in-memory totals are recounted when the database is recreated or
  restored; a missing price list or an unreadable budgets file applies the fail modes instead of reading as zero
  spend (`GIUGGIAI_METER_BUDGETS_FAIL`); `billing`, `warn_at`, `block_at` and `limit` are validated (a non-USD currency without `fx.USD` warns);
  `check_remote` applies the same rules as `check()` when the server cannot be reached; async SDK methods behind
  sync decorators also run the budget check off the event loop.

### Changed
- Database schema v2 (`gm.SCHEMA_VERSION`): columns `usage.billing`, `usage.unit_id`, `usage.unit_kind`, table
  `outcomes`, indexes on (source, log_time) and unit_id. Added automatically and only additively when 0.2 opens a
  database; 0.1.x keeps reading and writing it.

## [0.1.1] - 2026-10-19

### Added
- Prices by provider: `prices.pick(idx, model, date, provider=None)` prefers an entry whose `provider` matches the call's provider (spellings normalised with `providers.normalize`), so the same model id served by two providers can have two prices. Lists without `provider` work unchanged.

### Changed
- `report.monthly`, `report.unpriced` and reconciliation now pass each row's provider to the price lookup. **Note:** if your price list has several entries with different `provider` values for the same model, each call now gets the price of its own provider (before, the most recent entry by date won).
- `connect()` initialises each database (directory, owner-only files, WAL, schema) once per path per process instead of on every insert; it still returns a new connection on each call.
- Async clients: the usage of an awaited call is recorded in a worker thread (`asyncio.to_thread`), so SQLite or HTTP I/O no longer blocks the event loop.

### Fixed
- Docs (`README.md`, `llms.txt`, `docs/providers.md`): describe pricing by (provider, model) instead of model id only.

## [0.1.0] - 2026-10-19
First public release.

### Added
- Per-call logging, monthly report and reconciliation with the provider bills, optional HTTP ingest.
- `gm.wrap(client, project=...)`: one line and every call is recorded, response unchanged (Anthropic, OpenAI and OpenAI-compatible APIs, Google genai, Mistral, Cohere, Amazon Bedrock Converse, Ollama; sync and async). Streaming passes through unrecorded.
- `gm.log_response(...)`: recognises the response shape by itself (OpenAI chat and Responses, Anthropic, Gemini, Cohere, Bedrock, Ollama, DashScope), on SDK objects and raw JSON; aliases `log_gemini`, `log_grok`, `log_qwen`, `log_mistral`, `log_cohere`, `log_bedrock`, `log_ollama`.
- `providers.py`: provider recognition from base URL, client class and model id, in extendable tables.
- `report --html FILE [--official Provider=USD ...]`: self-contained HTML report (no scripts, no external resources).
- `init` (example price list, never overwrites) and `demo` (fake data in a temporary database, HTML report) commands.
- `giuggiai-meter` console command and `--version`.
- Documentation: new README, `docs/providers.md`, project website (`docs/index.html`), press kit, `llms.txt`, `AGENTS.md`, `CITATION.cff`, images in `docs/assets/`.
- The price list may be an object `{"_note": ..., "prices": [...]}`; the example list ships inside the package.

### Fixed
- OpenAI-style, Gemini, Responses and DashScope responses: cached input tokens are no longer counted twice (`input_tokens` is now the uncached input).

### Security
- The ingest token is never sent on redirects; a warning is printed when it travels over plain http to a remote host.
- Ingest validates every field, rejects oversized or malformed bodies, drops slow clients and requires a token of at least 16 characters.
- Database files are created readable by their owner only; the hostname is never recorded.
