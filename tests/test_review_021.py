"""0.2.1: budgets cannot be bypassed by streams, raw responses, remote hosts or attribution; unpriced models,
future-dated rows and fresh installs are handled (fixes of the 0.2.0 review)."""
import asyncio
import datetime
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from conftest_gov import cfg, setup_env, spend

import giuggiai_meter as gm
from giuggiai_meter import budgets as B
from giuggiai_meter.ingest import make_handler

TOKEN = "test-" + "only-not-a-secret"
TODAY = datetime.date.today().isoformat()
MSG = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    B._LAST_GOOD.clear()
    B._STALE.clear()
    B._CFG_CACHE.clear()
    B._UNPRICED_WARNED.clear()
    monkeypatch.delenv("GIUGGIAI_METER_BUDGETS_FAIL", raising=False)
    return setup_env(tmp_path, monkeypatch)


def rows(*cols):
    with gm.connect() as c:
        return c.execute(f"SELECT {', '.join(cols or ('slug', 'model', 'input_tokens', 'output_tokens'))} FROM usage "
                         "ORDER BY rowid").fetchall()


# ------------------------------------------------------------------ fake SDK clients

class Completions:
    def __init__(self, events=None):
        self.calls, self.events = [], events

    def create(self, **kw):
        self.calls.append(kw)
        if kw.get("stream"):
            return iter(self.events(kw))
        return {"id": f"r{len(self.calls)}", "model": kw["model"], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


_sid = iter(range(10**6))


def openai_chunks(kw):
    sid = f"s{next(_sid)}"  # one id per stream: the request_id of its row
    yield {"id": sid, "model": kw["model"], "choices": [{"delta": {"content": "hello"}}]}
    if (kw.get("stream_options") or {}).get("include_usage"):
        yield {"id": sid, "model": kw["model"], "choices": [],
               "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 40}}}


def client(base_url="https://api.openai.com/v1", events=openai_chunks):
    comp = Completions(events)
    return NS(chat=NS(completions=comp), base_url=base_url), comp


# ------------------------------------------------------------------ streams (REV-02)

def test_openai_stream_asks_for_usage_and_records_it():
    c, comp = client()
    assert len(list(gm.wrap(c, project="p", provider="OpenAI").chat.completions.create(model="big", messages=MSG,
                                                                                        stream=True))) == 2
    assert comp.calls[0]["stream_options"] == {"include_usage": True}
    assert rows("slug", "model", "input_tokens", "output_tokens", "cache_read_tokens") == [("p", "big", 60, 20, 40)]


def test_stream_options_left_alone_on_other_hosts_and_when_set():
    c, comp = client(base_url="https://llm-gateway.example/v1")
    list(gm.wrap(c, project="p").chat.completions.create(model="big", messages=MSG, stream=True))
    c2, comp2 = client()
    list(gm.wrap(c2, project="p").chat.completions.create(model="big", messages=MSG, stream=True,
                                                          stream_options={"include_usage": False}))
    assert "stream_options" not in comp.calls[0] and comp2.calls[0]["stream_options"] == {"include_usage": False}
    est = rows("request_id", "output_tokens")
    assert all(r[0].startswith("gm-est-") and r[1] == 1 for r in est)  # "hello" ~ 1 token


def test_stream_blocked_once_its_spend_reaches_the_limit():
    c, comp = client()
    w = gm.wrap(c, project="p", provider="OpenAI", budgets=cfg({"id": "b", "limit": 0.004}))
    blocked = 0
    for _ in range(5):  # each stream: 60 input + 40 cached + 20 output tokens of "big" = 0.0012 USD
        try:
            list(w.chat.completions.create(model="big", messages=MSG, stream=True))
        except gm.BudgetExceeded:
            blocked += 1
    assert len(comp.calls) == 4 and blocked == 1


ANTHROPIC_EVENTS = [
    NS(type="message_start", message=NS(id="msg_s", model="claude-x", usage=NS(
        input_tokens=50, output_tokens=1, cache_read_input_tokens=30, cache_creation_input_tokens=0))),
    NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text="Hello")),
    NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=77)),
    NS(type="message_stop"),
]


class Anthropic:
    def __init__(self):
        self.messages = NS(create=self._create, stream=self._stream)

    def _create(self, **kw):
        return iter(ANTHROPIC_EVENTS)

    def _stream(self, **kw):
        return Manager()


class Manager:  # MessageStreamManager: `with client.messages.stream(...) as s`
    def __enter__(self):
        self.s = NS(text_stream=iter(["Hello"]), get_final_message=lambda: NS(
            id="msg_m", type="message", model="claude-x", usage=NS(input_tokens=8, output_tokens=4)))
        return self.s

    def __exit__(self, *exc):
        return False


def test_anthropic_stream_events_recorded():
    list(gm.wrap(Anthropic(), project="a").messages.create(model="claude-x", stream=True, messages=MSG))
    assert rows("model", "input_tokens", "output_tokens", "cache_read_tokens") == [("claude-x", 50, 77, 30)]


def test_anthropic_messages_stream_manager_checked_and_recorded():
    w = gm.wrap(Anthropic(), project="a", budgets=cfg({"id": "b", "limit": 5}))
    with w.messages.stream(model="claude-x", messages=MSG) as s:
        assert "".join(s.text_stream) == "Hello"
        s.get_final_message()
    assert rows() == [("a", "claude-x", 8, 4)]
    spend(6, day=TODAY, project="a")
    with pytest.raises(gm.BudgetExceeded):
        w.messages.stream(model="claude-x", messages=MSG)


def test_manager_left_without_final_message_still_recorded():
    with gm.wrap(Anthropic(), project="a").messages.stream(model="claude-x", messages=MSG) as s:
        list(s.text_stream)
    assert len(rows()) == 1 and rows("request_id")[0][0].startswith("gm-est-")


def test_gemini_bedrock_streams_use_the_usage_in_their_events():
    gem = NS(models=NS(generate_content_stream=lambda **kw: iter([
        NS(text="a", usage_metadata=NS(prompt_token_count=7, candidates_token_count=1)),
        NS(text="b", usage_metadata=NS(prompt_token_count=7, candidates_token_count=3, thoughts_token_count=2))])))
    list(gm.wrap(gem, project="g").models.generate_content_stream(model="gemini-2.5-flash", contents="x"))
    br = NS(converse_stream=lambda **kw: {"stream": iter([{"contentBlockDelta": {"delta": {"text": "x"}}},
                                                           {"metadata": {"usage": {"inputTokens": 9, "outputTokens": 4}}}])})
    list(gm.wrap(br, project="b").converse_stream(modelId="anthropic.claude-x", messages=[])["stream"])
    assert rows() == [("g", "gemini-2.5-flash", 7, 5), ("b", "anthropic.claude-x", 9, 4)]


def test_async_stream_recorded():
    async def agen(kw):
        for ev in openai_chunks(kw):
            yield ev

    calls = []

    async def create(**kw):
        calls.append(kw)
        return agen(kw)

    c = NS(chat=NS(completions=NS(create=create)), base_url="https://api.openai.com/v1")

    async def main():
        s = await gm.wrap(c, project="p", budgets=cfg({"id": "b", "limit": 5})).chat.completions.create(
            model="big", messages=MSG, stream=True)
        return [e async for e in s]

    assert len(asyncio.run(main())) == 2
    assert rows() == [("p", "big", 60, 20)]


def test_stream_closed_half_way_is_recorded_once():
    c, _ = client()
    s = gm.wrap(c, project="p", provider="OpenAI").chat.completions.create(model="big", messages=MSG, stream=True)
    next(s)
    s.close()  # the agent stops reading: what was used so far is recorded, once
    s.close()
    del s
    assert len(rows()) == 1


# ------------------------------------------------------------------ raw responses (REV-02)

class Raw:
    def __init__(self, parsed):
        self.parsed, self.n = parsed, 0

    def parse(self):
        self.n += 1
        return self.parsed


def raw_client():
    calls = []

    def create(**kw):
        calls.append(kw)
        return Raw({"id": f"raw{len(calls)}", "model": kw["model"], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    comp = NS(create=create, with_raw_response=NS(create=create))
    return NS(chat=NS(completions=comp), base_url="https://api.openai.com/v1"), calls


def test_with_raw_response_is_checked_and_recorded():
    c, calls = raw_client()
    w = gm.wrap(c, project="p", budgets=cfg({"id": "b", "limit": 5}))
    r = w.chat.completions.with_raw_response.create(model="big", messages=MSG)
    assert r.parse()["id"] == "raw1"
    assert rows() == [("p", "big", 10, 5)]
    spend(6, day=TODAY)
    with pytest.raises(gm.BudgetExceeded):
        w.chat.completions.with_raw_response.create(model="big", messages=MSG)
    assert len(calls) == 1


# ------------------------------------------------------------------ attribution (REV-03)

def test_budget_on_source_applies_with_the_source_from_the_environment(monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_SOURCE", "agents/research")
    spend(6, day=TODAY, source="agents/research")
    c, comp = client()
    with pytest.raises(gm.BudgetExceeded):
        gm.wrap(c, project="p", provider="OpenAI",
                budgets=cfg({"id": "b", "limit": 5, "match": {"source": "agents/*"}})).chat.completions.create(
            model="big", messages=MSG)
    assert comp.calls == []


def test_budget_on_provider_applies_when_the_provider_comes_from_the_model():
    # a generic OpenAI-compatible client pointed at a gateway: rows are attributed from the model id
    path = Path(gm.prices.prices_path())
    path.write_text(json.dumps(json.loads(path.read_text()) + [
        {"model": "gpt-4o", "provider": "OpenAI", "input": 2.5, "output": 10, "effective_from": "2026-01-01"}]))
    c, comp = client(base_url="https://llm-gateway.example/v1")
    w = gm.wrap(c, project="p", budgets=cfg({"id": "b", "limit": 1, "match": {"provider": "OpenAI"}}))
    w.chat.completions.create(model="gpt-4o", messages=MSG)
    assert rows("provider") == [("OpenAI",)]
    spend(2, day=TODAY, model="gpt-4o", provider="OpenAI")  # 2M tokens of gpt-4o = 5 USD > 1
    with pytest.raises(gm.BudgetExceeded):
        w.chat.completions.create(model="gpt-4o", messages=MSG)
    assert len(comp.calls) == 1


# ------------------------------------------------------------------ unpriced models (REV-04)

def test_unpriced_model_blocks_a_fail_closed_budget():
    d = gm.check("p", model="brand-new-model", provider="OpenAI", config=cfg({"id": "b", "limit": 5, "fail": "closed"}))
    assert not d.allow and "not in the price list" in d.reason


def test_unpriced_model_warns_on_a_fail_open_budget_and_is_recorded(capsys):
    c, comp = client()
    w = gm.wrap(c, project="p", provider="OpenAI", budgets=cfg({"id": "b", "limit": 5}))
    w.chat.completions.create(model="brand-new-model", messages=MSG)
    assert len(comp.calls) == 1 and rows("model") == [("brand-new-model",)]
    d = gm.check("p", model="brand-new-model", provider="OpenAI", config=cfg({"id": "b", "limit": 5}))
    assert d.allow and d.action == "warn" and "spend not counted" in d.reason
    assert "not in the price list" in capsys.readouterr().err


def test_unpriced_calls_in_the_window_block_a_fail_closed_budget():
    gm.log_usage("p", provider="OpenAI", model="brand-new-model", input_tokens=50_000_000, log_time=f"{TODAY} 10:00:00")
    d = gm.check("p", model="big", provider="OpenAI", config=cfg({"id": "b", "limit": 5, "fail": "closed"}))
    assert not d.allow and "1 call(s) in the window have no price" in d.reason


# ------------------------------------------------------------------ dates (REV-05)

def test_rows_dated_tomorrow_count_now():
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    spend(6, day=tomorrow)
    for period in ("day", "week", "month", "total"):
        assert gm.check("p", model="big", config=cfg({"id": "b", "limit": 5, "period": period})).action == "block"


# ------------------------------------------------------------------ remote hosts (REV-01)

@pytest.fixture
def server(env, tmp_path):
    db, prices = env
    budgets = tmp_path / "server-budgets.json"
    budgets.write_text(json.dumps(cfg({"id": "srv", "limit": 5, "fail": "closed"})))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(TOKEN, db, str(budgets), prices))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_wrap_on_a_remote_host_asks_the_server(server, monkeypatch, tmp_path):
    _, url = server
    monkeypatch.setenv("GIUGGIAI_METER_URL", url + "/ingest")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", TOKEN)
    monkeypatch.setenv("GIUGGIAI_METER_DB", str(tmp_path / "container-local.db"))  # never created: rows go to the server
    c, comp = client()
    w = gm.wrap(c, project="p", provider="OpenAI", budgets=True)
    w.chat.completions.create(model="big", messages=MSG)  # the server's budget applies (no local budgets file)
    spend(6, day=TODAY)  # logged through the ingest, like every row of this host
    with pytest.raises(gm.BudgetExceeded) as e:
        w.chat.completions.create(model="big", messages=MSG)
    assert e.value.decision.budget_id == "srv" and len(comp.calls) == 1
    assert not (tmp_path / "container-local.db").exists()


def test_remote_down_applies_the_fail_mode_the_server_gave(server, monkeypatch):
    srv, url = server
    monkeypatch.setenv("GIUGGIAI_METER_URL", url + "/ingest")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", TOKEN)
    assert gm.check_remote("p", model="big", provider="OpenAI").fail == "closed"
    srv.shutdown()
    srv.server_close()
    d = gm.check_remote("p", model="big", provider="OpenAI")
    assert not d.allow and "unreachable" in d.reason and d.budget_id == "srv"


def test_remote_down_without_any_budget_known_follows_budgets_fail(monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_URL", "http://127.0.0.1:9/ingest")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", TOKEN)
    assert gm.check_remote("p", model="big").allow  # default: open, with a warning
    monkeypatch.setenv("GIUGGIAI_METER_BUDGETS_FAIL", "closed")
    assert not gm.check_remote("p", model="big").allow


def test_remote_wrong_token_is_reported_and_never_simply_allowed(server, monkeypatch, capsys):
    _, url = server
    monkeypatch.setenv("GIUGGIAI_METER_URL", url + "/ingest")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", "wrong-" + TOKEN)
    monkeypatch.setenv("GIUGGIAI_METER_BUDGETS_FAIL", "closed")
    d = gm.check_remote("p", model="big")
    assert not d.allow and "token refused" in d.reason
    assert "refused the token" in capsys.readouterr().err


def test_server_prices_token_counts_sent_by_the_client(server, monkeypatch):
    _, url = server
    monkeypatch.setenv("GIUGGIAI_METER_URL", url + "/ingest")
    monkeypatch.setenv("GIUGGIAI_METER_TOKEN", TOKEN)
    # 600k input tokens of "big" = 6 USD > 5: blocked by the server's estimate, not by recorded spend
    assert gm.check_remote("p", model="big", input_tokens=600_000).action == "block"
