"""Plans: calls covered by a flat subscription (a coding-assistant plan, a team seat, a committed-use deal).

Such calls cost nothing per token, but they still have a list-price value: comparing that value with the monthly fee
tells you whether the plan pays for itself, and keeps per-token budgets from counting money you never spend.

A call belongs to a plan when it was logged with billing="subscription:<id>", or, when logged without billing,
when it matches the plan's `match` patterns in the budgets file (same keys as budgets: project, source, provider,
model). billing="free" counts zero everywhere. Everything else is "api" (paid per token).
"""
from __future__ import annotations

import contextlib
from collections import defaultdict

from . import prices as P


def billing_for(row: dict, plans: list[dict]) -> str:
    """'subscription:<id>', 'free' or 'api' for a usage row ({project or slug, source, provider, model, billing})."""
    from .budgets import matches

    b = row.get("billing")
    if b:
        low = str(b).strip().lower()
        if low in ("api", "free"):
            return low
        if low.startswith("subscription:") and len(low) > len("subscription:"):
            return "subscription:" + str(b).strip().split(":", 1)[1]
        return "api"  # unknown values count as paid per token (never silently free)
    r = row if "project" in row else {**row, "project": row.get("slug")}
    for p in plans or []:
        if p.get("match") and matches(p["match"], r):
            return f"subscription:{p['id']}"
    return "api"


def report(month: str, price_idx: dict, config, db: str | None = None) -> list[dict]:
    """Per plan for YYYY-MM: calls, list_value (what the calls would cost per token, in the config currency), fee,
    multiple (list_value / fee) and cash (what those calls cost per token: always 0)."""
    from . import connect
    from .budgets import _config, to_currency

    cfg = _config(config)
    plans = {p["id"]: p for p in cfg["plans"]}
    agg: dict[str, dict] = defaultdict(lambda: {"calls": 0, "list_usd": 0.0})
    with contextlib.closing(connect(db)) as c:
        rows = c.execute("SELECT slug, source, provider, model, log_time, input_tokens, output_tokens, "
                         "cache_creation_tokens, cache_read_tokens, billing FROM usage WHERE substr(log_time, 1, 7) = ?",
                         (month,)).fetchall()
    for slug, src, prov, model, t, it, ot, cw, cr, bill in rows:
        b = billing_for({"project": slug, "source": src, "provider": prov, "model": model, "billing": bill}, cfg["plans"])
        if not b.startswith("subscription:"):
            continue
        a = agg[b.split(":", 1)[1]]
        a["calls"] += 1
        a["list_usd"] += P.cost(P.pick(price_idx, model, (t or "")[:10], prov), it or 0, ot or 0, cw or 0, cr or 0)
    out = []
    for pid in sorted(set(plans) | set(agg)):
        p, a = plans.get(pid, {"monthly_fee": 0.0, "currency": cfg["currency"]}), agg.get(pid, {"calls": 0, "list_usd": 0.0})
        value = round(to_currency(a["list_usd"], cfg), 4)
        fee = _fee_in(p, cfg)
        out.append({"plan": pid, "calls": a["calls"], "list_value": value, "fee": fee,
                    "multiple": round(value / fee, 2) if fee else None, "cash": 0.0, "currency": cfg["currency"]})
    return out


def _fee_in(plan: dict, cfg: dict) -> float:
    """The plan's monthly fee in the config currency (fx gives units of config currency per unit of plan currency)."""
    fee, cur = float(plan.get("monthly_fee") or 0), plan.get("currency") or cfg["currency"]
    if cur == cfg["currency"]:
        return round(fee, 4)
    return round(fee * cfg["fx"].get(cur, 1.0), 4)
