"""Monthly cost report per project and provider, and reconciliation against the providers' official totals."""
from __future__ import annotations

import contextlib
import csv
import io
from collections import defaultdict

from . import connect
from . import prices as P


def monthly(month: str, price_idx: dict, db: str | None = None) -> list[dict]:
    """Rows {slug, provider, calls, input_tokens, output_tokens, cost_usd, unpriced_calls} for YYYY-MM."""
    agg: dict[tuple, dict] = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                                    "cost_usd": 0.0, "unpriced_calls": 0})
    with contextlib.closing(connect(db)) as c:
        rows = c.execute("SELECT slug, provider, model, log_time, input_tokens, output_tokens, cache_creation_tokens, "
                         "cache_read_tokens FROM usage WHERE substr(log_time, 1, 7) = ?", (month,)).fetchall()
    for slug, provider, model, t, it, ot, cw, cr in rows:
        price = P.pick(price_idx, model, (t or "")[:10])
        a = agg[(slug, provider or "?")]
        a["calls"] += 1
        a["input_tokens"] += it or 0
        a["output_tokens"] += ot or 0
        a["cost_usd"] += P.cost(price, it or 0, ot or 0, cw or 0, cr or 0)
        a["unpriced_calls"] += 0 if price else 1
    return [{"slug": s, "provider": p, **{k: (round(v, 4) if k == "cost_usd" else v) for k, v in a.items()}}
            for (s, p), a in sorted(agg.items())]


def unpriced(month: str, price_idx: dict, db: str | None = None) -> list[dict]:
    """Calls of YYYY-MM whose model has no price: rows {slug, provider, model, calls}, most frequent first."""
    agg: dict[tuple, int] = defaultdict(int)
    with contextlib.closing(connect(db)) as c:
        rows = c.execute("SELECT slug, provider, model, log_time FROM usage WHERE substr(log_time, 1, 7) = ?",
                         (month,)).fetchall()
    for slug, provider, model, t in rows:
        if not P.pick(price_idx, model, (t or "")[:10]):
            agg[(slug, provider or "?", model or "?")] += 1
    return [{"slug": s, "provider": p, "model": m, "calls": n}
            for (s, p, m), n in sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))]


def reconcile(report: list[dict], official: dict[str, float]) -> list[dict]:
    """Compare computed cost per provider with the official monthly cost from each provider console (USD).
    Returns rows {provider, computed_usd, official_usd, diff_usd, diff_pct}."""
    comp: dict[str, float] = defaultdict(float)
    for r in report:
        comp[r["provider"]] += r["cost_usd"]
    out = []
    for prov in sorted(set(comp) | set(official)):
        c, o = round(comp.get(prov, 0.0), 4), official.get(prov)
        diff = None if o is None else round(c - o, 4)
        pct = None if (o in (None, 0)) else round(100 * (c - o) / o, 1)
        out.append({"provider": prov, "computed_usd": c, "official_usd": o, "diff_usd": diff, "diff_pct": pct})
    return out


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def to_table(rows: list[dict]) -> str:
    if not rows:
        return "(no data)"
    cols = list(rows[0].keys())
    width = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    line = "  ".join(c.ljust(width[c]) for c in cols)
    return "\n".join([line, "  ".join("-" * width[c] for c in cols)]
                     + ["  ".join(str(r[c]).ljust(width[c]) for c in cols) for r in rows])


from ._html import to_html  # noqa: F401 - re-exported: report.to_html(...)
