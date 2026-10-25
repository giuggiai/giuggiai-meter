"""Budgets: decide BEFORE a model call whether it may run, run on a cheaper model, or must stop.

A budgets file (JSON) describes limits on spend, matched on project / source / provider / model:

    {
      "currency": "EUR",
      "fx": {"USD": 0.92},                       # 1 USD = 0.92 EUR (prices are in USD)
      "budgets": [
        {"id": "agents", "match": {"source": "agents/*"}, "limit": 50, "period": "month",
         "warn_at": 0.8, "on_exceed": "degrade", "degrade_to": "small-model", "block_at": 60},
        {"id": "prepaid-credit", "match": {"project": "*"}, "limit": 190, "period": "total", "since": "2026-10-01",
         "on_exceed": "block", "fail": "closed"}
      ],
      "plans": [{"id": "team-plan", "monthly_fee": 200, "currency": "USD", "match": {"source": "cli/*"}}]
    }

    import giuggiai_meter as gm
    d = gm.check("support-bot", model="large-model", source="agents/reply", estimate_usd=0.02)
    if not d.allow: ...                          # or let gm.wrap(client, ..., budgets=True) do it for you

Rules
- `match` keys are shell-style patterns (`*`, `?`, `[..]`); a missing key matches everything. Several budgets may
  match one call: the most severe decision wins (block > degrade > warn > allow).
- `period`: day | week (from Monday) | month | total (from `since`, a YYYY-MM-DD date; default: all time).
- Spend counts what you pay per token: calls covered by a plan (see plans.py) or marked billing="free" count 0.
- `on_exceed` at `limit`: "block" (deny), "degrade" (allow on `degrade_to`; deny from `block_at`, if set) or
  "warn" (allow, flagged). `warn_at` (0-1) flags the call earlier. A call is judged with its estimate added.
- `fail`: what to do when the spend cannot be read (missing or broken database): "open" (default, allow) or "closed".
- check() never writes to the database. Running totals are kept in the memory of the process and refreshed from
  the rows added since the previous check (by rowid), so a check costs one small indexed query.
- check() and the call are not atomic: with N calls in flight at once, a budget can be overshot by up to N estimates.
"""
from __future__ import annotations

import dataclasses
import datetime
import fnmatch
import json
import os
import sqlite3
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from . import prices as P

_SEVERITY = {"allow": 0, "warn": 1, "degrade": 2, "block": 3}
_MATCH_KEYS = ("project", "source", "provider", "model")


class BudgetError(ValueError):
    """The budgets file is malformed."""


@dataclasses.dataclass
class Decision:
    """Outcome of a check. `model` is the model to use (changed when the decision is "degrade")."""

    allow: bool
    action: str
    model: str | None
    budget_id: str | None = None
    spent: float = 0.0
    limit: float | None = None
    remaining: float | None = None
    currency: str = "USD"
    reason: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class BudgetExceeded(Exception):
    """Raised by a wrapped client (gm.wrap(..., budgets=...)) when a budget blocks the call. `.decision` says why."""

    def __init__(self, decision: Decision):
        super().__init__(decision.reason or f"budget {decision.budget_id} exceeded")
        self.decision = decision


# ---------------------------------------------------------------- configuration

def budgets_path() -> str:
    return os.environ.get("GIUGGIAI_METER_BUDGETS") or str(Path.home() / ".config/giuggiai-meter/budgets.json")


def parse(data: dict) -> dict:
    """Validate a decoded budgets file and fill the defaults."""
    if not isinstance(data, dict):
        raise BudgetError("the budgets file must be a JSON object")
    cur = str(data.get("currency") or "USD").upper()
    try:
        fx = {str(k).upper(): float(v) for k, v in (data.get("fx") or {}).items()}
    except (TypeError, ValueError):
        raise BudgetError("fx values must be numbers") from None
    if any(v <= 0 for v in fx.values()):
        raise BudgetError("fx values must be positive")
    fx.setdefault(cur, 1.0)
    if cur != "USD" and "USD" not in fx and (data.get("budgets") or data.get("plans")):
        from . import _warn

        _warn(f"[giuggiai_meter] budgets in {cur} without fx.USD: USD prices are counted 1:1 as {cur}; add \"fx\": "
              f"{{\"USD\": <{cur} per USD>}}")
    out = []
    for b in data.get("budgets") or []:
        if not isinstance(b, dict) or not b.get("id"):
            raise BudgetError("every budget needs an id")
        period = b.get("period", "month")
        if period not in ("day", "week", "month", "total"):
            raise BudgetError(f"{b['id']}: period must be day, week, month or total")
        on = b.get("on_exceed", "block")
        if on not in ("block", "degrade", "warn"):
            raise BudgetError(f"{b['id']}: on_exceed must be block, degrade or warn")
        if on == "degrade" and not b.get("degrade_to"):
            raise BudgetError(f"{b['id']}: on_exceed=degrade needs degrade_to")
        fail = b.get("fail", "open")
        if fail not in ("open", "closed"):
            raise BudgetError(f"{b['id']}: fail must be open or closed")
        try:
            limit = float(b["limit"])
            warn_at = None if b.get("warn_at") is None else float(b["warn_at"])
            block_at = None if b.get("block_at") is None else float(b["block_at"])
        except (KeyError, TypeError, ValueError):
            raise BudgetError(f"{b['id']}: limit (a number) required; warn_at and block_at must be numbers") from None
        if limit < 0:
            raise BudgetError(f"{b['id']}: limit must be >= 0")
        if warn_at is not None and not (0 < warn_at <= 1):
            raise BudgetError(f"{b['id']}: warn_at is a fraction of the limit, between 0 and 1")
        if block_at is not None and block_at < limit:
            raise BudgetError(f"{b['id']}: block_at must be >= limit")
        since = b.get("since")
        if since is not None:
            datetime.date.fromisoformat(str(since)[:10])
        out.append({"id": str(b["id"]), "match": dict(b.get("match") or {}), "limit": limit, "period": period,
                    "since": None if since is None else str(since)[:10], "warn_at": warn_at,
                    "on_exceed": on, "degrade_to": b.get("degrade_to"), "block_at": block_at, "fail": fail})
    plans = []
    for p in data.get("plans") or []:
        if not isinstance(p, dict) or not p.get("id"):
            raise BudgetError("every plan needs an id")
        plans.append({"id": str(p["id"]), "monthly_fee": float(p.get("monthly_fee") or 0),
                      "currency": str(p.get("currency") or cur).upper(), "match": dict(p.get("match") or {})})
    return {"currency": cur, "fx": fx, "budgets": out, "plans": plans}


_CFG_CACHE: dict[str, tuple] = {}
_LAST_GOOD: dict[str, dict] = {}


def load(path: str | None = None) -> dict:
    """Load and validate the budgets file; a missing file means no budgets (every call allowed).
    Raises BudgetError (or OSError / ValueError) when the file exists but cannot be read or is malformed."""
    p = path or budgets_path()
    try:
        mtime = os.stat(p).st_mtime_ns
    except OSError:
        return parse({})
    hit = _CFG_CACHE.get(p)
    if hit and hit[0] == mtime:
        return hit[1]
    cfg = parse(json.loads(Path(p).read_text(encoding="utf-8")))
    _CFG_CACHE[p] = (mtime, cfg)
    _LAST_GOOD[p] = cfg
    return cfg



_PARSED: dict[str, dict] = {}


def _config(config) -> dict:
    """A validated config from None (default file), a path or a dict. Equal dicts share one parsed object, so the
    in-memory ledger survives across checks even when the caller builds the dict each time."""
    if config is None or isinstance(config, (str, os.PathLike)):
        return load(None if config is None else str(config))
    key = json.dumps(config, sort_keys=True, default=str)
    hit = _PARSED.get(key)
    if hit is None:
        if len(_PARSED) > 64:
            _PARSED.clear()
        hit = _PARSED[key] = parse(config)
    return hit


_PRICE_CACHE: dict[str, tuple] = {}


def _prices(prices) -> dict:
    """The price index. Raises FileNotFoundError when there is no price list: spend cannot be priced, so the
    budgets' fail mode decides (a missing list must not read as zero spend)."""
    if isinstance(prices, dict):
        if not prices:
            raise FileNotFoundError("empty price list")
        return prices
    p = str(prices) if prices else P.prices_path()
    try:
        mtime = os.stat(p).st_mtime_ns
    except OSError:
        raise FileNotFoundError(p) from None
    hit = _PRICE_CACHE.get(p)
    if hit and hit[0] == mtime:
        return hit[1]
    idx = P.load(p)
    _PRICE_CACHE[p] = (mtime, idx)
    return idx


def matches(match: dict, row: dict) -> bool:
    """True when every pattern in `match` matches the row ({project, source, provider, model})."""
    for k in _MATCH_KEYS:
        pat = match.get(k)
        if pat is None or pat == "*":
            continue
        val = row.get(k)
        if val is None or not fnmatch.fnmatchcase(str(val), str(pat)):
            return False
    return True


def to_currency(usd: float, cfg: dict) -> float:
    return usd * cfg["fx"].get("USD", 1.0) if cfg["currency"] != "USD" else usd


# ---------------------------------------------------------------- spend ledger (in memory, incremental)

def window_start(period: str, since: str | None, today: datetime.date) -> str:
    """First day (YYYY-MM-DD) counted by a budget on `today`."""
    if period == "day":
        return today.isoformat()
    if period == "week":
        return (today - datetime.timedelta(days=today.weekday())).isoformat()
    if period == "month":
        return today.replace(day=1).isoformat()
    return since or "0000-00-00"


class Ledger:
    """Cash spend per budget and per day for one database, budgets file and price list, updated by rowid."""

    def __init__(self, db: str, cfg: dict, idx: dict, min_day: str = "0000-00-00"):
        self.db, self.cfg, self.idx = db, cfg, idx
        self.min_day = min_day  # rows dated before this never count in any window: they are skipped
        self._reset()

    def _reset(self) -> None:
        self.last_rowid = 0
        self.last_request_id = None
        self.file_id = None
        self.days: dict[str, dict[str, float]] = {b["id"]: {} for b in self.cfg["budgets"]}
        self.unpriced: dict[str, int] = {b["id"]: 0 for b in self.cfg["budgets"]}
        if not hasattr(self, "lock"):
            self.lock = threading.Lock()

    def refresh(self) -> None:
        from .plans import billing_for

        with self.lock:
            if not os.path.exists(self.db):
                raise FileNotFoundError(self.db)
            c = sqlite3.connect(f"file:{urllib.parse.quote(self.db)}?mode=ro", uri=True, timeout=5)
            try:
                cols = {r[1] for r in c.execute("PRAGMA table_info(usage)")}
                if not cols:
                    raise sqlite3.OperationalError("no usage table")
                st = os.stat(self.db)
                fid = (st.st_dev, st.st_ino)
                if self.last_rowid:  # same file and same last row? otherwise it was recreated or restored: recount
                    r = c.execute("SELECT request_id FROM usage WHERE rowid = ?", (self.last_rowid,)).fetchone()
                    if fid != self.file_id or r is None or r[0] != self.last_request_id:
                        self._reset()
                self.file_id = fid
                billing = "billing" if "billing" in cols else "NULL"
                top = c.execute("SELECT rowid, request_id FROM usage ORDER BY rowid DESC LIMIT 1").fetchone()
                rows = c.execute(
                    f"SELECT rowid, log_time, slug, source, provider, model, input_tokens, output_tokens, "
                    f"cache_creation_tokens, cache_read_tokens, {billing}, request_id FROM usage "
                    f"WHERE rowid > ? AND rowid <= ? AND log_time >= ? ORDER BY rowid",
                    (self.last_rowid, top[0] if top else 0, self.min_day)).fetchall()
            finally:
                c.close()
            budgets, plans = self.cfg["budgets"], self.cfg["plans"]
            if top:
                self.last_rowid, self.last_request_id = top
            for _rid, t, slug, src, prov, model, it, ot, cw, cr, bill, _req in rows:
                row = {"project": slug, "source": src, "provider": prov, "model": model, "billing": bill}
                hit = [b for b in budgets if matches(b["match"], row)]
                if not hit:
                    continue
                day = (t or "")[:10]
                if billing_for(row, plans) != "api":
                    amount = 0.0
                else:
                    price = P.pick(self.idx, model, day, prov)
                    amount = to_currency(P.cost(price, it or 0, ot or 0, cw or 0, cr or 0), self.cfg)
                    if not price:
                        for b in hit:
                            self.unpriced[b["id"]] += 1
                for b in hit:
                    d = self.days[b["id"]]
                    d[day] = d.get(day, 0.0) + amount

    def spent(self, budget: dict, today: datetime.date) -> float:
        start = window_start(budget["period"], budget["since"], today)
        end = today.isoformat()
        return round(sum(v for k, v in self.days[budget["id"]].items() if start <= k <= end), 6)


_LEDGERS: dict[tuple, Ledger] = {}
_LEDGERS_LOCK = threading.Lock()


def horizon(cfg: dict, today: datetime.date) -> str:
    """First day any budget (or the 7-day pace of the forecast) can look at on `today`."""
    starts = [window_start(b["period"], b["since"], today) for b in cfg["budgets"]]
    starts.append((today - datetime.timedelta(days=7)).isoformat())
    return min(starts)


def ledger(db: str | None, cfg: dict, idx: dict, today: datetime.date | None = None) -> Ledger:
    """The (cached) ledger for this database, budgets and prices, refreshed with the rows added since last time.
    The first refresh reads only rows dated inside the windows of `today` (indexed on log_time is not needed:
    rows are read once, by rowid, and older ones are skipped)."""
    from . import db_path

    path = os.path.abspath(db or db_path())
    first = horizon(cfg, today or datetime.date.today())
    key = (path, id(cfg), id(idx))
    with _LEDGERS_LOCK:
        led = _LEDGERS.get(key)
        if led is None:
            if len(_LEDGERS) > 32:
                _LEDGERS.clear()
            led = _LEDGERS[key] = Ledger(path, cfg, idx, first)
        elif first < led.min_day:  # asked about an older window than what was read: read further back once
            with led.lock:
                led.min_day = first
                led._reset()
    led.refresh()
    return led


# ---------------------------------------------------------------- the check

def _estimate(estimate_usd, input_tokens, output_tokens, model, provider, idx, cfg, today) -> float:
    usd = float(estimate_usd or 0)
    if not usd and (input_tokens or output_tokens) and model:
        usd = P.cost(P.pick(idx, model, today.isoformat(), provider), int(input_tokens or 0), int(output_tokens or 0))
    return to_currency(usd, cfg)


def _judge(b: dict, spent: float, est: float, model: str | None, cur: str) -> Decision:
    lim, after = b["limit"], spent + est
    base = {"budget_id": b["id"], "spent": round(spent, 6), "limit": lim, "remaining": round(lim - spent, 6), "currency": cur}
    if after > lim or spent >= lim:
        on = b["on_exceed"]
        if on == "block":
            return Decision(False, "block", model, reason=f"budget {b['id']}: {after:.4f} of {lim:g} {cur}", **base)
        if on == "degrade":
            if b["block_at"] is not None and after > b["block_at"]:
                return Decision(False, "block", model,
                                reason=f"budget {b['id']}: {after:.4f} over block_at {b['block_at']:g} {cur}", **base)
            if model == b["degrade_to"]:
                return Decision(True, "warn", model, reason=f"budget {b['id']}: over limit, already on {model}", **base)
            return Decision(True, "degrade", b["degrade_to"],
                            reason=f"budget {b['id']}: over {lim:g} {cur}, switched to {b['degrade_to']}", **base)
        return Decision(True, "warn", model, reason=f"budget {b['id']}: over {lim:g} {cur}", **base)
    if b["warn_at"] is not None and after >= float(b["warn_at"]) * lim:
        return Decision(True, "warn", model, reason=f"budget {b['id']}: {after:.4f} of {lim:g} {cur}", **base)
    return Decision(True, "allow", model, **base)


def check(project, model=None, provider=None, source=None, estimate_usd=0.0, input_tokens=0, output_tokens=0,
          db=None, config=None, prices=None, today: datetime.date | None = None) -> Decision:
    """Decide whether a call may run. Never raises: a missing or unreadable database or price list applies the
    budgets' `fail` mode; an unreadable budgets file applies its last good version, else $GIUGGIAI_METER_BUDGETS_FAIL."""
    cfg = _local_config(config, model)
    if isinstance(cfg, Decision):
        return cfg
    stale = cfg.get("_stale", "")
    cur = cfg["currency"]
    from .providers import normalize

    row = {"project": project, "source": source, "provider": normalize(provider) if provider else provider, "model": model}
    hit = [b for b in cfg["budgets"] if matches(b["match"], row)]
    if not hit:
        return Decision(True, "allow", model, currency=cur, reason="no budget applies")
    today = today or datetime.date.today()
    try:
        idx = _prices(prices)
        led = ledger(db, cfg, idx, today)
        est = _estimate(estimate_usd, input_tokens, output_tokens, model, row["provider"], idx, cfg, today)
        decisions = [_judge(b, led.spent(b, today), est, model, cur) for b in hit]
    except Exception as e:  # noqa: BLE001 - unreadable spend: apply the fail mode
        return fail_decision(hit, model, cur, f"spend unavailable ({type(e).__name__}){stale}")
    d = max(decisions, key=lambda d: (_SEVERITY[d.action], -(d.remaining if d.remaining is not None else 0)))
    if stale:
        d.reason = (d.reason or d.action) + stale
    return d


def _local_config(config, model):
    """The budgets to apply, or a Decision when they cannot be read. A file that breaks after a good read keeps its
    last good version (marked with _stale); with none, $GIUGGIAI_METER_BUDGETS_FAIL decides. Never raises."""
    try:
        return _config(config)
    except Exception as e:  # noqa: BLE001 - malformed or unreadable budgets: never raise into the caller
        key = budgets_path() if config is None else str(config) if isinstance(config, (str, os.PathLike)) else None
        if key not in _LAST_GOOD:
            return _unreadable_decision(model, e)
        from . import _warn

        _warn(f"[giuggiai_meter] budgets unreadable, using the last good version: {type(e).__name__}: {str(e)[:120]}")
        return {**_LAST_GOOD[key], "_stale": f" (budgets file unreadable, last good version applied: {type(e).__name__})"}


def _unreadable_decision(model, err: Exception) -> Decision:
    """The budgets cannot be read and this process never read a good version of them:
    $GIUGGIAI_METER_BUDGETS_FAIL decides, "open" (default, allow) or "closed" (block every call)."""
    why = f"budgets unreadable ({type(err).__name__}: {str(err)[:120]})"
    from . import _warn

    _warn(f"[giuggiai_meter] {why}")
    if os.environ.get("GIUGGIAI_METER_BUDGETS_FAIL", "open").lower() == "closed":
        return Decision(False, "block", model, reason=f"{why}; GIUGGIAI_METER_BUDGETS_FAIL=closed")
    return Decision(True, "allow", model, reason=f"{why}; failing open")


def fail_decision(hit: list[dict], model, cur: str, why: str) -> Decision:
    """Decision when the spend cannot be read: deny if any matching budget is fail=closed, else allow."""
    closed = [b for b in hit if b["fail"] == "closed"]
    if closed:
        return Decision(False, "block", model, budget_id=closed[0]["id"], limit=closed[0]["limit"], currency=cur,
                        reason=f"{why}; budget {closed[0]['id']} fails closed")
    return Decision(True, "allow", model, budget_id=hit[0]["id"], limit=hit[0]["limit"], currency=cur,
                    reason=f"{why}; failing open")


# ---------------------------------------------------------------- remote check (containers, other hosts)

def _base_url() -> str | None:
    url = os.environ.get("GIUGGIAI_METER_URL")
    if not url:
        return None
    url = url.rstrip("/")
    return url.removesuffix("/ingest")


def check_remote(project, model=None, provider=None, source=None, estimate_usd=0.0, config=None,
                 timeout: float = 2.0) -> Decision:
    """Ask the ingest server ($GIUGGIAI_METER_URL, token $GIUGGIAI_METER_TOKEN) for a decision.

    When the server cannot be reached: with a local budgets file, its `fail` modes decide; without one, allow."""
    base, token = _base_url(), os.environ.get("GIUGGIAI_METER_TOKEN", "")
    q = {"project": project, "model": model, "provider": provider, "source": source, "estimate_usd": estimate_usd}
    try:
        if not base:
            raise RuntimeError("GIUGGIAI_METER_URL not set")
        if not base.lower().startswith(("http://", "https://")):
            raise RuntimeError("GIUGGIAI_METER_URL must be http(s)")
        from . import _OPENER, _warn

        host = (urllib.parse.urlsplit(base).hostname or "").lower()
        if base.lower().startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
            _warn("[giuggiai_meter] warning: the meter token is sent over plain http to a remote host; use https")

        url = f"{base}/check?" + urllib.parse.urlencode({k: v for k, v in q.items() if v not in (None, "")})
        req = urllib.request.Request(url, headers={"X-Meter-Token": token})
        with _OPENER.open(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return Decision(**{f.name: d.get(f.name) for f in dataclasses.fields(Decision) if f.name in d})
    except Exception as e:  # noqa: BLE001 - unreachable meter: fail mode
        why = f"meter unreachable ({type(e).__name__})"
        cfg = _local_config(config, model)
        if isinstance(cfg, Decision):  # local budgets unreadable: same rule as check()
            cfg.reason = f"{why}; {cfg.reason}"
            return cfg
        from .providers import normalize

        row = {"project": project, "source": source, "provider": normalize(provider) if provider else provider,
               "model": model}
        hit = [b for b in cfg["budgets"] if matches(b["match"], row)]
        if hit:
            return fail_decision(hit, model, cfg["currency"], why)
        return Decision(True, "allow", model, reason=why)
