"""Wrap an Anthropic client once: every messages.create call is recorded under the project.

    pip install anthropic giuggiai-meter
    export ANTHROPIC_API_KEY=...        # read by the SDK from the environment
    python examples/wrap_anthropic.py
    giuggiai-meter report --html report.html
"""
import os

from anthropic import Anthropic

import giuggiai_meter as gm

client = gm.wrap(Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]), project="newsletter-bot")

msg = client.messages.create(
    model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
    max_tokens=200,
    messages=[{"role": "user", "content": "Write a two-line intro for this week's newsletter."}],
)
print(msg.content[0].text)  # the SDK response, unchanged
print("recorded:", msg.usage.input_tokens, "input tokens,", msg.usage.output_tokens, "output tokens")
