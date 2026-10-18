"""One-line client wrapping: every model call on a wrapped SDK client is recorded automatically.

    import giuggiai_meter as gm
    client = gm.wrap(Anthropic(), project="newsletter-bot")
    client = gm.wrap(OpenAI(base_url=..., api_key=...), project="support-bot", provider="DeepSeek")

No SDK is imported: the wrapper is a generic proxy that intercepts methods by attribute path and reads the
usage from the response by its shape (see log_response).
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import sys

from . import providers

# attribute path of the resource -> methods recorded on it. Extend here to cover another SDK.
ROUTES: dict[tuple, frozenset] = {
    (): frozenset({"chat", "generate", "converse"}),      # Cohere chat, Ollama chat/generate, Bedrock converse
    ("messages",): frozenset({"create"}),                 # Anthropic
    ("beta", "messages"): frozenset({"create"}),          # Anthropic beta
    ("chat", "completions"): frozenset({"create", "parse"}),          # OpenAI and OpenAI-compatible APIs
    ("beta", "chat", "completions"): frozenset({"create", "parse"}),
    ("responses",): frozenset({"create", "parse"}),       # OpenAI Responses API
    ("chat",): frozenset({"complete", "complete_async"}),  # Mistral (mistralai SDK)
    ("models",): frozenset({"generate_content"}),         # Google genai
    ("aio", "models"): frozenset({"generate_content"}),   # Google genai, async
}
_PREFIXES = {p[:i] for p in ROUTES for i in range(1, len(p) + 1)}
_REWRAP = {"with_options", "copy"}  # methods returning a new client: the copy stays metered


def _warn(where: str, e: BaseException) -> None:
    with contextlib.suppress(Exception):  # never break the caller, not even with a broken stderr
        sys.stderr.write(f"[giuggiai_meter] {where}: {type(e).__name__}: {e}\n")


def detect_provider(client) -> str | None:
    """Provider guessed from the client's base URL, then from its class name (None when it must come from the model)."""
    try:
        return providers.from_client(client)
    except Exception as e:  # noqa: BLE001
        _warn("detect_provider", e)
        return None


def _record(meter: dict, resp, kwargs: dict) -> None:
    """Record one response. Never raises."""
    try:
        from . import _usage

        u = _usage.extract(resp)
        if u is None:  # raw/streaming objects carry no usage: nothing to record
            return
        model = next((v for v in (kwargs.get("model"), kwargs.get("modelId")) if isinstance(v, str)), None)
        prov = meter["provider"] or _usage.resolve_provider(None, u["model"] or model, u["shape"]) or meter["class_hint"]
        import giuggiai_meter as gm  # looked up at call time

        gm.log_response(meter["project"], resp, provider=prov, workspace=meter["workspace"], fallback_model=model)
    except Exception as e:  # noqa: BLE001 - never break the caller
        _warn("wrap: logging failed", e)


def _metered(fn, meter: dict):
    @functools.wraps(fn)
    def call(*args, **kwargs):
        if kwargs.get("stream"):  # streaming: passed through untouched, not recorded
            return fn(*args, **kwargs)
        result = fn(*args, **kwargs)
        try:
            if inspect.isawaitable(result):
                return _await_and_record(result, meter, kwargs)
            _record(meter, result, kwargs)
        except Exception as e:  # noqa: BLE001
            _warn("wrap", e)
        return result

    return call


async def _await_and_record(awaitable, meter: dict, kwargs: dict):
    resp = await awaitable
    _record(meter, resp, kwargs)
    return resp


class MeteredClient:
    """Transparent proxy around an SDK client (or one of its resources). Use `gm.wrap(...)` to build it."""

    __slots__ = ("_gm_meter", "_gm_path", "_gm_target")

    def __init__(self, target, path: tuple, meter: dict):
        object.__setattr__(self, "_gm_target", target)
        object.__setattr__(self, "_gm_path", path)
        object.__setattr__(self, "_gm_meter", meter)

    @property
    def __wrapped__(self):
        return self._gm_target

    def __getattr__(self, name):
        attr = getattr(self._gm_target, name)
        try:
            path = self._gm_path + (name,)
            if callable(attr) and name in ROUTES.get(self._gm_path, ()):
                return _metered(attr, self._gm_meter)
            if not self._gm_path and name in _REWRAP and callable(attr):
                meter = self._gm_meter

                @functools.wraps(attr)
                def rewrap(*a, **k):
                    return MeteredClient(attr(*a, **k), (), meter)

                return rewrap
            if path in _PREFIXES and not callable(attr):
                return MeteredClient(attr, path, self._gm_meter)
        except Exception as e:  # noqa: BLE001
            _warn("wrap", e)
        return attr

    def __setattr__(self, name, value):
        setattr(self._gm_target, name, value)

    def __dir__(self):
        return dir(self._gm_target)

    def __repr__(self):
        return f"<metered project={self._gm_meter['project']!r} {self._gm_target!r}>"

    def __enter__(self):
        self._gm_target.__enter__()
        return self

    def __exit__(self, *exc):
        return self._gm_target.__exit__(*exc)

    async def __aenter__(self):
        await self._gm_target.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._gm_target.__aexit__(*exc)


def wrap(client, project: str, provider: str | None = None, workspace: str | None = None):
    """Return `client` with automatic usage logging under `project`.

    Recorded calls (the SDK's response is returned unchanged):
      - Anthropic:            client.messages.create(...), client.beta.messages.create(...)
      - OpenAI and every OpenAI-compatible API (DeepSeek, xAI Grok, Qwen compatible mode, Groq, Together, Fireworks,
        OpenRouter, Perplexity, Moonshot, Zhipu, Azure OpenAI, Ollama /v1, ...):
                              client.chat.completions.create(...) / .parse(...), client.responses.create(...) / .parse(...)
      - Google genai:         client.models.generate_content(...), client.aio.models.generate_content(...)
      - Mistral (mistralai):  client.chat.complete(...), client.chat.complete_async(...)
      - Cohere:               client.chat(...)
      - Amazon Bedrock:       client.converse(...)            (boto3 "bedrock-runtime" client)
      - Ollama:               client.chat(...), client.generate(...)  (also the `ollama` module itself)
    Async clients (AsyncAnthropic, AsyncOpenAI, ...) work the same way: `await client.messages.create(...)`.

    `provider` names who bills the call. When omitted: the client's base URL (providers.HOSTS), then its class
    (providers.CLIENT_HINTS); a generic OpenAI/Anthropic SDK pointed at an unknown host is attributed by the
    model id (providers.MODEL_PREFIXES), falling back to the SDK name.

    Not recorded (passed through untouched, never an error): streaming (`stream=True`, `.stream(...)`,
    `generate_content_stream`, `converse_stream`, `chat_stream`, `stream_async`), `with_raw_response` /
    `with_streaming_response`, batches, embeddings and every other method.
    Logging never raises: if recording fails, a line goes to stderr and the response is returned as usual.
    Everything else on the client behaves as on the original; `wrapped.__wrapped__` is the original client.
    """
    if isinstance(client, MeteredClient):
        client = client.__wrapped__
    meter = {"project": str(project), "workspace": workspace,
             "provider": providers.normalize(provider) if provider else detect_provider(client),
             "class_hint": None}
    with contextlib.suppress(Exception):
        meter["class_hint"] = providers.class_hint(client)
    return MeteredClient(client, (), meter)
