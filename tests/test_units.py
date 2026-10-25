import asyncio
import json
import threading

import pytest
from conftest_gov import PRICES, TODAY, cfg, setup_env, spend

import giuggiai_meter as gm
from giuggiai_meter import forecast as F
from giuggiai_meter import prices as P
from giuggiai_meter import report as R
from giuggiai_meter.__main__ import main
from giuggiai_meter.advise import advise

IDX = P.parse(PRICES)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    return setup_env(tmp_path, monkeypatch)


def units_of_last_rows(n):
    with gm.connect() as c:
        return [r[0] for r in c.execute("SELECT unit_id FROM usage ORDER BY rowid DESC LIMIT ?", (n,))]


# ------------------------------------------------------------------ unit context

def test_unit_block_tags_calls():
    with gm.unit("t-1", kind="reply"):
        gm.log_usage("p", provider="OpenAI", model="big", input_tokens=1)
    with gm.connect() as c:
        assert c.execute("SELECT unit_id, unit_kind FROM usage").fetchone() == ("t-1", "reply")


def test_unit_nesting_innermost_wins():
    with gm.unit("outer"):
        with gm.unit("inner"):
            gm.log_usage("p", model="big")
        gm.log_usage("p", model="big")
    assert units_of_last_rows(2) == ["outer", "inner"]


def test_unit_env_default(monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_UNIT", "job-7")
    gm.log_usage("p", model="big")
    assert units_of_last_rows(1) == ["job-7"]


def test_no_unit_outside_block():
    gm.log_usage("p", model="big")
    assert units_of_last_rows(1) == [None]


def test_unit_follows_asyncio():
    async def work():
        await asyncio.sleep(0)
        await asyncio.to_thread(gm.log_usage, "p", model="big")

    async def main_():
        with gm.unit("async-1"):
            await asyncio.gather(work())

    asyncio.run(main_())
    assert units_of_last_rows(1) == ["async-1"]


def test_unit_follows_bound_thread():
    with gm.unit("thread-1"):
        t = threading.Thread(target=gm.bind(lambda: gm.log_usage("p", model="big")))
    t.start()
    t.join()
    assert units_of_last_rows(1) == ["thread-1"]


# ------------------------------------------------------------------ cost per unit

def test_cost_per_accepted_unit():
    for i in range(4):
        spend(2, unit=f"u{i}", kind="reply")
    gm.outcome("u0", True)
    gm.outcome("u1", True)
    gm.outcome("u2", False)
    (row,) = R.units(IDX, kind="reply")
    assert row["units"] == 4 and row["with_outcome"] == 3 and row["accepted"] == 2
    assert row["cost"] == pytest.approx(8, abs=1e-4)
    assert row["cost_per_unit"] == pytest.approx(2, abs=1e-4)
    assert row["cost_per_accepted"] == pytest.approx(4, abs=1e-4)
    assert row["acceptance_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_unit_attributed_to_costliest_model():
    spend(5, unit="u", kind="k", model="big")
    spend(1, unit="u", kind="k", model="small")
    (row,) = R.units(IDX, kind="k")
    assert row["model"] == "big" and row["cost"] == pytest.approx(6, abs=1e-4)


def test_outcome_latest_wins_and_kind_from_outcome():
    spend(1, unit="u")
    gm.outcome("u", False, kind="k")
    gm.outcome("u", True, kind="k")
    (row,) = R.units(IDX, kind="k")
    assert row["accepted"] == 1


def test_outcome_never_raises(tmp_path):
    assert gm.outcome("u", True, db=str(tmp_path / "missing-dir" / "x" / "\0bad")) is False


def _population(model, n, accepted, cost_each, kind="reply"):
    for i in range(n):
        uid = f"{model}-{kind}-{i}"
        spend(cost_each, unit=uid, kind=kind, model=model)
        gm.outcome(uid, i < accepted)


def test_advise_recommends_cheaper_model():
    _population("big", 20, 18, 1.0)      # 90 %
    _population("small", 20, 17, 0.1)    # 85 %: within 5 points, 10x cheaper
    res = advise("reply", IDX, current="big")
    assert res["recommend"] == "small" and res["current"] == "big"


def test_advise_needs_min_n():
    _population("big", 20, 18, 1.0)
    _population("small", 19, 19, 0.1)
    assert advise("reply", IDX, current="big")["recommend"] is None


def test_advise_rejects_quality_drop():
    _population("big", 20, 18, 1.0)      # 90 %
    _population("small", 20, 16, 0.1)    # 80 %: drop of 10 points
    assert advise("reply", IDX, current="big")["recommend"] is None


def test_advise_six_point_drop_rejected():
    _population("big", 50, 45, 1.0)      # 90 %
    _population("small", 50, 42, 0.1)    # 84 %: drop of 6 points
    assert advise("reply", IDX, current="big")["recommend"] is None


def test_advise_no_data():
    assert advise("nothing", IDX)["recommend"] is None


def test_advise_cli(capsys):
    _population("big", 20, 18, 1.0)
    _population("small", 20, 18, 0.1)
    assert main(["advise", "--kind", "reply", "--current", "big", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["recommend"] == "small"


# ------------------------------------------------------------------ forecast

def test_forecast_exhaust_date():
    for d in range(9, 16):                         # 7 days x 2 USD = 2 USD/day
        spend(2, day=f"2026-10-{d:02d}")
    (row,) = F.status(cfg({"id": "b", "limit": 30, "period": "total"}), IDX, today=TODAY)
    assert row["spent"] == pytest.approx(14) and row["burn_per_day"] == pytest.approx(2)
    assert row["exhaust_date"] == "2026-10-23" and row["state"] == "ok"   # 16 left / 2 per day = 8 days


def test_forecast_month_projection_and_warn():
    for d in range(9, 16):
        spend(2, day=f"2026-10-{d:02d}")
    (row,) = F.status(cfg({"id": "b", "limit": 40, "period": "month"}), IDX, today=TODAY)
    assert row["projected_period_end"] == pytest.approx(14 + 2 * 16)   # 16 days left in October
    assert row["state"] == "warn"                                       # runs out on 2026-10-28 < 31


def test_forecast_over_and_exit_codes():
    spend(50)
    rows = F.status(cfg({"id": "b", "limit": 40}), IDX, today=TODAY)
    assert rows[0]["state"] == "over" and rows[0]["exhaust_date"] == TODAY.isoformat()
    assert F.exit_code(rows) == 2
    assert F.exit_code([{"state": "warn"}, {"state": "ok"}]) == 1 and F.exit_code([{"state": "ok"}]) == 0


def test_forecast_no_spend():
    (row,) = F.status(cfg({"id": "b", "limit": 40}), IDX, today=TODAY)
    assert row["exhaust_date"] is None and row["state"] == "ok"


def test_status_cli_json_and_exit(tmp_path, capsys):
    spend(50)
    (tmp_path / "budgets.json").write_text(json.dumps(cfg({"id": "b", "limit": 40})))
    assert main(["status", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["budgets"][0]["budget_id"] == "b" and out["budgets"][0]["state"] == "over"
