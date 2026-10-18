"""Price list: one JSON file, prices in USD per million tokens, with an `effective_from` date per entry.

File format (see examples/prices.example.json):

    [{"model": "my-model", "provider": "OpenAI", "input": 1.0, "output": 2.0,
      "cache_write": 0, "cache_read": 0.1, "effective_from": "2026-01-01"}]

The file may also be an object {"_note": "...", "prices": [ ...entries... ]}; keys starting with "_" are comments.
The entry used for a call is the latest one with effective_from <= the call date (calls older than the
first entry use the first entry). Prices are looked up by model id only, not by provider.
`python -m giuggiai_meter init` copies an example list (EXAMPLE prices, not real ones) to prices_path().
"""
from __future__ import annotations

import json
import os
import re
from importlib import resources
from pathlib import Path


def prices_path() -> str:
    return os.environ.get("GIUGGIAI_METER_PRICES") or str(Path.home() / ".config/giuggiai-meter/prices.json")


def example_text() -> str:
    """The example price list shipped inside the package (fake model names and EXAMPLE prices)."""
    return resources.files("giuggiai_meter").joinpath("data/prices.example.json").read_text(encoding="utf-8")


def parse(data) -> dict[str, list[dict]]:
    """Index a decoded price list (a list of entries, or {"prices": [...]}) by model, sorted by effective_from."""
    rows = data.get("prices", []) if isinstance(data, dict) else data
    idx: dict[str, list[dict]] = {}
    for r in rows:
        if isinstance(r, dict) and r.get("model") and r.get("active", True):
            idx.setdefault(r["model"], []).append(r)
    for entries in idx.values():
        entries.sort(key=lambda x: x["effective_from"])
    return idx


def load(path: str | None = None) -> dict[str, list[dict]]:
    """Load the price list, indexed by model and sorted by effective_from."""
    return parse(json.loads(Path(path or prices_path()).read_text(encoding="utf-8")))


def load_example() -> dict[str, list[dict]]:
    """The packaged example price list, indexed (used by the demo)."""
    return parse(json.loads(example_text()))


def norm_model(m: str | None) -> str | None:
    """Base model id: drops a `provider/` prefix and a trailing date (-YYYYMMDD or -YYYY-MM-DD)."""
    if not m:
        return m
    m = re.sub(r"^[a-z0-9_-]+/", "", m)
    m = re.sub(r"-\d{8}$", "", m)
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", m)


def pick(idx: dict, model: str | None, date_str: str) -> dict | None:
    """Price entry valid on date_str (YYYY-MM-DD) for model, or None when the model is not in the list."""
    cands = idx.get(model) or idx.get(norm_model(model)) or []
    chosen = None
    for r in cands:
        if r["effective_from"] <= date_str:
            chosen = r
    return chosen or (cands[0] if cands else None)


def cost(price: dict | None, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float:
    """Cost in USD of one call (0.0 when the model has no price)."""
    if not price:
        return 0.0
    usd = (input_tokens * (price.get("input") or 0) + output_tokens * (price.get("output") or 0)
           + cache_write * (price.get("cache_write") or 0) + cache_read * (price.get("cache_read") or 0)) / 1e6
    return round(usd, 6)
