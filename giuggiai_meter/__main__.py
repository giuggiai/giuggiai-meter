"""Command line: giuggiai-meter <command>   (or: python -m giuggiai_meter <command>)

  init       [--path FILE]                                  create the price list from the bundled EXAMPLE (never overwrites)
  demo       [--out FILE]                                   fake data in a temporary database -> giuggiai-meter-demo.html
  report     --month YYYY-MM [--csv | --html FILE] [--official Provider=USD ...]   cost per project and provider
  reconcile  --month YYYY-MM --official Provider=USD ...   computed vs official monthly cost per provider
  ingest     [--host 127.0.0.1] [--port 8788]              HTTP ingest server (needs GIUGGIAI_METER_TOKEN)
  log        --slug S --provider P --model M --input N --output N    record one call by hand (tests, scripts)
"""
from __future__ import annotations

import argparse
import datetime
import os
import random
import sys
import tempfile
from pathlib import Path

from . import __version__, db_path, insert_row, log_usage
from . import prices as P
from . import report as R
from ._html import month_label


def _official(pairs: list[str]) -> dict[str, float]:
    out = {}
    for x in pairs:
        k, sep, v = x.partition("=")
        try:
            if not sep or not k.strip():
                raise ValueError
            out[k.strip()] = float(v)
        except ValueError:
            raise SystemExit(f"--official expects Provider=USD (for example Anthropic=12.40), got {x!r}") from None
    return out


def _write(path: str, text: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def cmd_init(path: str | None) -> int:
    target = Path(path or P.prices_path()).expanduser()
    if target.exists():
        print(f"Price list already exists, left untouched: {target}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(P.example_text(), encoding="utf-8")
        print(f"Created the price list: {target}")
        print("It contains EXAMPLE models and prices: replace them with the real ones from each provider's pricing page.")
    print("""
Next steps:
  1. Edit the price list above (USD per million tokens, one entry per model, with effective_from).
  2. Wrap your client once:
         import giuggiai_meter as gm
         client = gm.wrap(Anthropic(), project="my-bot")
         client = gm.wrap(OpenAI(base_url=..., api_key=...), project="support-bot", provider="DeepSeek")
  3. See the month:   giuggiai-meter report --html report.html
     Compare with the provider bills:   giuggiai-meter report --html report.html --official Anthropic=12.40
  Want a preview first?   giuggiai-meter demo""")
    print(f"\nCalls are stored in {db_path()}")
    return 0


_DEMO_PROJECTS = (  # slug, provider, model, calls, input range, output range
    ("newsletter-bot", "Anthropic", "example-large", 140, (2_000, 9_000), (400, 1_600)),
    ("support-bot", "DeepSeek", "example-chat", 520, (800, 4_000), (150, 900)),
    ("support-bot", "OpenAI", "example-small", 260, (600, 3_000), (100, 600)),
    ("research-agent", "Anthropic", "example-large", 60, (12_000, 40_000), (1_500, 5_000)),
    ("research-agent", "Google", "example-gemini", 90, (6_000, 30_000), (800, 3_000)),
    ("research-agent", "OpenAI", "example-unlisted", 18, (3_000, 9_000), (500, 2_000)),
    ("tagging-cron", "OpenAI", "example-small", 900, (300, 1_200), (20, 80)),
    ("tagging-cron", "Mistral", "example-mistral", 300, (300, 1_200), (20, 80)),
    ("local-summaries", "Ollama", "example-local", 400, (500, 3_000), (100, 500)),
)


def demo_rows(month: str, seed: int = 7) -> list[dict]:
    """Fake usage rows for `month` (deterministic). One model is deliberately missing from the price list."""
    rnd = random.Random(seed)
    y, m = (int(x) for x in month.split("-"))
    today = datetime.date.today()
    last = today.day if (today.year, today.month) == (y, m) else 28
    out = []
    for slug, prov, model, n, (i0, i1), (o0, o1) in _DEMO_PROJECTS:
        for k in range(n):
            t = f"{y:04d}-{m:02d}-{rnd.randint(1, last):02d} {rnd.randint(7, 22):02d}:{rnd.randint(0, 59):02d}:00"
            cached = rnd.randint(0, i0) if prov == "Anthropic" else 0
            out.append({"request_id": f"gm-demo-{slug}-{prov}-{k}", "log_time": t, "slug": slug, "provider": prov,
                        "model": model, "input_tokens": rnd.randint(i0, i1), "output_tokens": rnd.randint(o0, o1),
                        "cache_creation_tokens": 0, "cache_read_tokens": cached, "workspace": None,
                        "source": "demo", "created_at": t})
    return out


def cmd_demo(out_path: str, month: str) -> int:
    idx = P.load_example()
    with tempfile.TemporaryDirectory(prefix="giuggiai-meter-demo-") as td:
        db = os.path.join(td, "demo.db")
        for row in demo_rows(month):
            insert_row(row, db)
        rows = R.monthly(month, idx, db)
        missing = R.unpriced(month, idx, db)
    computed = {r["provider"]: r["computed_usd"] for r in R.reconcile(rows, {})}
    # pretend console totals: one matches, one is a little higher (unmetered calls), one has no figure
    official = {"Anthropic": round(computed.get("Anthropic", 0) * 1.02, 2), "OpenAI": round(computed.get("OpenAI", 0) * 1.18, 2)}
    html = R.to_html(rows, month, R.reconcile(rows, official), missing, title=f"Demo · LLM costs · {month_label(month)}")
    print(R.to_table(rows))
    print(f"\nDemo report (fake data, example prices): {_write(out_path, html)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="giuggiai-meter", description="Per-project LLM cost metering")
    ap.add_argument("--version", action="version", version=f"giuggiai-meter {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    this_month = datetime.date.today().strftime("%Y-%m")
    ini = sub.add_parser("init", help="create the price list from the bundled example (never overwrites)")
    ini.add_argument("--path", help="price list file (default: $GIUGGIAI_METER_PRICES or ~/.config/giuggiai-meter/prices.json)")
    d = sub.add_parser("demo", help="fake data in a temporary database and an HTML report")
    d.add_argument("--out", default="giuggiai-meter-demo.html")
    d.add_argument("--month", default=this_month)
    r = sub.add_parser("report", help="cost per project and provider")
    r.add_argument("--month", default=this_month)
    fmt = r.add_mutually_exclusive_group()
    fmt.add_argument("--csv", action="store_true")
    fmt.add_argument("--html", metavar="FILE", help="write a self-contained HTML report to FILE")
    r.add_argument("--official", nargs="+", default=[], metavar="Provider=USD", help="official totals (HTML report)")
    r.add_argument("--prices")
    c = sub.add_parser("reconcile", help="computed vs official monthly cost per provider")
    c.add_argument("--month", default=this_month)
    c.add_argument("--official", nargs="+", default=[], metavar="Provider=USD")
    c.add_argument("--prices")
    i = sub.add_parser("ingest", help="HTTP ingest server")
    i.add_argument("--host", default="127.0.0.1")
    i.add_argument("--port", type=int, default=8788)
    lg = sub.add_parser("log", help="record one call by hand")
    for k in ("slug", "provider", "model"):
        lg.add_argument(f"--{k}", required=k == "slug")
    lg.add_argument("--input", type=int, default=0)
    lg.add_argument("--output", type=int, default=0)
    a = ap.parse_args(argv)

    if a.cmd == "init":
        return cmd_init(a.path)
    if a.cmd == "demo":
        return cmd_demo(a.out, a.month)
    if a.cmd == "log":
        print(log_usage(a.slug, a.provider, a.model, input_tokens=a.input, output_tokens=a.output))
        return 0
    if a.cmd == "ingest":
        from .ingest import serve
        serve(a.host, a.port)
        return 0
    try:
        idx = P.load(a.prices)
    except FileNotFoundError as e:
        raise SystemExit(f"price list not found: {e.filename} (run `giuggiai-meter init` to create one)") from None
    rows = R.monthly(a.month, idx)
    official = _official(a.official)
    if a.cmd == "report":
        if a.html:
            rec = R.reconcile(rows, official) if official else None
            print(_write(a.html, R.to_html(rows, a.month, rec, R.unpriced(a.month, idx))))
        else:
            print(R.to_csv(rows) if a.csv else R.to_table(rows))
        return 0
    print(R.to_table(R.reconcile(rows, official)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
