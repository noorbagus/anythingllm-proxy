"""csmart.routing.token_limits — per-model max_tokens floor/ceil resolution.

Extracted verbatim from csmart_proxy.py:947-985. Pure: imports only
csmart.app.config constants. _log is optional (structured logging may not be
importable in isolation); falls back to a noop that never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from any_proxy.app.config import _MODEL_TOKEN_LIMITS, MAX_TOKENS_CEIL, MAX_TOKENS_FLOOR

try:
    from any_proxy.logging.structured import _log
except ImportError:  # pragma: no cover
    def _log(event: str, **fields: Any) -> None:  # type: ignore[misc]
        return None


def _model_token_limits(model_name: str) -> Tuple[int, int]:
    """Resolve (floor, ceil) for a model name. Falls back to global default."""
    lower = (model_name or "").lower()
    for entry in _MODEL_TOKEN_LIMITS:
        if any(k in lower for k in entry["keys"]):
            return entry["floor"], entry["ceil"]
    return MAX_TOKENS_FLOOR, MAX_TOKENS_CEIL


def clamp_max_tokens(body: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp client max_tokens to [floor, ceil]. Emits a structured JSON log
    (TOKEN_CLAMP) tracking original, applied, floor, ceil, and action so
    max-token and reasoning-budget behavior can be traced per model."""
    mt = body.get("max_tokens")
    if not isinstance(mt, int):
        _log("TOKEN_CLAMP", model=body.get("model", ""), requested=mt,
             applied=mt, floor=None, ceil=None, action="skip_non_int")
        return body
    floor, ceil = _model_token_limits(body.get("model", ""))
    if mt < floor:
        applied = floor
        action = "raised_to_floor"
    elif mt > ceil:
        applied = ceil
        action = "lowered_to_ceil"
    else:
        applied = mt
        action = "kept"
    body["max_tokens"] = applied
    _log("TOKEN_CLAMP", model=body.get("model", ""), requested=mt,
         applied=applied, floor=floor, ceil=ceil, action=action)
    return body


__all__ = [
    "_model_token_limits",
    "clamp_max_tokens",
]
