"""Plug-and-play features: gm.wrap, HTML report, init, demo, console entry point."""
import asyncio
import json
import os
import threading
from types import SimpleNamespace

import pytest

import giuggiai_meter as gm
from giuggiai_meter import prices as P
from giuggiai_meter import report as R
from giuggiai_meter.__main__ import main

PRICES = [
    {"model": "model-a", "provider": "OpenAI", "input": 1.0, "output": 2.0, "effective_from": "2026-01-01"},
    {"model": "model-b", "provider": "Anthropic", "input": 3.0, "output": 15.0, "effective_from": "2026-01-01"},
]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_DB", str(tmp_path / "usage.db"))
    monkeypatch.delenv("GIUGGIAI_METER_URL", raising=False)
    monkeypatch.delenv("GIUGGIAI_METER_TOKEN", raising=False)
    p = tmp_path / "prices.json"
    p.write_text(json.dumps(PRICES))
    monkeypatch.setenv("GIUGGIAI_METER_PRICES", str(p))
    return tmp_path


def rows():
    with gm.connect() as c:
        return sorted(c.execute("SELECT slug, provider, model, input_tokens, output_tokens, cache_read_tokens "
                                "FROM usage").fetchall())


# ---------- fake SDK clients (class names matter: provider detection) ----------

class _Messages:
    def __init__(self):
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        if kw.get("stream"):
            return iter(["event-1", "event-2"])
        return SimpleNamespace(id=f"msg_{self.calls}", type="message", model=kw["model"], content=["hi"],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20,
                                                     cache_creation_input_tokens=0, cache_read_input_tokens=30))

    def count_tokens(self, **kw):
        return SimpleNamespace(input_tokens=7)


class Anthropic:
    def __init__(self):
        self.messages = _Messages()
        self.api_key = "x"

    def with_options(self, **kw):
        return self


class _Completions:
    n = 0

    def create(self, **kw):
        if kw.get("stream"):
            return iter(["chunk"])
        _Completions.n += 1
        return SimpleNamespace(id=f"chatcmpl_{_Completions.n}", object="chat.completion", model=kw["model"],
                               usage=SimpleNamespace(prompt_tokens=50, completion_tokens=7,
                                                     prompt_tokens_details=SimpleNamespace(cached_tokens=10)))


class _Responses:
    def create(self, **kw):
        return SimpleNamespace(id="resp_1", object="response", model=kw["model"],
                               usage=SimpleNamespace(input_tokens=40, output_tokens=9,
                                                     input_tokens_details=SimpleNamespace(cached_tokens=4)))


class OpenAI:
    def __init__(self, base_url="https://api.openai.com/v1"):
        self.base_url = base_url
        self.chat = SimpleNamespace(completions=_Completions())
        self.responses = _Responses()
        self.models = SimpleNamespace(list=lambda: ["model-a"])


class _AsyncMessages:
    async def create(self, **kw):
        return SimpleNamespace(id="msg_async", type="message", model=kw["model"],
                               usage=SimpleNamespace(input_tokens=11, output_tokens=3))


class AsyncAnthropic:
    def __init__(self):
        self.messages = _AsyncMessages()


# ---------- wrap ----------

def test_wrap_anthropic_records_and_returns_response_unchanged():
    raw = Anthropic()
    client = gm.wrap(raw, project="newsletter-bot")
    resp = client.messages.create(model="model-b", max_tokens=10, messages=[])
    assert resp.id == "msg_1" and resp.content == ["hi"]
    assert isinstance(resp, SimpleNamespace)  # the SDK object itself, not a copy
    assert client.messages.count_tokens(model="model-b").input_tokens == 7  # other methods untouched, not recorded
    assert client.api_key == "x" and client.__wrapped__ is raw
    assert rows() == [("newsletter-bot", "Anthropic", "model-b", 100, 20, 30)]


def test_wrap_openai_chat_and_responses_with_provider():
    client = gm.wrap(OpenAI(base_url="https://api.deepseek.com"), project="support-bot", provider="DeepSeek")
    r1 = client.chat.completions.create(model="model-a", messages=[])
    r2 = client.responses.create(model="model-a", input="hi")
    assert r1.id.startswith("chatcmpl_") and r2.id == "resp_1"
    assert client.models.list() == ["model-a"]
    assert rows() == [("support-bot", "DeepSeek", "model-a", 36, 9, 4), ("support-bot", "DeepSeek", "model-a", 40, 7, 10)]


def test_wrap_detects_provider():
    gm.wrap(OpenAI(), project="p1").chat.completions.create(model="model-a")
    gm.wrap(OpenAI(base_url="https://api.deepseek.com/v1"), project="p2").chat.completions.create(model="model-a")
    gm.wrap(Anthropic(), project="p3").messages.create(model="model-b")
    assert [(r[0], r[1]) for r in rows()] == [("p1", "OpenAI"), ("p2", "DeepSeek"), ("p3", "Anthropic")]


def test_wrap_streaming_without_usage_is_recorded_as_an_estimate():
    # events pass through unchanged; the stream sends no usage, so the row is an estimate (~4 characters per token)
    client = gm.wrap(Anthropic(), project="p")
    assert list(client.messages.create(model="model-b", stream=True)) == ["event-1", "event-2"]
    assert list(gm.wrap(OpenAI(), project="p").chat.completions.create(model="model-a", stream=True)) == ["chunk"]
    assert rows() == [("p", "Anthropic", "model-b", 0, 3, 0), ("p", "OpenAI", "model-a", 0, 1, 0)]
    with gm.connect() as c:
        assert all(r[0].startswith("gm-est-") for r in c.execute("SELECT request_id FROM usage"))


def test_wrap_logging_error_never_propagates(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("logger exploded")

    monkeypatch.setattr(gm, "log_response", boom)
    resp = gm.wrap(Anthropic(), project="p").messages.create(model="model-b")
    assert resp.id == "msg_1"
    assert "logger exploded" in capsys.readouterr().err


def test_wrap_unwritable_database_never_propagates(monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_DB", "/proc/forbidden/usage.db")
    assert gm.wrap(OpenAI(), project="p").chat.completions.create(model="model-a").id.startswith("chatcmpl_")


def test_wrap_sdk_errors_still_reach_the_caller():
    def create(**kw):
        raise ValueError("bad request")

    class Failing:
        messages = SimpleNamespace(create=create)

    with pytest.raises(ValueError, match="bad request"):
        gm.wrap(Failing(), project="p").messages.create(model="m")


def test_wrap_async_client():
    client = gm.wrap(AsyncAnthropic(), project="async-bot")
    resp = asyncio.run(client.messages.create(model="model-b"))
    assert resp.id == "msg_async"
    assert rows() == [("async-bot", "Anthropic", "model-b", 11, 3, 0)]


def test_wrap_async_records_off_the_event_loop(monkeypatch):
    raw = AsyncAnthropic()
    sentinel = SimpleNamespace(id="msg_same", type="message", model="model-b",
                               usage=SimpleNamespace(input_tokens=5, output_tokens=2))

    async def create(**kw):
        return sentinel

    raw.messages.create = create
    seen = {}
    real = gm.log_response

    def spy(*a, **kw):
        seen["thread"] = threading.get_ident()
        return real(*a, **kw)

    monkeypatch.setattr(gm, "log_response", spy)

    async def main():
        seen["loop"] = threading.get_ident()
        return await gm.wrap(raw, project="async-bot").messages.create(model="model-b")

    assert asyncio.run(main()) is sentinel  # the very same object, unchanged
    assert seen["thread"] != seen["loop"]  # recording ran in a worker thread
    assert rows() == [("async-bot", "Anthropic", "model-b", 5, 2, 0)]


def test_wrap_async_recording_failure_never_propagates(monkeypatch, capsys):
    import giuggiai_meter._wrap as W

    async def boom(*a, **kw):
        raise RuntimeError("no thread")

    monkeypatch.setattr(W.asyncio, "to_thread", boom)
    resp = asyncio.run(gm.wrap(AsyncAnthropic(), project="async-bot").messages.create(model="model-b"))
    assert resp.id == "msg_async" and rows() == []
    assert "async logging failed" in capsys.readouterr().err


def test_wrap_is_idempotent_and_keeps_copies_metered():
    raw = Anthropic()
    client = gm.wrap(gm.wrap(raw, project="p"), project="q")
    assert client.__wrapped__ is raw
    client.with_options(timeout=5).messages.create(model="model-b")
    assert rows()[0][0] == "q"


# ---------- prices ----------

def test_prices_object_format_and_packaged_example():
    idx = P.parse({"_note": "comment", "prices": [{"model": "m", "input": 1, "output": 1, "effective_from": "2026-01-01"},
                                                  {"_comment": "ignored"}]})
    assert list(idx) == ["m"]
    data = json.loads(P.example_text())
    assert "_note" in data and all(e["model"].startswith("example-") for e in data["prices"])
    assert P.load_example()["example-large"][0]["output"] == 15.0


# ---------- HTML report ----------

def test_html_report_escapes_and_is_self_contained():
    evil = '<script>alert("x")</script>'
    gm.log_usage(evil, "OpenAI", "model-a", 1_000_000, 0, log_time="2026-03-05 10:00:00")
    gm.log_usage("p2", "Anthropic", "model-b", 0, 1_000_000, log_time="2026-03-07 10:00:00")
    gm.log_usage("p2", "Anthropic", "no-price-<b>", 10, 10, log_time="2026-03-08 10:00:00")
    idx = P.load()
    rep = R.monthly("2026-03", idx)
    html = R.to_html(rep, "2026-03", R.reconcile(rep, {"OpenAI": 1.25, "Anthropic": 15.0}), R.unpriced("2026-03", idx))
    assert html.startswith("<!doctype html>") and "<style>" in html
    assert "<script" not in html and "&lt;script&gt;" in html and "no-price-&lt;b&gt;" in html
    for external in ("http://", "https://", "<link", "src=", "@import", "url("):
        assert external not in html
    assert "$16.00" in html  # total, in big
    assert "March 2026" in html and "Reconciliation" in html and "-20.0%" in html
    assert "1 call without a price" in html


def test_html_report_empty_month():
    html = R.to_html([], "2026-01")
    assert "No calls recorded" in html and "$0" in html


def test_cli_report_html(tmp_path, capsys):
    gm.log_usage("p1", "OpenAI", "model-a", 1_000_000, 0, log_time="2026-03-05 10:00:00")
    out = tmp_path / "sub" / "report.html"
    assert main(["report", "--month", "2026-03", "--html", str(out), "--official", "OpenAI=1.10"]) == 0
    html = out.read_text()
    assert "p1" in html and "Reconciliation" in html
    assert str(out.resolve()) in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["report", "--month", "2026-03", "--html", str(out), "--official", "OpenAI"])


# ---------- init / demo / version ----------

def test_init_creates_and_never_overwrites(tmp_path, capsys):
    target = tmp_path / "cfg" / "prices.json"
    assert main(["init", "--path", str(target)]) == 0
    assert "_note" in json.loads(target.read_text())
    assert "Next steps" in capsys.readouterr().out
    target.write_text("[]")
    assert main(["init", "--path", str(target)]) == 0
    assert target.read_text() == "[]"
    assert "left untouched" in capsys.readouterr().out


def test_init_default_path_follows_env(tmp_path, monkeypatch):
    target = tmp_path / "env-prices.json"
    monkeypatch.setenv("GIUGGIAI_METER_PRICES", str(target))
    assert main(["init"]) == 0 and target.exists()


def test_demo_writes_html_in_current_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["demo"]) == 0
    f = tmp_path / "giuggiai-meter-demo.html"
    html = f.read_text()
    assert "newsletter-bot" in html and "Reconciliation" in html and "example-unlisted" in html
    assert str(f.resolve()) in capsys.readouterr().out
    assert not os.path.exists(os.environ["GIUGGIAI_METER_DB"])  # the demo never touches the real database


def test_version(capsys):
    with pytest.raises(SystemExit) as ex:
        main(["--version"])
    assert ex.value.code == 0 and capsys.readouterr().out.strip() == f"giuggiai-meter {gm.__version__}"


def test_isinstance_preserved():
    import giuggiai_meter as gm

    class FakeClient:
        base_url = "https://api.openai.com/v1"

    w = gm.wrap(FakeClient(), project="p")
    assert isinstance(w, FakeClient)


def test_stderr_none_never_raises(monkeypatch):
    import sys

    import giuggiai_meter as gm
    monkeypatch.setattr(sys, "stderr", None)
    assert gm.log_response("p", object()) is None


def test_official_names_normalized():
    from giuggiai_meter.__main__ import _official
    assert _official(["openai=41.2", "ANTHROPIC=1"]) == {"OpenAI": 41.2, "Anthropic": 1.0}
