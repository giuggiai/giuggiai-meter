"""Model advice from your own results: is a cheaper model just as good for this kind of work?

    giuggiai-meter advise --kind support-reply            # needs gm.unit(...) + gm.outcome(...) data

A model is recommended over the current one only when, for units of that kind with a recorded outcome:
- both have at least `min_n` units with an outcome (default 20);
- its acceptance rate is at most `max_drop` below the current model's (default 0.05 = 5 percentage points);
- its cost per accepted unit is lower. Among those, the cheapest per accepted unit wins.
The current model is the one given, or else the one with most units of that kind.
"""
from __future__ import annotations

from ._unitreport import units


def advise(kind: str, price_idx: dict, db: str | None = None, min_n: int = 20, max_drop: float = 0.05,
           current: str | None = None) -> dict:
    """{recommend: model or None, current, reason, table}."""
    table = units(price_idx, db, kind=kind)
    if not table:
        return {"recommend": None, "current": current, "reason": f"no units of kind {kind!r}", "table": table}
    by = {r["model"]: r for r in table}
    cur = current or max(table, key=lambda r: (r["units"], r["model"]))["model"]
    base = by.get(cur)
    if base is None or base["with_outcome"] < min_n or base["acceptance_rate"] is None:
        return {"recommend": None, "current": cur, "table": table,
                "reason": f"current model {cur!r} has fewer than {min_n} units with an outcome"}
    best = None
    for r in table:
        if r["model"] == cur or r["with_outcome"] < min_n or r["cost_per_accepted"] is None:
            continue
        if r["acceptance_rate"] < base["acceptance_rate"] - max_drop - 1e-12:
            continue
        if base["cost_per_accepted"] is not None and r["cost_per_accepted"] >= base["cost_per_accepted"]:
            continue
        if best is None or r["cost_per_accepted"] < best["cost_per_accepted"]:
            best = r
    if best is None:
        return {"recommend": None, "current": cur, "table": table,
                "reason": f"no model with ≥ {min_n} outcomes is cheaper per accepted unit within {max_drop:.0%} acceptance"}
    saving = (1 - best["cost_per_accepted"] / base["cost_per_accepted"]) if base["cost_per_accepted"] else None
    return {"recommend": best["model"], "current": cur, "table": table,
            "reason": (f"{best['model']}: acceptance {best['acceptance_rate']:.0%} vs {base['acceptance_rate']:.0%} "
                       f"(n={best['with_outcome']} vs {base['with_outcome']}), cost per accepted unit "
                       f"{best['cost_per_accepted']:.4f} vs {base['cost_per_accepted']:.4f} USD"
                       + (f" ({saving:.0%} less)" if saving is not None else ""))}
