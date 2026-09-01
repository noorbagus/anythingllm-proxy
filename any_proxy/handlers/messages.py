"""csmart.handlers.messages — Anthropic /v1/messages handler (W3).

Slim orchestrator extracted from ``csmart_proxy.py`` handle_messages: parse ->
sanitize/clamp -> OpenAI detection -> tier routing -> steering -> 3-region
alignment -> transform -> ProxyStreamer/SSE adapters -> redact -> stream.

One-way deps: transform/routing/security/streaming/logging/app.config +
app.keepalive (leaf state). Never imports app.factory (no cycle).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from any_proxy.app.config import (
    MOCK_MODE,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_CHAT_COMPLETIONS_PATH,
    OPENAI_MESSAGES_PATH,
    OPENAI_MODEL_ALIASES,
    OPENAI_RESPONSES_PATH,
    SYSTEM_STEERING_PROMPT,
    UPSTREAM_API_KEY,
    UPSTREAM_BASE_URL,
    UPSTREAM_TIMEOUT,
)
from any_proxy.app.keepalive import set_active_model, set_prefix_snapshot, touch_request
from any_proxy.logging.structured import _log
from any_proxy.routing.model import (
    align_prefix_3_region,
    clean_openai_model_name,
    detect_openai_endpoint_type,
    is_anthropic_native_model,
    is_openai_model,
    route_model_tier,
)
from any_proxy.routing.token_limits import clamp_max_tokens
from any_proxy.security.guardrails import sanitize_payload
from any_proxy.streaming.proxy_streamer import ProxyStreamer
from any_proxy.streaming.redactor import StreamingRedactor
from any_proxy.streaming.sse import _format_event, _sse_source, get_upstream_transport
from any_proxy.transform.anthropic_to_openai import (
    _extract_system_text,
    transform_anthropic_to_openai_chat,
    transform_anthropic_to_openai_responses,
)
from any_proxy.transform.openai_to_anthropic import (
    set_active_model as _set_transform_active_model,
)
from any_proxy.transform.openai_to_anthropic import (
    transform_openai_chat_to_anthropic_json,
    transform_openai_responses_sse_to_anthropic,
    transform_openai_responses_to_anthropic_json,
    transform_openai_sse_to_anthropic,
)

router = APIRouter()


def _upstream_headers(request: Request) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {UPSTREAM_API_KEY}",
        "x-api-key": UPSTREAM_API_KEY,  # Anthropic /v1/messages requires x-api-key, not Bearer
        "Content-Type": "application/json",
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
    }


def _mock_anthropic_json(model_name: str = "") -> Dict[str, Any]:
    """Canned non-stream Anthropic Messages JSON (mock mode)."""
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": "[MOCK] Non-stream response dari csmart. Format spec-compliant Anthropic. "
                    "Kalau Claude Code render ini, masalah ada di upstream (format ditolak).",
        }],
        "model": model_name or "claude-3-5-sonnet-20241022",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 28},
    }


async def _mock_anthropic_stream(model_name: str = "") -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    """Canned spec-compliant Anthropic Messages SSE stream (mock mode).

    Text-only at index 0 (no thinking). If this still fails to render, Claude
    Code's renderer is rejecting something else entirely.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    model = model_name or "claude-3-5-sonnet-20241022"
    text = ("[MOCK] Halo dari csmart — upstream di-skip. Text-only stream "
            "(no thinking block) untuk isolasi: kalau ini render, masalah thinking; "
            "kalau tidak, masalah lebih dalam.")
    yield "message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
    }
    yield "content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    yield "content_block_delta", {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }
    yield "content_block_stop", {"type": "content_block_stop", "index": 0}
    yield "message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 42, "input_tokens": 10},
    }
    # message_stop with usage (litellm pattern) — some strict clients read usage here
    yield "message_stop", {"type": "message_stop", "usage": {"input_tokens": 10, "output_tokens": 42}}


@router.post("/v1/messages", response_model=None)
async def handle_messages(request: Request) -> StreamingResponse | JSONResponse:
    touch_request()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    session_key = request.headers.get("x-csmart-session") or "default"
    if not UPSTREAM_API_KEY:
        _log("UPSTREAM_KEY_MISSING", warning=True)

    body = clamp_max_tokens(body)
    sanitize_payload(body)

    # -------------------------------------------------------------------------
    # Step 1: Detect OpenAI models BEFORE model tier routing overrides name
    # OpenAI detection is based on original model name from client request
    # -------------------------------------------------------------------------
    original_model = body.get("model", "")
    # Anthropic-native models (minimax-/qwen3) take precedence: they are served
    # by OpenCode Go's Anthropic-compatible /messages — not OpenAI protocol.
    is_anthropic_native = is_anthropic_native_model(original_model)
    is_openai = (not is_anthropic_native) and is_openai_model(original_model)
    endpoint_type = detect_openai_endpoint_type(original_model) if is_openai else "anthropic"
    cleaned_model = clean_openai_model_name(original_model)
    if is_openai:
        cleaned_model = OPENAI_MODEL_ALIASES.get(cleaned_model, cleaned_model)

    _log("OPENAI_DETECTION",
        original_model=original_model,
        cleaned_model=cleaned_model,
        is_openai=is_openai,
        is_anthropic_native=is_anthropic_native,
        endpoint_type=endpoint_type
    )

    # -------------------------------------------------------------------------
    # Step 2: Heuristic model tier routing (flash vs flagship)
    # -------------------------------------------------------------------------
    routed_model = route_model_tier(body, session_key)
    set_active_model(routed_model)
    _set_transform_active_model(routed_model)
    if is_anthropic_native:
        # OpenCode Go Anthropic-native: preserve the client model (minimax-m*/qwen3.*).
        # The tier router would rewrite it to deepseek-chat → 401 upstream.
        body["model"] = cleaned_model
    else:
        body["model"] = routed_model

    # -------------------------------------------------------------------------
    # Step 3: System Prompt Steering for OpenAI-native models
    # Inject before 3-region alignment so steering is part of the immutable prefix
    # -------------------------------------------------------------------------
    if is_openai:
        # Inject steering prompt into system (based on original detection).
        # PREPEND (first block) so the model reads it before the huge Claude Code
        # agent prompt — appended steering was drowned and the model fabricated
        # tasks on a bare greeting. First position = highest emphasis.
        steering_block = {"type": "text", "text": SYSTEM_STEERING_PROMPT}
        current_system = body.get("system", "")
        if isinstance(current_system, str):
            # Convert string to list format and prepend
            if current_system.strip():
                body["system"] = [
                    steering_block,
                    {"type": "text", "text": current_system},
                ]
            else:
                body["system"] = [steering_block]
        elif isinstance(current_system, list):
            # Already list format, prepend
            body["system"] = [steering_block] + current_system
        else:
            # Fallback: convert to string and prepend
            body["system"] = f"{SYSTEM_STEERING_PROMPT}\n\n{_extract_system_text(current_system)}"

    # -------------------------------------------------------------------------
    # 3-region prefix alignment (includes steering now for cache stability)
    # -------------------------------------------------------------------------
    body = align_prefix_3_region(body)
    set_prefix_snapshot({"system": body.get("system", []), "tools": body.get("tools", [])})

    _system_text = _extract_system_text(body.get("system", []))
    _log(
        "INBOUND_REQUEST",
        model=routed_model,
        session=session_key,
        messages=len(body.get("messages", [])),
        system_chars=len(_system_text),
        tools_count=len(body.get("tools", [])),
        is_openai=is_openai,
    )

    if is_openai:
        # OpenAI endpoints don't need anthropic-version header
        # Use separate OPENAI_API_KEY for OpenAI-native endpoints
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        # Select endpoint and transform request
        if endpoint_type == "chat_completions":
            upstream_url = f"{OPENAI_BASE_URL}{OPENAI_CHAT_COMPLETIONS_PATH}"
            transformed_body = transform_anthropic_to_openai_chat(body)
            transformed_body["model"] = cleaned_model
        elif endpoint_type == "responses":
            upstream_url = f"{OPENAI_BASE_URL}{OPENAI_RESPONSES_PATH}"
            transformed_body = transform_anthropic_to_openai_responses(body)
            transformed_body["model"] = cleaned_model
        else:
            upstream_url = f"{OPENAI_BASE_URL}{OPENAI_CHAT_COMPLETIONS_PATH}"
            transformed_body = transform_anthropic_to_openai_chat(body)
            transformed_body["model"] = cleaned_model

        _log("OPENAI_REQUEST_TRANSFORM",
            upstream_url=upstream_url,
            endpoint_type=endpoint_type,
            input_model=original_model,
            output_model=cleaned_model,
            input_messages=len(body.get("messages", [])),
            output_messages=(
                len(transformed_body.get("messages", []))
                if endpoint_type == "chat_completions"
                else len(transformed_body.get("input", []))
            )
        )
    else:
        if is_anthropic_native:
            # OpenCode Go Anthropic-compatible /messages: passthrough with the
            # client model preserved; Anthropic endpoints need x-api-key (K7).
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "x-api-key": OPENAI_API_KEY,
                "Content-Type": "application/json",
                "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
            }
            upstream_url = f"{OPENAI_BASE_URL}{OPENAI_MESSAGES_PATH}"
        else:
            # DeepSeek Anthropic-native endpoint includes anthropic-version header
            headers = _upstream_headers(request)
            upstream_url = f"{UPSTREAM_BASE_URL}/v1/messages"
        transformed_body = body

    # Debug: dump the exact upstream request once per env flag (CSMART_DUMP_BODY=1)
    if os.getenv("CSMART_DUMP_BODY") == "1":
        try:
            with open("/tmp/csmart-body-dump.json", "w") as _f:
                json.dump({"url": upstream_url, "body": transformed_body}, _f, indent=2)
            _log("BODY_DUMPED", path="/tmp/csmart-body-dump.json", url=upstream_url)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Step 3: Response transformation (OpenAI -> Anthropic format)
    # -------------------------------------------------------------------------
    async def generator() -> AsyncGenerator[bytes, None]:
        redactor = StreamingRedactor()

        if MOCK_MODE:
            _log("MOCK_STREAM", endpoint_type=endpoint_type, model=cleaned_model,
                 response_model=original_model)
            async for _event_name, anthropic_event in _mock_anthropic_stream(original_model):
                event_bytes = _format_event(_event_name, anthropic_event)
                out = redactor.feed(event_bytes.decode("utf-8", errors="replace"))
                if out:
                    yield out.encode("utf-8")
            final = redactor.flush()
            if final:
                yield final.encode("utf-8")
            return

        if is_openai and endpoint_type == "chat_completions":
            # For OpenAI chat completions: transform SSE format
            async for _event_name, anthropic_event in transform_openai_sse_to_anthropic(
                _sse_source("POST", upstream_url, headers, transformed_body)
            ):
                event_bytes = _format_event(_event_name, anthropic_event)
                out = redactor.feed(event_bytes.decode("utf-8", errors="replace"))
                if out:
                    yield out.encode("utf-8")
            final = redactor.flush()
            if final:
                yield final.encode("utf-8")
        elif is_openai and endpoint_type == "responses":
            # For OpenAI Responses API: special transform SSE format
            async for _event_name, anthropic_event in transform_openai_responses_sse_to_anthropic(
                _sse_source("POST", upstream_url, headers, transformed_body)
            ):
                event_bytes = _format_event(_event_name, anthropic_event)
                out = redactor.feed(event_bytes.decode("utf-8", errors="replace"))
                if out:
                    yield out.encode("utf-8")
            final = redactor.flush()
            if final:
                yield final.encode("utf-8")
        else:
            # Anthropic native: use existing ProxyStreamer which handles CCR/shadowing
            streamer = ProxyStreamer("POST", upstream_url, headers, transformed_body)
            async for chunk in streamer.run():
                out = redactor.feed(chunk.decode("utf-8", errors="replace"))
                if out:
                    yield out.encode("utf-8")
            final = redactor.flush()
            if final:
                yield final.encode("utf-8")

    # -------------------------------------------------------------------------
    # Step 4: Non-streaming path (Claude Code retries with stream:false when the
    # streaming attempt returns no events). Return a proper JSON response —
    # NOT an event-stream — otherwise Claude Code reports "malformed response".
    # -------------------------------------------------------------------------
    if body.get("stream") is False:
        _log("NON_STREAMING_REQUEST", endpoint_type=endpoint_type, model=cleaned_model)
        if MOCK_MODE:
            _log("MOCK_NON_STREAM", endpoint_type=endpoint_type, model=cleaned_model,
                 response_model=original_model)
            return JSONResponse(_mock_anthropic_json(original_model))
        try:
            transformed_body["stream"] = False
            payload_bytes = json.dumps(transformed_body, sort_keys=True).encode("utf-8")
            async with httpx.AsyncClient(
                transport=get_upstream_transport(), timeout=UPSTREAM_TIMEOUT
            ) as client:
                resp = await client.post(upstream_url, headers=headers, content=payload_bytes)
                resp_json = resp.json()
            if resp.status_code >= 400:
                _log("NON_STREAMING_UPSTREAM_ERROR", status=resp.status_code)
                raise HTTPException(status_code=502, detail=f"Upstream error: {resp.status_code}")

            if is_openai and endpoint_type == "responses":
                anthropic_json = transform_openai_responses_to_anthropic_json(resp_json, cleaned_model)
            elif is_openai and endpoint_type == "chat_completions":
                anthropic_json = transform_openai_chat_to_anthropic_json(resp_json, cleaned_model)
            else:
                anthropic_json = resp_json
            _log("NON_STREAMING_RESPONSE", content_blocks=len(anthropic_json.get("content", [])))
            return JSONResponse(anthropic_json)
        except HTTPException:
            raise
        except Exception as exc:
            _log("NON_STREAMING_ERROR", error=str(exc)[:200])
            raise HTTPException(status_code=502, detail=f"Non-streaming upstream failed: {exc}")

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router", "handle_messages"]