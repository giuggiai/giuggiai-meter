"""Cost per accepted result and model advice, with simulated data: python examples/units_and_advice.py"""
import os
import random
import tempfile

tmp = tempfile.mkdtemp()
os.environ["GIUGGIAI_METER_DB"] = os.path.join(tmp, "usage.db")

import giuggiai_meter as gm
from giuggiai_meter import prices as P
from giuggiai_meter import report as R
from giuggiai_meter.advise import advise

IDX = P.parse([{"model": "large", "input": 10, "output": 30, "effective_from": "2026-01-01"},
               {"model": "small", "input": 0.5, "output": 1.5, "effective_from": "2026-01-01"}])
rnd = random.Random(1)
for model, quality in (("large", 0.92), ("small", 0.9)):
    for k in range(30):
        ticket = f"{model}-ticket-{k}"
        with gm.unit(ticket, kind="support-reply"):          # every call in the block belongs to this ticket
            gm.log_usage("helpdesk", provider="OpenAI", model=model, input_tokens=3_000, output_tokens=500)
        gm.outcome(ticket, accepted=rnd.random() < quality)  # e.g. the customer marked the answer as helpful

print(R.to_table(R.units(IDX, kind="support-reply")))
res = advise("support-reply", IDX, current="large")
print("\nrecommend:", res["recommend"], "-", res["reason"])
