"""log_response shapes, provider recognition and wrap() on non-OpenAI SDKs, with fake responses (dicts and objects)."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

import giuggiai_meter as gm
from giuggiai_meter import providers


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIUGGIAI_METER_DB", str(tmp_path / "usage.db"))
    monkeypatch.delenv("GIUGGIAI_METER_URL", raising=False)
    monkeypatch.delenv("GIUGGIAI_METER_TOKEN", raising=False)
    p = tmp_path / "prices.json"
    p.write_text(json.dumps([]))
    monkeypatch.setenv("GIUGGIAI_METER_PRICES", str(p))


def rows():
    with gm.connect() as c:
        return c.execute("SELECT slug, provider, model, input_tokens, output_tokens, cache_creation_tokens, "
                         "cache_read_tokens FROM usage ORDER BY rowid").fetchall()


def obj(d):
    """Recursively turn a dict into attribute objects (like SDK response classes)."""
    if isinstance(d, dict):
        return NS(**{k: obj(v) for k, v in d.items()})
    return d


# (name, response as raw JSON dict, expected (provider, model, input, output, cache_write, cache_read))
SHAPES = [
    ("openai-chat", {"id": "c1", "model": "gpt-4.1-mini", "usage": {"prompt_tokens": 50, "completion_tokens": 7,
                                                                    "prompt_tokens_details": {"cached_tokens": 10}}},
     ("OpenAI", "gpt-4.1-mini", 40, 7, 0, 10)),
    ("deepseek-chat", {"id": "c2", "model": "deepseek-chat", "usage": {"prompt_tokens": 30, "completion_tokens": 3,
                                                                       "prompt_cache_hit_tokens": 12}},
     ("DeepSeek", "deepseek-chat", 18, 3, 0, 12)),
    ("grok", {"id": "c3", "model": "grok-4", "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
     ("xAI", "grok-4", 9, 2, 0, 0)),
    ("openai-responses", {"id": "r1", "object": "response", "model": "o4-mini",
                          "usage": {"input_tokens": 40, "output_tokens": 9, "input_tokens_details": {"cached_tokens": 4}}},
     ("OpenAI", "o4-mini", 36, 9, 0, 4)),
    ("anthropic", {"id": "m1", "type": "message", "model": "claude-sonnet-4-5",
                   "usage": {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 5,
                             "cache_read_input_tokens": 30}},
     ("Anthropic", "claude-sonnet-4-5", 100, 20, 5, 30)),
    ("gemini-snake", {"response_id": "g1", "model_version": "gemini-2.5-flash",
                      "usage_metadata": {"prompt_token_count": 70, "candidates_token_count": 11,
                                         "thoughts_token_count": 4, "cached_content_token_count": 20}},
     ("Google", "gemini-2.5-flash", 50, 15, 0, 20)),
    ("gemini-camel", {"responseId": "g2", "modelVersion": "gemini-2.5-pro",
                      "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 2, "thoughtsTokenCount": 1}},
     ("Google", "gemini-2.5-pro", 8, 3, 0, 0)),
    ("cohere-v2", {"id": "co1", "usage": {"billed_units": {"input_tokens": 12, "output_tokens": 6},
                                          "tokens": {"input_tokens": 200, "output_tokens": 6}}},
     ("Cohere", None, 12, 6, 0, 0)),
    ("cohere-v1", {"generation_id": "co2", "meta": {"billed_units": {"input_tokens": 5, "output_tokens": 1}}},
     ("Cohere", None, 5, 1, 0, 0)),
    ("bedrock", {"ResponseMetadata": {"RequestId": "b1"}, "output": {"message": {}},
                 "usage": {"inputTokens": 21, "outputTokens": 4, "cacheReadInputTokens": 3, "cacheWriteInputTokens": 2}},
     ("AWS Bedrock", None, 21, 4, 2, 3)),
    ("ollama", {"model": "llama3.2", "prompt_eval_count": 26, "eval_count": 290},
     ("Ollama", "llama3.2", 26, 290, 0, 0)),
    ("dashscope", {"request_id": "q1", "output": {"text": "hi"}, "usage": {"input_tokens": 14, "output_tokens": 5}},
     ("Alibaba Qwen", None, 14, 5, 0, 0)),
    ("mistral", {"id": "mi1", "model": "mistral-large-latest", "usage": {"prompt_tokens": 16, "completion_tokens": 4}},
     ("Mistral", "mistral-large-latest", 16, 4, 0, 0)),
]


@pytest.mark.parametrize("as_object", [False, True], ids=["dict", "object"])
@pytest.mark.parametrize("name,resp,expected", SHAPES, ids=[s[0] for s in SHAPES])
def test_log_response_shapes(name, resp, expected, as_object):
    r = obj(resp) if as_object else resp
    assert gm.log_response(f"p-{name}", r) is not None
    assert rows() == [(f"p-{name}",) + expected]


def test_log_response_explicit_provider_and_model():
    gm.log_response("p", {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}, provider="together", model="m")
    assert rows() == [("p", "Together", "m", 1, 1, 0, 0)]


def test_log_response_no_usage_and_garbage_never_raise(capsys):
    assert gm.log_response("p", {"choices": []}) is None
    assert gm.log_response("p", None) is None
    assert gm.log_response("p", {"usage": {"prompt_tokens": "lots"}}) is not None  # bad numbers count as 0
    assert "no token usage" in capsys.readouterr().err


def test_aliases():
    u = {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    gm.log_grok("a", {"id": "1", **u})
    gm.log_qwen("b", {"id": "2", **u})
    gm.log_mistral("c", {"id": "3", **u})
    gm.log_gemini("d", {"usageMetadata": {"promptTokenCount": 1}})
    gm.log_cohere("e", {"meta": {"billed_units": {"input_tokens": 1}}})
    gm.log_bedrock("f", {"usage": {"inputTokens": 1, "outputTokens": 1}}, model="anthropic.claude-x")
    gm.log_ollama("g", {"prompt_eval_count": 1, "eval_count": 1})
    assert [(r[0], r[1]) for r in rows()] == [("a", "xAI"), ("b", "Alibaba Qwen"), ("c", "Mistral"), ("d", "Google"),
                                              ("e", "Cohere"), ("f", "AWS Bedrock"), ("g", "Ollama")]
    assert rows()[5][2] == "anthropic.claude-x"


# ---------- provider recognition ----------

@pytest.mark.parametrize("url,expected", [
    ("https://api.openai.com/v1", "OpenAI"),
    ("https://api.anthropic.com", "Anthropic"),
    ("https://api.deepseek.com", "DeepSeek"),
    ("https://api.x.ai/v1", "xAI"),
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "Alibaba Qwen"),
    ("https://generativelanguage.googleapis.com/v1beta/openai/", "Google"),
    ("https://us-central1-aiplatform.googleapis.com", "Google"),
    ("https://api.mistral.ai/v1", "Mistral"),
    ("https://api.groq.com/openai/v1", "Groq"),
    ("https://api.together.xyz/v1", "Together"),
    ("https://api.fireworks.ai/inference/v1", "Fireworks"),
    ("https://openrouter.ai/api/v1", "OpenRouter"),
    ("https://api.perplexity.ai", "Perplexity"),
    ("https://api.moonshot.ai/v1", "Moonshot"),
    ("https://open.bigmodel.cn/api/paas/v4/", "Zhipu"),
    ("https://api.z.ai/api/paas/v4/", "Zhipu"),
    ("https://api.cohere.com/compatibility/v1", "Cohere"),
    ("https://my-resource.openai.azure.com/", "Azure OpenAI"),
    ("http://localhost:11434/v1", "Ollama"),
    ("https://bedrock-runtime.us-east-1.amazonaws.com", "AWS Bedrock"),
    ("https://llm.example.com/v1", None),
])
def test_provider_from_base_url(url, expected):
    assert providers.from_base_url(url) == expected


@pytest.mark.parametrize("model,expected", [
    ("gpt-4.1", "OpenAI"), ("o3-mini", "OpenAI"), ("o1", "OpenAI"), ("claude-opus-4-1", "Anthropic"),
    ("gemini-2.5-pro", "Google"), ("grok-4", "xAI"), ("deepseek-reasoner", "DeepSeek"), ("qwen-max", "Alibaba Qwen"),
    ("mistral-small-latest", "Mistral"), ("codestral-latest", "Mistral"), ("ministral-8b", "Mistral"),
    ("command-r-plus", "Cohere"), ("llama-3.3-70b-versatile", "Meta"), ("kimi-k2", "Moonshot"),
    ("moonshot-v1-8k", "Moonshot"), ("glm-4.5", "Zhipu"), ("anthropic/claude-3.5-sonnet", "Anthropic"),
    ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", "Anthropic"), ("my-finetune", None), (None, None),
])
def test_provider_from_model(model, expected):
    assert providers.from_model(model) == expected


def test_normalize():
    assert providers.normalize("openai") == "OpenAI"
    assert providers.normalize("Grok") == "xAI"
    assert providers.normalize("z.ai") == "Zhipu"
    assert providers.normalize("AWS Bedrock") == "AWS Bedrock"
    assert providers.normalize("Alibaba Qwen") == "Alibaba Qwen"
    assert providers.normalize("My Gateway") == "My Gateway"
    assert providers.normalize("") is None


def test_unknown_host_with_openai_sdk_uses_the_model():
    class OpenAI:
        base_url = "https://llm.example.com/v1"

        def __init__(self):
            self.chat = NS(completions=NS(create=lambda **kw: {"id": kw["model"], "model": kw["model"],
                                                                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))

    c = gm.wrap(OpenAI(), project="gw")
    c.chat.completions.create(model="qwen-plus")
    c.chat.completions.create(model="my-finetune")
    assert [r[1] for r in rows()] == ["Alibaba Qwen", "OpenAI"]  # model prefix, then the SDK name


# ---------- wrap() on other SDKs ----------

class _GenaiModels:
    def generate_content(self, model, contents, config=None):
        return obj({"response_id": "g-" + contents, "model_version": model,
                    "usage_metadata": {"prompt_token_count": 7, "candidates_token_count": 3}})

    def generate_content_stream(self, model, contents):
        return iter(["part"])


class _AsyncGenaiModels:
    async def generate_content(self, model, contents):
        return obj({"usage_metadata": {"prompt_token_count": 2, "candidates_token_count": 1}})


class Client:  # google.genai.Client stand-in: provider must come from __module__
    __module__ = "google.genai.client"

    def __init__(self):
        self.models = _GenaiModels()
        self.aio = NS(models=_AsyncGenaiModels())


def test_wrap_google_genai_sync_async_and_stream():
    c = gm.wrap(Client(), project="gem")
    assert c.models.generate_content(model="gemini-2.5-flash", contents="hi").response_id == "g-hi"
    asyncio.run(c.aio.models.generate_content(model="gemini-2.5-pro", contents="x"))
    assert list(c.models.generate_content_stream(model="gemini-2.5-flash", contents="s")) == ["part"]
    assert rows() == [("gem", "Google", "gemini-2.5-flash", 7, 3, 0, 0), ("gem", "Google", "gemini-2.5-pro", 2, 1, 0, 0),
                      ("gem", "Google", "gemini-2.5-flash", 4, 1, 0, 0)]  # the stream sent no usage: estimate


class Mistral:
    def __init__(self):
        self.chat = NS(complete=lambda **kw: obj({"id": "mi", "model": kw["model"],
                                                  "usage": {"prompt_tokens": 5, "completion_tokens": 2}}),
                       stream=lambda **kw: iter(["e"]))


class ClientV2:
    __module__ = "cohere.client_v2"

    def chat(self, model, messages):
        return obj({"id": "co", "usage": {"billed_units": {"input_tokens": 4, "output_tokens": 2}}})


class BedrockRuntime:  # boto3 builds this class at runtime
    __module__ = "botocore.client"

    def __init__(self):
        self.meta = NS(endpoint_url="https://bedrock-runtime.eu-west-1.amazonaws.com")

    def converse(self, modelId, messages):
        return {"ResponseMetadata": {"RequestId": "rq"}, "usage": {"inputTokens": 9, "outputTokens": 3}}

    def converse_stream(self, modelId, messages):
        return {"stream": iter([])}


class OllamaClient:
    __module__ = "ollama._client"

    def chat(self, model, messages, stream=False):
        if stream:
            return iter(["tok"])
        return {"model": model, "prompt_eval_count": 6, "eval_count": 8}

    def generate(self, model, prompt):
        return {"model": model, "prompt_eval_count": 1, "eval_count": 2}


def test_wrap_mistral_cohere_bedrock_ollama():
    assert gm.wrap(Mistral(), project="mi").chat.complete(model="mistral-small-latest", messages=[]).id == "mi"
    assert list(gm.wrap(Mistral(), project="mi").chat.stream(model="m")) == ["e"]
    gm.wrap(ClientV2(), project="co").chat(model="command-a", messages=[])
    br = gm.wrap(BedrockRuntime(), project="br")
    assert br.converse(modelId="anthropic.claude-x", messages=[])["usage"]["inputTokens"] == 9
    assert list(br.converse_stream(modelId="anthropic.claude-x", messages=[])["stream"]) == []
    ol = gm.wrap(OllamaClient(), project="ol")
    ol.chat(model="llama3.2", messages=[])
    ol.generate(model="qwen3", prompt="hi")
    assert list(ol.chat(model="llama3.2", messages=[], stream=True)) == ["tok"]
    # streams without usage in their events are recorded as estimates (request body and streamed text / 4)
    assert rows() == [("mi", "Mistral", "mistral-small-latest", 5, 2, 0, 0), ("mi", "Mistral", "m", 0, 0, 0, 0),
                      ("co", "Cohere", "command-a", 4, 2, 0, 0),
                      ("br", "AWS Bedrock", "anthropic.claude-x", 9, 3, 0, 0),
                      ("br", "AWS Bedrock", "anthropic.claude-x", 4, 0, 0, 0), ("ol", "Ollama", "llama3.2", 6, 8, 0, 0),
                      ("ol", "Ollama", "qwen3", 1, 2, 0, 0), ("ol", "Ollama", "llama3.2", 4, 0, 0, 0)]


def test_wrap_module_like_ollama():
    import types

    mod = types.ModuleType("ollama")
    mod.chat = lambda model, messages: {"model": model, "prompt_eval_count": 3, "eval_count": 4}
    gm.wrap(mod, project="m").chat(model="gemma3", messages=[])
    assert rows() == [("m", "Ollama", "gemma3", 3, 4, 0, 0)]


def test_cached_tokens_priced_once():
    """OpenAI-style totals include cached tokens: stored input is the uncached part, so cost counts each token once."""
    from giuggiai_meter import prices as P
    from giuggiai_meter import report as R
    idx = P.parse([{"model": "m", "provider": "OpenAI", "input": 1.0, "output": 0, "cache_read": 0.1,
                    "effective_from": "2026-01-01"}])
    gm.log_response("p", {"id": "x", "model": "m", "created": 0, "usage": {
        "prompt_tokens": 1_000_000, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 400_000}}},
        provider="OpenAI")
    import datetime
    rep = R.monthly(datetime.date.today().strftime("%Y-%m"), idx)
    assert rep[0]["cost_usd"] == 0.64  # 600k uncached x 1.0 + 400k cached x 0.1
