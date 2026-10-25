"""Cost per unit of work and per ACCEPTED unit, by kind and by model (the model that spent most on the unit)."""
from __future__ import annotations

import contextlib
from collections import defaultdict

from . import prices as P


def units(price_idx: dict, db: str | None = None, kind: str | None = None, month: str | None = None) -> list[dict]:
    """Rows {kind, model, units, with_outcome, accepted, cost, cost_per_unit, cost_per_accepted, acceptance_rate}.

    cost is the list cost in USD of every call of the unit. A unit is attributed to the model that cost the most in it.
    acceptance_rate = accepted / units with an outcome; cost_per_accepted = cost / accepted (None when 0 accepted)."""
    from . import connect

    q = ("SELECT u.unit_id, COALESCE(u.unit_kind, o.unit_kind), u.provider, u.model, u.log_time, u.input_tokens, "
         "u.output_tokens, u.cache_creation_tokens, u.cache_read_tokens, o.accepted FROM usage u "
         "LEFT JOIN outcomes o ON o.unit_id = u.unit_id WHERE u.unit_id IS NOT NULL")
    args: list = []
    if month:
        q += " AND substr(u.log_time, 1, 7) = ?"
        args.append(month)
    with contextlib.closing(connect(db)) as c:
        rows = c.execute(q, args).fetchall()
    per_unit: dict[str, dict] = {}
    for uid, k, prov, model, t, it, ot, cw, cr, acc in rows:
        u = per_unit.setdefault(uid, {"kind": None, "by_model": defaultdict(float), "cost": 0.0, "accepted": acc})
        u["kind"] = u["kind"] or k
        cost = P.cost(P.pick(price_idx, model, (t or "")[:10], prov), it or 0, ot or 0, cw or 0, cr or 0)
        u["cost"] += cost
        u["by_model"][model or "?"] += cost
    agg: dict[tuple, dict] = defaultdict(lambda: {"units": 0, "with_outcome": 0, "accepted": 0, "cost": 0.0})
    for u in per_unit.values():
        k = u["kind"] or "?"
        if kind is not None and k != kind:
            continue
        model = max(u["by_model"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        a = agg[(k, model)]
        a["units"] += 1
        a["cost"] += u["cost"]
        if u["accepted"] is not None:
            a["with_outcome"] += 1
            a["accepted"] += int(u["accepted"])
    out = []
    for (k, m), a in sorted(agg.items()):
        out.append({"kind": k, "model": m, "units": a["units"], "with_outcome": a["with_outcome"],
                    "accepted": a["accepted"], "cost": round(a["cost"], 6),
                    "cost_per_unit": round(a["cost"] / a["units"], 6),
                    "cost_per_accepted": round(a["cost"] / a["accepted"], 6) if a["accepted"] else None,
                    "acceptance_rate": round(a["accepted"] / a["with_outcome"], 4) if a["with_outcome"] else None})
    return out
