"""csmart.app.config — pure env layer (verbatim from csmart_proxy.py:65-273).

REQ: No csmart.* import. All os.getenv keys/defaults verbatim.
DESIGN: One-way: routing/token_limits.py + routing/model.py import from here, not vice versa.
Guard: OPENAI_BASE_URL rstrip("/") anti //v1/v1.
"""
from __future__ import annotations

import json
import os
import re

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

def _load_gateway_env() -> None:
    """Load the PrivateLink gateway env files so ANTHROPIC_AUTH_TOKEN is found
    even when only the proxy script is started (no prior env export)."""
    for path in (
        "/Volumes/Xugab/LAB/PrivateLink/credentials/.env",
        "/Volumes/Xugab/LAB/PrivateLink/.env.local",
    ):
        if load_dotenv is not None and os.path.exists(path):
            load_dotenv(path, override=False)


_load_gateway_env()

# =====================================================================
# CONFIGURATION & CONSTANTS
# =====================================================================
UPSTREAM_BASE_URL = (
    os.getenv("ANTHROPIC_UPSTREAM_URL")
    or os.getenv("UPSTREAM_BASE_URL")
    or "https://api.deepseek.com/anthropic"
)
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY") or os.getenv("OPENAI_API_KEY", "") or os.getenv("ANTHROPIC_AUTH_TOKEN", "")
# OpenAI API key (also used as fallback for UPSTREAM when .env.local holds it)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PROXY_HOST = os.getenv("CSMART_HOST", "127.0.0.1")
PROXY_PORT = int(os.getenv("CSMART_PORT", "8080"))
DB_PATH = os.getenv("CSMART_DB", "csmart_state.db")

FLASH_MODEL = os.getenv("CSMART_FLASH_MODEL", "deepseek-chat")
FLAGSHIP_MODEL = os.getenv("CSMART_FLAGSHIP_MODEL", "deepseek-reasoner")
UPSTREAM_TIMEOUT = float(os.getenv("CSMART_UPSTREAM_TIMEOUT", "120"))
MAX_TOKENS_FLOOR = int(os.getenv("CSMART_MIN_MAX_TOKENS", "4096"))
MAX_TOKENS_CEIL = int(os.getenv("CSMART_MAX_MAX_TOKENS", "16384"))
# Batas floor/cap per-model (match by substring, urut dari paling spesifik).
# Cap = batas output model agar tidak terpotong; floor = minimal agar reasoning
# tidak memakan seluruh budget. Nilai env override untuk tiap model bila diisi.
_MODEL_TOKEN_LIMITS: List[Dict[str, Any]] = [
    {
        "keys": ["deepseek-v4", "deepseek-v4-flash"],
        "floor": int(os.getenv("CSMART_MAX_TOKENS_FLOOR_DEEPSEEK", "8192")),
        "ceil": int(os.getenv("CSMART_MAX_TOKENS_CEIL_DEEPSEEK", "32768")),
    },
    {
        "keys": ["muse-spark", "muse-"],
        "floor": int(os.getenv("CSMART_MAX_TOKENS_FLOOR_MUSE", "8192")),
        "ceil": int(os.getenv("CSMART_MAX_TOKENS_CEIL_MUSE", "131072")),
    },
]
MAX_ROUNDS = int(os.getenv("CSMART_MAX_SHADOW_ROUNDS", "5"))

# Sanitizer (noise reduction)
ANSI_ESCAPE_REGEX = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SANITIZE_TRUNCATE_BYTES = int(os.getenv("CSMART_SANITIZE_MAX_BYTES", "2048"))
SANITIZE_TRUNCATE_LINES = int(os.getenv("CSMART_SANITIZE_MAX_LINES", "40"))

# Reversible CCR
CCR_MIN_BYTES = int(os.getenv("CSMART_CCR_MIN_BYTES", "3072"))
CCR_PREVIEW_LINES = int(os.getenv("CSMART_CCR_PREVIEW_LINES", "10"))

# DLP
DLP_ALLOW = [w for w in os.getenv("CSMART_DLP_ALLOW", "").split(",") if w]

# Mock mode — skip upstream call, return canned Anthropic response. Diagnostic
# only: helps isolate "is it the upstream format that's rejected" vs "is it the
# proxy transform that's broken" vs "is it Claude Code's renderer".
MOCK_MODE = os.getenv("CSMART_MOCK_RESPONSES", "0") == "1"

# Secret vault at-rest
VAULT_PERSIST = os.getenv("CSMART_VAULT_PERSIST", "0") == "1"
VAULT_KEY = os.getenv("CSMART_VAULT_KEY", "")

# Mask style: "hash" (default, zero-info) -> placeholder __CSMART_SEC_<hash>__,
# tidak ada byte secret pun ikut ke upstream/log. "preserve" (opsional) ->
# prefix+suffix (mis. sk-ant...90ab); CAVEAT: 10 char ikut ke upstream, hanya
# dipakai kalau log tracking prefix-suffix memang dibutuhkan.
MASK_STYLE = os.getenv("CSMART_MASK_STYLE", "hash").strip().lower()

# Keepalive (jaga KV-cache TTL provider, biasanya 5 menit)
KEEPALIVE_TICK = int(os.getenv("CSMART_KEEPALIVE_TICK", "30"))
KEEPALIVE_WINDOW_START = int(os.getenv("CSMART_KEEPALIVE_WINDOW_START", "270"))
KEEPALIVE_WINDOW_END = int(os.getenv("CSMART_KEEPALIVE_WINDOW_END", "300"))

# Heuristic router triggers (flagship tier)
_COMPLEX_TRIGGERS = [
    t.strip()
    for t in os.getenv(
        "CSMART_ROUTE_FLAGSHIP_KEYWORDS",
        "architecture,refactor whole system,security audit,database migration,redesign,multi-file refactor",
    ).split(",")
    if t.strip()
]

# OpenAI-native model detection and endpoints
OPENAI_MODEL_PATTERNS = [
    t.strip()
    for t in os.getenv(
        "CSMART_OPENAI_PATTERNS",
        "gpt-,o1-,o3-,muse-,text-,davinci-,curie-,deepseek-,"
        "glm-,kimi-,longcat-,mimo-,hy3,hy4-,grok-",
    ).split(",")
    if t.strip()
]
OPENAI_BASE_URL = os.getenv(
    "CSMART_OPENAI_BASE_URL",
    os.getenv("OPENAI_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
)
OPENAI_CHAT_COMPLETIONS_PATH = os.getenv(
    "CSMART_OPENAI_CHAT_PATH", "/chat/completions"
)
OPENAI_RESPONSES_PATH = os.getenv(
    "CSMART_OPENAI_RESPONSES_PATH", "/responses"
)
# Model families served by the OpenAI Responses endpoint (/responses) per the
# OpenCode Go model table. Everything else OpenAI (glm-,kimi-,longcat-,
# deepseek-,mimo-,hy3-,hy4-,o1-,o3-,text-...) defaults to chat completions.
# JANGAN tambahkan "opencode-" di sini (atau di OPENAI_MODEL_PATTERNS): itu
# org prefix "opencode-go/<id>", bukan model family - semua model OpenCode Go
# bakal ke-hijack ke /responses (lihat kasus opencode-go/hy3 -> 502).
OPENAI_RESPONSES_MODEL_PATTERNS = [
    t.strip()
    for t in os.getenv(
        "CSMART_RESPONSES_PATTERNS",
        "grok-,gpt-5.6,muse-",
    ).split(",")
    if t.strip()
]
# Anthropic-compatible /messages endpoint on the same OpenCode Go base
# ({OPENAI_BASE_URL}/messages, @ai-sdk/anthropic).
OPENAI_MESSAGES_PATH = os.getenv("CSMART_OPENAI_MESSAGES_PATH", "/messages")

# Model-id aliases applied on the OpenAI path only. OpenCode Go doesn't serve
# DeepSeek's real API ids (deepseek-chat / deepseek-reasoner - those belong to
# the DeepSeek upstream passthrough), so map them to OpenCode Go's v4 ids so the
# documented FLASH/FLAGSHIP defaults keep working when routed to OpenCode Go.
OPENAI_MODEL_ALIASES = {
    os.getenv("CSMART_ALIAS_DEEPSEEK_CHAT", "deepseek-chat"): os.getenv(
        "CSMART_ALIAS_DEEPSEEK_CHAT_TO", "deepseek-v4-flash"
    ),
    os.getenv("CSMART_ALIAS_DEEPSEEK_REASONER", "deepseek-reasoner"): os.getenv(
        "CSMART_ALIAS_DEEPSEEK_REASONER_TO", "deepseek-v4-pro"
    ),
}

# System Prompt Steering for OpenAI-native models
# Instructs model to follow Claude Code tool use format exactly
SYSTEM_STEERING_PROMPT = """You are a helpful coding assistant in a terminal chat. Follow EXACTLY the Claude Code tool use format.

TOP PRIORITY — RESPOND TO WHAT WAS ACTUALLY ASKED:
- If the user only greets you (halo, hi, hai, hello) or makes small talk, reply naturally in 1-2 short sentences. DO NOT analyze, DO NOT run commands, DO NOT create files, DO NOT propose plans.
- NEVER invent or fabricate a task, document, plan, or file that the user did not ask for.
- NEVER create files, folders, or run shell commands unless the user explicitly asks you to.
- NEVER guess or assume the working directory; use the real current directory. Do not fabricate paths.
- NEVER explore, list, or read files/directories unless the user explicitly asks. In particular, do NOT read graphify output (e.g. graphify-out/graph.json, graphify-out/*.md, graphify-merged/*), do NOT run graphify, and do NOT run `ls`, `find`, or `cat` just to "understand the codebase" — unless the user directly requests it.
- Read the user's last message literally and do exactly that, nothing more.
- Do NOT add unsolicited notes, caveats, or guesses about content the user did not send (e.g. do not claim the user attached an image, diagram, or task that is not in the message). If the message is only a greeting, reply with ONLY the greeting and nothing else.

TOOL USE FORMAT (when you ARE asked to take action):
- You MUST format tool calls as a JSON array in the tool_use block.
- You MUST NOT add extra preamble, explanations, or thinking outside the content block.
- You MUST use the provided tool definitions when the user asks to take action.
- You MUST follow the input_schema exactly when generating tool_use calls.
- You MUST NEVER imitate terminal UI or status indicators: no "✻", "Crunched for Ns", "done <time>", "…", spinners, checkmarks, or fake progress text.
- Always respond directly to the user's message in natural language.
"""

# Model families served by that Anthropic-compatible /messages endpoint
# (minimax-m*, qwen3.*). These speak Anthropic-native protocol - NO OpenAI
# protocol transform, client model name preserved verbatim, raw SSE passthrough.
ANTHROPIC_NATIVE_MODEL_PATTERNS = [
    t.strip()
    for t in os.getenv(
        "CSMART_ANTHROPIC_NATIVE_PATTERNS",
        "minimax-,qwen3",
    ).split(",")
    if t.strip()
]

# --- Track A: Model Resolver & Config (OPENAI_MODEL_MAP) ---
# JSON env CSMART_OPENAI_MODEL_MAP: Key=alias, value={target, endpoint_type}
# endpoint_type: "responses" | "chat_completions" | "messages"
# Default map dari docs/endpoints-opencode.md:
#   responses=[grok-4.6,gpt-5.6-luna,muse-spark-1.2-contributor]
#   chat=[glm-*,kimi-*,deepseek-*,mimo-*,hy3,hy4]
#   messages=[minimax-m*,qwen3*]
def _load_openai_model_map():
    _defaults = {
        # responses
        "grok-4.6": {"target": "grok-4.6", "endpoint_type": "responses"},
        "grok-4": {"target": "grok-4.6", "endpoint_type": "responses"},
        "gpt-5.6-luna": {"target": "gpt-5.6-luna", "endpoint_type": "responses"},
        "muse-spark-1.2-contributor": {"target": "muse-spark-1.2-contributor", "endpoint_type": "responses"},
        "muse-spark": {"target": "muse-spark-1.2-contributor", "endpoint_type": "responses"},
        # chat
        "glm-*": {"target": "glm-5.3", "endpoint_type": "chat_completions"},
        "glm-": {"target": "glm-5.3", "endpoint_type": "chat_completions"},
        "kimi-*": {"target": "kimi-k3", "endpoint_type": "chat_completions"},
        "kimi-": {"target": "kimi-k3", "endpoint_type": "chat_completions"},
        "deepseek-*": {"target": "deepseek-v4-flash", "endpoint_type": "chat_completions"},
        "deepseek-": {"target": "deepseek-v4-flash", "endpoint_type": "chat_completions"},
        "mimo-*": {"target": "mimo-v2.5", "endpoint_type": "chat_completions"},
        "mimo-": {"target": "mimo-v2.5", "endpoint_type": "chat_completions"},
        "hy3": {"target": "hy3", "endpoint_type": "chat_completions"},
        "hy4": {"target": "hy4-preview", "endpoint_type": "chat_completions"},
        "hy4-*": {"target": "hy4-preview", "endpoint_type": "chat_completions"},
        "hy4-": {"target": "hy4-preview", "endpoint_type": "chat_completions"},
        # messages (Anthropic-native on OpenCode Go)
        "minimax-m*": {"target": "minimax-m3", "endpoint_type": "messages"},
        "minimax-m3": {"target": "minimax-m3", "endpoint_type": "messages"},
        "minimax-": {"target": "minimax-m3", "endpoint_type": "messages"},
        "qwen3*": {"target": "qwen3.8-max", "endpoint_type": "messages"},
        "qwen3": {"target": "qwen3.8-max", "endpoint_type": "messages"},
        "qwen3-*": {"target": "qwen3.8-max", "endpoint_type": "messages"},
    }
    raw = __import__("os").getenv("CSMART_OPENAI_MODEL_MAP", "").strip()
    if raw:
        try:
            _parsed = __import__("json").loads(raw)
            if isinstance(_parsed, dict):
                for _k, _v in _parsed.items():
                    if isinstance(_v, dict) and "endpoint_type" in _v:
                        _et = str(_v.get("endpoint_type", "")).strip().lower()
                        if _et in ("chat", "chat_completions", "chat.completions"):
                            _et = "chat_completions"
                        elif _et in ("response", "responses"):
                            _et = "responses"
                        elif _et in ("messages", "anthropic", "message"):
                            _et = "messages"
                        _tgt = str(_v.get("target", _k)).strip() or _k
                        _defaults[_k] = {"target": _tgt, "endpoint_type": _et}
                    elif isinstance(_v, str):
                        _et = _v.strip().lower()
                        if _et in ("chat", "chat_completions"):
                            _et = "chat_completions"
                        elif _et in ("response", "responses"):
                            _et = "responses"
                        elif _et in ("messages", "message"):
                            _et = "messages"
                        _defaults[_k] = {"target": _k, "endpoint_type": _et}
        except Exception:
            pass
    return _defaults


OPENAI_MODEL_MAP = _load_openai_model_map()

# Model-id aliases applied on the OpenAI path only. OpenCode Go doesn't serve
# DeepSeek's real API ids (deepseek-chat / deepseek-reasoner - those belong to
# the DeepSeek upstream passthrough), so map them to OpenCode Go's v4 ids so the
# documented FLASH/FLAGSHIP defaults keep working when routed to OpenCode Go.
OPENAI_MODEL_ALIASES = {
    os.getenv("CSMART_ALIAS_DEEPSEEK_CHAT", "deepseek-chat"): os.getenv(
        "CSMART_ALIAS_DEEPSEEK_CHAT_TO", "deepseek-v4-flash"
    ),
    os.getenv("CSMART_ALIAS_DEEPSEEK_REASONER", "deepseek-reasoner"): os.getenv(
        "CSMART_ALIAS_DEEPSEEK_REASONER_TO", "deepseek-v4-pro"
    ),
}

# System Prompt Steering for OpenAI-native models
# Instructs model to follow Claude Code tool use format exactly
SYSTEM_STEERING_PROMPT = """You are a helpful coding assistant in a terminal chat. Follow EXACTLY the Claude Code tool use format.

TOP PRIORITY — RESPOND TO WHAT WAS ACTUALLY ASKED:
- If the user only greets you (halo, hi, hai, hello) or makes small talk, reply naturally in 1-2 short sentences. DO NOT analyze, DO NOT run commands, DO NOT create files, DO NOT propose plans.
- NEVER invent or fabricate a task, document, plan, or file that the user did not ask for.
- NEVER create files, folders, or run shell commands unless the user explicitly asks you to.
- NEVER guess or assume the working directory; use the real current directory. Do not fabricate paths.
- NEVER explore, list, or read files/directories unless the user explicitly asks. In particular, do NOT read graphify output (e.g. graphify-out/graph.json, graphify-out/*.md, graphify-merged/*), do NOT run graphify, and do NOT run `ls`, `find`, or `cat` just to "understand the codebase" — unless the user directly requests it.
- Read the user's last message literally and do exactly that, nothing more.
- Do NOT add unsolicited notes, caveats, or guesses about content the user did not send (e.g. do not claim the user attached an image, diagram, or task that is not in the message). If the message is only a greeting, reply with ONLY the greeting and nothing else.

TOOL USE FORMAT (when you ARE asked to take action):
- You MUST format tool calls as a JSON array in the tool_use block.
- You MUST NOT add extra preamble, explanations, or thinking outside the content block.
- You MUST use the provided tool definitions when the user asks to take action.
- You MUST follow the input_schema exactly when generating tool_use calls.
- You MUST NEVER imitate terminal UI or status indicators: no "✻", "Crunched for Ns", "done <time>", "…", spinners, checkmarks, or fake progress text.
- Always respond directly to the user's message in natural language.
"""

__all__ = [
    "_load_gateway_env",
    "UPSTREAM_BASE_URL",
    "UPSTREAM_API_KEY",
    "OPENAI_API_KEY",
    "PROXY_HOST",
    "PROXY_PORT",
    "DB_PATH",
    "FLASH_MODEL",
    "FLAGSHIP_MODEL",
    "UPSTREAM_TIMEOUT",
    "MAX_TOKENS_FLOOR",
    "MAX_TOKENS_CEIL",
    "_MODEL_TOKEN_LIMITS",
    "MAX_ROUNDS",
    "ANSI_ESCAPE_REGEX",
    "SANITIZE_TRUNCATE_BYTES",
    "SANITIZE_TRUNCATE_LINES",
    "CCR_MIN_BYTES",
    "CCR_PREVIEW_LINES",
    "DLP_ALLOW",
    "MOCK_MODE",
    "VAULT_PERSIST",
    "VAULT_KEY",
    "MASK_STYLE",
    "KEEPALIVE_TICK",
    "KEEPALIVE_WINDOW_START",
    "KEEPALIVE_WINDOW_END",
    "_COMPLEX_TRIGGERS",
    "OPENAI_MODEL_PATTERNS",
    "OPENAI_BASE_URL",
    "OPENAI_CHAT_COMPLETIONS_PATH",
    "OPENAI_RESPONSES_PATH",
    "OPENAI_MESSAGES_PATH",
    "OPENAI_MODEL_ALIASES",
    "SYSTEM_STEERING_PROMPT",
    "OPENAI_RESPONSES_MODEL_PATTERNS",
    "ANTHROPIC_NATIVE_MODEL_PATTERNS",
    "_load_openai_model_map",
    "OPENAI_MODEL_MAP",
    "OPENAI_MODEL_ALIASES",
    "SYSTEM_STEERING_PROMPT",
]
