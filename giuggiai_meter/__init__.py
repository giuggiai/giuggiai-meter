"""giuggiai-meter: per-call LLM usage logging, per-project cost reports and monthly reconciliation.

Wrap the client once and every call is recorded (Anthropic, OpenAI and every OpenAI-compatible API,
Google Gemini, Mistral, Cohere, Amazon Bedrock, Ollama):

    import giuggiai_meter as gm
    client = gm.wrap(Anthropic(), project="my-project")    # or gm.wrap(OpenAI(...), project=..., provider="DeepSeek")
    client.messages.create(...)                             # recorded, response unchanged

Or log one line after each model call; log_response recognises the response by its shape:

    resp = client.messages.create(...)
    gm.log_response("my-project", resp)                    # any supported SDK object or raw JSON dict
    gm.log_anthropic("my-project", resp)          # Anthropic Messages API
    gm.log_openai("my-project", resp)             # OpenAI-compatible APIs (OpenAI, DeepSeek, Perplexity, ...)

Design rules:
- the logger never raises: metering must never break the caller;
- it stores tokens and metadata only (no prompt or completion text) in a local SQLite file;
- prices live in one price list (a JSON file), applied when you build reports, not when you log.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

__version__ = "0.1.0"
__all__ = ["connect", "db_path", "log_anthropic", "log_bedrock", "log_cohere", "log_deepseek", "log_gemini", "log_grok",
           "log_mistral", "log_ollama", "log_openai", "log_perplexity", "log_qwen", "log_response", "log_usage", "wrap"]

_LOCK = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS usage(
  request_id TEXT PRIMARY KEY,
  log_time TEXT NOT NULL,
  slug TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  cache_creation_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  workspace TEXT,
  source TEXT,
  synced INTEGER DEFAULT 0,
  sync_error TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_synced ON usage(synced);
CREATE INDEX IF NOT EXISTS ix_usage_time ON usage(log_time);
"""
FIELDS = ("request_id", "log_time", "slug", "provider", "model", "input_tokens", "output_tokens",
          "cache_creation_tokens", "cache_read_tokens", "workspace", "source", "created_at")


def db_path() -> str:
    """SQLite file: $GIUGGIAI_METER_DB, otherwise ~/.local/share/giuggiai-meter/usage.db."""
    return os.environ.get("GIUGGIAI_METER_DB") or str(Path.home() / ".local/share/giuggiai-meter/usage.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open (and create if needed) the usage database. The file is made readable by its owner only."""
    p = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(p)), mode=0o700, exist_ok=True)
    # create the database, -wal and -shm files as owner-only before SQLite opens them (no process-wide umask change)
    for f in (p, p + "-wal", p + "-shm"):
        try:
            os.close(os.open(f, os.O_CREAT | os.O_WRONLY, 0o600))
        except OSError:
            pass
    c = sqlite3.connect(p, timeout=10)
    c.execute("PRAGMA journal_mode=WAL;")
    c.executescript(_DDL)
    for f in (p, p + "-wal", p + "-shm"):
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
    return c


def _norm_provider(p):
    from .providers import normalize
    return normalize(p)


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _warn(msg: str) -> None:
    """Write a diagnostic line to stderr. Never raises (stderr may be None under pythonw or some daemons)."""
    with contextlib.suppress(Exception):
        sys.stderr.write(msg if msg.endswith("\n") else msg + "\n")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the ingest token must not travel to another URL."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_HTTP_PAUSE_S = 60  # after a failure the ingest is skipped for this long (rows go to local storage)
_http_down_until = 0.0


def _emit_http(row: dict) -> bool:
    global _http_down_until
    url, token = os.environ.get("GIUGGIAI_METER_URL"), os.environ.get("GIUGGIAI_METER_TOKEN")
    if not (url and token):
        return False
    if not url.lower().startswith(("http://", "https://")):
        _warn("[giuggiai_meter] GIUGGIAI_METER_URL must be http(s); falling back to local storage")
        return False
    if time.monotonic() < _http_down_until:
        return False
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if url.lower().startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
        _warn("[giuggiai_meter] warning: the ingest token is sent over plain http to a remote host; use https")
    try:
        req = urllib.request.Request(url, data=json.dumps(row).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Meter-Token": token})
        with _OPENER.open(req, timeout=2) as r:
            r.read()
        return True
    except Exception as e:  # noqa: BLE001 - never break the caller
        _http_down_until = time.monotonic() + _HTTP_PAUSE_S
        _warn(f"[giuggiai_meter] HTTP ingest failed ({type(e).__name__}); local storage for the next {_HTTP_PAUSE_S} s")
        return False


def insert_row(row: dict, path: str | None = None) -> None:
    """Insert a usage row (idempotent on request_id)."""
    with _LOCK, contextlib.closing(connect(path)) as c, c:
        c.execute(f"INSERT OR IGNORE INTO usage({','.join(FIELDS)},synced) VALUES({','.join('?' * len(FIELDS))},0)",
                  [row.get(k) for k in FIELDS])


def log_usage(slug, provider=None, model=None, input_tokens=0, output_tokens=0, cache_creation_tokens=0,
              cache_read_tokens=0, request_id=None, workspace=None, source=None, log_time=None):
    """Record one model call. Idempotent on request_id. Never raises: returns the request_id or None."""
    try:
        row = {
            "request_id": request_id or f"gm-{uuid.uuid4().hex}",
            "log_time": log_time or _now(),
            "slug": str(slug),
            "provider": _norm_provider(provider),
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_creation_tokens": int(cache_creation_tokens or 0),
            "cache_read_tokens": int(cache_read_tokens or 0),
            "workspace": workspace,
            "source": source or os.environ.get("GIUGGIAI_METER_SOURCE"),  # no hostname by default (privacy)
            "created_at": _now(),
        }
        if _emit_http(row):  # remote ingest first when configured (containers, other hosts)
            return row["request_id"]
        insert_row(row)
        return row["request_id"]
    except Exception as e:  # noqa: BLE001 - never break the caller
        _warn(f"[giuggiai_meter] log_usage error: {e}\n")
        return None


def _g(obj, *names, default=0):
    for n in names:
        v = getattr(obj, n, None)
        if v is None and isinstance(obj, dict):
            v = obj.get(n)
        if v is not None:
            return v
    return default


def _rid(response):
    rid = getattr(response, "id", None) or (response.get("id") if isinstance(response, dict) else None)
    return "gm-" + (str(rid) if rid else uuid.uuid4().hex)


def log_anthropic(slug, response, model=None, workspace=None):
    """Read `usage` from an Anthropic Messages response and record it. Never raises."""
    try:
        u = _g(response, "usage", default={}) or {}
        return log_usage(slug, provider="Anthropic", model=model or _g(response, "model", default=None),
                         input_tokens=_g(u, "input_tokens"), output_tokens=_g(u, "output_tokens"),
                         cache_creation_tokens=_g(u, "cache_creation_input_tokens"),
                         cache_read_tokens=_g(u, "cache_read_input_tokens"), request_id=_rid(response),
                         workspace=workspace)
    except Exception as e:  # noqa: BLE001 - never break the caller
        _warn(f"[giuggiai_meter] log_anthropic error: {e}\n")
        return None


def log_openai(slug, response, provider="OpenAI", model=None, workspace=None):
    """Read `usage` from an OpenAI-compatible response (OpenAI, DeepSeek, Perplexity, ...) and record it. Never raises.
    Works with Chat Completions (prompt/completion tokens) and the Responses API (input/output tokens)."""
    try:
        u = _g(response, "usage", default={}) or {}
        details = _g(u, "prompt_tokens_details", "input_tokens_details", default={}) or {}
        cached = _g(details, "cached_tokens", default=0) or _g(u, "prompt_cache_hit_tokens", default=0)
        return log_usage(slug, provider=provider, model=model or _g(response, "model", default=None),
                         # convention: input_tokens = uncached input (OpenAI-style totals include the cached part)
                         input_tokens=max(0, int(_g(u, "prompt_tokens", "input_tokens") or 0) - int(cached or 0)),
                         output_tokens=_g(u, "completion_tokens", "output_tokens"),
                         cache_read_tokens=cached, request_id=_rid(response), workspace=workspace)
    except Exception as e:  # noqa: BLE001 - never break the caller
        _warn(f"[giuggiai_meter] log_openai error: {e}\n")
        return None


def log_deepseek(slug, response, model=None, workspace=None):
    return log_openai(slug, response, provider="DeepSeek", model=model, workspace=workspace)


def log_perplexity(slug, response, model=None, workspace=None):
    return log_openai(slug, response, provider="Perplexity", model=model, workspace=workspace)


def log_response(project, response, provider=None, model=None, workspace=None, fallback_model=None):
    """Record any supported LLM response, SDK object or raw JSON dict, recognised by its shape. Never raises.

    Shapes: OpenAI Chat Completions and every OpenAI-compatible API (prompt/completion tokens), OpenAI Responses API,
    Anthropic Messages, Google Gemini (usage_metadata, also camelCase JSON), Cohere (billed_units), Amazon Bedrock
    Converse (inputTokens/outputTokens), Ollama (prompt_eval_count/eval_count), Alibaba DashScope native.
    `provider`: who bills the call; when omitted it comes from the shape or the model id (see providers.py).
    `model` overrides the model in the response; `fallback_model` is used only when the response names none.
    Returns the request_id, or None when nothing was recorded (no usage in the response, or an error)."""
    try:
        from ._usage import extract, resolve_provider

        u = extract(response)
        if u is None:
            _warn("[giuggiai_meter] log_response: no token usage found in the response, nothing recorded\n")
            return None
        mdl = model or u["model"] or fallback_model
        rid = "gm-" + (str(u["id"]) if u["id"] else uuid.uuid4().hex)
        return log_usage(project, provider=resolve_provider(provider, mdl, u["shape"]), model=mdl,
                         input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                         cache_creation_tokens=u["cache_creation_tokens"], cache_read_tokens=u["cache_read_tokens"],
                         request_id=rid, workspace=workspace)
    except Exception as e:  # noqa: BLE001 - never break the caller
        _warn(f"[giuggiai_meter] log_response error: {e}\n")
        return None


def log_gemini(project, response, model=None, workspace=None):
    """Google Gemini (google-genai / google-generativeai objects, or REST JSON)."""
    return log_response(project, response, provider="Google", model=model, workspace=workspace)


def log_grok(project, response, model=None, workspace=None):
    """xAI Grok (OpenAI-compatible API)."""
    return log_response(project, response, provider="xAI", model=model, workspace=workspace)


def log_qwen(project, response, model=None, workspace=None):
    """Alibaba Qwen: DashScope native or OpenAI-compatible mode."""
    return log_response(project, response, provider="Alibaba Qwen", model=model, workspace=workspace)


def log_mistral(project, response, model=None, workspace=None):
    """Mistral (mistralai SDK `chat.complete`, or OpenAI-compatible JSON)."""
    return log_response(project, response, provider="Mistral", model=model, workspace=workspace)


def log_cohere(project, response, model=None, workspace=None):
    """Cohere chat (v1 meta.billed_units or v2 usage.billed_units)."""
    return log_response(project, response, provider="Cohere", model=model, workspace=workspace)


def log_bedrock(project, response, model=None, workspace=None):
    """Amazon Bedrock Converse API (boto3 `converse`); pass model= (the Converse response does not name it)."""
    return log_response(project, response, provider="AWS Bedrock", model=model, workspace=workspace)


def log_ollama(project, response, model=None, workspace=None):
    """Ollama native API (`chat` / `generate`)."""
    return log_response(project, response, provider="Ollama", model=model, workspace=workspace)


from ._wrap import wrap  # imported last: it needs the log_* functions above
