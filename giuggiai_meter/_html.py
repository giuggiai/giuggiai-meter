"""Self-contained HTML report: one file, inline CSS, no scripts, no external resources."""
from __future__ import annotations

import datetime
from collections import defaultdict
from html import escape

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
# provider colours, in order of cost; red is kept for warnings
_SERIES = ("#5BB4D9", "#2F6B4F", "#F2C94C", "#15171C", "#8FCBE5", "#6E9F86", "#C9A43A", "#5D5F66")
_TOLERANCE_PCT = 5.0

_CSS = """
:root{--ink:#15171C;--paper:#FBFAF6;--line:#E2E0D8;--yellow:#F2C94C;--green:#2F6B4F;--red:#B8430F;--blue:#5BB4D9;
--muted:#5D5F66;--card:#FFFFFF;--track:#EFEDE6}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans",sans-serif;
font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
.wrap{max-width:980px;margin:0 auto;padding:28px 16px 48px}
.brand{display:flex;align-items:center;gap:10px;font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
.mark{width:18px;height:20px;flex:0 0 auto}
.brand span.sub{color:var(--muted);font-weight:600}
h1{font-size:clamp(30px,5.5vw,48px);line-height:1.05;letter-spacing:-.025em;margin:18px 0 0;font-weight:800}
.hero{margin-top:22px;border-top:3px solid var(--ink);padding-top:18px}
.eyebrow{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.total{font-size:clamp(60px,14vw,120px);font-weight:800;letter-spacing:-.045em;line-height:1;margin:6px 0 4px}
.total .hl{background:linear-gradient(transparent 60%,var(--yellow) 60%,var(--yellow) 90%,transparent 90%)}
.total .cents{font-size:.42em;letter-spacing:-.02em}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-top:20px;background:var(--card);
border:1px solid var(--line)}
.kpi{padding:14px 16px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);margin:0 -1px -1px 0}
.kpi b{display:block;font-size:24px;line-height:1.2;letter-spacing:-.01em}
.kpi.warn b{color:var(--red)}
section{margin-top:44px}
h2{display:flex;justify-content:space-between;gap:12px;font-size:12px;font-weight:800;letter-spacing:.12em;
text-transform:uppercase;margin:0 0 10px;padding-bottom:8px;border-bottom:2px solid var(--ink)}
h2 span{color:var(--muted);font-weight:600;letter-spacing:.04em;text-transform:none}
.bar-row{display:grid;grid-template-columns:minmax(0,200px) minmax(0,1fr) 120px;gap:14px;align-items:center;
padding:9px 0;border-bottom:1px solid var(--line)}
.name{font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track{height:16px;background:var(--track);display:flex;overflow:hidden}
.seg{height:100%;min-width:2px}
.amt{text-align:right;font-weight:750}
.amt small{display:block;font-weight:500;color:var(--muted);font-size:12px}
.amt small .bad{color:var(--red);font-weight:700}
.strip{display:flex;height:30px;border:2px solid var(--ink);background:var(--track);overflow:hidden}
.legend{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:0 24px;margin-top:10px}
.legend.wide{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.legend div{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}
.legend .lbl{flex:1;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:12px;height:12px;flex:none}
.pct{color:var(--muted);font-size:13px;min-width:44px;text-align:right}
.scroll{overflow-x:auto;border:1px solid var(--line);background:var(--card);-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
padding:11px 12px;border-bottom:2px solid var(--ink);white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
.num{text-align:right}
tr.warn td:first-child{box-shadow:inset 4px 0 0 var(--red)}
tfoot td{font-weight:800;border-top:2px solid var(--ink)}
.chip{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:750;line-height:1.6}
.chip.bad{background:var(--red);color:#fff}
.chip.ok{background:var(--green);color:#fff}
.chip.none{background:var(--line);color:var(--ink)}
.callout{margin-top:28px;border:2px solid var(--red);background:#FBEDE6;padding:14px 16px}
.callout p{margin:0 0 8px}
.callout strong{color:var(--red)}
.callout ul{margin:0;padding-left:20px}
.callout code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
.note{font-size:13px;color:var(--muted);margin:10px 0 0}
.empty{padding:28px 16px;border:1px dashed var(--line);text-align:center;color:var(--muted);background:var(--card)}
footer{margin-top:52px;padding-top:12px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
@media (max-width:600px){
.bar-row{grid-template-columns:minmax(0,1fr) auto;gap:6px 12px}
.bar-row .track{grid-column:1/-1;grid-row:2}
.kpi b{font-size:20px}
.brand .sub{display:none}
}
@media print{body{background:#fff}.scroll{overflow:visible}}
"""


def _usd(x: float | None) -> str:
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    a = abs(x)
    return f"{sign}${a:,.4f}" if 0 < a < 0.01 else f"{sign}${a:,.2f}"


def _int(n: int) -> str:
    return f"{int(n or 0):,}"


def _tok(n: int) -> str:
    n = int(n or 0)
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def _pct(part: float, whole: float) -> float:
    return 0.0 if not whole else max(0.0, min(100.0, 100.0 * part / whole))


def month_label(month: str) -> str:
    try:
        y, m = month.split("-")
        return f"{_MONTHS[int(m) - 1]} {int(y)}"
    except (ValueError, IndexError):
        return month


def _big_total(total: float) -> str:
    whole, _, cents = f"{abs(total):,.2f}".partition(".")
    sign = "-" if total < 0 else ""
    return f'<span class="hl">{sign}${whole}<span class="cents">.{cents}</span></span>'


def to_html(rows: list[dict], month: str, reconciliation: list[dict] | None = None,
            unpriced: list[dict] | None = None, title: str | None = None, generated_at: str | None = None) -> str:
    """Render the monthly report as one self-contained HTML page (inline CSS, no scripts, no external resources).

    rows: output of report.monthly(); reconciliation: output of report.reconcile() (optional);
    unpriced: output of report.unpriced() (optional). Every value taken from the data is HTML-escaped.
    """
    from . import __version__

    e = escape
    total = sum(r["cost_usd"] for r in rows)
    calls = sum(r["calls"] for r in rows)
    tin = sum(r["input_tokens"] for r in rows)
    tout = sum(r["output_tokens"] for r in rows)
    n_unpriced = sum(r["unpriced_calls"] for r in rows)

    by_prov: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    by_proj: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0, "unpriced": 0, "prov": defaultdict(float)})
    for r in rows:
        by_prov[r["provider"]]["cost"] += r["cost_usd"]
        by_prov[r["provider"]]["calls"] += r["calls"]
        pj = by_proj[r["slug"]]
        pj["cost"] += r["cost_usd"]
        pj["calls"] += r["calls"]
        pj["unpriced"] += r["unpriced_calls"]
        pj["prov"][r["provider"]] += r["cost_usd"]
    prov_order = sorted(by_prov, key=lambda p: (-by_prov[p]["cost"], -by_prov[p]["calls"], p))
    color = {p: _SERIES[i % len(_SERIES)] for i, p in enumerate(prov_order)}
    proj_order = sorted(by_proj, key=lambda s: (-by_proj[s]["cost"], -by_proj[s]["calls"], s))
    top = max((by_proj[s]["cost"] for s in proj_order), default=0.0)

    heading = title or f"LLM costs · {month_label(month)}"
    out: list[str] = [
        "<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light">', '<meta name="referrer" content="no-referrer">',
        f"<title>{e(heading)}</title>", f"<style>{_CSS}</style>", "</head>", "<body>", '<main class="wrap">',
        '<div class="brand"><svg class="mark" viewBox="0 0 72 80" aria-hidden="true"><path d="M4 4h64v64l-8 8-8-8-8 8-8-8-8 8-8-8-8 8-8-8z" fill="#15171C"/><path d="M48 26a16 16 0 1 0 2 16H37" fill="none" stroke="#F2C94C" stroke-width="7" stroke-linecap="round"/></svg>giuggiai-meter <span class="sub">· monthly LLM cost report</span></div>',
        f"<h1>{e(heading)}</h1>",
        '<div class="hero">', f'<div class="eyebrow">Total computed cost · {e(month)}</div>',
        f'<div class="total">{_big_total(total)}</div>',
        '<div class="kpis">',
        f'<div class="kpi"><span class="eyebrow">Calls</span><b>{_int(calls)}</b></div>',
        f'<div class="kpi"><span class="eyebrow">Projects</span><b>{len(by_proj)}</b></div>',
        f'<div class="kpi"><span class="eyebrow">Input tokens</span><b title="{_int(tin)}">{_tok(tin)}</b></div>',
        f'<div class="kpi"><span class="eyebrow">Output tokens</span><b title="{_int(tout)}">{_tok(tout)}</b></div>',
        (f'<div class="kpi{" warn" if n_unpriced else ""}"><span class="eyebrow">Unpriced calls</span>'
         f"<b>{_int(n_unpriced)}</b></div>"),
        "</div>", "</div>",
    ]

    if unpriced:
        items = "".join(f'<li><code>{e(str(u["model"]))}</code> — {e(str(u["slug"]))} / {e(str(u["provider"]))}: '
                        f'{_int(u["calls"])} call{"s" if u["calls"] != 1 else ""}</li>' for u in unpriced[:20])
        more = f"<li>…and {len(unpriced) - 20} more</li>" if len(unpriced) > 20 else ""
        out.append(f'<div class="callout"><p><strong>{_int(n_unpriced)} call{"s" if n_unpriced != 1 else ""} '
                   f"without a price</strong> are counted as $0. Add these models to your price list:</p>"
                   f"<ul>{items}{more}</ul></div>")
    elif n_unpriced:
        out.append(f'<div class="callout"><p><strong>{_int(n_unpriced)} call{"s" if n_unpriced != 1 else ""} without '
                   "a price</strong> are counted as $0: add their models to your price list "
                   "(rows marked in red below).</p></div>")

    if not rows:
        out.append('<section><div class="empty">No calls recorded in this month.</div></section>')
    else:
        # cost per project, each bar split by provider
        out.append(f"<section><h2>Cost per project <span>{len(by_proj)} project{'s' if len(by_proj) != 1 else ''}"
                   "</span></h2>")
        for s in proj_order:
            pj = by_proj[s]
            width = _pct(pj["cost"], top)
            segs = "".join(
                f'<div class="seg" style="width:{_pct(c, pj["cost"]):.2f}%;background:{color[p]}" '
                f'title="{e(p)}: {_usd(c)}"></div>'
                for p, c in sorted(pj["prov"].items(), key=lambda kv: (-kv[1], kv[0])) if c > 0)
            warn = f' · <span class="bad">{_int(pj["unpriced"])} unpriced</span>' if pj["unpriced"] else ""
            out.append(f'<div class="bar-row"><div class="name" title="{e(s)}">{e(s)}</div>'
                       f'<div class="track"><div style="display:flex;width:{width:.2f}%">{segs}</div></div>'
                       f'<div class="amt">{_usd(pj["cost"])}<small>{_int(pj["calls"])} calls{warn}</small></div></div>')
        out.append('<div class="legend">' + "".join(
            f'<div><span class="dot" style="background:{color[p]}"></span><span class="lbl">{e(p)}</span></div>'
            for p in prov_order) + "</div></section>")

        # cost per provider
        out.append(f"<section><h2>Cost per provider <span>{len(prov_order)} provider"
                   f"{'s' if len(prov_order) != 1 else ''}</span></h2>")
        out.append('<div class="strip">' + "".join(
            f'<div class="seg" style="width:{_pct(by_prov[p]["cost"], total):.2f}%;background:{color[p]}" '
            f'title="{e(p)}: {_usd(by_prov[p]["cost"])}"></div>' for p in prov_order if by_prov[p]["cost"] > 0) + "</div>")
        out.append('<div class="legend wide">' + "".join(
            f'<div><span class="dot" style="background:{color[p]}"></span><span class="lbl">{e(p)}</span>'
            f'<b>{_usd(by_prov[p]["cost"])}</b><span class="pct">{_pct(by_prov[p]["cost"], total):.0f}%</span></div>'
            for p in prov_order) + "</div></section>")

        # reconciliation with the official totals
        if reconciliation:
            out.append("<section><h2>Reconciliation <span>computed vs provider console</span></h2>"
                       '<div class="scroll"><table><thead><tr><th>Provider</th><th class="num">Computed</th>'
                       '<th class="num">Official</th><th class="num">Difference</th><th class="num">Diff %</th>'
                       "<th>Check</th></tr></thead><tbody>")
            for r in reconciliation:
                pct = r.get("diff_pct")
                if r.get("official_usd") is None:
                    chip = '<span class="chip none">no official figure</span>'
                elif pct is None:
                    chip = '<span class="chip none">official is $0</span>'
                elif abs(pct) <= _TOLERANCE_PCT:
                    chip = '<span class="chip ok">within 5%</span>'
                else:
                    chip = '<span class="chip bad">check</span>'
                pct_s = "—" if pct is None else f"{pct:+.1f}%"
                out.append(f'<tr><td>{e(str(r["provider"]))}</td><td class="num">{_usd(r["computed_usd"])}</td>'
                           f'<td class="num">{_usd(r.get("official_usd"))}</td>'
                           f'<td class="num">{_usd(r.get("diff_usd"))}</td><td class="num">{pct_s}</td>'
                           f"<td>{chip}</td></tr>")
            out.append("</tbody></table></div><p class=\"note\">Negative difference: the meter sees less than the bill "
                       "(calls not metered, or models without a price). Positive: the price list is higher than "
                       "what was billed.</p></section>")

        # detail
        out.append('<section><h2>Detail <span>project × provider</span></h2><div class="scroll"><table><thead><tr>'
                   '<th>Project</th><th>Provider</th><th class="num">Calls</th><th class="num">Input tokens</th>'
                   '<th class="num">Output tokens</th><th class="num">Cost</th><th class="num">Unpriced</th>'
                   "</tr></thead><tbody>")
        for r in sorted(rows, key=lambda r: (-r["cost_usd"], r["slug"], r["provider"])):
            bad = r["unpriced_calls"] > 0
            unp = f'<span class="chip bad">{_int(r["unpriced_calls"])}</span>' if bad else "0"
            cls = ' class="warn"' if bad else ""
            out.append(f'<tr{cls}><td>{e(str(r["slug"]))}</td>'
                       f'<td>{e(str(r["provider"]))}</td><td class="num">{_int(r["calls"])}</td>'
                       f'<td class="num">{_int(r["input_tokens"])}</td><td class="num">{_int(r["output_tokens"])}</td>'
                       f'<td class="num">{_usd(r["cost_usd"])}</td><td class="num">{unp}</td></tr>')
        out.append(f'</tbody><tfoot><tr><td>Total</td><td></td><td class="num">{_int(calls)}</td>'
                   f'<td class="num">{_int(tin)}</td><td class="num">{_int(tout)}</td><td class="num">{_usd(total)}</td>'
                   f'<td class="num">{_int(n_unpriced)}</td></tr></tfoot></table></div></section>')

    when = generated_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out.append(f"<footer>Generated {e(when)} by giuggiai-meter {e(__version__)}. Costs are computed from your price list "
               "at the price valid on each call date; calls without a price count as $0. Official totals are the "
               "figures you passed from the provider consoles.</footer>")
    out += ["</main>", "</body>", "</html>", ""]
    return "\n".join(out)
