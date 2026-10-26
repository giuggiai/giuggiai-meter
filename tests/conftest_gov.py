"""Shared helpers for the governance tests (budgets, plans, units, forecast)."""
import datetime
import itertools
import json

import giuggiai_meter as gm

PRICES = [
    {"model": "big", "provider": "OpenAI", "input": 10.0, "output": 30.0, "effective_from": "2026-01-01"},
    {"model": "small", "provider": "OpenAI", "input": 1.0, "output": 3.0, "effective_from": "2026-01-01"},
    {"model": "claude-x", "provider": "Anthropic", "input": 3.0, "output": 15.0, "effective_from": "2026-01-01"},
]
TODAY = datetime.date(2026, 10, 15)  # a Thursday
_ids = itertools.count()


def setup_env(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps(PRICES))
    monkeypatch.setenv("GIUGGIAI_METER_DB", str(db))
    monkeypatch.setenv("GIUGGIAI_METER_PRICES", str(prices))
    monkeypatch.setenv("GIUGGIAI_METER_BUDGETS", str(tmp_path / "budgets.json"))
    for k in ("GIUGGIAI_METER_URL", "GIUGGIAI_METER_TOKEN", "GIUGGIAI_METER_UNIT", "GIUGGIAI_METER_UNIT_KIND",
              "GIUGGIAI_METER_SOURCE"):
        monkeypatch.delenv(k, raising=False)
    from giuggiai_meter import budgets as B

    B._REMOTE_FAIL.clear()  # what a remote meter said in an earlier test must not leak into this one
    monkeypatch.setattr(B, "_remote_down_until", 0.0)
    return str(db), str(prices)


def spend(usd, day="2026-10-15", project="p", source=None, model="big", provider="OpenAI", billing=None, unit=None,
          kind=None):
    """Record a call costing exactly `usd` on the `big` model (1M input tokens = 10 USD)."""
    tokens = round(usd * 100_000) if model == "big" else round(usd * 1_000_000)
    return gm.log_usage(project, provider=provider, model=model, input_tokens=tokens, request_id=f"t-{next(_ids)}",
                        source=source, log_time=f"{day} 12:00:00", billing=billing, unit_id=unit, unit_kind=kind)


def cfg(*budgets, currency="USD", fx=None, plans=None):
    return {"currency": currency, "fx": fx or {}, "budgets": list(budgets), "plans": plans or []}
