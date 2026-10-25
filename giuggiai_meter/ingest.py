"""HTTP ingest: receives usage rows from containers or other hosts and stores them in the local database.

Run:  GIUGGIAI_METER_TOKEN=... python -m giuggiai_meter ingest [--host 127.0.0.1] [--port 8788]
POST /ingest with header X-Meter-Token and a JSON row (request_id and slug required). GET /health -> {"ok": true}.
GET /check?project=&model=&provider=&source=&estimate_usd= with X-Meter-Token -> the budget Decision as JSON, judged
on this server's database with its budgets file ($GIUGGIAI_METER_BUDGETS) and price list ($GIUGGIAI_METER_PRICES).
Binds to 127.0.0.1 by default. Outside localhost, put it behind a reverse proxy with HTTPS (the token travels in a header).
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import _now, insert_row

MAX_BODY = 65536
MIN_TOKEN = 16
_TEXT = {"request_id": 128, "log_time": 32, "slug": 128, "provider": 64, "model": 128, "workspace": 128,
         "source": 128, "created_at": 32, "billing": 64, "unit_id": 128, "unit_kind": 64}
_INT = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")


class BadRow(ValueError):
    pass


def clean_row(row: object) -> dict:
    """Validate an incoming row: short strings for text fields, non-negative integers for token counts."""
    if not isinstance(row, dict):
        raise BadRow("body must be a JSON object")
    out: dict = {}
    for k, n in _TEXT.items():
        v = row.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or len(v) > n:
            raise BadRow(f"{k}: string of at most {n} characters expected")
        out[k] = v
    for k in _INT:
        v = row.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise BadRow(f"{k}: non-negative integer expected")
        out[k] = v
    if not out.get("request_id") or not out.get("slug"):
        raise BadRow("request_id and slug required")
    out.setdefault("created_at", _now())
    out.setdefault("log_time", out["created_at"])
    return out


def make_handler(token: str, db: str | None = None, budgets=None, prices=None):
    if not token or len(token) < MIN_TOKEN:
        raise ValueError(f"the ingest token must be at least {MIN_TOKEN} characters")
    secret = token.encode()

    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # seconds per connection: slow clients are dropped

        def _send(self, code: int, obj: dict) -> None:
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _authorized(self) -> bool:
            given = self.headers.get("X-Meter-Token", "").encode("utf-8", "replace")
            return hmac.compare_digest(given, secret)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            url = urllib.parse.urlsplit(self.path)
            if url.path.rstrip("/") == "/check":
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                try:
                    q = {k: v[0][:128] for k, v in urllib.parse.parse_qs(url.query).items()}
                    est = float(q.get("estimate_usd") or 0)
                    if not (0 <= est < 1e6):
                        raise ValueError("estimate_usd out of range")
                    from .budgets import check

                    d = check(q.get("project", ""), model=q.get("model"), provider=q.get("provider"),
                              source=q.get("source"), estimate_usd=est, db=db, config=budgets, prices=prices)
                    return self._send(200, d.to_dict())
                except ValueError as e:
                    return self._send(400, {"error": str(e)[:200]})
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(f"[giuggiai_meter] check error: {type(e).__name__}\n")
                    return self._send(500, {"error": "internal error"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                if self.path.rstrip("/") != "/ingest":
                    return self._send(404, {"error": "not found"})
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                try:
                    n = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self._send(411, {"error": "Content-Length required"})
                if n < 0:
                    return self._send(400, {"error": "invalid Content-Length"})
                if n > MAX_BODY:
                    return self._send(413, {"error": "body too large"})
                row = clean_row(json.loads(self.rfile.read(n) or b"{}"))
                insert_row(row, db)
                return self._send(200, {"ok": True, "request_id": row["request_id"]})
            except (BadRow, ValueError) as e:
                return self._send(400, {"error": str(e)[:200]})
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[giuggiai_meter] ingest error: {type(e).__name__}\n")
                return self._send(500, {"error": "internal error"})

        def log_message(self, *a):  # quiet
            pass

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8788, db: str | None = None) -> None:
    token = os.environ.get("GIUGGIAI_METER_TOKEN", "")
    if len(token) < MIN_TOKEN:
        raise SystemExit(f"set GIUGGIAI_METER_TOKEN (at least {MIN_TOKEN} characters) before starting the ingest server")
    print(f"giuggiai-meter ingest on {host}:{port}")
    ThreadingHTTPServer((host, port), make_handler(token, db)).serve_forever()
