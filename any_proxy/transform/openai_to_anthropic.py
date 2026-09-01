"""csmart.transform.openai_to_anthropic - OpenAI -> Anthropic (SSE + JSON).

Extracted verbatim from csmart_proxy.py (region 1742-2539): OpenAI Chat/Responses
SSE + JSON -> Anthropic Messages. Reuses SSE helpers from any_proxy.streaming.sse
(_parse_sse_data/_iter_sse_events/_format_event/_safe_json_loads) - no reimplementation.
Preserves reasoning_content->thinking, streaming tool_calls, cache_read_input_tokens.
Mock helpers (_mock_anthropic_json/_mock_anthropic_stream) -> W3 mock-mode caller.

Deps (one-way, cycle-free): streaming.sse, logging.structured, app.config.
No import of transform.anthropic_to_openai (parallel track).
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from any_proxy.app.config import FLASH_MODEL
from any_proxy.logging.structured import _log
from any_proxy.streaming.sse import _format_event, _iter_sse_events, _parse_sse_data, _safe_json_loads

_active_model: str = FLASH_MODEL


def set_active_model(model: str) -> None:
    global _active_model
    _active_model = model


async def transform_openai_sse_to_anthropic(
    sse_events: AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None],
) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    """Transform OpenAI Chat Completions SSE stream → Anthropic Messages SSE stream."""
    sent_message_start = False
    # If the upstream dies without a terminal event (message_stop via [DONE] or
    # finish_reason), emit an explicit incomplete-stream error — never leave the client hanging.
    saw_terminal_event = False
    events_processed = 0
    text_emitted = 0
    # deepseek delta.reasoning_content -> Anthropic thinking block at index 0.
    reasoning_open = False
    text_index = 0
    text_started = False
    emit_reasoning = os.getenv("CSMART_EMIT_REASONING", "1") == "1"
    # Streaming tool_use: block opened with content_block_start, args via
    # input_json_delta, closed with content_block_stop. Map OpenAI tool index -> block.
    openai_tool_index_to_block = {}  # openai delta index -> anthropic block index
    open_tool_blocks = []            # anthropic block indices currently open (in order)
    # usage.prompt_tokens_details.cached_tokens -> cache_read_input_tokens (emit when > 0).
    cache_read_tokens = 0

    def _stop_reason(fr: Any) -> str:
        return "max_tokens" if fr == "length" else ("tool_use" if fr == "tool_calls" else "end_turn")

    def _msg_delta(stop: str) -> Dict[str, Any]:
        usage = {"input_tokens": 0, "output_tokens": text_emitted}
        # Emit cache_read_input_tokens only when a real value was captured (> 0).
        if cache_read_tokens > 0:
            usage["cache_read_input_tokens"] = cache_read_tokens
        return {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": usage,
        }

    def _msg_stop_usage() -> Dict[str, Any]:
        usage = {"input_tokens": 0, "output_tokens": text_emitted}
        if cache_read_tokens > 0:
            usage["cache_read_input_tokens"] = cache_read_tokens
        return usage

    _log("OPENAI_SSE_TRANSFORM_START", status="started")

    async for _, openai_event in sse_events:
        events_processed += 1
        # Capture prompt caching usage from the final chunk (OpenAI sends usage
        # non-null on the last chunk). prompt_tokens_details.cached_tokens -> cache_read.
        oai_usage = openai_event.get("usage")
        if isinstance(oai_usage, dict):
            ptd = (oai_usage.get("prompt_tokens_details") or {})
            if isinstance(ptd, dict):
                ct = ptd.get("cached_tokens")
                if isinstance(ct, (int, float)) and ct > 0:
                    cache_read_tokens = int(ct)
        # OpenAI sends [DONE] at end of stream (marked as sentinel dict)
        if openai_event.get("__openai_done"):
            if reasoning_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
            if text_started:
                yield "content_block_stop", {"type": "content_block_stop", "index": text_index}
            for block_idx in open_tool_blocks:
                yield "content_block_stop", {"type": "content_block_stop", "index": block_idx}
            open_tool_blocks.clear()
            _log("OPENAI_SSE_TRANSFORM_DONE", events_processed=events_processed, text_emitted=text_emitted)
            yield "message_delta", _msg_delta("end_turn")
            yield "message_stop", {"type": "message_stop", "usage": _msg_stop_usage()}
            saw_terminal_event = True
            break

        choices = openai_event.get("choices", [])
        if not choices:
            _log("OPENAI_SSE_SKIP", reason="no_choices")
            continue

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        if not sent_message_start:
            yield "message_start", {
                "type": "message_start",
                "message": {
                    "id": f"msg_{uuid.uuid4().hex[:24]}",
                    "type": "message",
                    "role": "assistant",
                    "model": _active_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
            sent_message_start = True

        # Handle reasoning_content (deepseek chain-of-thought) -> thinking block
        reasoning = delta.get("reasoning_content")
        if emit_reasoning and reasoning and not reasoning_open:
            # First reasoning chunk opens a thinking block at index 0.
            # LiteLLM emits signature:"" for non-signable providers (deepseek).
            yield "content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            }
            reasoning_open = True
            text_index = 1
        if reasoning_open and reasoning:
            yield "content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": reasoning},
            }

        if "content" in delta and delta["content"] is not None:
            text = delta["content"]
            if reasoning_open:
                # Thinking block finished; visible answer lives at index 1.
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
                reasoning_open = False
            if text:
                if not text_started:
                    yield "content_block_start", {
                        "type": "content_block_start",
                        "index": text_index,
                        "content_block": {"type": "text", "text": ""},
                    }
                    text_started = True
                text_emitted += len(text)
                yield "content_block_delta", {
                    "type": "content_block_delta",
                    "index": text_index,
                    "delta": {"type": "text_delta", "text": text},
                }

        if "tool_calls" in delta and delta["tool_calls"] is not None:
            if reasoning_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
                reasoning_open = False
            for tool_call_delta in delta["tool_calls"]:
                oai_index = tool_call_delta.get("index", 0)
                fn = tool_call_delta.get("function", {})
                name = fn.get("name")
                args = fn.get("arguments", "")
                tool_id = tool_call_delta.get("id")

                # First chunk of a tool call carries id + name -> open the block.
                if oai_index not in openai_tool_index_to_block:
                    block_idx = text_index + 1 + len(open_tool_blocks)  # continuous indexing
                    openai_tool_index_to_block[oai_index] = block_idx
                    open_tool_blocks.append(block_idx)
                    yield "content_block_start", {
                        "type": "content_block_start",
                        "index": block_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id or "",
                            "name": name or "",
                            "input": {},
                        },
                    }
                block_idx = openai_tool_index_to_block[oai_index]

                # Arguments stream in subsequent chunks -> emit deltas.
                if args:
                    yield "content_block_delta", {
                        "type": "content_block_delta",
                        "index": block_idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args,
                        },
                    }

        if finish_reason is not None:
            if reasoning_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
            if text_started:
                yield "content_block_stop", {"type": "content_block_stop", "index": text_index}
            for block_idx in open_tool_blocks:
                yield "content_block_stop", {"type": "content_block_stop", "index": block_idx}
            open_tool_blocks.clear()
            yield "message_delta", _msg_delta(_stop_reason(finish_reason))
            yield "message_stop", {"type": "message_stop", "usage": _msg_stop_usage()}
            saw_terminal_event = True
            break

    # Upstream ended without a terminal event (connection drop / timeout).
    # Never leave the client hanging: emit a spec-compliant incomplete error.
    if not saw_terminal_event:
        _log("OPENAI_SSE_TRANSFORM_INCOMPLETE", events_processed=events_processed, text_emitted=text_emitted)
        yield "error", {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": ("csmart: provider stream ended before message_stop; "
                            "response incomplete, content may be truncated"),
            },
        }


async def transform_openai_responses_sse_to_anthropic(
    sse_events: AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None],
) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    """Transform OpenAI Responses API SSE stream → Anthropic Messages SSE stream.

    Event map: response.created -> message_start; output_text.delta -> text
    content_block_delta; output_item.added(fn_call) -> tool_use start; function_call_
    arguments.delta -> input_json delta; output_item.done(fn_call) -> tool_use stop;
    response.completed -> message_stop; ping/metadata skipped.
    """
    # id/model/usage captured incrementally (response.created / .completed) so the
    # strict Anthropic parser gets a full message_start shape.
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    model_name = ""
    input_tokens = 0
    output_tokens = 0
    stop_reason = "end_turn"
    sent_message_start = False
    text_block_started = False
    text_emitted = 0
    tool_index = 0  # running index for tool_use content blocks
    tool_args_streamed = False  # whether partial args were streamed for current tool
    counts: Dict[str, int] = {}  # per upstream event type, for observability
    # K2b: reasoning -> thinking block at high index 1000 (avoid colliding with
    # text index 0 and tool blocks 1..N); closed on .done / text / tool.
    thinking_block_open = False
    thinking_index = 1000
    # K3: usage.input_tokens_details.cached_tokens -> cache_read_input_tokens (>0 only).
    cache_read_tokens = 0

    def _ms_payload() -> Dict[str, Any]:
        """Full Anthropic message_start payload with id/model/usage."""
        return {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model_name,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        }

    def _md_payload() -> Dict[str, Any]:
        """Anthropic message_delta payload (stop_reason + final usage)."""
        usage = {
            "output_tokens": output_tokens,
            "input_tokens": input_tokens,
        }
        if cache_read_tokens > 0:
            usage["cache_read_input_tokens"] = cache_read_tokens
        return {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": usage,
        }

    _log("OPENAI_RESPONSES_SSE_TRANSFORM", status="started")

    async for event_name, openai_event in sse_events:
        event_type = event_name
        if event_type is None:
            continue
        counts[event_type] = counts.get(event_type, 0) + 1

        # ---- upstream failure: never swallow it (would yield an empty 200) ---
        if event_type == "error":
            err = openai_event.get("error", {})
            if not isinstance(err, dict):
                err = {}
            # Log only the error class — never the raw body (can echo request fragments).
            _log("UPSTREAM_ERROR",
                 status_code=err.get("status_code"),
                 error_type=str(err.get("type", ""))[:80] if isinstance(err, dict) else "")
            yield "error", {
                "type": "error",
                "error": {
                    "type": "upstream_error",
                    "status_code": err.get("status_code"),
                    "message": f"csmart upstream error: {err.get('message', 'upstream rejected request')}",
                },
            }
            break

        # ---- lifecycle ---------------------------------------------------
        if event_type == "response.created":
            resp_info = openai_event.get("response", {}) or {}
            if isinstance(resp_info, dict):
                rid = resp_info.get("id")
                if isinstance(rid, str) and rid:
                    msg_id = f"msg_{rid[:24]}"
                rmodel = resp_info.get("model")
                if isinstance(rmodel, str) and rmodel:
                    model_name = rmodel
            yield "message_start", _ms_payload()
            sent_message_start = True
            continue

        if event_type == "response.completed":
            if thinking_block_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                thinking_block_open = False
            if text_block_started:
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
            resp_info = openai_event.get("response", {}) or {}
            status = "completed"
            if isinstance(resp_info, dict):
                status = resp_info.get("status", "completed") or "completed"
                # Capture final usage (overrides placeholder 0s from message_start)
                usage = resp_info.get("usage", {}) or {}
                if isinstance(usage, dict):
                    input_tokens = int(usage.get("input_tokens", 0) or 0)
                    output_tokens = int(usage.get("output_tokens", 0) or 0)
                    itd = usage.get("input_tokens_details") or {}
                    if isinstance(itd, dict):
                        ct = itd.get("cached_tokens")
                        if isinstance(ct, (int, float)) and ct > 0:
                            cache_read_tokens = int(ct)
                # stop_reason: tool_use if last item is function_call; else map
                # incomplete_details.reason to Anthropic stop_reason.
                output_items = resp_info.get("output", []) or []
                if isinstance(output_items, list) and output_items:
                    last = output_items[-1]
                    if isinstance(last, dict) and last.get("type") == "function_call":
                        stop_reason = "tool_use"
                if status != "completed":
                    inc = resp_info.get("incomplete_details", {}) or {}
                    reason = inc.get("reason", "") if isinstance(inc, dict) else ""
                    if reason == "max_output_tokens":
                        stop_reason = "max_tokens"
                    elif reason == "content_filter":
                        stop_reason = "refusal"
            if status != "completed" and text_emitted == 0 and tool_index == 0:
                # Never swallow an empty failed upstream (would look like an empty "done").
                _log("UPSTREAM_ERROR", status_code=None,
                     message=f"upstream response status={status}, no text emitted")
                yield "error", {
                    "type": "error",
                    "error": {"type": "upstream_incomplete",
                              "message": f"csmart upstream: response status={status}, no text",
                              "status_code": None},
                }
                break  # error is terminal — do not emit completion events after it
            # message_delta (stop_reason + final usage) BEFORE message_stop
            yield "message_delta", _md_payload()
            _stop_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
            if cache_read_tokens > 0:
                _stop_usage["cache_read_input_tokens"] = cache_read_tokens
            yield "message_stop", {"type": "message_stop", "usage": _stop_usage}
            _log("OPENAI_RESPONSES_SSE_TRANSFORM", status="completed", text_emitted=text_emitted,
                 upstream_status=status, events=counts, stop_reason=stop_reason,
                 input_tokens=input_tokens, output_tokens=output_tokens)
            break

        # ---- text streaming (PRIMARY source of assistant text) ------------
        if event_type == "response.output_text.delta":
            if not sent_message_start:
                yield "message_start", _ms_payload()
                sent_message_start = True
            if thinking_block_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                thinking_block_open = False
            if not text_block_started:
                yield "content_block_start", {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
                text_block_started = True
            # Responses API: delta is a STRING ("delta": "text..."), not {"text": ...}.
            delta = openai_event.get("delta", "")
            text = delta.get("text", "") if isinstance(delta, dict) else delta
            if text:
                text_emitted += len(text)
                yield "content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                }
            continue

        # ---- final text (no-delta providers) ------------------------------
        if event_type == "response.output_text.done":
            # Providers may emit only the final full text; skip if already streamed.
            if text_emitted == 0:
                delta = openai_event.get("delta", "")
                text = delta.get("text", "") if isinstance(delta, dict) else delta
                if not text:
                    text = openai_event.get("text", "")
                    if isinstance(text, dict):
                        text = text.get("text", "")
                if text:
                    if not sent_message_start:
                        yield "message_start", _ms_payload()
                        sent_message_start = True
                    if thinking_block_open:
                        yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                        thinking_block_open = False
                    if not text_block_started:
                        yield "content_block_start", {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        }
                        text_block_started = True
                    text_emitted += len(text)
                    yield "content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    }
            continue

        # ---- tool call start ---------------------------------------------
        if event_type == "response.output_item.added":
            item = openai_event.get("item", {})
            if item.get("type") == "function_call":
                if thinking_block_open:
                    yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                    thinking_block_open = False
                tool_index += 1
                tool_args_streamed = False
                yield "content_block_start", {
                    "type": "content_block_start",
                    "index": tool_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": item.get("call_id") or item.get("id") or f"call_{tool_index}",
                        "name": item.get("name", ""),
                        "input": {},
                    },
                }
            elif item.get("type") == "message" and not sent_message_start:
                yield "message_start", _ms_payload()
                sent_message_start = True
            continue

        # ---- tool call args streaming ------------------------------------
        if event_type == "response.function_call_arguments.delta":
            args = openai_event.get("delta", "")
            if args:
                tool_args_streamed = True
                yield "content_block_delta", {
                    "type": "content_block_delta",
                    "index": tool_index,
                    "delta": {"type": "input_json_delta", "partial_json": args},
                }
            continue

        # ---- tool call args final (no-delta providers) --------------------
        if event_type == "response.function_call_arguments.done":
            # Some providers emit ONLY the full final args string (in {"delta": ...}).
            # If nothing was streamed yet, emit the full string so input isn't left {}.
            if not tool_args_streamed:
                args = openai_event.get("delta", "")
                if isinstance(args, dict):
                    args = args.get("arguments") or args.get("delta") or ""
                if not args:
                    args = openai_event.get("arguments", "")
                if args:
                    tool_args_streamed = True  # guard a duplicate/late .done
                    yield "content_block_delta", {
                        "type": "content_block_delta",
                        "index": tool_index,
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    }
            continue

        # ---- finalization events ------------------------------------------
        if event_type == "response.output_item.done":
            item = openai_event.get("item", {})
            if item.get("type") == "function_call":
                # C1b: args may arrive ONLY in final output_item.done (no function_call_
                # arguments.delta/.done fire). Backfill so input isn't left {}. Guarded.
                if not tool_args_streamed:
                    args = item.get("arguments", "")
                    if isinstance(args, dict):
                        try:
                            args = json.dumps(args, sort_keys=True)
                        except (TypeError, ValueError):
                            args = str(args)
                    if args:
                        tool_args_streamed = True
                        yield "content_block_delta", {
                            "type": "content_block_delta",
                            "index": tool_index,
                            "delta": {"type": "input_json_delta", "partial_json": args},
                        }
                yield "content_block_stop", {"type": "content_block_stop", "index": tool_index}
            elif item.get("type") == "message":
                # Safety net: emit full text if deltas never fired.
                if not text_block_started:
                    content = item.get("content", [])
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            txt = part.get("text", "")
                            if txt:
                                if not sent_message_start:
                                    yield "message_start", _ms_payload()
                                    sent_message_start = True
                                yield "content_block_start", {
                                    "type": "content_block_start",
                                    "index": 0,
                                    "content_block": {"type": "text", "text": ""},
                                }
                                text_block_started = True
                                text_emitted += len(txt)
                                yield "content_block_delta", {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {"type": "text_delta", "text": txt},
                                }
            continue

        # ---- reasoning / thinking stream (Responses API) ------------------
        # response.reasoning_summary_text.delta / response.reasoning_text.delta
        # carry chain-of-thought fragments -> Anthropic thinking block at the
        # anti-collision index. Closed on .done, or when text/tool appears.
        if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            if not sent_message_start:
                yield "message_start", _ms_payload()
                sent_message_start = True
            if not thinking_block_open:
                yield "content_block_start", {
                    "type": "content_block_start",
                    "index": thinking_index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                }
                thinking_block_open = True
            delta = openai_event.get("delta", "")
            text = delta.get("text", "") if isinstance(delta, dict) else delta
            if text:
                yield "content_block_delta", {
                    "type": "content_block_delta",
                    "index": thinking_index,
                    "delta": {"type": "thinking_delta", "thinking": text},
                }
            continue

        if event_type in ("response.reasoning_summary_text.done", "response.reasoning_text.done"):
            if thinking_block_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                thinking_block_open = False
            continue

        # ---- everything else (ping, response.in_progress, content_part.*) skipped
        continue


def transform_openai_responses_to_anthropic_json(
    payload: Dict[str, Any], model: str = ""
) -> Dict[str, Any]:
    """Transform OpenAI Responses API JSON response → Anthropic Messages JSON response.

    Non-streaming path. Responses output items: message (output_text parts),
    reasoning (summary parts), function_call (tool_use).
    """
    content: List[Dict[str, Any]] = []
    stop_reason = "end_turn"

    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for part in item.get("content", []) or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    txt = part.get("text", "")
                    if txt:
                        content.append({"type": "text", "text": txt})
        elif itype == "reasoning":
            # Responses API: reasoning summary is a list of parts, each with "text".
            for part in (item.get("summary") or []):
                if isinstance(part, dict) and part.get("text"):
                    content.append({
                        "type": "thinking",
                        "thinking": part.get("text"),
                        "signature": None,
                    })
        elif itype == "function_call":
            content.append({
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id") or f"call_{len(content)}",
                "name": item.get("name", ""),
                "input": _safe_json_loads(item.get("arguments", "")),
            })
            stop_reason = "tool_use"

    usage = payload.get("usage", {}) or {}
    anthropic_usage = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }
    # K3: OpenAI usage.input_tokens_details.cached_tokens -> cache_read_input_tokens
    itd = usage.get("input_tokens_details") or {}
    if isinstance(itd, dict):
        ct = itd.get("cached_tokens")
        if ct:
            anthropic_usage["cache_read_input_tokens"] = int(ct)
    return {
        "id": f"msg_{payload.get('id', 'resp')[:8]}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model or payload.get("model", ""),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


def transform_openai_chat_to_anthropic_json(
    payload: Dict[str, Any], model: str = ""
) -> Dict[str, Any]:
    """Transform OpenAI Chat Completions JSON response → Anthropic Messages JSON response."""
    content: List[Dict[str, Any]] = []
    stop_reason = "end_turn"
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    # Thinking block must precede text in Anthropic content
    rc = msg.get("reasoning_content")
    if isinstance(rc, str) and rc.strip():
        content.append({"type": "thinking", "thinking": rc, "signature": None})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {}) or {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or f"call_{len(content)}",
            "name": fn.get("name", ""),
            "input": _safe_json_loads(fn.get("arguments", "")),
        })
        stop_reason = "tool_use"
    usage = payload.get("usage", {}) or {}
    anthropic_usage = {
        "input_tokens": int(usage.get("prompt_tokens", 0)),
        "output_tokens": int(usage.get("completion_tokens", 0)),
    }
    # K3: OpenAI usage.prompt_tokens_details.cached_tokens -> cache_read_input_tokens
    ptd = usage.get("prompt_tokens_details") or {}
    if isinstance(ptd, dict):
        ct = ptd.get("cached_tokens")
        if ct:
            anthropic_usage["cache_read_input_tokens"] = int(ct)
    return {
        "id": f"msg_{payload.get('id', 'chat')[:8]}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model or payload.get("model", ""),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


__all__ = [
    "transform_openai_sse_to_anthropic",
    "transform_openai_responses_sse_to_anthropic",
    "transform_openai_responses_to_anthropic_json",
    "transform_openai_chat_to_anthropic_json",
    "set_active_model",
]