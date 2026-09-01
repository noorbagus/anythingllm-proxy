"""csmart.handlers.openai — OpenAI-shaped endpoints (W3).

handle_openai_chat / handle_openai_responses / handle_models + catch-all
passthrough extracted from ``csmart_proxy.py``. Includes the Track-B
double-``/v1`` guard: when forwarding an OpenAI model to OPENAI_BASE_URL (which
already ends in ``/v1``), the ``v1/`` prefix of the incoming path is stripped so
the target never becomes ``.../v1/v1/...``.

One-way deps: config/routing/transform/logging/streaming. Never imports
app.factory (no cycle).
"""
from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator, Dict, List

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import time

from any_proxy.app.config import (
    OPENAI_BASE_URL,
    OPENAI_CHAT_COMPLETIONS_PATH,
    OPENAI_MODEL_ALIASES,
    OPENAI_MODEL_MAP,
    OPENAI_MODEL_PATTERNS,
    OPENAI_RESPONSES_PATH,
    PROXY_PORT,
    UPSTREAM_API_KEY,
    UPSTREAM_BASE_URL,
    UPSTREAM_TIMEOUT,
)
from any_proxy.logging.structured import _log, get_trace_id
from any_proxy.routing.model import (
    _openai_upstream_headers,
    clean_openai_model_name,
    is_openai_model,
    resolve_openai_endpoint,
)
from any_proxy.streaming.sse import _format_openai_chat_sse, get_upstream_transport
from any_proxy.transform.anthropic_to_openai import transform_openai_chat_to_responses
from any_proxy.transform.openai_responses_to_chat import (
    responses_json_to_chat_json,
    responses_sse_to_chat_sse,
)

router = APIRouter()

_PASSTHROUGH_HEADERS = {"anthropic-version"}

# PoC mock — only active on 18080 (proof server), only for muse-spark
_MOCK_MODEL = "muse-spark-1.2-contributor"
_MOCK_TEXT = "[PoC dummy] muse-spark mock — 18080 OK"


_MOCK_ENV_ON = os.getenv("CSMART_MOCK_MUSE_SPARK", "0") not in ("0", "", "false", "False", "off")


def _is_mock_model(model: str, cleaned: str) -> bool:
    if not (PROXY_PORT == 18080 and (model == _MOCK_MODEL or cleaned == _MOCK_MODEL)):
        return False
    return _MOCK_ENV_ON


def _trace() -> str:
    return get_trace_id() or "-"


def _clip(v, n: int = 400) -> str:
    try:
        s = str(v)
    except Exception:
        s = "<unrepr>"
    return s[:n]


def _mock_chat_json(model_id: str) -> dict:
    now = int(time.time())
    return {
        "id": f"chatcmpl-mock-{now}",
        "object": "chat.completion",
        "created": now,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": _MOCK_TEXT},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 12, "total_tokens": 12},
    }


def _mock_responses_json(model_id: str) -> dict:
    now = int(time.time())
    return {
        "id": f"resp_mock_{now}",
        "object": "response",
        "created_at": now,
        "status": "completed",
        "model": model_id,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": _MOCK_TEXT}],
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 12, "total_tokens": 12},
    }


@router.post("/v1/chat/completions")
async def handle_openai_chat(request: Request) -> StreamingResponse:
    """OpenAI Chat Completions endpoint for AnythingLLM (and compatibles).

    Route via resolve_openai_endpoint(). If endpoint_type=="responses" ->
    transform_openai_chat_to_responses() then forward to OPENAI_BASE_URL+OPENAI_RESPONSES_PATH
    else forward directly to /chat/completions. Auth via _openai_upstream_headers().
    """
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")
    model = str(body.get("model", "")).strip()
    cleaned = clean_openai_model_name(model)
    if cleaned in OPENAI_MODEL_ALIASES:
        cleaned = OPENAI_MODEL_ALIASES[cleaned]
    # PoC mock intercept — return dummy without hitting opencode (18080 only)
    if _is_mock_model(model, cleaned):
        is_stream = bool(body.get("stream"))
        _log("MOCK_MUSE_SPARK_CHAT", model=model, stream=is_stream, port=PROXY_PORT)
        if is_stream:

            async def _mock_gen():
                chunk = {
                    "id": f"chatcmpl-mock-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": _MOCK_MODEL,
                    "choices": [{"index": 0, "delta": {"content": _MOCK_TEXT}, "finish_reason": None}],
                }
                yield _format_openai_chat_sse(chunk)
                done = {
                    "id": f"chatcmpl-mock-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": _MOCK_MODEL,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield _format_openai_chat_sse(done)
                yield b"data: [DONE]\n\n"

            return StreamingResponse(_mock_gen(), media_type="text/event-stream")
        return JSONResponse(_mock_chat_json(_MOCK_MODEL))

    endpoint_type = resolve_openai_endpoint(model)
    _log("OPENAI_INGRESS", model=model, cleaned=cleaned, has_stream=bool(body.get("stream")), trace_id=_trace())
    _log("OPENAI_ROUTE", model=model, cleaned=cleaned, endpoint_type=endpoint_type, trace_id=_trace())
    headers = _openai_upstream_headers(request)
    if endpoint_type == "responses":
        payload = transform_openai_chat_to_responses(body)
        _log("OPENAI_TRANSFORM", from_keys=list(body.keys()), to_keys=list(payload.keys()), trace_id=_trace())
        # keep the (possibly aliased) model id in the transformed payload
        if cleaned and payload.get("model") != cleaned:
            payload["model"] = cleaned or payload.get("model")
        body_bytes = json.dumps(payload).encode("utf-8")
        target = f"{OPENAI_BASE_URL}{OPENAI_RESPONSES_PATH}"
        _log("OPENAI_CHAT_TO_RESPONSES", model=model, cleaned=cleaned, target=target)
    else:
        # chat_completions: forward as-is (preserve alias-cleaned model id)
        if cleaned and body.get("model") != cleaned:
            body["model"] = cleaned
        body_bytes = json.dumps(body).encode("utf-8")
        target = f"{OPENAI_BASE_URL}{OPENAI_CHAT_COMPLETIONS_PATH}"
        _log("OPENAI_CHAT_PASSTHROUGH", model=model, cleaned=cleaned, target=target, trace_id=_trace(), body_bytes=len(body_bytes))
    # stream passthrough — do not touch upstream SSE here
    headers.setdefault("Content-Type", "application/json")
    _log("OPENAI_UPSTREAM_REQUEST", target=target, method="POST", model=model, stream=bool(body.get("stream")), trace_id=_trace())

    # AnythingLLM Generic OpenAI expects chat.completions even when we route to responses upstream.
    # Follow mock format: stream -> chat.completion.chunk choices.delta.content
    is_stream = bool(body.get("stream"))
    needs_chat_transform = endpoint_type == "responses"

    async def _gen() -> AsyncGenerator[bytes, None]:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(transport=get_upstream_transport(), timeout=UPSTREAM_TIMEOUT) as client:
                req = client.build_request("POST", target, headers=headers, content=body_bytes)
                _log("OPENAI_UPSTREAM_SEND", target=target, trace_id=_trace())
                resp = await client.send(req, stream=True)
                _log("OPENAI_UPSTREAM_RESPONSE", target=target, status_code=resp.status_code, headers=dict(resp.headers), trace_id=_trace(), latency_ms=int((time.monotonic() - t0) * 1000))
                if resp.status_code >= 400:
                    body_p = b""
                    async for chunk in resp.aiter_bytes():
                        body_p += chunk
                        if len(body_p) > 2048:
                            break
                    _log("OPENAI_UPSTREAM_ERROR", target=target, status_code=resp.status_code, body=_clip(body_p.decode(errors="replace")), latency_ms=int((time.monotonic() - t0) * 1000), trace_id=_trace())
                    yield body_p
                    return
                if needs_chat_transform and not is_stream:
                    # non-stream: collect JSON, transform responses -> chat
                    raw = b""
                    async for chunk in resp.aiter_bytes():
                        raw += chunk
                    try:
                        payload_json = json.loads(raw.decode(errors="replace"))
                        chat_json = responses_json_to_chat_json(payload_json, model=cleaned or model)
                        _log("OPENAI_RESPONSES_TO_CHAT_JSON", trace_id=_trace(), model=cleaned or model)
                        yield json.dumps(chat_json).encode("utf-8")
                    except Exception as _e:
                        _log("OPENAI_RESPONSES_TO_CHAT_JSON_ERROR", trace_id=_trace(), error=str(_e)[:300])
                        yield raw
                    _log("OPENAI_UPSTREAM_STREAM_DONE", target=target, latency_ms=int((time.monotonic() - t0) * 1000), trace_id=_trace())
                    return
                if needs_chat_transform and is_stream:
                    # stream: responses SSE -> chat SSE
                    async def _upstream_bytes():
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                    first = True
                    async for out_chunk in responses_sse_to_chat_sse(
                        _upstream_bytes(), model=cleaned or model
                    ):
                        if first:
                            _log("OPENAI_UPSTREAM_STREAM_FIRST_CHUNK", target=target, bytes=len(out_chunk), trace_id=_trace())
                            _log("OPENAI_RESPONSES_TO_CHAT_SSE_STARTED", trace_id=_trace())
                            first = False
                        yield out_chunk
                    _log("OPENAI_UPSTREAM_STREAM_DONE", target=target, latency_ms=int((time.monotonic() - t0) * 1000), trace_id=_trace())
                    _log("OPENAI_RESPONSES_TO_CHAT_SSE_DONE", trace_id=_trace())
                    return
                first = True
                async for chunk in resp.aiter_bytes():
                    if first:
                        _log("OPENAI_UPSTREAM_STREAM_FIRST_CHUNK", target=target, bytes=len(chunk), trace_id=_trace())
                        first = False
                    yield chunk
                _log("OPENAI_UPSTREAM_STREAM_DONE", target=target, latency_ms=int((time.monotonic() - t0) * 1000), trace_id=_trace())
        except Exception as exc:
            _log("OPENAI_UPSTREAM_EXCEPTION", target=target, error=str(exc)[:800], error_type=type(exc).__name__, latency_ms=int((time.monotonic() - t0) * 1000), trace_id=_trace())
            raise

    media = "text/event-stream" if body.get("stream") else "application/json"
    _log("OPENAI_EGRESS", model=model, media=media, trace_id=_trace(), needs_chat_transform=needs_chat_transform, is_stream=is_stream)
    return StreamingResponse(_gen(), media_type=media)


@router.post("/v1/responses")
async def handle_openai_responses(request: Request) -> StreamingResponse:
    """OpenAI Responses passthrough for AnythingLLM that targets /v1/responses."""
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")
    model = str(body.get("model", "")).strip()
    cleaned = clean_openai_model_name(model)
    if cleaned in OPENAI_MODEL_ALIASES:
        cleaned = OPENAI_MODEL_ALIASES[cleaned]
    if cleaned and body.get("model") != cleaned:
        body["model"] = cleaned
    _log("OPENAI_INGRESS", model=model, cleaned=cleaned, has_stream=bool(body.get("stream")), trace_id=_trace())
    _log("OPENAI_ROUTE", model=model, cleaned=cleaned, endpoint_type="responses", trace_id=_trace())
    # PoC mock intercept for /v1/responses (18080 only)
    if _is_mock_model(model, cleaned):
        is_stream = bool(body.get("stream"))
        _log("MOCK_MUSE_SPARK_RESPONSES", model=model, stream=is_stream, port=PROXY_PORT)
        if is_stream:

            async def _mock_resp_gen():
                # minimal responses SSE
                yield _format_openai_chat_sse(
                    {
                        "type": "response.output_text.delta",
                        "delta": _MOCK_TEXT,
                    }
                )
                yield b"data: [DONE]\n\n"

            return StreamingResponse(_mock_resp_gen(), media_type="text/event-stream")
        return JSONResponse(_mock_responses_json(_MOCK_MODEL))
    headers = _openai_upstream_headers(request)
    headers.setdefault("Content-Type", "application/json")
    body_bytes = json.dumps(body).encode("utf-8")
    target = f"{OPENAI_BASE_URL}{OPENAI_RESPONSES_PATH}"
    _log("OPENAI_RESPONSES_PASSTHROUGH", model=model, cleaned=cleaned, target=target, trace_id=_trace(), body_bytes=len(body_bytes))
    _log("OPENAI_UPSTREAM_REQUEST", target=target, method="POST", model=model, stream=bool(body.get("stream")), trace_id=_trace())

    async def _gen() -> AsyncGenerator[bytes, None]:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(transport=get_upstream_transport(), timeout=UPSTREAM_TIMEOUT) as client:
                req = client.build_request("POST", target, headers=headers, content=body_bytes)
                _log("OPENAI_UPSTREAM_SEND", target=target, trace_id=_trace())
                resp = await client.send(req, stream=True)
                _log("OPENAI_UPSTREAM_RESPONSE", target=target, status_code=resp.status_code, headers=dict(resp.headers), trace_id=_trace(), latency_ms=int((time.monotonic() - t0) * 1000))
                if resp.status_code >= 400:
                    body_p = b""
                    async for chunk in resp.aiter_bytes():
                        body_p += chunk
                        if len(body_p) > 2048:
                            break
                    _log("OPENAI_UPSTREAM_ERROR", target=target, status_code=resp.status_code, body=_clip(body_p.decode(errors="replace")), latency_ms=int((time.monotonic() - t0) * 1000), trace_id=_trace())
                    yield body_p
                    return
                first = True
                async for chunk in resp.aiter_bytes():
                    if first:
                        _log("OPENAI_UPSTREAM_STREAM_FIRST_CHUNK", target=target, bytes=len(chunk), trace_id=_trace())
                        first = False
                    yield chunk
                _log("OPENAI_UPSTREAM_STREAM_DONE", target=target, latency_ms=int((time.monotonic() - t0) * 1000), trace_id=_trace())
        except Exception as exc:
            _log("OPENAI_UPSTREAM_EXCEPTION", target=target, error=str(exc)[:800], error_type=type(exc).__name__, latency_ms=int((time.monotonic() - t0) * 1000), trace_id=_trace())
            raise

    media = "text/event-stream" if body.get("stream") else "application/json"
    _log("OPENAI_EGRESS", model=model, media=media, trace_id=_trace())
    return StreamingResponse(_gen(), media_type=media)


@router.get("/v1/models")
async def handle_models(request: Request) -> JSONResponse:
    """Return OpenAI-shaped model list: union of OPENAI_MODEL_MAP + upstream GET /v1/models."""
    data: List[Dict[str, Any]] = []
    seen: set = set()
    for alias, info in OPENAI_MODEL_MAP.items():
        if alias not in seen:
            seen.add(alias)
            data.append({"id": alias, "object": "model", "created": 0, "owned_by": "opencode"})
        tgt = str(info.get("target", "")).strip()
        if tgt and tgt not in seen:
            seen.add(tgt)
            data.append({"id": tgt, "object": "model", "created": 0, "owned_by": "opencode"})
    # also include pattern families for completeness
    for pat in OPENAI_MODEL_PATTERNS:
        key = pat.rstrip("-*").strip()
        if key and key not in seen:
            seen.add(key)
            data.append({"id": pat, "object": "model", "created": 0, "owned_by": "opencode"})
    # fetch upstream models (best-effort)
    _upstream_models: List[Dict[str, Any]] = []
    headers = _openai_upstream_headers(request)
    try:
        async with httpx.AsyncClient(transport=get_upstream_transport(), timeout=10.0) as client:
            resp = await client.get(f"{OPENAI_BASE_URL}/v1/models", headers=headers)
            if resp.status_code < 400:
                j = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                raw = j.get("data") if isinstance(j, dict) else None
                if isinstance(raw, list):
                    for entry in raw:
                        if isinstance(entry, dict) and "id" in entry:
                            mid = str(entry["id"])
                            if mid not in seen:
                                seen.add(mid)
                                data.append({"id": mid, "object": "model", "created": entry.get("created", 0), "owned_by": entry.get("owned_by", "opencode")})
                        elif isinstance(entry, str) and entry not in seen:
                            seen.add(entry)
                            data.append({"id": entry, "object": "model", "created": 0, "owned_by": "opencode"})
    except Exception as exc:
        _log("MODELS_UPSTREAM_FETCH_FAILED", error=str(exc)[:200])
    # PoC: ensure muse-spark visible on 18080 even if upstream missing
    if PROXY_PORT == 18080 and _MOCK_MODEL not in seen:
        data.append({"id": _MOCK_MODEL, "object": "model", "created": 0, "owned_by": "opencode"})
        seen.add(_MOCK_MODEL)
    _log("MODELS_LIST", count=len(data))
    return JSONResponse({"object": "list", "data": data})


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def passthrough(request: Request, path: str) -> StreamingResponse:
    """Forward non-messages endpoints (e.g. GET /v1/models, /v1/messages/count_tokens).

    Refactored per Track B spec: if path startswith "v1/" and is_openai_model
    (from query/body), forward to OPENAI_BASE_URL else UPSTREAM_BASE_URL.
    Streaming logic unchanged. Includes the double-``/v1`` guard: OPENAI_BASE_URL
    already ends in ``/v1``, so the ``v1/`` prefix is stripped to avoid
    ``.../v1/v1/...``.
    """
    body_bytes = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    is_v1 = path.startswith("v1/") or path.startswith("/v1/")
    target_base = UPSTREAM_BASE_URL
    if is_v1:
        model_hint = ""
        try:
            model_hint = request.query_params.get("model", "") or ""
        except Exception:
            model_hint = ""
        if not model_hint and body_bytes:
            try:
                _j = json.loads(body_bytes.decode("utf-8"))
                if isinstance(_j, dict):
                    model_hint = str(_j.get("model", ""))
            except Exception:
                model_hint = ""
        if model_hint and is_openai_model(model_hint):
            target_base = OPENAI_BASE_URL
    if target_base == OPENAI_BASE_URL and path.startswith("v1/"):
        target = f"{OPENAI_BASE_URL}/{path[3:]}"
    else:
        target = f"{target_base}/{path}"
    headers: Dict[str, str] = {"Authorization": f"Bearer {UPSTREAM_API_KEY}"}
    for name, val in request.headers.items():
        if name.lower() in _PASSTHROUGH_HEADERS:
            headers[name.lower()] = val
    if body_bytes:
        headers.setdefault("Content-Type", "application/json")

    async def proxy_gen() -> AsyncGenerator[bytes, None]:
        async with httpx.AsyncClient(transport=get_upstream_transport(), timeout=UPSTREAM_TIMEOUT) as client:
            req = client.build_request(request.method, target, headers=headers, content=body_bytes)
            resp = await client.send(req, stream=True)
            async for chunk in resp.aiter_bytes():
                yield chunk

    return StreamingResponse(proxy_gen(), media_type="application/json")


__all__ = ["router", "handle_openai_chat", "handle_openai_responses", "handle_models", "passthrough"]