import json

import pytest
from conftest_gov import PRICES, cfg, setup_env, spend

from giuggiai_meter import prices as P
from giuggiai_meter import report as R
from giuggiai_meter.__main__ import main
from giuggiai_meter.plans import billing_for

IDX = P.parse(PRICES)
PLANS = [{"id": "team", "monthly_fee": 20, "currency": "USD", "match": {"source": "cli/*"}},
         {"id": "seat", "monthly_fee": 10, "currency": "USD", "match": {"project": "ide", "provider": "Anthropic"}}]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    return setup_env(tmp_path, monkeypatch)


def test_billing_explicit_wins():
    assert billing_for({"source": "cli/x", "billing": "api"}, PLANS) == "api"


def test_billing_match_source():
    assert billing_for({"project": "p", "source": "cli/run"}, PLANS) == "subscription:team"


def test_billing_match_project_provider():
    assert billing_for({"slug": "ide", "provider": "Anthropic"}, PLANS) == "subscription:seat"
    assert billing_for({"slug": "ide", "provider": "OpenAI"}, PLANS) == "api"


def test_billing_default_api():
    assert billing_for({"project": "p"}, []) == "api"


def test_plan_report_value_fee_multiple():
    spend(30, source="cli/a", day="2026-10-03")
    spend(10, source="cli/b", day="2026-10-04")
    spend(5, source="web", day="2026-10-04")          # api: not in any plan
    rows = {r["plan"]: r for r in R.plans("2026-10", IDX, cfg(plans=PLANS))}
    assert rows["team"]["calls"] == 2 and rows["team"]["list_value"] == pytest.approx(40)
    assert rows["team"]["fee"] == 20 and rows["team"]["multiple"] == pytest.approx(2.0) and rows["team"]["cash"] == 0
    assert rows["seat"]["calls"] == 0 and rows["seat"]["multiple"] == 0


def test_plan_report_explicit_billing():
    spend(8, billing="subscription:team")
    rows = {r["plan"]: r for r in R.plans("2026-10", IDX, cfg(plans=PLANS))}
    assert rows["team"]["list_value"] == pytest.approx(8)


def test_plan_report_other_month_excluded():
    spend(8, source="cli/a", day="2026-09-30")
    rows = {r["plan"]: r for r in R.plans("2026-10", IDX, cfg(plans=PLANS))}
    assert rows["team"]["calls"] == 0


def test_plan_report_currency_conversion():
    spend(10, source="cli/a")
    rows = {r["plan"]: r for r in R.plans("2026-10", IDX, cfg(plans=PLANS, currency="EUR", fx={"USD": 0.5}))}
    assert rows["team"]["list_value"] == pytest.approx(5) and rows["team"]["fee"] == pytest.approx(10)
    assert rows["team"]["currency"] == "EUR"


def test_plans_cli(tmp_path, capsys):
    spend(30, source="cli/a")
    (tmp_path / "budgets.json").write_text(json.dumps(cfg(plans=PLANS)))
    assert main(["plans", "--month", "2026-10"]) == 0
    assert "team" in capsys.readouterr().out
