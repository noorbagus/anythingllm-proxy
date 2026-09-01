"""csmart.handlers — route handlers (W3).

Re-exports the 5 FastAPI handlers: Anthropic Messages (messages.py) and
OpenAI-compat + passthrough (openai.py). Shared keepalive state (mutable globals
+ worker) lives in ``csmart.app.keepalive`` so both handlers and factory can use
it without an import cycle.
"""
from any_proxy.app.keepalive import (
    _active_model,
    _prefix_snapshot,
    keepalive_worker,
    last_keepalive_ok,
    last_request_timestamp,
)
from any_proxy.handlers.messages import (
    _mock_anthropic_json,
    _mock_anthropic_stream,
    _upstream_headers,
    handle_messages,
)
from any_proxy.handlers.openai import (
    _PASSTHROUGH_HEADERS,
    handle_models,
    handle_openai_chat,
    handle_openai_responses,
    passthrough,
)

__all__ = [
    "handle_messages",
    "keepalive_worker",
    "_upstream_headers",
    "_mock_anthropic_json",
    "_mock_anthropic_stream",
    "last_request_timestamp",
    "last_keepalive_ok",
    "_prefix_snapshot",
    "_active_model",
    "handle_openai_chat",
    "handle_openai_responses",
    "handle_models",
    "passthrough",
    "_PASSTHROUGH_HEADERS",
]