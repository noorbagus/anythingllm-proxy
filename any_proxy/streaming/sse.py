"""csmart.streaming.sse — pure SSE utils (no DB).

Ekstrak dari ``csmart_proxy.py:2827`` (_format_event) +
``csmart_proxy.py:2618``-adjacent (_parse_sse_data, _iter_sse_events, _sse_source)
dan ``csmart_proxy.py:2466`` (_format_openai_chat_sse, _safe_json_loads).

Pure — tidak sentuh DB, tidak import proxy. Struktur log via injectable
callback (hapus dependensi ``_log`` langsung); fallback ke
``csmart.logging.structured`` agar tetap trackable sebagai structured JSONL.

Interface (W1):
    _parse_sse_data(data_lines)  -> Dict
    _iter_sse_events(resp)       -> AsyncGenerator[(event_name, payload)]
    _sse_source(method, url, headers, body, transport, timeout) -> AsyncGenerator
    _format_event(event_name, payload) -> bytes
    _format_openai_chat_sse(payload) -> bytes
    _safe_json_loads(raw) -> Any

Graphify community 37 — streaming/SSE path.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Structured log — injectable, no hard dep pada csmart_proxy._log
# ---------------------------------------------------------------------------
try:  # pragma: no cover - import guard
    from any_proxy.logging.structured import _log as _sse_log  # type: ignore
except ImportError:  # fallback: no-op agar pure & hermetic
    def _sse_log(event: str, **fields: Any) -> None:  # type: ignore[no-redef]
        pass

# injectable override (hapus dependensi _log langsung)
_log_callback: Optional[Callable[..., None]] = None


def set_sse_logger(callback: Optional[Callable[..., None]]) -> None:
    """Inject custom structured logger (dipakai W2/ProxyStreamer)."""
    global _log_callback
    _log_callback = callback


def _emit(ev: str, level: str = "INFO", **fields: Any) -> None:
    """Emit structured JSONL — via injected callback atau fallback."""
    try:
        if _log_callback is not None:
            _log_callback(ev, level=level, **fields)
        else:
            _sse_log(ev, level=level, **fields)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Upstream transport/timeout — mirror csmart_proxy.py:95 + 2549
# Tidak import proxy; nilai default dari env, bisa di-override per-call
# dan via setter untuk hermetic/mock tests (httpx.MockTransport).
# ---------------------------------------------------------------------------
UPSTREAM_TIMEOUT: float = float(os.getenv("CSMART_UPSTREAM_TIMEOUT", "120"))
_UPSTREAM_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


def set_upstream_transport(transport: Optional[httpx.AsyncBaseTransport]) -> None:
    """Override transport global (untuk mock/hermetic tests)."""
    global _UPSTREAM_TRANSPORT
    _UPSTREAM_TRANSPORT = transport


def get_upstream_transport() -> Optional[httpx.AsyncBaseTransport]:
    """Read current transport global (dynamic — avoids stale import binding)."""
    return _UPSTREAM_TRANSPORT


# ---------------------------------------------------------------------------
# Pure SSE helpers
# ---------------------------------------------------------------------------

def _safe_json_loads(raw: Any) -> Any:
    """Parse JSON string, fall back to {} on failure (pure, no log)."""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _parse_sse_data(data_lines: List[str]) -> Dict[str, Any]:
    """Join ``data:`` lines and JSON-decode into payload dict.

    Compat:
    - ``_iter_sse_events`` sudah strip ``data:`` prefix sebelum append,
      sehingga ``data_lines`` biasanya berisi JSON murni.
    - Hermetic test memanggil ``_parse_sse_data(['data: {"a":1}'])`` langsung
      (prefix belum di-strip) — dukung kedua bentuk dengan strip opsional.
    - Special ``[DONE]`` sentinel dari OpenAI SSE → ``{"__openai_done": True}``.
    """
    # Strip optional "data:" prefix per-line (hermetic compat)
    cleaned: List[str] = []
    for ln in data_lines:
        if ln.startswith("data:"):
            cleaned.append(ln[len("data:"):].strip())
        else:
            cleaned.append(ln)
    raw = "\n".join(cleaned)
    raw_stripped = raw.strip()
    if raw_stripped == "[DONE]":
        return {"__openai_done": True}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
        _emit("SSE_PARSE_NON_DICT", level="DEBUG", raw=raw[:200])
        return {"type": "error", "error": {"type": "invalid_payload", "message": raw[:200]}}
    except json.JSONDecodeError:
        _emit("SSE_PARSE_ERROR", level="DEBUG", raw=raw[:200])
        return {"type": "error", "error": {"type": "invalid_payload", "message": raw[:200]}}


async def _iter_sse_events(
    resp: httpx.Response,
) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    """Parse httpx streaming response into ``(event_name, payload)`` tuples.

    Pure — tidak akses DB. Yield per SSE frame (blank line delimited).
    """
    data_lines: List[str] = []
    event_name: Optional[str] = None
    async for raw_line in resp.aiter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                payload = _parse_sse_data(data_lines)
                _emit("SSE_EVENT", level="DEBUG", sse_event=event_name or "message", has_error=payload.get("type") == "error")
                yield event_name, payload
                data_lines = []
                event_name = None
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
        # ignore other SSE fields (id:, retry:, :)
    if data_lines:
        payload = _parse_sse_data(data_lines)
        _emit("SSE_EVENT_TRAILING", level="DEBUG", sse_event=event_name or "message")
        yield event_name, payload


async def _sse_source(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    timeout: Optional[float] = None,
) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    """Open upstream stream, canonical-serialize body, yield SSE events.

    Pure — tidak akses DB. ``transport``/``timeout`` injectable untuk
    hermetic (MockTransport) dan agar tidak depend pada global proxy.
    """
    payload_bytes = json.dumps(body, sort_keys=True).encode("utf-8")
    tr = transport if transport is not None else _UPSTREAM_TRANSPORT
    to = timeout if timeout is not None else UPSTREAM_TIMEOUT
    t0 = time.monotonic()
    _emit("SSE_SOURCE_OPEN", level="INFO", method=method, url=url, body_keys=list(body.keys()), body_bytes=len(payload_bytes), trace_id=None)
    async with httpx.AsyncClient(transport=tr, timeout=to) as client:
        async with client.stream(method, url, headers=headers, content=payload_bytes) as resp:
            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")[:400]
                _emit("SSE_UPSTREAM_ERROR", level="WARN", status=resp.status_code, body=err_body[:200])
                yield "error", {
                    "type": "error",
                    "error": {"type": "upstream_error", "status_code": resp.status_code, "message": err_body},
                }
                return
            async for event_name, payload in _iter_sse_events(resp):
                yield event_name, payload
    _emit("SSE_SOURCE_DONE", level="INFO", method=method, url=url, latency_ms=int((time.monotonic() - t0) * 1000))


def _format_event(event_name: Optional[str], payload: Dict[str, Any]) -> bytes:
    """Format Anthropic SSE frame ``event: ...\\ndata: ...\\n\\n``."""
    etype = str(payload.get("type") or event_name or "message")
    return f"event: {etype}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


def _format_openai_chat_sse(payload: Dict[str, Any]) -> bytes:
    """Format Chat Completions chunk as SSE ``data:`` line.

    Terminal sentinel ``{"__openai_done": True}`` -> ``data: [DONE]``.
    """
    if payload.get("__openai_done"):
        return b"data: [DONE]\n\n"
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


__all__ = [
    "_parse_sse_data",
    "_iter_sse_events",
    "_sse_source",
    "_format_event",
    "_format_openai_chat_sse",
    "_safe_json_loads",
    "UPSTREAM_TIMEOUT",
    "_UPSTREAM_TRANSPORT",
    "set_upstream_transport",
    "get_upstream_transport",
    "set_sse_logger",
]
