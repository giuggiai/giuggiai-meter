"""Budgets before the call, with a stand-in client (no API key needed): python examples/budget_guard.py

A wrapped client checks the budgets first: over the limit, the call runs on the cheaper model; past block_at it is
refused with BudgetExceeded and never reaches the provider."""
import os
import tempfile
from types import SimpleNamespace

tmp = tempfile.mkdtemp()
os.environ["GIUGGIAI_METER_DB"] = os.path.join(tmp, "usage.db")
os.environ["GIUGGIAI_METER_PRICES"] = os.path.join(tmp, "prices.json")
with open(os.environ["GIUGGIAI_METER_PRICES"], "w") as f:
    f.write('[{"model": "large", "provider": "OpenAI", "input": 10, "output": 30, "effective_from": "2026-01-01"},'
            ' {"model": "small", "provider": "OpenAI", "input": 0.5, "output": 1.5, "effective_from": "2026-01-01"}]')

import giuggiai_meter as gm

BUDGETS = {"currency": "USD", "budgets": [{"id": "agent", "match": {"source": "agents/*"}, "limit": 1.0,
                                            "period": "day", "on_exceed": "degrade", "degrade_to": "small",
                                            "block_at": 1.42}]}


class FakeOpenAI:
    base_url = "https://api.openai.com/v1"

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, model, messages, **kw):
        return {"id": os.urandom(4).hex(), "model": model, "usage": {"prompt_tokens": 40_000, "completion_tokens": 2_000}}


client = gm.wrap(FakeOpenAI(), project="research", source="agents/researcher", budgets=BUDGETS)
for i in range(1, 8):
    try:
        r = client.chat.completions.create(model="large", messages=[{"role": "user", "content": "summarise"}])
        print(f"call {i}: ran on {r['model']}")
    except gm.BudgetExceeded as e:
        print(f"call {i}: refused before reaching the provider - {e.decision.reason}")
