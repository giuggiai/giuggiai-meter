import asyncio
import json
import os
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from conftest_gov import TODAY, cfg, setup_env, spend

import giuggiai_meter as gm
from giuggiai_meter import budgets as B
from giuggiai_meter.ingest import make_handler

TOKEN = "test-" + "only-not-a-secret"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    return setup_env(tmp_path, monkeypatch)


def chk(config, **kw):
    kw.setdefault("today", TODAY)
    return gm.check(kw.pop("project", "p"), config=config, **kw)


# ------------------------------------------------------------------ schema v2

def test_schema_version():
    assert gm.SCHEMA_VERSION == 2


def test_migrate_is_additive_and_idempotent(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(gm._DDL)
    c.execute("INSERT INTO usage(request_id, log_time, slug, created_at) VALUES('a', '2026-01-01', 's', '2026-01-01')")
    c.commit()
    c.close()
    first = gm.migrate(str(p))
    assert {"usage.billing", "usage.unit_id", "usage.unit_kind", "outcomes"} <= set(first["added"])
    assert gm.migrate(str(p))["added"] == []
    c = sqlite3.connect(p)
    assert c.execute("SELECT count(*) FROM usage").fetchone()[0] == 1
    c.close()


def test_new_columns_recorded():
    gm.log_usage("p", provider="OpenAI", model="big", billing="free", unit_id="u1", unit_kind="k", request_id="x1")
    with gm.connect() as c:
        assert c.execute("SELECT billing, unit_id, unit_kind FROM usage WHERE request_id='x1'").fetchone() == ("free", "u1", "k")


# ------------------------------------------------------------------ config

def test_no_budgets_file_allows():
    d = gm.check("p", model="big")
    assert d.allow and d.action == "allow" and d.reason == "no budget applies"


def test_budgets_file_from_env(tmp_path):
    (tmp_path / "budgets.json").write_text(json.dumps(cfg({"id": "b", "limit": 1})))
    spend(2)
    assert gm.check("p", model="big", today=TODAY).action == "block"


@pytest.mark.parametrize("bad", [
    {"budgets": [{"limit": 1}]},
    {"budgets": [{"id": "x", "limit": 1, "period": "year"}]},
    {"budgets": [{"id": "x", "limit": 1, "on_exceed": "stop"}]},
    {"budgets": [{"id": "x", "limit": 1, "on_exceed": "degrade"}]},
    {"budgets": [{"id": "x", "limit": 1, "fail": "maybe"}]},
    {"budgets": [{"id": "x"}]},
])
def test_invalid_config_rejected(bad):
    with pytest.raises(B.BudgetError):
        B.parse(bad)


def test_defaults():
    b = B.parse({"budgets": [{"id": "x", "limit": 5}]})["budgets"][0]
    assert (b["period"], b["on_exceed"], b["fail"]) == ("month", "block", "open")


# ------------------------------------------------------------------ decisions

def test_allow_under_limit():
    spend(1)
    d = chk(cfg({"id": "b", "limit": 5}), model="big")
    assert d.allow and d.action == "allow" and d.spent == pytest.approx(1) and d.remaining == pytest.approx(4)


def test_block_over_limit():
    spend(6)
    d = chk(cfg({"id": "b", "limit": 5}), model="big")
    assert not d.allow and d.action == "block" and d.budget_id == "b"


def test_estimate_pushes_over():
    spend(4)
    c = cfg({"id": "b", "limit": 5})
    assert chk(c, model="big", estimate_usd=0.5).action == "allow"
    assert chk(c, model="big", estimate_usd=2).action == "block"


def test_estimate_from_tokens():
    spend(4)
    assert chk(cfg({"id": "b", "limit": 5}), model="big", input_tokens=200_000).action == "block"  # 2 USD


def test_warn_at():
    spend(4.1)
    d = chk(cfg({"id": "b", "limit": 5, "warn_at": 0.8}), model="big")
    assert d.allow and d.action == "warn"


def test_on_exceed_warn_allows():
    spend(9)
    d = chk(cfg({"id": "b", "limit": 5, "on_exceed": "warn"}), model="big")
    assert d.allow and d.action == "warn"


def test_degrade_switches_model():
    spend(6)
    d = chk(cfg({"id": "b", "limit": 5, "on_exceed": "degrade", "degrade_to": "small"}), model="big")
    assert d.allow and d.action == "degrade" and d.model == "small"


def test_degrade_then_block_at():
    spend(11)
    d = chk(cfg({"id": "b", "limit": 5, "on_exceed": "degrade", "degrade_to": "small", "block_at": 10}), model="big")
    assert not d.allow and d.action == "block"


def test_degrade_already_on_cheap_model():
    spend(6)
    d = chk(cfg({"id": "b", "limit": 5, "on_exceed": "degrade", "degrade_to": "small"}), model="small")
    assert d.allow and d.action == "warn" and d.model == "small"


def test_most_severe_budget_wins():
    spend(6, source="agents/a")
    c = cfg({"id": "soft", "limit": 1, "on_exceed": "warn"}, {"id": "hard", "limit": 5, "match": {"source": "agents/*"}})
    d = chk(c, model="big", source="agents/a")
    assert d.action == "block" and d.budget_id == "hard"


def test_wildcard_match_on_source():
    spend(6, source="agents/a")
    spend(6, source="jobs/x")
    c = cfg({"id": "b", "limit": 10, "match": {"source": "agents/*"}})
    assert chk(c, model="big", source="agents/z").action == "allow"   # only the 6 USD of agents/* count
    assert chk(c, model="big", source="jobs/x").reason == "no budget applies"


def test_match_on_project_provider_model():
    spend(6, project="bot", model="big")
    spend(6, project="bot", model="claude-x", provider="Anthropic")
    c = cfg({"id": "b", "limit": 7, "match": {"project": "bot", "provider": "OpenAI", "model": "b*"}})
    assert chk(c, project="bot", model="big", provider="OpenAI").action == "allow"


def test_period_day_week_month():
    spend(3, day="2026-10-15")   # today (Thursday)
    spend(3, day="2026-10-12")   # Monday this week
    spend(3, day="2026-10-02")   # this month
    spend(3, day="2026-09-30")   # last month
    for period, expected in (("day", 3), ("week", 6), ("month", 9), ("total", 12)):
        assert chk(cfg({"id": period, "limit": 100, "period": period}), model="big").spent == pytest.approx(expected)


def test_total_since():
    spend(5, day="2026-09-01")
    spend(2, day="2026-10-02")
    d = chk(cfg({"id": "b", "limit": 100, "period": "total", "since": "2026-10-01"}), model="big")
    assert d.spent == pytest.approx(2)


def test_fx_currency():
    spend(10)
    d = chk(cfg({"id": "b", "limit": 100}, currency="EUR", fx={"USD": 0.9}), model="big")
    assert d.currency == "EUR" and d.spent == pytest.approx(9)


def test_free_and_subscription_rows_count_zero():
    spend(10, billing="free")
    spend(10, source="cli/x")
    c = cfg({"id": "b", "limit": 1}, plans=[{"id": "team", "monthly_fee": 20, "match": {"source": "cli/*"}}])
    assert chk(c, model="big").spent == pytest.approx(0)


def test_incremental_ledger_sees_new_rows():
    c = cfg({"id": "b", "limit": 5})
    assert chk(c, model="big").action == "allow"
    spend(6)
    assert chk(c, model="big").action == "block"


def test_missing_db_means_nothing_recorded_yet_fail_open(tmp_path):
    d = chk(cfg({"id": "b", "limit": 5}), model="big", db=str(tmp_path / "nope.db"))
    assert d.allow and d.spent == 0


def test_missing_db_does_not_lock_a_fail_closed_budget_forever(tmp_path):
    # a fresh install has no database until the first call is logged: fail=closed must not block that first call
    d = chk(cfg({"id": "b", "limit": 5, "fail": "closed"}), model="big", db=str(tmp_path / "nope.db"))
    assert d.allow and d.action == "allow" and d.spent == 0


def test_fail_closed_when_db_broken(tmp_path):
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"not a database at all" * 100)
    d = chk(cfg({"id": "b", "limit": 5, "fail": "closed"}), model="big", db=str(bad))
    assert not d.allow


def test_check_never_writes(env):
    db, _ = env
    spend(1)
    before = os.stat(db).st_mtime_ns
    chk(cfg({"id": "b", "limit": 5}), model="big")
    assert os.stat(db).st_mtime_ns == before


def test_decision_to_dict():
    d = chk(cfg({"id": "b", "limit": 5}), model="big")
    assert set(d.to_dict()) == {"allow", "action", "model", "budget_id", "spent", "limit", "remaining", "currency", "reason",
                                "fail"}


# ------------------------------------------------------------------ wrap

class FakeOpenAI:
    """Minimal stand-in for an OpenAI-style client: counts the calls that reach it."""

    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.base_url = "https://api.openai.com/v1"

    def _create(self, **kw):
        self.calls.append(kw)
        return {"model": kw["model"], "id": f"r{len(self.calls)}",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_wrap_block_raises_before_call():
    spend(6)
    fake = FakeOpenAI()
    client = gm.wrap(fake, project="p", budgets=cfg({"id": "b", "limit": 5}))
    with pytest.raises(gm.BudgetExceeded) as e:
        client.chat.completions.create(model="big", messages=[{"role": "user", "content": "hi"}])
    assert fake.calls == [] and e.value.decision.budget_id == "b"


def test_wrap_degrade_rewrites_model():
    spend(6)
    fake = FakeOpenAI()
    c = cfg({"id": "b", "limit": 5, "on_exceed": "degrade", "degrade_to": "small"})
    gm.wrap(fake, project="p", budgets=c).chat.completions.create(model="big", messages=[])
    assert fake.calls[0]["model"] == "small"


def test_wrap_without_budgets_unchanged():
    spend(600)
    fake = FakeOpenAI()
    gm.wrap(fake, project="p").chat.completions.create(model="big", messages=[])
    assert len(fake.calls) == 1


def test_wrap_records_source():
    fake = FakeOpenAI()
    gm.wrap(fake, project="p", source="agents/x").chat.completions.create(model="big", messages=[])
    with gm.connect() as c:
        assert c.execute("SELECT source FROM usage WHERE model='big' AND source IS NOT NULL").fetchone() == ("agents/x",)


def test_wrap_broken_budget_lets_call_through():
    fake = FakeOpenAI()
    gm.wrap(fake, project="p", budgets={"budgets": [{"limit": 1}]}).chat.completions.create(model="big", messages=[])
    assert len(fake.calls) == 1


# ------------------------------------------------------------------ remote check

@pytest.fixture
def server(env, tmp_path):
    db, prices = env
    budgets = tmp_path / "server-budgets.json"
    budgets.write_text(json.dumps(cfg({"id": "b", "limit": 5})))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(TOKEN, db, str(budgets), prices))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", str(budgets)
    srv.shutdown()


def test_remote_check_same_as_local(server, monkeypatch):
    url, budgets = server
    spend(3)
    monkeypatch.setenv("GIUGGIAI_METER_URL", url + "/ingest")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", TOKEN)
    for est in (0, 1, 2.5, 3):
        remote = gm.check_remote("p", model="big", estimate_usd=est)
        local = gm.check("p", model="big", estimate_usd=est, config=budgets)
        assert (remote.action, remote.spent, remote.budget_id) == (local.action, local.spent, local.budget_id)


def test_remote_check_needs_token(server):
    import urllib.error
    import urllib.request

    url, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url + "/check?project=p")
    assert e.value.code == 401


def test_remote_unreachable_fail_modes(monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", TOKEN)
    assert gm.check_remote("p", model="big").allow
    closed = cfg({"id": "b", "limit": 5, "fail": "closed"})
    assert not gm.check_remote("p", model="big", config=closed).allow


def test_async_wrap_block():
    spend(6)

    class AsyncFake(FakeOpenAI):
        async def _acreate(self, **kw):
            return self._create(**kw)

    fake = AsyncFake()
    fake.chat = SimpleNamespace(completions=SimpleNamespace(create=fake._acreate))
    client = gm.wrap(fake, project="p", budgets=cfg({"id": "b", "limit": 5}))
    with pytest.raises(gm.BudgetExceeded):
        asyncio.run(client.chat.completions.create(model="big", messages=[]))
    assert fake.calls == []
