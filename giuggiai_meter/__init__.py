"""giuggiai-meter: per-call LLM usage logging, per-project cost reports and monthly reconciliation.

Log one line after each model call:

    import giuggiai_meter as gm
    resp = client.messages.create(...)
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
import urllib.request
import uuid
from pathlib import Path

__version__ = "0.1.0"
__all__ = ["connect", "db_path", "log_anthropic", "log_deepseek", "log_openai", "log_perplexity", "log_usage"]

_LOCK = threading.Lock()
_PROVIDERS = {"anthropic": "Anthropic", "openai": "OpenAI", "deepseek": "DeepSeek", "perplexity": "Perplexity",
              "mistral": "Mistral", "google": "Google"}

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
    if not p:
        return None
    return _PROVIDERS.get(str(p).strip().lower(), str(p).strip())


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _emit_http(row: dict) -> bool:
    url, token = os.environ.get("GIUGGIAI_METER_URL"), os.environ.get("GIUGGIAI_METER_TOKEN")
    if not (url and token):
        return False
    if not url.lower().startswith(("http://", "https://")):
        sys.stderr.write("[giuggiai_meter] GIUGGIAI_METER_URL must be http(s); falling back to local storage\n")
        return False
    try:
        req = urllib.request.Request(url, data=json.dumps(row).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Meter-Token": token})
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception as e:  # noqa: BLE001 - never break the caller
        sys.stderr.write(f"[giuggiai_meter] HTTP ingest failed: {e}\n")
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
        sys.stderr.write(f"[giuggiai_meter] log_usage error: {e}\n")
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
        sys.stderr.write(f"[giuggiai_meter] log_anthropic error: {e}\n")
        return None


def log_openai(slug, response, provider="OpenAI", model=None, workspace=None):
    """Read `usage` from an OpenAI-compatible response (OpenAI, DeepSeek, Perplexity, ...) and record it. Never raises."""
    try:
        u = _g(response, "usage", default={}) or {}
        details = _g(u, "prompt_tokens_details", default={}) or {}
        cached = _g(details, "cached_tokens", default=0) or _g(u, "prompt_cache_hit_tokens", default=0)
        return log_usage(slug, provider=provider, model=model or _g(response, "model", default=None),
                         input_tokens=_g(u, "prompt_tokens", "input_tokens"),
                         output_tokens=_g(u, "completion_tokens", "output_tokens"),
                         cache_read_tokens=cached, request_id=_rid(response), workspace=workspace)
    except Exception as e:  # noqa: BLE001 - never break the caller
        sys.stderr.write(f"[giuggiai_meter] log_openai error: {e}\n")
        return None


def log_deepseek(slug, response, model=None, workspace=None):
    return log_openai(slug, response, provider="DeepSeek", model=model, workspace=workspace)


def log_perplexity(slug, response, model=None, workspace=None):
    return log_openai(slug, response, provider="Perplexity", model=model, workspace=workspace)
