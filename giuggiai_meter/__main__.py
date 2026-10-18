"""Command line: python -m giuggiai_meter <command>

  report     --month YYYY-MM [--csv]                       cost per project and provider
  reconcile  --month YYYY-MM --official Provider=USD ...   computed vs official monthly cost per provider
  ingest     [--host 127.0.0.1] [--port 8788]              HTTP ingest server (needs GIUGGIAI_METER_TOKEN)
  log        --slug S --provider P --model M --input N --output N    record one call by hand (tests, scripts)
"""
from __future__ import annotations

import argparse
import datetime
import sys

from . import log_usage
from . import prices as P
from . import report as R


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="giuggiai_meter", description="Per-project LLM cost metering")
    sub = ap.add_subparsers(dest="cmd", required=True)
    this_month = datetime.date.today().strftime("%Y-%m")
    r = sub.add_parser("report")
    r.add_argument("--month", default=this_month)
    r.add_argument("--csv", action="store_true")
    r.add_argument("--prices")
    c = sub.add_parser("reconcile")
    c.add_argument("--month", default=this_month)
    c.add_argument("--official", nargs="+", default=[], metavar="Provider=USD")
    c.add_argument("--prices")
    i = sub.add_parser("ingest")
    i.add_argument("--host", default="127.0.0.1")
    i.add_argument("--port", type=int, default=8788)
    lg = sub.add_parser("log")
    for k in ("slug", "provider", "model"):
        lg.add_argument(f"--{k}", required=k == "slug")
    lg.add_argument("--input", type=int, default=0)
    lg.add_argument("--output", type=int, default=0)
    a = ap.parse_args(argv)

    if a.cmd == "log":
        print(log_usage(a.slug, a.provider, a.model, input_tokens=a.input, output_tokens=a.output))
        return 0
    if a.cmd == "ingest":
        from .ingest import serve
        serve(a.host, a.port)
        return 0
    rows = R.monthly(a.month, P.load(a.prices))
    if a.cmd == "report":
        print(R.to_csv(rows) if a.csv else R.to_table(rows))
        return 0
    official = {}
    for x in a.official:
        k, _, v = x.partition("=")
        official[k] = float(v)
    print(R.to_table(R.reconcile(rows, official)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
