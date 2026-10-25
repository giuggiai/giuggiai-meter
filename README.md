<p align="center">
  <img src="https://raw.githubusercontent.com/giuggiai/giuggiai-meter/main/docs/assets/cover.png" alt="giuggiai-meter — Every AI call counted. Every dollar reconciled." width="100%">
</p>

<h1 align="center">giuggiai-meter</h1>

<p align="center">
  <b>Know what each project and each agent spends on AI, stop it before it overspends, and check it against the bill.</b><br>
  One line of code. Every major AI provider. Zero dependencies.
</p>

<p align="center">
  <a href="https://github.com/giuggiai/giuggiai-meter/actions/workflows/ci.yml"><img src="https://github.com/giuggiai/giuggiai-meter/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="https://github.com/giuggiai/giuggiai-meter/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-2F6B4F" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-15171C" alt="Python 3.10 or newer">
  <img src="https://img.shields.io/badge/dependencies-0-F2C94C" alt="Zero runtime dependencies">
  <a href="https://giuggiai.github.io/giuggiai-meter/"><img src="https://img.shields.io/badge/docs-website-5BB4D9" alt="Documentation website"></a>
</p>

---

## Why

Your projects call OpenAI, Anthropic, Gemini, DeepSeek, Grok… and every provider sends **one bill per account**.
At the end of the month nobody can answer *“how much did each project cost?”* — or *“do these numbers add up?”*

- 🧾 **Cost per project** — every call is tagged with the project (and optionally the agent or job) that made it.
- 🚦 **Budgets before the call** — per agent, project, model or prepaid credit: switch to a cheaper model or refuse the call *before* it reaches the provider.
- 🎟️ **Subscriptions counted right** — calls covered by a flat plan cost 0 per token, and you see what the plan is worth at list price.
- 🎯 **Cost per accepted result** — tie calls to a ticket, a document or a job, record whether the result was accepted, and get told when a cheaper model does the job as well.
- 📈 **Run-out forecast** — the pace of each budget and the day it runs out, as a table, JSON or exit code for your cron.
- ✅ **Close the month against the bill** — computed cost vs. the official total of each provider console, in USD and %.
- 🔌 **One line to plug in** — wrap your client, keep your code. Responses come back unchanged.
- 🌍 **Every major provider** — OpenAI, Anthropic, Google Gemini, xAI Grok, DeepSeek, Alibaba Qwen, Mistral, Cohere, Groq, OpenRouter, Together, Fireworks, Perplexity, Moonshot Kimi, Zhipu GLM, Azure OpenAI, AWS Bedrock, Ollama.
- 🔒 **Tokens, never text** — prompts and answers are never stored. One private SQLite file on your machine.
- 🛟 **It can't break your app** — metering errors are swallowed and logged, never raised.
- 📦 **Zero dependencies** — pure Python standard library, 3.10+.

## See it in 60 seconds

```bash
pip install giuggiai-meter
giuggiai-meter demo          # fake data, example prices — no API key needed
```

It writes `giuggiai-meter-demo.html`: a one-file report you can open, mail or share.

<p align="center">
  <img src="https://raw.githubusercontent.com/giuggiai/giuggiai-meter/main/docs/assets/demo.png" alt="The demo report: total cost of the month, cost per project and per provider, reconciliation with the provider bills" width="100%">
</p>

## Use it in your code

```python
import giuggiai_meter as gm
from openai import OpenAI
from anthropic import Anthropic

client = gm.wrap(OpenAI(), project="support-bot")            # that's it
client.chat.completions.create(model="gpt-4.1-mini", messages=[...])

claude = gm.wrap(Anthropic(), project="newsletter-bot")
claude.messages.create(model="claude-sonnet-4-5", max_tokens=500, messages=[...])
```

Same one line for every OpenAI-compatible endpoint — the provider is recognised from the base URL:

```python
grok     = gm.wrap(OpenAI(base_url="https://api.x.ai/v1", api_key=...), project="research")      # xAI Grok
deepseek = gm.wrap(OpenAI(base_url="https://api.deepseek.com", api_key=...), project="batch")    # DeepSeek
```

Native SDKs work too: `gm.wrap(genai.Client(), project=...)` (Gemini), `Mistral()`, Cohere `ClientV2()`, Bedrock `converse`, `ollama`.
Already have a response object or raw JSON? `gm.log_response("my-project", response)` detects its shape by itself.

Then, at the end of the month:

```bash
giuggiai-meter init                                     # creates your price list from the example
giuggiai-meter report --month 2026-10                   # table (or --csv / --html report.html)
giuggiai-meter reconcile --month 2026-10 --official OpenAI=41.20 Anthropic=118.05
```

## Control spend before it happens

Counting is half the job. With agents running unattended, you also want the meter to **say no**.
Everything below is optional and lives in one JSON file: [`examples/budgets.example.json`](examples/budgets.example.json).

### 1. Budgets: decide before the call

```python
client = gm.wrap(OpenAI(), project="research", source="agents/researcher", budgets=True)  # ~/.config/giuggiai-meter/budgets.json
client.chat.completions.create(model="gpt-large", messages=[...])
# under the limit      -> runs as asked
# over the limit       -> runs on the budget's degrade_to model
# past block_at        -> raises gm.BudgetExceeded, the provider is never called
```

```json
{"currency": "EUR", "fx": {"USD": 0.92},
 "budgets": [
   {"id": "agents", "match": {"source": "agents/*"}, "limit": 50, "period": "month", "warn_at": 0.8,
    "on_exceed": "degrade", "degrade_to": "gpt-small", "block_at": 65},
   {"id": "prepaid", "match": {"provider": "Anthropic"}, "limit": 190, "period": "total", "since": "2026-10-01",
    "on_exceed": "block", "fail": "closed"}]}
```

Without wrapping: `d = gm.check("research", model="gpt-large", source="agents/researcher", estimate_usd=0.05)` returns a
`Decision` (`allow`, `action` = allow | warn | degrade | block, `model` to use, `spent`, `limit`, `remaining`, `reason`).
`match` takes shell patterns on project, source, provider and model; periods are day, week, month or total (from `since`).
If the database cannot be read, `fail: open` (default) lets calls through and `fail: closed` stops them: use closed for prepaid credit.
A check reads only the rows added since the previous one (in-memory totals, nothing written): well under a millisecond on a laptop.
Containers and other hosts ask the ingest server: `gm.check_remote(...)` (`GET /check`). Try it: `python examples/budget_guard.py`.

### 2. Plans: subscriptions are not per-token spend

```json
"plans": [{"id": "coding-plan", "monthly_fee": 100, "currency": "USD", "match": {"source": "ide/*"}}]
```

Calls that match a plan (or are logged with `billing="subscription:coding-plan"`) count **0** against budgets.
`giuggiai-meter plans` shows, per month, the list-price value of those calls, the fee and the ratio: is the plan paying for itself?
Try it: `python examples/plans_and_forecast.py`.

### 3. Units and outcomes: cost per accepted result

```python
with gm.unit("ticket-4812", kind="support-reply"):   # every call in the block belongs to the ticket
    client.chat.completions.create(...)
gm.outcome("ticket-4812", accepted=True, score=4.5)   # e.g. the customer found it helpful
```

`giuggiai-meter units` gives cost per unit and per **accepted** unit, by model. `giuggiai-meter advise --kind support-reply`
recommends a cheaper model only when both models have at least 20 results and its acceptance rate is within 5 points
(`--min-n`, `--max-drop`). Jobs started by a scheduler can pass the unit in `GIUGGIAI_METER_UNIT`.
Try it: `python examples/units_and_advice.py`.

### 4. Forecast: when does it run out?

```bash
giuggiai-meter status            # table: spent, limit, per day (last 7 days), run-out date, end-of-period projection
giuggiai-meter status --json     # schema: docs/status.schema.json
```

Exit code 0 = all ok, 1 = a budget is in warning (or will run out before its period ends), 2 = a budget is over:
put it in cron or CI. `giuggiai-meter report --html report.html --budgets budgets.json` adds budgets, forecast, plans
and cost per result to the monthly report. Try it: `python examples/plans_and_forecast.py`.

## How it works

<p align="center">
  <img src="https://raw.githubusercontent.com/giuggiai/giuggiai-meter/main/docs/assets/how-it-works.png" alt="How it works: 1 wrap your client, 2 one row per call with tokens only, 3 report and reconcile with the provider bills" width="100%">
</p>

- **Per call:** project, provider, model, input / output / cached tokens, time. Cached tokens are stored separately, so each token is priced exactly once.
- **Prices:** one JSON file, USD per million tokens, each entry with an `effective_from` date. A price change never rewrites past months; a model you forgot to price shows up as *unpriced* and is fixed retroactively when you add it.
- **Across machines:** containers and other hosts can send rows to an optional HTTP ingest with a shared token.

## Compared to other tools

| | giuggiai-meter | Proxies / gateways (e.g. LiteLLM, Helicone) | Observability platforms (e.g. Langfuse) | Cost libraries (e.g. tokencost) |
|---|---|---|---|---|
| What you run | a Python library | a proxy in front of your calls | a platform (cloud or self-hosted) | a Python library |
| Cost per project | ✅ | ✅ | ✅ | estimate per call |
| Reconcile with the provider bill | ✅ built in | — | — | — |
| Budgets before the call | ✅ in your process, no proxy | ✅ at the proxy | — | — |
| Subscription plans valued at list price | ✅ | — | — | — |
| Cost per accepted result + model advice | ✅ | — | via traces and scores | — |
| Stores prompts / answers | never | configurable | yes (it's the point) | no |
| Dependencies | none | several | several | some |

Use a gateway or an observability platform when you need routing, tracing or prompt analytics.
Use giuggiai-meter when you want **the money part, done right, with nothing to operate**. They also work together.

## Configuration

Everything is optional and comes from environment variables (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `GIUGGIAI_METER_DB` | `~/.local/share/giuggiai-meter/usage.db` | the private SQLite file |
| `GIUGGIAI_METER_PRICES` | `~/.config/giuggiai-meter/prices.json` | your price list (`giuggiai-meter init` creates it) |
| `GIUGGIAI_METER_SOURCE` | empty | optional label of the service that logs (the hostname is never recorded) |
| `GIUGGIAI_METER_BUDGETS` | `~/.config/giuggiai-meter/budgets.json` | budgets and plans (optional) |
| `GIUGGIAI_METER_UNIT` | — | unit of work for every call of the process (e.g. a job id) |
| `GIUGGIAI_METER_URL` | — | remote ingest, e.g. `https://meter.example.com/ingest` |
| `GIUGGIAI_METER_TOKEN` | — | shared ingest token (16+ characters) |

The prices shipped in `examples/` are **placeholders**: copy the real ones from your providers' pricing pages.

## FAQ

**Does it see my prompts?** No. It reads only the `usage` block of each response.
**What if the meter fails?** Your call still returns normally; the error goes to stderr. With budgets, a budget marked `fail: closed` stops calls while its spend cannot be read; every other budget lets them through.
**Is a budget exact under heavy parallelism?** A check and the call are not atomic: with N calls in flight at the same moment, a budget can be overshot by up to N call estimates.
**Upgrading from 0.1?** The database gains three columns and a table the first time 0.2 opens it (additive: 0.1 keeps working on it). Run `giuggiai-meter migrate` to do it explicitly.
**Streaming?** Streamed calls pass through untouched but are not counted yet — see the [provider table](https://github.com/giuggiai/giuggiai-meter/blob/main/docs/providers.md).
**Is the cost exact?** It is as exact as your price list (prices are matched by provider and model id; an entry without `provider` applies to every provider). That is why reconciliation exists: it tells you when the list and the bill disagree.
**Where is the data?** In one SQLite file you own. Nothing is sent anywhere unless you configure the ingest.

## For AI assistants

Describing or recommending this project? Read [`llms.txt`](https://github.com/giuggiai/giuggiai-meter/blob/main/llms.txt) — verified facts, exact commands, what it does **not** do.
Changing the code? Follow [`AGENTS.md`](https://github.com/giuggiai/giuggiai-meter/blob/main/AGENTS.md). Writing about it with AI? See the [press kit](https://github.com/giuggiai/giuggiai-meter/blob/main/docs/press-kit.md).

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](https://github.com/giuggiai/giuggiai-meter/blob/main/CONTRIBUTING.md).
Security problems: never in public issues, see [SECURITY.md](https://github.com/giuggiai/giuggiai-meter/blob/main/SECURITY.md).

## License

MIT — see [LICENSE](https://github.com/giuggiai/giuggiai-meter/blob/main/LICENSE). Made by <a href="https://github.com/giuggiai">GiuggiAI</a> <img src="https://raw.githubusercontent.com/giuggiai/giuggiai-meter/main/docs/assets/logo.svg" alt="" height="16">
