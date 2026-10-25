"""Forecast: how fast each budget is being spent and when it runs out.

burn_per_day is the average daily spend over the last 7 days (today included). exhaust_date is today when the budget
is already spent, else today + remaining / burn (None when nothing is being spent). projected_period_end is spend so
far + burn x days left in the period (None for `total` budgets). state: "over" when spent >= limit; "warn" when
spent >= warn_at x limit, or when the budget would run out before its period ends; otherwise "ok".
"""
from __future__ import annotations

import calendar
import datetime
import math

from .budgets import Ledger, _config, _prices, horizon, ledger


def _period_end(period: str, today: datetime.date) -> datetime.date | None:
    if period == "day":
        return today
    if period == "week":
        return today + datetime.timedelta(days=6 - today.weekday())
    if period == "month":
        return today.replace(day=calendar.monthrange(today.year, today.month)[1])
    return None


def status(config=None, price_idx=None, db: str | None = None, today: datetime.date | None = None) -> list[dict]:
    cfg = _config(config)
    idx = _prices(price_idx)
    today = today or datetime.date.today()
    try:
        led = ledger(db, cfg, idx, today)
    except FileNotFoundError:  # nothing recorded yet: every budget is untouched
        led = Ledger(db or "", cfg, idx, horizon(cfg, today))
    out = []
    for b in cfg["budgets"]:
        spent = led.spent(b, today)
        days = led.days[b["id"]]
        week = [(today - datetime.timedelta(days=i)).isoformat() for i in range(7)]
        burn = round(sum(days.get(d, 0.0) for d in week) / 7, 6)
        remaining = round(b["limit"] - spent, 6)
        if remaining <= 0:
            exhaust = today
        elif burn > 0:
            exhaust = today + datetime.timedelta(days=math.ceil(remaining / burn))
        else:
            exhaust = None
        end = _period_end(b["period"], today)
        projected = None if end is None else round(spent + burn * (end - today).days, 6)
        if spent >= b["limit"]:
            state = "over"
        elif (b["warn_at"] is not None and spent >= float(b["warn_at"]) * b["limit"]) or (
                end is not None and exhaust is not None and exhaust <= end):
            state = "warn"
        else:
            state = "ok"
        out.append({"budget_id": b["id"], "period": b["period"], "spent": spent, "limit": b["limit"],
                    "remaining": remaining, "currency": cfg["currency"], "burn_per_day": burn,
                    "exhaust_date": None if exhaust is None else exhaust.isoformat(),
                    "projected_period_end": projected, "unpriced_calls": led.unpriced[b["id"]], "state": state})
    return out


def exit_code(rows: list[dict]) -> int:
    """0 all ok, 1 at least one warn, 2 at least one over."""
    states = {r["state"] for r in rows}
    return 2 if "over" in states else 1 if "warn" in states else 0
