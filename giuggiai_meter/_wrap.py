"""One-line client wrapping: every model call on a wrapped SDK client is recorded automatically.

    import giuggiai_meter as gm
    client = gm.wrap(Anthropic(), project="newsletter-bot")
    client = gm.wrap(OpenAI(base_url=..., api_key=...), project="support-bot", provider="DeepSeek")

No SDK is imported: the wrapper is a generic proxy that intercepts methods by attribute path and reads the
usage from the response by its shape (see log_response).
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
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

        gm.log_response(meter["project"], resp, provider=prov, workspace=meter["workspace"], fallback_model=model,
                        source=meter.get("source"))
    except Exception as e:  # noqa: BLE001 - never break the caller
        _warn("wrap: logging failed", e)


def _estimate_tokens(kwargs: dict) -> tuple[int, int]:
    """Rough (input, output) token estimate of a request: ~4 characters per token of the request body, and the
    requested max output. Used only to judge a call against its budget before it runs."""
    body = {k: v for k, v in kwargs.items() if k in ("messages", "input", "contents", "prompt", "system", "instructions")}
    try:
        n_in = len(json.dumps(body, default=str)) // 4
    except Exception:  # noqa: BLE001
        n_in = 0
    n_out = next((int(kwargs[k]) for k in ("max_tokens", "max_output_tokens", "max_completion_tokens")
                  if isinstance(kwargs.get(k), int)), 0)
    return n_in, n_out


def _guard(meter: dict, kwargs: dict) -> None:
    """Apply the budgets before the call: raise BudgetExceeded on block, switch kwargs['model'] on degrade.
    Any other failure lets the call through (the budgets' fail mode is applied inside check())."""
    from .budgets import BudgetExceeded, check

    try:
        model = next((v for v in (kwargs.get("model"), kwargs.get("modelId")) if isinstance(v, str)), None)
        n_in, n_out = _estimate_tokens(kwargs)
        cfg = meter["budgets"]
        d = check(meter["project"], model=model, provider=meter["provider"], source=meter.get("source"),
                  input_tokens=n_in, output_tokens=n_out, config=None if cfg is True else cfg)
    except Exception as e:  # noqa: BLE001 - a broken budget check never breaks the caller
        _warn("wrap: budget check failed", e)
        return
    if d.action == "block":
        raise BudgetExceeded(d)
    if d.action == "degrade" and d.model:
        kwargs["model" if "model" in kwargs or "modelId" not in kwargs else "modelId"] = d.model


def _metered(fn, meter: dict):
    if meter.get("budgets") is not None and inspect.iscoroutinefunction(inspect.unwrap(fn)):
        @functools.wraps(fn)
        async def acall(*args, **kwargs):  # async clients: the budget check runs off the event loop
            await asyncio.to_thread(_guard, meter, kwargs)
            resp = await fn(*args, **kwargs)
            if not kwargs.get("stream"):
                try:
                    await asyncio.to_thread(_record, meter, resp, kwargs)
                except Exception as e:  # noqa: BLE001
                    _warn("wrap: async logging failed", e)
            return resp

        return acall

    @functools.wraps(fn)
    def call(*args, **kwargs):
        if meter.get("budgets") is not None:
            _guard(meter, kwargs)
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
    try:  # SQLite / HTTP I/O runs in a worker thread, never blocking the event loop
        await asyncio.to_thread(_record, meter, resp, kwargs)
    except Exception as e:  # noqa: BLE001 - never break the caller
        _warn("wrap: async logging failed", e)
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

    @property
    def __class__(self):  # isinstance(wrapped, OpenAI) stays True, like wrapt.ObjectProxy
        return type(self._gm_target)

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


def wrap(client, project: str, provider: str | None = None, workspace: str | None = None, budgets=None,
         source: str | None = None):
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

    `budgets` (True = the default budgets file, or a path, or a dict): every recorded call is checked first
    (see budgets.py). A blocked call raises `BudgetExceeded` without reaching the provider; a degraded call runs
    on the budget's `degrade_to` model. `source` labels the calls (an agent, a job) for budgets and reports.
    """
    if isinstance(client, MeteredClient):
        client = client.__wrapped__
    meter = {"project": str(project), "workspace": workspace, "budgets": budgets, "source": source,
             "provider": providers.normalize(provider) if provider else detect_provider(client),
             "class_hint": None}
    with contextlib.suppress(Exception):
        meter["class_hint"] = providers.class_hint(client)
    return MeteredClient(client, (), meter)
