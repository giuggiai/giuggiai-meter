"""Regression tests for the 0.2.0 pre-release review (concurrency, restored databases, broken inputs)."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest
from conftest_gov import TODAY, cfg, setup_env, spend

import giuggiai_meter as gm
from giuggiai_meter import budgets as B
from giuggiai_meter.plans import billing_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    B._LAST_GOOD.clear()
    B._STALE.clear()
    B._CFG_CACHE.clear()
    monkeypatch.delenv("GIUGGIAI_METER_BUDGETS_FAIL", raising=False)
    return setup_env(tmp_path, monkeypatch)


WORKER = """
import sys
sys.path.insert(0, {root!r})
import giuggiai_meter as gm
for i in range(40):
    assert gm.log_usage("p", provider="OpenAI", model="big", input_tokens=1, request_id=f"w{{sys.argv[1]}}-{{i}}") is not None
"""


def test_concurrent_upgrade_loses_no_rows(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript(gm._DDL)  # a 0.1.x database
    c.close()
    script = tmp_path / "w.py"
    script.write_text(WORKER.format(root=ROOT))
    env = {**os.environ, "GIUGGIAI_METER_DB": str(db)}
    procs = [subprocess.Popen([sys.executable, str(script), str(k)], env=env, stderr=subprocess.PIPE) for k in range(10)]
    errs = [p.communicate(timeout=120)[1].decode() for p in procs]
    assert all(p.returncode == 0 for p in procs), errs
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM usage").fetchone()[0] == 400
    assert {"billing", "unit_id", "unit_kind"} <= {r[1] for r in c.execute("PRAGMA table_info(usage)")}
    c.close()


def test_insert_falls_back_to_v1_columns(tmp_path, monkeypatch):
    db = tmp_path / "v1.db"
    c = sqlite3.connect(db)
    c.executescript(gm._DDL)
    c.close()
    monkeypatch.setattr(gm, "_migrate", lambda c: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    gm._READY.clear()
    gm.insert_row({"request_id": "r", "log_time": "2026-10-01", "slug": "s", "created_at": "x", "unit_id": "u"}, str(db))
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM usage").fetchone()[0] == 1
    c.close()


def test_ledger_recounts_a_recreated_database(env):
    db, _ = env
    c = cfg({"id": "b", "limit": 20, "period": "total"})
    spend(10)
    assert gm.check("p", model="big", config=c, today=TODAY).spent == pytest.approx(10)
    os.remove(db)
    for f in (db + "-wal", db + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    gm._READY.clear()
    spend(30)
    spend(30)
    d = gm.check("p", model="big", config=c, today=TODAY)
    assert d.spent == pytest.approx(60) and d.action == "block"


def test_ledger_recounts_a_restored_backup(env, tmp_path):
    db, _ = env
    c = cfg({"id": "b", "limit": 100, "period": "total"})
    spend(5)
    backup = tmp_path / "bak.db"
    shutil.copy(db, backup)
    spend(7)
    assert gm.check("p", model="big", config=c, today=TODAY).spent == pytest.approx(12)
    src = sqlite3.connect(backup)
    dst = sqlite3.connect(db)
    src.backup(dst)  # restore in place (same file)
    src.close()
    dst.close()
    spend(1)
    assert gm.check("p", model="big", config=c, today=TODAY).spent == pytest.approx(6)


def test_broken_default_budgets_never_raises(tmp_path):
    (tmp_path / "budgets.json").write_text("{ not json")
    d = gm.check("p", model="big")
    assert d.allow and "unreadable" in d.reason


def test_broken_budgets_fail_closed_by_env(tmp_path, monkeypatch):
    (tmp_path / "budgets.json").write_text("{ not json")
    monkeypatch.setenv("GIUGGIAI_METER_BUDGETS_FAIL", "closed")
    assert not gm.check("p", model="big").allow


def test_budgets_broken_after_good_read_keeps_last_good(tmp_path):
    f = tmp_path / "budgets.json"
    f.write_text(json.dumps(cfg({"id": "b", "limit": 5})))
    spend(6)
    assert gm.check("p", model="big", today=TODAY).action == "block"
    f.write_text("{ broken")
    os.utime(f, (1, 1))
    d = gm.check("p", model="big", today=TODAY)
    assert d.action == "block" and "last good" in d.reason


def test_wrap_with_broken_budgets_and_env_closed(tmp_path, monkeypatch):
    from test_budgets import FakeOpenAI

    (tmp_path / "budgets.json").write_text("{ broken")
    monkeypatch.setenv("GIUGGIAI_METER_BUDGETS_FAIL", "closed")
    fake = FakeOpenAI()
    with pytest.raises(gm.BudgetExceeded):
        gm.wrap(fake, project="p", budgets=True).chat.completions.create(model="big", messages=[])
    assert fake.calls == []


def test_missing_price_list_triggers_fail_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_PRICES", str(tmp_path / "no-prices.json"))
    spend(1)
    assert not gm.check("p", model="big", config=cfg({"id": "b", "limit": 5, "fail": "closed"}), today=TODAY).allow
    d = gm.check("p", model="big", config=cfg({"id": "b", "limit": 5}), today=TODAY)
    assert d.allow and "failing open" in d.reason


def test_status_cli_without_prices(tmp_path, monkeypatch):
    from giuggiai_meter.__main__ import main

    monkeypatch.setenv("GIUGGIAI_METER_PRICES", str(tmp_path / "no-prices.json"))
    (tmp_path / "budgets.json").write_text(json.dumps(cfg({"id": "b", "limit": 5})))
    with pytest.raises(SystemExit, match="price list not found"):
        main(["status"])


@pytest.mark.parametrize("value,expected", [("API", "api"), (" free ", "free"), ("Subscription:team", "subscription:team"),
                                            ("whatever", "api"), ("subscription:", "api")])
def test_billing_values_normalised(value, expected):
    assert billing_for({"billing": value}, [{"id": "team", "match": {}}]) == expected


def test_billing_subscription_to_an_unknown_plan_is_paid_per_token(capsys):
    plans = [{"id": "team", "match": {}}]
    assert billing_for({"billing": "subscription:teem"}, plans) == "api"  # a typo is never silently free
    assert billing_for({"billing": "subscription:team"}, []) == "api"     # plan removed from the budgets file
    assert "names no plan" in capsys.readouterr().err


def test_unknown_billing_not_stored():
    gm.log_usage("p", model="big", billing="lol", request_id="b1")
    with gm.connect() as c:
        assert c.execute("SELECT billing FROM usage WHERE request_id='b1'").fetchone() == (None,)


@pytest.mark.parametrize("bad", [
    {"fx": {"USD": "abc"}, "budgets": [{"id": "x", "limit": 1}]},
    {"fx": {"USD": -1}, "budgets": [{"id": "x", "limit": 1}]},
    {"budgets": [{"id": "x", "limit": -1}]},
    {"budgets": [{"id": "x", "limit": 1, "warn_at": 2}]},
    {"budgets": [{"id": "x", "limit": 1, "warn_at": "a"}]},
    {"budgets": [{"id": "x", "limit": 5, "on_exceed": "degrade", "degrade_to": "s", "block_at": 3}]},
])
def test_more_invalid_configs(bad):
    with pytest.raises(B.BudgetError):
        B.parse(bad)


def test_check_remote_rejects_non_http(monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_URL", "file:///etc/passwd")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", "x" * 20)
    d = gm.check_remote("p", model="big")
    assert d.allow and "unreachable" in d.reason


def test_currency_without_fx_warns(capsys):
    cfg_ = B.parse({"currency": "EUR", "budgets": [{"id": "x", "limit": 1}]})
    assert cfg_["fx"] == {"EUR": 1.0} and "without fx.USD" in capsys.readouterr().err


def test_remote_unreachable_applies_local_closed_budget_in_other_currency(monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", "x" * 20)
    local = {"currency": "EUR", "budgets": [{"id": "b", "limit": 5, "fail": "closed"}]}
    assert not gm.check_remote("p", model="big", config=local).allow


def test_remote_unreachable_with_broken_local_budgets(tmp_path, monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", "x" * 20)
    (tmp_path / "budgets.json").write_text("{ broken")
    assert gm.check_remote("p", model="big").allow
    monkeypatch.setenv("GIUGGIAI_METER_BUDGETS_FAIL", "closed")
    d = gm.check_remote("p", model="big")
    assert not d.allow and "unreachable" in d.reason


def test_async_guard_runs_off_the_loop_with_decorated_methods():
    import asyncio
    import functools
    import threading
    from types import SimpleNamespace

    from giuggiai_meter import _wrap

    seen = []
    real_guard = _wrap._guard

    def spy(meter, kwargs):
        seen.append(threading.current_thread() is threading.main_thread())
        return real_guard(meter, kwargs)

    async def create(**kw):
        return {"model": kw["model"], "id": "x", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    def sdk_decorator(fn):  # like the SDKs: a sync wrapper around the coroutine function
        @functools.wraps(fn)
        def inner(*a, **k):
            return fn(*a, **k)
        return inner

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=sdk_decorator(create))),
                             base_url="https://api.openai.com/v1")
    _wrap._guard = spy
    try:
        asyncio.run(gm.wrap(client, project="p", budgets=cfg({"id": "b", "limit": 99})).chat.completions.create(
            model="big", messages=[]))
    finally:
        _wrap._guard = real_guard
    assert seen == [False]


def test_ledger_widens_for_an_older_window():
    import datetime

    spend(4, day="2026-09-10")
    c = cfg({"id": "b", "limit": 100, "period": "month"})
    assert gm.check("p", model="big", config=c, today=TODAY).spent == pytest.approx(0)          # October
    assert gm.check("p", model="big", config=c, today=datetime.date(2026, 9, 20)).spent == pytest.approx(4)


def test_broken_file_keeps_one_ledger_and_warns_once(tmp_path, capsys):
    f = tmp_path / "budgets.json"
    f.write_text(json.dumps(cfg({"id": "b", "limit": 50})))
    spend(1)
    gm.check("p", model="big", today=TODAY)
    f.write_text("{ broken")
    os.utime(f, (1, 1))
    capsys.readouterr()
    n = len(B._LEDGERS)
    for _ in range(20):
        assert "last good" in gm.check("p", model="big", today=TODAY).reason
    assert len(B._LEDGERS) == n + 1
    assert capsys.readouterr().err.count("last good version") == 1


def test_invalid_since_is_a_budget_error():
    with pytest.raises(B.BudgetError):
        B.parse({"budgets": [{"id": "x", "limit": 1, "period": "total", "since": "ieri"}]})
