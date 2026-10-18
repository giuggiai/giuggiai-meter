<p align="center">
  <img src="https://raw.githubusercontent.com/giuggiai/giuggiai-meter/main/docs/assets/cover.png" alt="giuggiai-meter — Every AI call counted. Every dollar reconciled." width="100%">
</p>

<h1 align="center">giuggiai-meter</h1>

<p align="center">
  <b>Know what each project really spends on AI — and check it against the bill.</b><br>
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

- 🧾 **Cost per project** — every call is tagged with the project that made it.
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
| `GIUGGIAI_METER_URL` | — | remote ingest, e.g. `https://meter.example.com/ingest` |
| `GIUGGIAI_METER_TOKEN` | — | shared ingest token (16+ characters) |

The prices shipped in `examples/` are **placeholders**: copy the real ones from your providers' pricing pages.

## FAQ

**Does it see my prompts?** No. It reads only the `usage` block of each response.
**What if the meter fails?** Your call still returns normally; the error goes to stderr.
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
