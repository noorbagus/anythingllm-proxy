"""csmart.routing.model — pure model resolution, routing, endpoint detection.

Extracted verbatim from csmart_proxy.py (Track A: 276-360, 1128-1230). No DB,
no streaming, no csmart.* import beyond csmart.app.config. Keeps tool schemas
used by align_prefix_3_region (EXPAND/WEBSEARCH) so the module is self-contained.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from any_proxy.app.config import (
    ANTHROPIC_NATIVE_MODEL_PATTERNS,
    _COMPLEX_TRIGGERS,
    FLAGSHIP_MODEL,
    FLASH_MODEL,
    OPENAI_API_KEY,
    UPSTREAM_API_KEY,
    OPENAI_MODEL_MAP,
    OPENAI_MODEL_PATTERNS,
    OPENAI_RESPONSES_MODEL_PATTERNS,
)

# =====================================================================
# TOOL SCHEMAS (used by align_prefix_3_region)
# =====================================================================
EXPAND_TOOL_SCHEMA: Dict[str, Any] = {
    "name": "csmart_expand_symbol",
    "description": "Mengambil isi payload/file utuh yang sebelumnya dipadatkan oleh proxy csmart CCR.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ref_id": {"type": "string", "description": "ID referensi context, e.g. 'ref_8a1f4b2c'"}
        },
        "required": ["ref_id"],
    },
}

# Tool web search yang DIEKSEKUSI PROXY (bukan tool WebSearch bawaan Claude Code,
# yang tidak di-expose ke model pihak ketiga). Proxy jalan di host dengan akses
# internet penuh -> hasil di-feed balik sebagai tool_result ke model.
WEBSEARCH_TOOL_SCHEMA: Dict[str, Any] = {
    "name": "csmart_websearch",
    "description": "Cari informasi terkini dari internet (dieksekusi oleh proxy). Query harus ringkas dan spesifik, dalam bahasa Inggris bila mungkin.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query untuk dicari di web."}
        },
        "required": ["query"],
    },
}


# =====================================================================
# MODEL MATCHING
# =====================================================================
def _model_matches_alias(model, alias):
    _ml = model.lower()
    _al = alias.lower()
    if "*" in _al:
        _prefix = _al.replace("*", "")
        return _ml.startswith(_prefix) or _ml == _prefix.rstrip("-") or _prefix in _ml
    if _al.endswith("-"):
        return _al in _ml or _ml.startswith(_al)
    return _ml == _al or _al in _ml or _ml.startswith(_al)


def clean_openai_model_name(model_name):
    if not model_name:
        return model_name
    if "/" in model_name:
        if model_name.lower().startswith("opencode-go/"):
            return model_name.split("/", 1)[-1]
        return model_name.rsplit("/", 1)[-1]
    return model_name


def is_openai_model(model_name):
    if not model_name:
        return False
    _cleaned = clean_openai_model_name(model_name)
    _lower = _cleaned.lower()
    for _alias in OPENAI_MODEL_MAP:
        if _model_matches_alias(_cleaned, _alias):
            return True
    if any(pat.lower() in _lower for pat in OPENAI_MODEL_PATTERNS):
        return True
    if any(pat.lower() in _lower for pat in OPENAI_RESPONSES_MODEL_PATTERNS):
        return True
    if any(pat.lower() in _lower for pat in ANTHROPIC_NATIVE_MODEL_PATTERNS):
        return True
    return False


def resolve_openai_endpoint(model):
    if not model:
        return "chat_completions"
    _cleaned = clean_openai_model_name(model).lower()
    _orig_lower = model.lower()
    for _alias, _info in OPENAI_MODEL_MAP.items():
        if _model_matches_alias(_cleaned, _alias) or _model_matches_alias(_orig_lower, _alias):
            _et = str(_info.get("endpoint_type", "")).strip().lower()
            if _et in ("responses", "response"):
                return "responses"
            if _et in ("messages", "message", "anthropic"):
                return "messages"
            return "chat_completions"
    if any(pat.lower() in _cleaned for pat in ANTHROPIC_NATIVE_MODEL_PATTERNS):
        return "messages"
    if any(pat.lower() in _cleaned for pat in OPENAI_RESPONSES_MODEL_PATTERNS):
        return "responses"
    if "response" in _cleaned or "responses" in _cleaned:
        return "responses"
    return "chat_completions"


def _openai_upstream_headers(request):
    """Always use server key (UPSTREAM/OPENAI_API_KEY), ignore incoming Authorization.

    AnythingLLM Generic OpenAI sering kirim dummy Bearer (sk-anythingllm/sk-ollama)
    yang kalau di-forward ke opencode jadi 401 Invalid API key. Server key
    67 char sk-mGpHz*** dari .env.local sudah terbukti 200. Incoming tetap di-log
    (redacted) untuk audit per trace_id.
    """
    _server_key = (UPSTREAM_API_KEY or OPENAI_API_KEY or "").strip()
    _auth = ""
    try:
        _auth = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    except Exception:
        _auth = ""
    if _auth and str(_auth).strip():
        _bearer = str(_auth).strip()
        if not _bearer.lower().startswith("bearer "):
            _bearer = f"Bearer {_bearer}"
        try:
            from any_proxy.logging.structured import _log, get_trace_id
            from any_proxy.security.secrets import _redact
            _log(
                "OPENAI_INCOMING_AUTH",
                incoming_auth=_redact(_bearer),
                used="server_key_ignored_incoming",
                outgoing_len=len(_server_key),
                outgoing_prefix=_redact(_server_key[:12]),
                trace_id=get_trace_id(),
            )
        except Exception:
            pass
        return {
            "Authorization": f"Bearer {_server_key}",
            "Content-Type": "application/json",
        }
    try:
        from any_proxy.logging.structured import _log, get_trace_id
        from any_proxy.security.secrets import _redact
        _log(
            "OPENAI_INCOMING_AUTH",
            incoming_auth="(none)",
            used="server_key",
            outgoing_len=len(_server_key),
            outgoing_prefix=_redact(_server_key[:12]),
            trace_id=get_trace_id(),
        )
    except Exception:
        pass
    return {
        "Authorization": f"Bearer {_server_key}",
        "Content-Type": "application/json",
    }


def is_anthropic_native_model(model_name: str) -> bool:
    """Detect models served by the Anthropic-compatible /messages endpoint
    (OpenCode Go: minimax-m*, qwen3.*). These are Anthropic-native - no OpenAI
    protocol transform, model name preserved verbatim, raw SSE passthrough.
    Takes precedence over ``is_openai_model`` (a model is one or the other)."""
    lower_name = model_name.lower()
    return any(pattern.lower() in lower_name for pattern in ANTHROPIC_NATIVE_MODEL_PATTERNS)


def detect_openai_endpoint_type(model_name: str) -> str:
    """Detect which OpenAI endpoint to use (chat_completions or responses).
    Thin wrapper over resolve_openai_endpoint (Track A) - preserves existing call sites."""
    _rt = resolve_openai_endpoint(model_name)
    if _rt == "messages":
        return "chat_completions"
    return _rt


# =====================================================================
# 4. 3-REGION PREFIX ALIGNER
# =====================================================================
def align_prefix_3_region(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Sort tools deterministically, register expand tool, stamp cache marker on
    the last immutable-prefix block. Deterministic for byte-identical cache."""
    system_prompts = payload.get("system", [])
    tools = list(payload.get("tools", []))
    messages = payload.get("messages", [])

    names = [t.get("name") for t in tools if isinstance(t, dict)]
    if "csmart_expand_symbol" not in names:
        tools.append(EXPAND_TOOL_SCHEMA)
    if "csmart_websearch" not in names:
        tools.append(WEBSEARCH_TOOL_SCHEMA)
    tools = sorted(tools, key=lambda t: t.get("name", ""))

    if tools:
        for t in tools:
            if isinstance(t, dict):
                t.pop("cache_control", None)
        if isinstance(tools[-1], dict):
            tools[-1]["cache_control"] = {"type": "ephemeral"}
    elif isinstance(system_prompts, list) and system_prompts:
        for s in system_prompts:
            if isinstance(s, dict):
                s.pop("cache_control", None)
        if isinstance(system_prompts[-1], dict):
            system_prompts[-1]["cache_control"] = {"type": "ephemeral"}

    payload["system"] = system_prompts
    payload["tools"] = tools
    payload["messages"] = messages
    return payload


# =====================================================================
# 5. HEURISTIC MODEL ROUTER (Flash vs Flagship, pinned per session)
# =====================================================================
_session_model: Dict[str, Tuple[str, float]] = {}


def _extract_last_text(payload: Dict[str, Any]) -> str:
    messages = payload.get("messages", [])
    if not messages:
        return ""
    last = messages[-1]
    content = last.get("content", "")
    if isinstance(content, str):
        return content
    parts: List[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("text", "input_text") and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "tool_result":
                c = block.get("content")
                if isinstance(c, str):
                    parts.append(c)
    return "\n".join(parts)


def route_model_tier(payload: Dict[str, Any], session_key: str) -> str:
    """Pick a model for this request. Pinned per session (cache stability)."""
    now = time.time()
    cached = _session_model.get(session_key)
    if cached and now - cached[1] < 3600:
        return cached[0]
    text = _extract_last_text(payload).lower()
    model = FLAGSHIP_MODEL if any(t in text for t in _COMPLEX_TRIGGERS) else FLASH_MODEL
    _session_model[session_key] = (model, now)
    return model


__all__ = [
    "EXPAND_TOOL_SCHEMA",
    "WEBSEARCH_TOOL_SCHEMA",
    "_model_matches_alias",
    "clean_openai_model_name",
    "is_openai_model",
    "resolve_openai_endpoint",
    "_openai_upstream_headers",
    "is_anthropic_native_model",
    "detect_openai_endpoint_type",
    "align_prefix_3_region",
    "route_model_tier",
    "_extract_last_text",
]
