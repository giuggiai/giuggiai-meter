# giuggiai-meter

**Know what each project really spends on AI — and check it against the bill.**

`giuggiai-meter` records every LLM call (tokens and model, never the text) with one line of code,
builds a monthly cost report per project and provider from a single price list, and reconciles
the computed cost with the official total shown in each provider's console.

> Part of the GiuggiAI open tools. Released under the MIT License.

## Why

When several projects share a few AI providers, provider consoles show totals per account, not per project.
At the end of the month nobody can say how much each project costs, or whether the numbers add up.
Bigger platforms (proxies, observability suites) can track costs too; this is a tiny library with
**no dependencies** that does one thing: count calls per project and close the month against the bill.

- **One line per call** — `gm.log_anthropic("my-project", resp)` or `gm.log_openai("my-project", resp)`.
- **Never breaks your code** — the logger catches every error; metering cannot crash the caller.
- **No prompts stored** — only tokens, model, project, time, plus the optional `source`/`workspace` labels you set.
  The database files are readable by their owner only.
- **One price list** — prices with an `effective_from` date, applied when reporting, so a price change never rewrites history.
- **Monthly reconciliation** — computed cost vs. official cost per provider, with the difference in USD and %.
- **Works across containers** — an optional HTTP ingest receives rows from other hosts with a shared token.

## Quick start

```bash
git clone https://github.com/giuggiai/giuggiai-meter.git
cd giuggiai-meter
python3 -m venv .venv && . .venv/bin/activate
pip install .
python examples/quickstart.py
```

Expected output (two sample calls priced with `examples/prices.example.json`):

```
slug          provider   calls  input_tokens  output_tokens  cost_usd  unpriced_calls
------------  ---------  -----  ------------  -------------  --------  --------------
demo-project  Anthropic  1      40000         8000           0.24      0
demo-project  OpenAI     1      120000        30000          0.036     0
```

## Use it in your code

```python
import giuggiai_meter as gm

resp = client.messages.create(model="...", max_tokens=500, messages=[...])
gm.log_anthropic("newsletter-bot", resp)

resp = openai_client.chat.completions.create(model="...", messages=[...])
gm.log_openai("support-bot", resp)                      # OpenAI
gm.log_openai("support-bot", resp, provider="DeepSeek") # any OpenAI-compatible API

gm.log_usage("batch-job", "Mistral", "some-model", input_tokens=1200, output_tokens=300)  # anything else
```

## Reports and reconciliation

```bash
# cost per project and provider for a month (table, or --csv)
python -m giuggiai_meter report --month 2026-10

# compare with the official totals you read in each provider console (USD)
python -m giuggiai_meter reconcile --month 2026-10 --official OpenAI=41.20 Anthropic=118.05
```

Calls whose model is missing from the price list are counted in `unpriced_calls`: add the model and the report is fixed retroactively.

## Configuration

All settings come from environment variables (see `.env.example`). No value is hard-coded.

| Variable | Required | Meaning |
|---|---|---|
| `GIUGGIAI_METER_DB` | no | SQLite file (default `~/.local/share/giuggiai-meter/usage.db`) |
| `GIUGGIAI_METER_PRICES` | for reports | price list JSON (default `~/.config/giuggiai-meter/prices.json`) |
| `GIUGGIAI_METER_SOURCE` | no | label of the machine or service that logs (default: empty; the hostname is never recorded) |
| `GIUGGIAI_METER_URL` | no | remote ingest URL (`https://meter.example.com/ingest`) for containers and other hosts |
| `GIUGGIAI_METER_TOKEN` | with ingest | shared token (at least 16 characters) for the ingest server and its clients |

Price list format — USD per million tokens, one entry per price period:

```json
[{"model": "example-small", "provider": "OpenAI", "input": 0.15, "output": 0.60,
  "cache_read": 0.075, "effective_from": "2026-01-01"}]
```

The prices in `examples/` are placeholders: copy the real ones from your providers' pricing pages.

## Remote ingest (optional)

```bash
export GIUGGIAI_METER_TOKEN=$(python -c "import secrets; print(secrets.token_hex(24))")
python -m giuggiai_meter ingest --host 127.0.0.1 --port 8788
```

Clients with `GIUGGIAI_METER_URL` and `GIUGGIAI_METER_TOKEN` set send rows over HTTP instead of writing locally
(2-second timeout; if the ingest is unreachable the row is kept in the local database instead).
The server binds to 127.0.0.1 by default. To reach it from other hosts, put it behind a reverse proxy **with HTTPS**:
the token travels in a request header and must never cross a network in clear text.

## How it works

```
your code ── gm.log_*() ──> SQLite (one row per call, tokens only)
containers ── HTTP + token ──> ingest ──┘
report / reconcile <── price list (JSON, dated) + official totals from provider consoles
```

## Status

Version 0.1.0 — early release, standard library only, Python 3.10+. New features are decided by the maintainers.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: see [SECURITY.md](SECURITY.md), never in public issues.

## License

MIT — see [LICENSE](LICENSE).
