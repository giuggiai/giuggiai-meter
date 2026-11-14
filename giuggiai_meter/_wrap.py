"""One-line client wrapping: every model call on a wrapped SDK client is recorded automatically.

    import giuggiai_meter as gm
    client = gm.wrap(Anthropic(), project="newsletter-bot")
    client = gm.wrap(OpenAI(base_url=..., api_key=...), project="support-bot", provider="DeepSeek")

No SDK is imported: the wrapper is a generic proxy that intercepts methods by attribute path and reads the
usage from the response by its shape (see log_response). Streams are passed through event by event and recorded when
they end, with the usage the provider sends in the stream (or an estimate when it sends none).
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import sys
import uuid

from . import providers

# attribute path of the resource -> methods recorded on it. Extend here to cover another SDK.
ROUTES: dict[tuple, frozenset] = {
    (): frozenset({"chat", "generate", "converse", "converse_stream", "chat_stream"}),  # Cohere, Ollama, Bedrock
    ("messages",): frozenset({"create", "stream"}),                       # Anthropic
    ("beta", "messages"): frozenset({"create", "stream"}),                # Anthropic beta
    ("chat", "completions"): frozenset({"create", "parse", "stream"}),    # OpenAI and OpenAI-compatible APIs
    ("beta", "chat", "completions"): frozenset({"create", "parse", "stream"}),
    ("responses",): frozenset({"create", "parse", "stream"}),             # OpenAI Responses API
    ("chat",): frozenset({"complete", "complete_async", "stream", "stream_async"}),  # Mistral (mistralai SDK)
    ("models",): frozenset({"generate_content", "generate_content_stream"}),         # Google genai
    ("aio", "models"): frozenset({"generate_content", "generate_content_stream"}),   # Google genai, async
}
STREAM_METHODS = frozenset({"stream", "stream_async", "generate_content_stream", "converse_stream", "chat_stream"})
_RAW = frozenset({"with_raw_response", "with_streaming_response"})  # same methods, response wrapped by the SDK
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


def _model(kwargs: dict) -> str | None:
    return next((v for v in (kwargs.get("model"), kwargs.get("modelId")) if isinstance(v, str)), None)


def _provider(meter: dict, model: str | None, shape: str | None = None) -> str | None:
    """Who bills the call: the same rule for the budget check (before) and the usage row (after)."""
    from . import _usage

    if meter["provider"]:
        return meter["provider"]
    default, host_specific = _usage.SHAPES.get(shape, (None, False))
    if host_specific:
        return default
    return providers.from_model(model) or meter["class_hint"] or default


def _record(meter: dict, resp, kwargs: dict) -> None:
    """Record one response. Never raises."""
    try:
        from . import _usage

        u = _usage.extract(resp)
        if u is None:  # raw objects carry no usage: nothing to record
            return
        model = _model(kwargs)
        import giuggiai_meter as gm  # looked up at call time

        gm.log_response(meter["project"], resp, provider=_provider(meter, u["model"] or model, u["shape"]),
                        workspace=meter["workspace"], fallback_model=model, source=meter.get("source"))
    except Exception as e:  # noqa: BLE001 - never break the caller
        _warn("wrap: logging failed", e)


def _log(meter: dict, u: dict, kwargs: dict, request_id: str | None = None) -> None:
    """Write one usage row from an extracted usage dict ({input_tokens, output_tokens, ..., model, id, shape})."""
    import giuggiai_meter as gm  # looked up at call time

    model = u.get("model") or _model(kwargs)
    rid = request_id or "gm-" + (str(u["id"]) if u.get("id") else uuid.uuid4().hex)
    gm.log_usage(meter["project"], provider=_provider(meter, model, u.get("shape")), model=model,
                 input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                 cache_creation_tokens=u.get("cache_creation_tokens", 0), cache_read_tokens=u.get("cache_read_tokens", 0),
                 request_id=rid, workspace=meter["workspace"], source=meter.get("source"))


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
    The call is judged with the project / source / provider / model its usage row will get. When usage goes to a
    remote ingest ($GIUGGIAI_METER_URL) the spend lives on that server, so the server is asked (check_remote); if it
    cannot answer, the budgets' fail mode applies. A failure of the check itself never breaks the caller."""
    from .budgets import BudgetExceeded, check, check_remote, remote_enabled

    try:
        model = _model(kwargs)
        n_in, n_out = _estimate_tokens(kwargs)
        cfg = meter["budgets"]
        args = {"model": model, "provider": _provider(meter, model), "source": meter.get("source"),
                "input_tokens": n_in, "output_tokens": n_out, "config": None if cfg is True else cfg}
        d = check_remote(meter["project"], **args) if remote_enabled() else check(meter["project"], **args)
    except Exception as e:  # noqa: BLE001 - a broken budget check never breaks the caller
        _warn("wrap: budget check failed", e)
        return
    if d.action == "block":
        raise BudgetExceeded(d)
    if d.action == "degrade" and d.model:
        kwargs["model" if "model" in kwargs or "modelId" not in kwargs else "modelId"] = d.model


def _include_usage(meter: dict, path: tuple, kwargs: dict) -> None:
    """Ask the official OpenAI API to send token usage at the end of a chat stream (other hosts may reject it)."""
    if path[-2:] == ("chat", "completions") and meter["provider"] == "OpenAI" and "stream_options" not in kwargs:
        kwargs["stream_options"] = {"include_usage": True}


def _metered(fn, meter: dict, path: tuple = (), name: str = "", raw: str | None = None):
    is_stream_method = name in STREAM_METHODS

    def before(kwargs: dict) -> bool:
        if meter.get("budgets") is not None:
            _guard(meter, kwargs)
        streaming = is_stream_method or bool(kwargs.get("stream"))
        if streaming and not raw:
            _include_usage(meter, path, kwargs)
        return streaming

    def after(result, kwargs: dict, streaming: bool):
        if raw:
            return _raw_result(result, meter, kwargs, streaming, raw)
        if streaming:
            return _stream_result(result, meter, kwargs)
        _record(meter, result, kwargs)
        return result

    if meter.get("budgets") is not None and inspect.iscoroutinefunction(inspect.unwrap(fn)):
        @functools.wraps(fn)
        async def acall(*args, **kwargs):  # async clients: the budget check runs off the event loop
            streaming = await asyncio.to_thread(before, kwargs)
            resp = await fn(*args, **kwargs)
            return await _after_async(after, resp, kwargs, streaming)

        return acall

    @functools.wraps(fn)
    def call(*args, **kwargs):
        streaming = before(kwargs)
        result = fn(*args, **kwargs)
        try:
            if inspect.isawaitable(result):
                return _await_then(result, after, kwargs, streaming)
            return after(result, kwargs, streaming)
        except Exception as e:  # noqa: BLE001
            _warn("wrap", e)
        return result

    return call


async def _after_async(after, resp, kwargs: dict, streaming: bool):
    if streaming:  # wrapping a stream does no I/O
        try:
            return after(resp, kwargs, streaming)
        except Exception as e:  # noqa: BLE001 - never break the caller
            _warn("wrap", e)
            return resp
    try:  # SQLite / HTTP I/O runs in a worker thread, never blocking the event loop
        return await asyncio.to_thread(after, resp, kwargs, streaming)
    except Exception as e:  # noqa: BLE001 - never break the caller
        _warn("wrap: async logging failed", e)
        return resp


async def _await_then(awaitable, after, kwargs: dict, streaming: bool):
    resp = await awaitable
    return await _after_async(after, resp, kwargs, streaming)


# ---------------------------------------------------------------- streams

class _StreamUsage:
    """Collects token usage from the events of a stream, whatever the SDK: OpenAI chat chunks (usage on the last
    chunk), OpenAI Responses events (response.completed), Anthropic events (message_start + message_delta), Gemini
    chunks (cumulative usage_metadata), Bedrock ConverseStream (metadata event), Mistral (event.data), Cohere
    (message-end), Ollama (last chunk). Also counts streamed text, for an estimate when no usage arrives."""

    def __init__(self):
        self.usage: dict | None = None
        self.anthropic: dict | None = None
        self.chars = 0

    def feed(self, ev) -> None:
        with contextlib.suppress(Exception):  # a strange event never breaks the stream
            self._feed(ev)

    def _feed(self, ev) -> None:
        from ._usage import _get, extract

        kind = _get(ev, "type")
        if kind == "message_start":
            msg = _get(ev, "message")
            u = _get(msg, "usage")
            self.anthropic = {"id": _get(msg, "id"), "model": _get(msg, "model"), "type": "message",
                              "usage": {k: _get(u, k) or 0 for k in ("input_tokens", "output_tokens",
                                        "cache_creation_input_tokens", "cache_read_input_tokens")}}
            return
        if kind == "message_delta":
            u = _get(ev, "usage")
            if self.anthropic is not None and u is not None:
                for k in self.anthropic["usage"]:
                    v = _get(u, k)
                    if v is not None:
                        self.anthropic["usage"][k] = v
            return
        self._count_text(ev)
        cands = [ev, _get(ev, "data"), _get(ev, "response")]
        md = _get(ev, "metadata")
        if md is not None and _get(md, "usage") is not None:
            cands.append({"usage": _get(md, "usage")})               # Bedrock ConverseStream
        delta = _get(ev, "delta")
        if delta is not None and _get(delta, "usage") is not None:
            cands.append({"usage": _get(delta, "usage"), "id": _get(ev, "id")})  # Cohere v2 message-end
        for c in cands:
            if c is None or isinstance(c, (str, bytes)):
                continue
            u = extract(c)
            if u is not None:
                self.usage = u

    def _count_text(self, ev) -> None:
        from ._usage import _get

        if isinstance(ev, str):
            self.chars += len(ev)
            return
        for c in (_get(_get(ev, "delta"), "text"), _get(ev, "delta") if isinstance(_get(ev, "delta"), str) else None,
                  _get(_get(_get(ev, "choices") and _get(ev, "choices")[0], "delta"), "content")):
            if isinstance(c, str):
                self.chars += len(c)

    def final(self, snapshot=None) -> dict | None:
        from ._usage import extract

        if self.anthropic is not None:
            return extract(self.anthropic)
        if self.usage is None and snapshot is not None:
            return extract(snapshot)
        return self.usage


_FINAL_METHODS = frozenset({"get_final_message", "get_final_completion", "get_final_response"})
_SNAPSHOTS = ("current_message_snapshot", "current_completion_snapshot")


class _MeteredStream:
    """Transparent proxy around a stream (iterator, async iterator, or a stream manager used with `with`): events
    pass through unchanged; when the stream ends, is closed or is left, one usage row is written. Never raises."""

    __slots__ = ("_gm_acc", "_gm_done", "_gm_inner", "_gm_it", "_gm_kwargs", "_gm_meter", "_gm_target")

    def __init__(self, target, meter: dict, kwargs: dict):
        for k, v in (("_gm_target", target), ("_gm_inner", None), ("_gm_it", None), ("_gm_acc", _StreamUsage()),
                     ("_gm_meter", meter), ("_gm_kwargs", dict(kwargs)), ("_gm_done", False)):
            object.__setattr__(self, k, v)

    @property
    def _gm_obj(self):
        return self._gm_inner if self._gm_inner is not None else self._gm_target

    @property
    def __class__(self):  # isinstance(stream, openai.Stream) stays True
        return type(self._gm_obj)

    def _gm_finish(self, final=None) -> None:
        """Write the usage row once: the usage seen in the stream, else an estimate. Never raises."""
        if self._gm_done:
            return
        object.__setattr__(self, "_gm_done", True)
        try:
            from ._usage import extract

            snap = None
            for name in _SNAPSHOTS:
                with contextlib.suppress(Exception):
                    snap = snap or getattr(self._gm_obj, name, None)
            u = (extract(final) if final is not None else None) or self._gm_acc.final(snap)
            rid = None
            if u is None:  # the provider sent no usage: estimate it, so budgets still grow
                n_in, _ = _estimate_tokens(self._gm_kwargs)
                u = {"input_tokens": n_in, "output_tokens": self._gm_acc.chars // 4, "cache_creation_tokens": 0,
                     "cache_read_tokens": 0, "model": None, "id": None, "shape": None}
                rid = f"gm-est-{uuid.uuid4().hex}"
            _log(self._gm_meter, u, self._gm_kwargs, rid)
        except Exception as e:  # noqa: BLE001 - never break the caller
            _warn("wrap: stream logging failed", e)

    # iteration
    def __iter__(self):
        return self

    def __next__(self):
        if self._gm_it is None:
            object.__setattr__(self, "_gm_it", iter(self._gm_obj))
        try:
            ev = next(self._gm_it)
        except StopIteration:
            self._gm_finish()
            raise
        self._gm_acc.feed(ev)
        return ev

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._gm_it is None:
            object.__setattr__(self, "_gm_it", self._gm_obj.__aiter__())
        try:
            ev = await self._gm_it.__anext__()
        except StopAsyncIteration:
            await asyncio.to_thread(self._gm_finish)
            raise
        self._gm_acc.feed(ev)
        return ev

    # stream managers (`with client.messages.stream(...) as s:`) and closable streams
    def __enter__(self):
        inner = self._gm_target.__enter__()
        if inner is not self._gm_target:
            object.__setattr__(self, "_gm_inner", inner)
        return self

    def __exit__(self, *exc):
        try:
            return self._gm_target.__exit__(*exc)
        finally:
            self._gm_finish()

    async def __aenter__(self):
        inner = await self._gm_target.__aenter__()
        if inner is not self._gm_target:
            object.__setattr__(self, "_gm_inner", inner)
        return self

    async def __aexit__(self, *exc):
        try:
            return await self._gm_target.__aexit__(*exc)
        finally:
            await asyncio.to_thread(self._gm_finish)

    def close(self):
        try:
            return self._gm_obj.close()
        finally:
            self._gm_finish()

    def __getattr__(self, name):
        attr = getattr(self._gm_obj, name)
        if name in _FINAL_METHODS and callable(attr):
            return self._gm_final(attr)
        return attr

    def _gm_final(self, fn):
        @functools.wraps(fn)
        def final(*a, **k):
            res = fn(*a, **k)
            if inspect.isawaitable(res):
                async def done():
                    r = await res
                    await asyncio.to_thread(self._gm_finish, r)
                    return r
                return done()
            self._gm_finish(res)
            return res
        return final

    def __del__(self):  # a stream dropped half-way still counts (estimate or the usage seen so far)
        with contextlib.suppress(Exception):
            if not self._gm_done:
                self._gm_finish()


def _stream_result(result, meter: dict, kwargs: dict):
    """Wrap what a streaming call returned. Bedrock ConverseStream returns a dict whose "stream" is the events."""
    if isinstance(result, dict) and "stream" in result:
        return {**result, "stream": _MeteredStream(result["stream"], meter, kwargs)}
    return _MeteredStream(result, meter, kwargs)


# ---------------------------------------------------------------- with_raw_response / with_streaming_response

def _raw_result(result, meter: dict, kwargs: dict, streaming: bool, raw: str):
    """A raw HTTP response wrapper: usage is recorded when it is parsed. A non-streaming with_raw_response is parsed
    right away (the SDKs cache the parsed object, so the caller's own parse() returns the same one)."""
    if raw == "with_raw_response" and not streaming:
        with contextlib.suppress(Exception):
            parsed = result.parse()
            if inspect.isawaitable(parsed):  # async SDK: parse() is a coroutine, recorded when the caller awaits it
                parsed.close()
            else:
                _record(meter, parsed, kwargs)
                return result
    return _MeteredRaw(result, meter, kwargs, streaming)


class _MeteredRaw:
    """Proxy around a raw response (or the context manager of with_streaming_response): parse() is recorded; a raw
    response that is never parsed is recorded as an estimate when its context ends."""

    __slots__ = ("_gm_done", "_gm_inner", "_gm_kwargs", "_gm_meter", "_gm_streaming", "_gm_target")

    def __init__(self, target, meter: dict, kwargs: dict, streaming: bool):
        for k, v in (("_gm_target", target), ("_gm_inner", None), ("_gm_meter", meter), ("_gm_kwargs", dict(kwargs)),
                     ("_gm_streaming", streaming), ("_gm_done", False)):
            object.__setattr__(self, k, v)

    @property
    def _gm_obj(self):
        return self._gm_inner if self._gm_inner is not None else self._gm_target

    def _gm_take(self, parsed):
        if self._gm_done:
            return parsed
        object.__setattr__(self, "_gm_done", True)
        if self._gm_streaming:
            return _stream_result(parsed, self._gm_meter, self._gm_kwargs)
        _record(self._gm_meter, parsed, self._gm_kwargs)
        return parsed

    def parse(self, *a, **k):
        res = self._gm_obj.parse(*a, **k)
        if inspect.isawaitable(res):
            async def done():
                return self._gm_take(await res)
            return done()
        return self._gm_take(res)

    def _gm_estimate(self):
        if self._gm_done:
            return
        object.__setattr__(self, "_gm_done", True)
        _MeteredStream(iter(()), self._gm_meter, self._gm_kwargs)._gm_finish()

    def __enter__(self):
        object.__setattr__(self, "_gm_inner", self._gm_target.__enter__())
        return self

    def __exit__(self, *exc):
        try:
            return self._gm_target.__exit__(*exc)
        finally:
            self._gm_estimate()

    async def __aenter__(self):
        object.__setattr__(self, "_gm_inner", await self._gm_target.__aenter__())
        return self

    async def __aexit__(self, *exc):
        try:
            return await self._gm_target.__aexit__(*exc)
        finally:
            await asyncio.to_thread(self._gm_estimate)

    def __getattr__(self, name):
        return getattr(self._gm_obj, name)


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
            raw = self._gm_path[-1] if self._gm_path and self._gm_path[-1] in _RAW else None
            route = self._gm_path[:-1] if raw else self._gm_path
            if callable(attr) and name in ROUTES.get(route, ()):
                return _metered(attr, self._gm_meter, route, name, raw)
            if name in _RAW and self._gm_path in ROUTES and not callable(attr):
                return MeteredClient(attr, path, self._gm_meter)
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

    Streams (`stream=True`, `.stream(...)`, `generate_content_stream`, `converse_stream`, `chat_stream`,
    `stream_async`) are passed through event by event and recorded when they end, with the usage the provider sends
    in the stream; when it sends none, input and output tokens are estimated (request_id "gm-est-..."). With the
    official OpenAI API, chat streams ask for that usage (stream_options.include_usage) unless you set stream_options.
    `with_raw_response` / `with_streaming_response` are checked and recorded when the response is parsed.
    Not recorded: batches, embeddings and every other method.
    Logging never raises: if recording fails, a line goes to stderr and the response is returned as usual.
    Everything else on the client behaves as on the original; `wrapped.__wrapped__` is the original client.

    `budgets` (True = the default budgets file, or a path, or a dict): every recorded call is checked first
    (see budgets.py). A blocked call raises `BudgetExceeded` without reaching the provider; a degraded call runs
    on the budget's `degrade_to` model. `source` labels the calls (an agent, a job) for budgets and reports
    (default: $GIUGGIAI_METER_SOURCE). When usage goes to a remote ingest ($GIUGGIAI_METER_URL), the check asks
    that server (check_remote): its budgets decide, and if it cannot answer, the budgets' fail mode applies.
    """
    if isinstance(client, MeteredClient):
        client = client.__wrapped__
    meter = {"project": str(project), "workspace": workspace, "budgets": budgets, "source": source,
             "provider": providers.normalize(provider) if provider else detect_provider(client),
             "class_hint": None}
    with contextlib.suppress(Exception):
        meter["class_hint"] = providers.class_hint(client)
    return MeteredClient(client, (), meter)
