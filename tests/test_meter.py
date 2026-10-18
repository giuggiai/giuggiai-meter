import json
import os
import stat
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import giuggiai_meter as gm
from giuggiai_meter import prices as P
from giuggiai_meter import report as R
from giuggiai_meter.__main__ import main
from giuggiai_meter.ingest import make_handler

T = "test-" + "only-not-a-secret"  # dummy ingest token used by the tests

PRICES = [
    {"model": "model-a", "provider": "OpenAI", "input": 1.0, "output": 2.0, "cache_read": 0.5, "effective_from": "2026-01-01"},
    {"model": "model-a", "provider": "OpenAI", "input": 2.0, "output": 4.0, "effective_from": "2026-06-01"},
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
        return c.execute("SELECT slug, provider, model, input_tokens, output_tokens, cache_read_tokens FROM usage").fetchall()


def test_log_usage_and_idempotency():
    rid = gm.log_usage("proj", "openai", "model-a", input_tokens=10, output_tokens=5, request_id="x1")
    gm.log_usage("proj", "openai", "model-a", input_tokens=10, output_tokens=5, request_id="x1")
    assert rid == "x1"
    assert rows() == [("proj", "OpenAI", "model-a", 10, 5, 0)]


def test_never_raises(monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_DB", "/proc/forbidden/usage.db")
    assert gm.log_usage("proj", input_tokens="not-a-number") is None


def test_extractors():
    a = SimpleNamespace(id="msg_1", model="model-b", usage=SimpleNamespace(
        input_tokens=100, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=30))
    o = {"id": "chat_1", "model": "model-a", "usage": {"prompt_tokens": 50, "completion_tokens": 7,
                                                        "prompt_tokens_details": {"cached_tokens": 10}}}
    gm.log_anthropic("p1", a)
    gm.log_openai("p2", o)
    assert sorted(rows()) == [("p1", "Anthropic", "model-b", 100, 20, 30), ("p2", "OpenAI", "model-a", 40, 7, 10)]  # input = uncached


def test_price_by_date_and_model_normalisation():
    idx = P.load()
    assert P.pick(idx, "model-a", "2026-03-01")["input"] == 1.0
    assert P.pick(idx, "model-a", "2026-07-01")["input"] == 2.0
    assert P.pick(idx, "openai/model-a-20260701", "2026-07-01")["input"] == 2.0
    assert P.pick(idx, "unknown", "2026-07-01") is None
    assert P.cost(P.pick(idx, "model-a", "2026-03-01"), 1_000_000, 1_000_000, 0, 1_000_000) == 3.5


def test_monthly_report_and_reconcile():
    gm.log_usage("p1", "OpenAI", "model-a", 1_000_000, 0, log_time="2026-03-05 10:00:00")
    gm.log_usage("p1", "OpenAI", "model-x", 1_000_000, 0, log_time="2026-03-06 10:00:00")
    gm.log_usage("p2", "Anthropic", "model-b", 0, 1_000_000, log_time="2026-03-07 10:00:00")
    gm.log_usage("p2", "Anthropic", "model-b", 0, 1_000_000, log_time="2026-04-01 10:00:00")
    rep = R.monthly("2026-03", P.load())
    assert {(r["slug"], r["provider"]): (r["calls"], r["cost_usd"], r["unpriced_calls"]) for r in rep} == {
        ("p1", "OpenAI"): (2, 1.0, 1), ("p2", "Anthropic"): (1, 15.0, 0)}
    rec = {r["provider"]: r for r in R.reconcile(rep, {"OpenAI": 1.25, "Anthropic": 15.0})}
    assert rec["OpenAI"]["diff_usd"] == -0.25 and rec["OpenAI"]["diff_pct"] == -20.0
    assert rec["Anthropic"]["diff_usd"] == 0.0


def test_cli_report(capsys):
    gm.log_usage("p1", "OpenAI", "model-a", 1_000_000, 0, log_time="2026-03-05 10:00:00")
    assert main(["report", "--month", "2026-03", "--csv"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("slug,provider,calls") and "p1,OpenAI,1" in out


def test_ingest_roundtrip():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(T))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/ingest"

    def post(token, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Meter-Token": token})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    assert post("wrong", {"request_id": "r1", "slug": "p"}) == 401
    assert post(T, {"slug": "p"}) == 400
    assert post(T, {"request_id": "r1", "slug": "p", "provider": "OpenAI", "input_tokens": 3}) == 200
    srv.shutdown()
    assert rows() == [("p", "OpenAI", None, 3, 0, 0)]


def test_extractors_never_raise():
    class Boom:
        @property
        def usage(self):
            raise RuntimeError("broken SDK object")

    assert gm.log_anthropic("p", Boom()) is None
    assert gm.log_openai("p", Boom()) is None
    assert gm.log_openai("p", {"id": 123, "usage": {"prompt_tokens": 1}}) == "gm-123"


def test_files_private(env):
    gm.log_usage("p", "OpenAI", "m", 1, 1)
    db = os.environ["GIUGGIAI_METER_DB"]
    for f in (db, db + "-wal", db + "-shm"):
        if os.path.exists(f):
            assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, f


def test_ingest_rejects_bad_input():
    with pytest.raises(ValueError):
        make_handler("")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(T))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import http.client
    def raw(body, length=None, token=T):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.putrequest("POST", "/ingest")
        c.putheader("X-Meter-Token", token)
        c.putheader("Content-Length", str(len(body) if length is None else length))
        c.endheaders()
        c.send(body)
        return c.getresponse().status
    assert raw(b"[]") == 400
    assert raw(json.dumps({"request_id": "r", "slug": "p", "input_tokens": "lots"}).encode()) == 400
    assert raw(json.dumps({"request_id": "r", "slug": "p", "model": {"x": 1}}).encode()) == 400
    assert raw(b"{}", length=-1) == 400
    assert raw(b"{}", length=10_000_000) == 413
    assert raw(b"{}", token="tòken") == 401
    srv.shutdown()


# ---------- prices by (provider, model) ----------

TWO_PROVIDERS = [
    {"model": "open-model", "provider": "Groq", "input": 1.0, "output": 1.0, "effective_from": "2026-01-01"},
    {"model": "open-model", "provider": "together", "input": 5.0, "output": 5.0, "effective_from": "2026-01-01"},
    {"model": "open-model", "input": 9.0, "output": 9.0, "effective_from": "2026-01-01"},
    {"model": "open-model", "provider": "Groq", "input": 2.0, "output": 2.0, "effective_from": "2026-06-01"},
]


def test_price_same_model_two_providers():
    idx = P.parse(TWO_PROVIDERS)
    assert P.pick(idx, "open-model", "2026-03-01", "groq")["input"] == 1.0
    assert P.pick(idx, "open-model", "2026-07-01", "Groq")["input"] == 2.0  # date logic kept per provider
    assert P.pick(idx, "open-model", "2026-07-01", "Together AI")["input"] == 5.0  # normalised on both sides
    assert P.pick(idx, "open-model", "2026-07-01", "Fireworks")["input"] == 9.0  # provider-less entry as fallback
    gm.log_usage("p", "Groq", "open-model", input_tokens=1_000_000, output_tokens=0)
    gm.log_usage("p", "Together", "open-model", input_tokens=1_000_000, output_tokens=0)
    month = gm._now()[:7]
    cost = {r["provider"]: r["cost_usd"] for r in R.monthly(month, idx)}
    assert cost["Together"] == 5.0 and cost["Groq"] in (1.0, 2.0)


def test_price_legacy_list_without_provider():
    idx = P.parse([{"model": "m", "input": 1.0, "output": 2.0, "effective_from": "2026-01-01"},
                   {"model": "m", "input": 3.0, "output": 4.0, "effective_from": "2026-06-01"}])
    assert P.pick(idx, "m", "2026-03-01", "OpenAI")["input"] == 1.0
    assert P.pick(idx, "m", "2026-07-01", "Anthropic")["input"] == 3.0
    assert P.pick(idx, "m", "2025-01-01")["input"] == 1.0  # older than the first entry: first entry
    assert P.pick(idx, "unknown", "2026-07-01", "OpenAI") is None


def test_price_other_provider_only_falls_back_to_model():
    idx = P.parse([{"model": "m", "provider": "OpenAI", "input": 1.0, "output": 2.0, "effective_from": "2026-01-01"}])
    assert P.pick(idx, "m", "2026-03-01", "Azure OpenAI")["input"] == 1.0  # model-only behaviour, as before
    gm.log_usage("p", "Azure OpenAI", "m", input_tokens=10, output_tokens=0)
    assert R.unpriced(gm._now()[:7], idx) == []


# ---------- connection reuse ----------

def test_connect_initialises_once_per_path(tmp_path, monkeypatch):
    calls = []
    real = gm._init_db
    monkeypatch.setattr(gm, "_init_db", lambda p: calls.append(p) or real(p))
    for _ in range(3):
        gm.log_usage("p", "OpenAI", "model-a", input_tokens=1, output_tokens=1)
    assert len(calls) == 1 and len(rows()) == 3
    other = tmp_path / "other" / "usage.db"
    monkeypatch.setenv("GIUGGIAI_METER_DB", str(other))
    gm.log_usage("p", "OpenAI", "model-a", input_tokens=1, output_tokens=1)
    assert len(calls) == 2 and len(rows()) == 1
    assert stat.S_IMODE(os.stat(other).st_mode) == 0o600


def test_connect_reinitialises_a_deleted_database(tmp_path):
    p = str(tmp_path / "gone.db")
    gm.connect(p).close()
    os.remove(p)
    with gm.connect(p) as c:  # file removed after init: schema is created again
        assert c.execute("SELECT count(*) FROM usage").fetchone() == (0,)


def test_connect_is_thread_safe(tmp_path):
    p = str(tmp_path / "threads.db")
    errors = []

    def work():
        try:
            for _ in range(5):
                with gm.connect(p) as c:
                    c.execute("SELECT count(*) FROM usage").fetchone()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
