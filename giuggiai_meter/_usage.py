"""Token usage from any common LLM response, SDK object or raw JSON dict, recognised by its shape."""
from __future__ import annotations

from . import providers

# shape -> (default provider, the shape alone identifies the host?)
SHAPES = {
    "gemini": ("Google", True),             # usage_metadata / usageMetadata
    "bedrock": ("AWS Bedrock", True),       # Converse: usage.inputTokens / outputTokens
    "cohere": ("Cohere", True),             # meta.billed_units / usage.billed_units
    "ollama": ("Ollama", True),             # prompt_eval_count / eval_count
    "dashscope": ("Alibaba Qwen", True),    # native DashScope: output + request_id + usage.input_tokens
    "openai-chat": (None, False),           # usage.prompt_tokens / completion_tokens (every OpenAI-compatible API)
    "openai-responses": ("OpenAI", False),  # Responses API: usage.input_tokens + input_tokens_details
    "anthropic": ("Anthropic", False),      # Messages API: usage.input_tokens + cache_*_input_tokens
}


def _get(obj, *names):
    """First non-None field among names, from an object attribute or a dict key."""
    if obj is None:
        return None
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return None


def _n(obj, *names) -> int:
    v = _get(obj, *names)
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


# Shapes whose input total already includes the cached tokens (OpenAI-style). Stored convention everywhere:
# input_tokens = UNCACHED input, cache_read_tokens = cached input, so each token is priced exactly once.
_INPUT_INCLUDES_CACHE = {"gemini", "openai-chat", "openai-responses", "dashscope"}


def extract(r) -> dict | None:
    """{shape, input_tokens (uncached), output_tokens, cache_creation_tokens, cache_read_tokens, model, id} or None."""
    u = _extract(r)
    if u and u["shape"] in _INPUT_INCLUDES_CACHE:
        u["input_tokens"] = max(0, u["input_tokens"] - u["cache_read_tokens"])
    return u


def _extract(r) -> dict | None:
    um = _get(r, "usage_metadata", "usageMetadata")
    if um is not None:  # Google Gemini (google-genai, google-generativeai, REST JSON)
        return {"shape": "gemini", "input_tokens": _n(um, "prompt_token_count", "promptTokenCount"),
                "output_tokens": _n(um, "candidates_token_count", "candidatesTokenCount")
                + _n(um, "thoughts_token_count", "thoughtsTokenCount"),
                "cache_creation_tokens": 0,
                "cache_read_tokens": _n(um, "cached_content_token_count", "cachedContentTokenCount"),
                "model": _get(r, "model_version", "modelVersion"), "id": _get(r, "response_id", "responseId")}
    u = _get(r, "usage")
    if u is not None and _get(u, "inputTokens", "outputTokens") is not None:  # Amazon Bedrock Converse
        return {"shape": "bedrock", "input_tokens": _n(u, "inputTokens"), "output_tokens": _n(u, "outputTokens"),
                "cache_creation_tokens": _n(u, "cacheWriteInputTokens"), "cache_read_tokens": _n(u, "cacheReadInputTokens"),
                "model": _get(r, "modelId", "model"), "id": _get(_get(r, "ResponseMetadata"), "RequestId")}
    billed = _get(_get(r, "meta"), "billed_units") or _get(u, "billed_units")
    if billed is not None:  # Cohere v1 (meta.billed_units) and v2 (usage.billed_units)
        return {"shape": "cohere", "input_tokens": _n(billed, "input_tokens"), "output_tokens": _n(billed, "output_tokens"),
                "cache_creation_tokens": 0, "cache_read_tokens": 0, "model": _get(r, "model"),
                "id": _get(r, "id", "generation_id", "response_id")}
    if _get(r, "prompt_eval_count", "eval_count") is not None:  # Ollama native API
        return {"shape": "ollama", "input_tokens": _n(r, "prompt_eval_count"), "output_tokens": _n(r, "eval_count"),
                "cache_creation_tokens": 0, "cache_read_tokens": 0, "model": _get(r, "model"), "id": None}
    if u is None:
        return None
    model, rid = _get(r, "model"), _get(r, "id")
    if _get(u, "prompt_tokens", "completion_tokens") is not None:  # OpenAI Chat Completions and compatibles
        details = _get(u, "prompt_tokens_details")
        return {"shape": "openai-chat", "input_tokens": _n(u, "prompt_tokens"), "output_tokens": _n(u, "completion_tokens"),
                "cache_creation_tokens": 0,
                "cache_read_tokens": _n(details, "cached_tokens") or _n(u, "prompt_cache_hit_tokens"),
                "model": model, "id": rid}
    if _get(u, "input_tokens", "output_tokens") is None:
        return None
    if _get(u, "input_tokens_details") is not None or _get(r, "object") == "response":  # OpenAI Responses API
        return {"shape": "openai-responses", "input_tokens": _n(u, "input_tokens"), "output_tokens": _n(u, "output_tokens"),
                "cache_creation_tokens": 0, "cache_read_tokens": _n(_get(u, "input_tokens_details"), "cached_tokens"),
                "model": model, "id": rid}
    if _get(r, "type") != "message" and _get(r, "output") is not None and _get(r, "request_id") is not None:
        return {"shape": "dashscope", "input_tokens": _n(u, "input_tokens"), "output_tokens": _n(u, "output_tokens"),
                "cache_creation_tokens": 0, "cache_read_tokens": _n(_get(u, "prompt_tokens_details"), "cached_tokens"),
                "model": model, "id": _get(r, "request_id")}
    return {"shape": "anthropic", "input_tokens": _n(u, "input_tokens"), "output_tokens": _n(u, "output_tokens"),
            "cache_creation_tokens": _n(u, "cache_creation_input_tokens"),
            "cache_read_tokens": _n(u, "cache_read_input_tokens"), "model": model, "id": rid}


def resolve_provider(explicit, model, shape: str) -> str | None:
    """explicit > shape that identifies the host (Gemini, Bedrock, Cohere, Ollama, DashScope) > model id > shape default."""
    if explicit:
        return providers.normalize(explicit)
    default, host_specific = SHAPES.get(shape, (None, False))
    if host_specific:
        return default
    return providers.from_model(model) or default
