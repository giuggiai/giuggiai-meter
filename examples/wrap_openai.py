"""Wrap an OpenAI-compatible client once: chat.completions.create and responses.create are recorded.

The same code works for any OpenAI-compatible provider: set base_url and name who bills the call.

    pip install openai giuggiai-meter
    export DEEPSEEK_API_KEY=...
    python examples/wrap_openai.py
    giuggiai-meter report --html report.html
"""
import os

from openai import OpenAI

import giuggiai_meter as gm

client = gm.wrap(
    OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com"),
    project="support-bot",
    provider="DeepSeek",  # optional here: also guessed from base_url
)

resp = client.chat.completions.create(
    model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    messages=[{"role": "user", "content": "Summarise this ticket in one line: the invoice PDF does not open."}],
)
print(resp.choices[0].message.content)  # the SDK response, unchanged
