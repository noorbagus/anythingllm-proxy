"""any_proxy.transform.openai_responses_to_chat — Responses → Chat (pure).

Inbound: AnythingLLM Generic OpenAI sends chat.completions; we transform to
responses for opencode (anthropic_to_openai.transform_openai_chat_to_responses).
Outbound must do the reverse for AnythingLLM to render: responses SSE/JSON →
chat.completions SSE/JSON. Mock in handlers/openai.py already emits
chat.completion.chunk (choices.delta.content) — follow that format.

Pure, no DB. Uses streaming/sse helpers for formatting.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple


def _chat_id_from_resp(resp_id: str) -> str:
    if resp_id.startswith("resp_"):
        return "chatcmpl-" + resp_id[5:]
    if resp_id.startswith("chatcmpl-"):
        return resp_id
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def responses_json_to_chat_json(payload: Dict[str, Any], model: str = "") -> Dict[str, Any]:
    """Non-stream: Responses JSON → Chat JSON (AnythingLLM)."""
    # collect text
    texts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "message":
            for part in item.get("content", []):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    texts.append(str(part.get("text", "")))
        elif t == "function_call":
            # opencode Responses function_call shape: id, name, arguments (json string)
            call_id = str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}")
            name = str(item.get("name") or "")
            args = item.get("arguments") or item.get("input") or ""
            if isinstance(args, dict):
                args = json.dumps(args)
            tool_calls.append(
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": str(args)}}
            )
        elif t == "reasoning":
            # ignore encrypted_content
            continue

    content = "".join(texts)
    # fallback: some providers put output_text at top level
    if not content and isinstance(payload.get("output_text"), str):
        content = payload["output_text"]

    resp_id = str(payload.get("id", ""))
    chat_id = _chat_id_from_resp(resp_id)
    created = int(payload.get("created_at") or payload.get("created") or time.time())
    out_model = model or str(payload.get("model", ""))

    # finish reason
    finish = "tool_calls" if tool_calls else "stop"
    # incomplete_details / error handling passthrough
    if payload.get("status") == "incomplete":
        finish = "length"

    usage = payload.get("usage") or {}
    # normalize usage to chat shape if present
    chat_usage = None
    if usage:
        chat_usage = {
            "prompt_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
            "completion_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
            "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
            if "input_tokens" in usage
            else usage,
        }
        # if already chat shape, keep
        if "prompt_tokens" in usage and "completion_tokens" in usage:
            chat_usage = usage

    msg: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls

    chat: Dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": out_model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
    }
    if chat_usage:
        chat["usage"] = chat_usage
    return chat


async def responses_sse_to_chat_sse(
    sse_lines: AsyncGenerator[bytes, None],
    *,
    model: str,
    chat_id: Optional[str] = None,
    created: Optional[int] = None,
) -> AsyncGenerator[bytes, None]:
    """Stream: Responses SSE bytes → Chat SSE bytes (choices.delta.content).

    sse_lines: async bytes lines from upstream (raw chunk stream). We parse SSE
    frames internally. Emits chat.completion.chunk frames + [DONE].
    Handles text delta + tool-call delta similarly to
    transform/openai_to_anthropic but targeting chat shape.
    """
    from any_proxy.streaming.sse import _format_openai_chat_sse

    _chat_id = chat_id or f"chatcmpl-{uuid.uuid4().hex[:24]}"
    _created = created or int(time.time())
    _model = model

    # tool-call tracking
    tool_idx = 0
    tool_id_map: Dict[str, int] = {}
    pending_tool_name: Dict[int, str] = {}

    # buffer for SSE parsing (upstream may split across chunks)
    buf = ""
    text_emitted = 0

    async for raw_chunk in sse_lines:
        try:
            buf += raw_chunk.decode(errors="replace")
        except Exception:
            continue
        # process complete SSE frames (double newline)
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            frame = frame.strip()
            if not frame:
                continue
            # collect data: lines
            data_lines: List[str] = []
            for ln in frame.splitlines():
                if ln.startswith("data:"):
                    data_lines.append(ln[5:].strip())
                elif ln.startswith("event:"):
                    continue
                else:
                    data_lines.append(ln)
            if not data_lines:
                continue
            raw = "\n".join(data_lines).strip()
            if raw == "[DONE]":
                # upstream done -> emit chat stop + DONE
                done = {
                    "id": _chat_id,
                    "object": "chat.completion.chunk",
                    "created": _created,
                    "model": _model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield _format_openai_chat_sse(done)
                yield b"data: [DONE]\n\n"
                return
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            ptype = str(payload.get("type", ""))

            # capture id/model from response.created
            if ptype == "response.created":
                resp = payload.get("response", {}) if isinstance(payload.get("response"), dict) else {}
                rid = str(resp.get("id") or payload.get("id") or "")
                if rid:
                    _chat_id = _chat_id_from_resp(rid)
                _model = str(resp.get("model") or _model)
                _created = int(resp.get("created_at") or resp.get("created") or _created)
                continue
            if ptype == "response.in_progress":
                continue

            # text delta -> chat delta.content
            if ptype == "response.output_text.delta":
                delta = payload.get("delta", "")
                text = delta.get("text", "") if isinstance(delta, dict) else str(delta)
                if text:
                    text_emitted += len(text)
                    chunk = {
                        "id": _chat_id,
                        "object": "chat.completion.chunk",
                        "created": _created,
                        "model": _model,
                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                    }
                    yield _format_openai_chat_sse(chunk)
                continue

            if ptype == "response.output_text.done":
                # fallback if no delta was emitted
                if text_emitted == 0:
                    delta = payload.get("delta", "")
                    if isinstance(delta, dict):
                        text = str(delta.get("text", ""))
                    else:
                        text = str(delta) if delta else str(payload.get("text", ""))
                    # also try payload.text
                    if not text:
                        text = str(payload.get("text", ""))
                    if text:
                        chunk = {
                            "id": _chat_id,
                            "object": "chat.completion.chunk",
                            "created": _created,
                            "model": _model,
                            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                        }
                        yield _format_openai_chat_sse(chunk)
                continue

            # tool call start
            if ptype == "response.output_item.added":
                item = payload.get("item", {})
                if isinstance(item, dict) and item.get("type") == "function_call":
                    call_id = str(item.get("id") or item.get("call_id") or f"call_{uuid.uuid4().hex[:12]}")
                    name = str(item.get("name") or "")
                    idx = tool_id_map.get(call_id)
                    if idx is None:
                        idx = tool_idx
                        tool_id_map[call_id] = idx
                        tool_idx += 1
                    pending_tool_name[idx] = name
                    chunk = {
                        "id": _chat_id,
                        "object": "chat.completion.chunk",
                        "created": _created,
                        "model": _model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": idx,
                                            "id": call_id,
                                            "type": "function",
                                            "function": {"name": name, "arguments": ""},
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield _format_openai_chat_sse(chunk)
                continue

            if ptype == "response.function_call_arguments.delta":
                delta = str(payload.get("delta", ""))
                item_id = str(payload.get("item_id", ""))
                # map item_id to idx, fallback 0
                idx = tool_id_map.get(item_id, 0)
                if delta:
                    chunk = {
                        "id": _chat_id,
                        "object": "chat.completion.chunk",
                        "created": _created,
                        "model": _model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {"index": idx, "function": {"arguments": delta}}
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield _format_openai_chat_sse(chunk)
                continue

            if ptype == "response.function_call_arguments.done":
                continue

            if ptype == "response.completed":
                # emit stop if not already via [DONE]
                resp = payload.get("response", {})
                # determine finish reason from output
                finish = "stop"
                if isinstance(resp, dict):
                    for out in resp.get("output", []):
                        if isinstance(out, dict) and out.get("type") == "function_call":
                            finish = "tool_calls"
                            break
                done = {
                    "id": _chat_id,
                    "object": "chat.completion.chunk",
                    "created": _created,
                    "model": _model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                }
                yield _format_openai_chat_sse(done)
                yield b"data: [DONE]\n\n"
                return

            # reasoning / content_part etc. ignored for chat text
            # (encrypted_content not forwarded)

    # upstream ended without terminal -> ensure DONE
    done = {
        "id": _chat_id,
        "object": "chat.completion.chunk",
        "created": _created,
        "model": _model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield _format_openai_chat_sse(done)
    yield b"data: [DONE]\n\n"


__all__ = ["responses_json_to_chat_json", "responses_sse_to_chat_sse", "_chat_id_from_resp"]
