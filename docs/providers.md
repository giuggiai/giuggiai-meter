# Supported providers

`gm.wrap(client, project=...)` records the calls below. Everything else on the client works exactly as before and is simply not recorded.

| Provider | Client you wrap | Recorded methods | Recognised by |
|---|---|---|---|
| OpenAI | `openai.OpenAI()` / `AsyncOpenAI()` | `chat.completions.create/parse`, `responses.create/parse` | SDK class, `api.openai.com` |
| Azure OpenAI | `openai.AzureOpenAI()` | same as OpenAI | `*.openai.azure.com` |
| Anthropic | `anthropic.Anthropic()` / `AsyncAnthropic()` | `messages.create`, `beta.messages.create` | SDK class |
| Google Gemini | `google.genai.Client()` | `models.generate_content`, `aio.models.generate_content` | SDK class, `generativelanguage.googleapis.com`, Vertex |
| xAI Grok | `OpenAI(base_url="https://api.x.ai/v1")` | OpenAI methods | `x.ai` |
| DeepSeek | `OpenAI(base_url="https://api.deepseek.com")` | OpenAI methods | `deepseek.com` |
| Alibaba Qwen | `OpenAI(base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1")` | OpenAI methods | `dashscope*.aliyuncs.com` |
| Mistral | `mistralai.Mistral()` | `chat.complete`, `chat.complete_async` | SDK class, `mistral.ai` |
| Cohere | `cohere.ClientV2()` | `chat` | SDK class, `api.cohere.*` |
| AWS Bedrock | `boto3.client("bedrock-runtime")` | `converse` | `bedrock-runtime` endpoint |
| Ollama | `ollama.Client()` | `chat`, `generate` | SDK class, port `11434` |
| Groq, Together, Fireworks, OpenRouter, Perplexity, Moonshot Kimi, Zhipu GLM | `OpenAI(base_url=...)` | OpenAI methods | their API host |

Any other OpenAI-compatible endpoint works too: pass `provider="Name"` to `wrap` if its host is not in the table.

## Streams and raw responses (0.2.1)

Streaming (`stream=True`, `.stream(...)`, `generate_content_stream`, `converse_stream`, `chat_stream`, `stream_async`) passes through event by event and is recorded when the stream ends, is closed or its `with` block is left, with the usage found in the events: OpenAI chat chunks (the meter sets `stream_options={"include_usage": True}` on the official OpenAI API unless you set `stream_options`), OpenAI Responses `response.completed`, Anthropic `message_start` + `message_delta`, Gemini `usage_metadata`, Bedrock `metadata`, Mistral, Cohere `message-end`, Ollama. When no usage arrives, the row is an estimate (request size and streamed text, ~4 characters per token; `request_id` starting with `gm-est-`).
`with_raw_response` / `with_streaming_response` are checked like the plain methods and recorded when the response is parsed.

## Not recorded (passed through untouched)

Batches and embeddings. For those, log by hand: `gm.log_usage(project, provider, model, input_tokens=..., output_tokens=...)`.

## Token convention

`input_tokens` are the **uncached** input tokens; cache reads and cache writes are stored in their own columns.
Providers that report a total including the cache (OpenAI-style, Gemini, Responses, DashScope) are converted, so each token is priced exactly once.

## Prices per provider

Price entries are matched by provider and model id. An entry with `"provider"` applies to calls of that provider only
(spellings are normalised: `"openai"` matches `OpenAI`); an entry without `"provider"` applies to every provider.
So the same open model served by two providers can have two prices; price lists without `"provider"` work unchanged.

## Add a provider

Edit the tables at the top of `giuggiai_meter/providers.py` (`ALIASES`, `HOSTS`, `CLIENT_HINTS`, `MODEL_PREFIXES`), add a test in `tests/test_providers.py`, open a pull request.
