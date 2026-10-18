"""Who bills a call: provider names, and how they are recognised from a client or a model id.

Extend by adding entries to the dicts below (insertion order is the matching order: first hit wins).

    ALIASES        lower-case spelling -> display name (normalize())
    HOSTS          regex on the API host (or host:port) -> provider (from_base_url())
    CLIENT_HINTS   substring of "module.ClassName" of the client, lower case -> provider (from_client())
    MODEL_PREFIXES regex on the model id -> provider (from_model())
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

ALIASES: dict[str, str] = {
    "openai": "OpenAI", "anthropic": "Anthropic", "deepseek": "DeepSeek",
    "xai": "xAI", "grok": "xAI", "x.ai": "xAI",
    "alibabaqwen": "Alibaba Qwen", "alibaba": "Alibaba Qwen", "qwen": "Alibaba Qwen", "dashscope": "Alibaba Qwen",
    "google": "Google", "gemini": "Google", "googlegemini": "Google", "vertex": "Google", "vertexai": "Google",
    "mistral": "Mistral", "mistralai": "Mistral",
    "groq": "Groq", "together": "Together", "togetherai": "Together", "fireworks": "Fireworks", "fireworksai": "Fireworks",
    "openrouter": "OpenRouter", "perplexity": "Perplexity",
    "moonshot": "Moonshot", "kimi": "Moonshot", "moonshotai": "Moonshot",
    "zhipu": "Zhipu", "zhipuai": "Zhipu", "glm": "Zhipu", "zai": "Zhipu", "z.ai": "Zhipu", "bigmodel": "Zhipu",
    "cohere": "Cohere", "azure": "Azure OpenAI", "azureopenai": "Azure OpenAI",
    "ollama": "Ollama", "bedrock": "AWS Bedrock", "aws": "AWS Bedrock", "awsbedrock": "AWS Bedrock",
    "amazonbedrock": "AWS Bedrock", "meta": "Meta",
}

HOSTS: dict[str, str] = {
    r"(^|\.)openai\.azure\.com$": "Azure OpenAI",
    r"(^|\.)api\.openai\.com$": "OpenAI",
    r"(^|\.)anthropic\.com$": "Anthropic",
    r"(^|\.)deepseek\.com$": "DeepSeek",
    r"(^|\.)x\.ai$": "xAI",
    r"^dashscope.*\.aliyuncs\.com$": "Alibaba Qwen",
    r"(^|\.)generativelanguage\.googleapis\.com$": "Google",
    r"aiplatform\.googleapis\.com$": "Google",
    r"(^|\.)mistral\.ai$": "Mistral",
    r"(^|\.)groq\.com$": "Groq",
    r"(^|\.)together\.(xyz|ai)$": "Together",
    r"(^|\.)fireworks\.ai$": "Fireworks",
    r"(^|\.)openrouter\.ai$": "OpenRouter",
    r"(^|\.)perplexity\.ai$": "Perplexity",
    r"^api\.moonshot\.": "Moonshot",
    r"(^|\.)bigmodel\.cn$": "Zhipu",
    r"(^|\.)z\.ai$": "Zhipu",
    r"^api\.cohere\.": "Cohere",
    r"bedrock-runtime": "AWS Bedrock",
    r":11434$": "Ollama",
}

CLIENT_HINTS: dict[str, str] = {
    "azureopenai": "Azure OpenAI",
    "bedrock": "AWS Bedrock",
    "vertex": "Google",
    "google.genai": "Google",
    "google.generativeai": "Google",
    "anthropic": "Anthropic",
    "mistral": "Mistral",
    "cohere": "Cohere",
    "ollama": "Ollama",
    "groq": "Groq",
    "together": "Together",
    "fireworks": "Fireworks",
    "openai": "OpenAI",
}

MODEL_PREFIXES: dict[str, str] = {
    r"^(gpt-|o1|o3|o4|chatgpt-|text-embedding-|davinci|babbage|whisper|tts-|dall-e)": "OpenAI",
    r"^claude": "Anthropic",
    r"^(gemini|gemma)": "Google",
    r"^grok": "xAI",
    r"^deepseek": "DeepSeek",
    r"^(qwen|qwq)": "Alibaba Qwen",
    r"^(mistral|codestral|ministral|magistral|pixtral|devstral|open-mistral|open-mixtral)": "Mistral",
    r"^command": "Cohere",
    r"^(kimi|moonshot)": "Moonshot",
    r"^(glm|chatglm)": "Zhipu",
    r"llama": "Meta",  # served by many hosts: the host, when known, wins
}

# the official hosts of the generic SDKs: any other base_url means an OpenAI/Anthropic-compatible third party
_GENERIC = {"OpenAI", "Anthropic"}
# attribute paths where SDK clients keep their endpoint
_URL_ATTRS = ("base_url", "_base_url", "endpoint", "_endpoint", "host", "_host", "server_url", "meta.endpoint_url",
              "_client.base_url", "_client._base_url", "sdk_configuration.server_url", "_api_client._http_options.base_url")

_CANON = {v.lower(): v for v in ALIASES.values()}


def _key(name: str) -> str:
    return re.sub(r"[\s_\-()]+", "", name.strip().lower())


def normalize(name) -> str | None:
    """Display name for a provider spelling ("openai" -> "OpenAI", "grok" -> "xAI"); unknown names are kept as given."""
    if not name:
        return None
    s = str(name).strip()
    low = s.lower()
    return _CANON.get(low) or ALIASES.get(low) or ALIASES.get(_key(s)) or s


def from_base_url(url) -> str | None:
    """Provider from an API base URL or endpoint (None when the host is not in HOSTS)."""
    if not url:
        return None
    u = str(url).strip().lower()
    parts = urlsplit(u if "//" in u else "//" + u)
    host, netloc = parts.hostname or "", parts.netloc.rsplit("@", 1)[-1]
    for rx, prov in HOSTS.items():
        if re.search(rx, host) or re.search(rx, netloc):
            return prov
    return None


def from_model(model) -> str | None:
    """Provider from a model id ("gpt-4.1" -> OpenAI, "us.anthropic.claude-..." -> Anthropic), None when unknown."""
    if not model:
        return None
    m = str(model).strip().lower()
    for cand in dict.fromkeys((m, m.rsplit("/", 1)[-1], m.rsplit(".", 1)[-1])):  # also "vendor/model", "us.vendor.model"
        for rx, prov in MODEL_PREFIXES.items():
            if re.search(rx, cand):
                return prov
    return None


def _attr_path(obj, path: str):
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def client_url(client) -> str | None:
    """The endpoint an SDK client talks to, when it exposes one."""
    for p in _URL_ATTRS:
        try:
            v = _attr_path(client, p)
        except Exception:  # noqa: BLE001 - odd SDK objects: keep looking
            v = None
        s = str(v) if v is not None else ""
        if s and ("." in s or ":" in s):
            return s
    return None


def from_client(client) -> str | None:
    """Provider from a client: its base URL first, then its module/class name.

    A generic SDK (OpenAI, Anthropic) pointed at a host not in HOSTS returns None, so that the model id decides
    at logging time (the class only says which protocol is spoken, not who bills)."""
    url = client_url(client)
    by_url = from_base_url(url)
    if by_url:
        return by_url
    prov = class_hint(client)
    if prov in _GENERIC and url:
        return None
    return prov


def class_hint(client) -> str | None:
    """Provider from the module/class name only (also for a module, e.g. `ollama`)."""
    if type(client).__name__ == "module":
        ident = str(getattr(client, "__name__", ""))
    else:
        ident = f"{type(client).__module__}.{type(client).__name__}"
    ident = ident.lower()
    return next((p for h, p in CLIENT_HINTS.items() if h in ident), None)
