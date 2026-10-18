"""Records two fake calls and prints this month's report (no API key needed)."""
import datetime

import giuggiai_meter as gm
from giuggiai_meter import prices, report

now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
gm.log_usage("demo-project", "OpenAI", "example-small", input_tokens=120_000, output_tokens=30_000, log_time=now)
gm.log_usage("demo-project", "Anthropic", "example-large", input_tokens=40_000, output_tokens=8_000, log_time=now)
rows = report.monthly(now[:7], prices.load("examples/prices.example.json"))
print(report.to_table(rows))
