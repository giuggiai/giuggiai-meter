"""Subscription plans and run-out forecast, with simulated data: python examples/plans_and_forecast.py"""
import datetime
import os
import tempfile

tmp = tempfile.mkdtemp()
os.environ["GIUGGIAI_METER_DB"] = os.path.join(tmp, "usage.db")

import giuggiai_meter as gm
from giuggiai_meter import forecast as F
from giuggiai_meter import prices as P
from giuggiai_meter import report as R

IDX = P.parse([{"model": "large", "input": 10, "output": 30, "effective_from": "2026-01-01"}])
CONFIG = {"currency": "USD",
          "budgets": [{"id": "agents", "match": {"source": "agents/*"}, "limit": 100, "period": "month", "warn_at": 0.8}],
          "plans": [{"id": "coding-plan", "monthly_fee": 20, "match": {"source": "ide/*"}}]}

today = datetime.date.today()
month = today.strftime("%Y-%m")
for d in range(min(today.day, 7)):                        # a week of work: 1M input tokens (10 USD) per day each
    day = (today - datetime.timedelta(days=d)).isoformat()
    gm.log_usage("research", provider="OpenAI", model="large", input_tokens=1_000_000, source="agents/researcher",
                 log_time=f"{day} 10:00:00")
    gm.log_usage("dev", provider="OpenAI", model="large", input_tokens=1_000_000, source="ide/editor",
                 log_time=f"{day} 11:00:00")

print("Plans (covered calls cost 0 per token):")
print(R.to_table(R.plans(month, IDX, CONFIG)))
print("\nBudgets and forecast:")
rows = F.status(CONFIG, IDX)
print(R.to_table(rows))
print("\nexit code for cron:", F.exit_code(rows))
