# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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
