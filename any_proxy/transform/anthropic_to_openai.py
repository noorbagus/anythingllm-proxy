"""csmart.transform.anthropic_to_openai — Anthropic → OpenAI (pure).

Extracted verbatim from csmart_proxy.py (region 1229-1739): Anthropic Messages API
→ OpenAI Chat Completions / Responses API transforms. Pure — no SSE, no DB, no
security/vault, no logging. Keeps reasoning_content / reasoning effort / tool_calls
round-trip behavior. Never raises — returns transformed payload.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

def _extract_system_text(system: Any) -> str:
    """Extract concatenated system text from Anthropic system format (str or list)."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return " ".join(
            block.get("text", "") for block in system if isinstance(block, dict)
        )
    return str(system)


def _convert_anthropic_tool_to_openai(anthropic_tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Anthropic tool format (input_schema) → OpenAI Chat Completions tool format.

    Chat Completions nests everything under ``function``:
      {"type":"function","function":{"name":...,"parameters":...}}
    """
    return {
        "type": "function",
        "function": {
            "name": anthropic_tool["name"],
            "description": anthropic_tool.get("description", ""),
            "parameters": anthropic_tool.get("input_schema", {}),
        },
    }


def _convert_anthropic_tool_to_openai_responses(anthropic_tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Anthropic tool format → OpenAI Responses API tool format.

    Responses API puts ``name`` at the TOP level (flat), NOT nested under
    ``function``. Sending Chat Completions format here causes upstream 400:
    "tools[0] missing required field name".
    """
    return {
        "type": "function",
        "name": anthropic_tool["name"],
        "description": anthropic_tool.get("description", ""),
        "parameters": anthropic_tool.get("input_schema", {}),
    }


def _convert_anthropic_message_to_openai(anth_msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert Anthropic message → LIST of OpenAI Chat Completions messages.

    Returns a list because a turn may expand to multiple OpenAI messages:
    an assistant turn with text + tool_use becomes ONE assistant message holding
    both ``content`` and ``tool_calls``, while each ``tool_result`` becomes its
    own standalone ``role:"tool"`` message (OpenAI requires tool results as a
    separate message, not merged into the user turn).
    """
    role = anth_msg.get("role", "user")
    content = anth_msg.get("content", "")

    # Anthropic content is either str or list[blocks]
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": str(content)}]

    messages: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            if txt:
                text_parts.append(txt)
        elif btype == "thinking":
            # K2b: preserve chain-of-thought from history. DeepSeek chat path
            # carries CoT via reasoning_content on the assistant message (not a
            # separate field); OpenAI modern models use thinking_blocks. We emit
            # reasoning_content so the round-trip does not lose the thinking.
            txt = block.get("thinking", "")
            if txt:
                reasoning_parts.append(txt)
        elif btype == "tool_use":
            # OpenAI embeds tool calls into the assistant message itself
            tool_calls.append({
                "id": block.get("id") or f"call_{len(tool_calls)}",
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), sort_keys=True),
                },
            })
        elif btype == "tool_result":
            # tool_result is its own role:"tool" message in OpenAI chat format
            tool_use_id = block.get("tool_use_id", "")
            raw = block.get("content", "")
            if isinstance(raw, list):
                raw = "".join(
                    p.get("text", "") for p in raw if isinstance(p, dict)
                )
            messages.append({
                "role": "tool",
                "tool_call_id": tool_use_id,
                "content": str(raw),
            })

    # Emit the role message (assistant may carry both text and tool_calls).
    # System messages that appear mid-conversation are kept as-is; OpenAI
    # accepts a system message anywhere in the array. A user turn containing
    # ONLY tool_result blocks produces just role:"tool" messages and no empty
    # user stub (a bare {"role":"user","content":""} corrupts the sequence).
    if role == "system":
        return [{"role": "system", "content": "".join(text_parts)}]

    if tool_calls or text_parts:
        msg: Dict[str, Any] = {"role": role, "content": "".join(text_parts)}
        if reasoning_parts and role == "assistant":
            msg["reasoning_content"] = "\n".join(reasoning_parts)
        if tool_calls:
            msg["tool_calls"] = tool_calls
        messages.insert(0, msg)
    elif reasoning_parts and role == "assistant":
        # Assistant turn with ONLY a thinking block — still preserve the CoT.
        messages.insert(0, {
            "role": "assistant",
            "content": "",
            "reasoning_content": "\n".join(reasoning_parts),
        })

    return messages


def transform_anthropic_to_openai_chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Transform Anthropic Messages API payload → OpenAI Chat Completions API payload."""
    # Extract system prompt
    system_text = _extract_system_text(payload.get("system", ""))

    # Convert all messages
    messages: List[Dict[str, Any]] = []

    # Add system message first if non-empty
    if system_text.strip():
        messages.append({"role": "system", "content": system_text})

    # Add conversation messages
    for anth_msg in payload.get("messages", []):
        if isinstance(anth_msg, dict):
            messages.extend(_convert_anthropic_message_to_openai(anth_msg))

    # Build OpenAI payload
    openai_payload: Dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
        "stream": True,
    }

    # Copy optional parameters if present
    if "max_tokens" in payload:
        openai_payload["max_tokens"] = payload["max_tokens"]
    if "temperature" in payload:
        openai_payload["temperature"] = payload["temperature"]
    if "top_p" in payload:
        openai_payload["top_p"] = payload["top_p"]

    # Cap reasoning/chain-of-thought on the chat path (deepseek). Reasoning-model
    # CoT can burn tens of thousands of tokens on a trivial prompt (observed: 2.8k
    # thinking tokens + hallucinated tangents like "helm" for a bare greeting).
    # reasoning_effort is honored by the upstream; effort "low" trims CoT length and
    # wandering. Toggle via CSMART_REASONING_EFFORT (default low; "" disables).
    _effort = os.getenv("CSMART_REASONING_EFFORT", "low").strip()
    if _effort:
        openai_payload["reasoning_effort"] = _effort

    # Convert tools if present
    anthropic_tools = payload.get("tools", [])
    if anthropic_tools:
        openai_tools = [
            _convert_anthropic_tool_to_openai(tool) for tool in anthropic_tools
        ]
        openai_payload["tools"] = openai_tools
        # Enable parallel tool calls by default (Anthropic-like behavior)
        openai_payload["parallel_tool_calls"] = True

    return openai_payload


def _convert_anthropic_message_to_openai_responses(
    anth_msg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Convert Anthropic message → LIST of OpenAI Responses API input items.

    Returns a list because an assistant turn with tool_use must become separate
    ``message`` + ``function_call`` items in the Responses ``input`` array — the
    chat-completions ``tool_calls`` field is rejected by ``/v1/responses``.
    Likewise ``tool_result`` becomes a standalone ``function_call_output`` item.
    """
    role = anth_msg.get("role", "user")
    content = anth_msg.get("content", "")

    if isinstance(content, str):
        return [{"type": "message", "role": role, "content": content}]

    # Block format: [{"type":"text","text":...}] or tool_use/tool_result
    items: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    reasoning_parts: List[str] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            if txt:
                text_parts.append(txt)
        elif btype == "thinking" and role == "assistant":
            # K2b: preserve chain-of-thought from history as a Responses API
            # "reasoning" input item (summary list). Keeps prompt cache + CoT
            # intact across turns.
            txt = block.get("thinking", "")
            if txt:
                reasoning_parts.append(txt)
        elif btype == "tool_use":
            items.append({
                "type": "function_call",
                "call_id": block.get("id") or f"call_{len(items)}",
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {}), sort_keys=True),
            })
        elif btype == "tool_result":
            # tool_result carries the output of a prior tool_use -> function_call_output
            tool_use_id = block.get("tool_use_id", "")
            raw = block.get("content", "")
            if isinstance(raw, list):
                # Text parts join verbatim; non-text parts (dicts) become JSON
                # so the Responses function_call_output.output stays valid JSON.
                parts: List[str] = []
                for p in raw:
                    if isinstance(p, dict):
                        if p.get("type") == "text":
                            parts.append(p.get("text", ""))
                        else:
                            try:
                                parts.append(json.dumps(p, ensure_ascii=False))
                            except (TypeError, ValueError):  # non-serializable block
                                parts.append(str(p))
                    else:
                        parts.append(str(p))
                raw = "".join(parts)
            elif isinstance(raw, dict):
                try:
                    raw = json.dumps(raw, ensure_ascii=False)
                except (TypeError, ValueError):
                    raw = str(raw)
            items.append({
                "type": "function_call_output",
                "call_id": tool_use_id,
                "output": str(raw),
            })

    # Emit text FIRST so item order mirrors the Anthropic content order
    # (text block precedes tool blocks in the original turn). K2b: assistant
    # thinking is emitted as a "reasoning" item at the very front (thinking
    # precedes text/tool blocks in an Anthropic assistant turn).
    if text_parts:
        text = "\n".join(text_parts)
        if role == "assistant":
            items.insert(0, {
                "type": "message",
                "role": role,
                "content": [{"type": "output_text", "text": text}],
            })
        else:
            items.insert(0, {"type": "message", "role": role, "content": text})
    if reasoning_parts and role == "assistant":
        reasoning_items = [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": t}]}
            for t in reasoning_parts
        ]
        items[0:0] = reasoning_items

    # Preserve the original turn even when both text and tools are empty.
    if not items:
        items.append({"type": "message", "role": role, "content": ""})
    return items


def transform_anthropic_to_openai_responses(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Transform Anthropic Messages API payload → OpenAI Responses API payload.

    Matching OpenCode Go / OpenAI Responses API format.
    """
    system_text = _extract_system_text(payload.get("system", ""))
    input_items: List[Dict[str, Any]] = []
    for m in payload.get("messages", []):
        # Each Anthropic message flattens to 1..N Responses items (message +
        # separate function_call / function_call_output items).
        input_items.extend(_convert_anthropic_message_to_openai_responses(m))
    openai_payload: Dict[str, Any] = {
        "model": payload.get("model"),
        "instructions": system_text,
        "input": input_items,
        "stream": True,
    }
    if "max_tokens" in payload:
        openai_payload["max_output_tokens"] = payload["max_tokens"]
    if "temperature" in payload:
        openai_payload["temperature"] = payload["temperature"]
    if "top_p" in payload:
        openai_payload["top_p"] = payload["top_p"]
    # Copy tools if present (Anthropic tools → OpenAI Responses tool format:
    # flat with ``name`` at top level, NOT nested under ``function``).
    anthropic_tools = payload.get("tools", [])
    if anthropic_tools:
        openai_tools = [
            _convert_anthropic_tool_to_openai_responses(tool) for tool in anthropic_tools
        ]
        openai_payload["tools"] = openai_tools
        openai_payload["parallel_tool_calls"] = True
    # Map Anthropic reasoning/thinking -> Responses API reasoning effort.
    # OpenCode gateway only accepts: off / minimal / low / medium / high (rejects "max").
    effort = _resolve_reasoning_effort(payload)
    if effort is not None:
        openai_payload["reasoning"] = {"effort": effort}
    return openai_payload



def transform_openai_chat_to_responses(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Transform OpenAI Chat Completions payload -> OpenAI Responses payload.
    Used by handle_openai_chat when resolve_openai_endpoint() returns "responses"
    (e.g. muse-spark-1.2-contributor) but client sent /v1/chat/completions.
    """
    messages = payload.get("messages", [])
    system_text = ""
    input_items: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            if isinstance(content, str):
                if content.strip():
                    system_text = (system_text + "\n" + content) if system_text else content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        t = part.get("text", "")
                        system_text = (system_text + "\n" + t) if system_text else t
                    elif isinstance(part, str):
                        system_text = (system_text + "\n" + part) if system_text else part
            continue
        # tool_calls -> function_call items
        tool_calls = m.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            # preserve text content if any
            if content:
                if isinstance(content, str) and content.strip():
                    input_items.append({"type": "message", "role": role, "content": content})
                elif isinstance(content, list):
                    txt = "".join(
                        pp.get("text", "") for pp in content if isinstance(pp, dict) and pp.get("type") == "text"
                    )
                    if txt.strip():
                        input_items.append({"type": "message", "role": role, "content": txt})
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", tc) if isinstance(tc.get("function"), dict) else tc
                # tc may be flat or nested
                if "function" in tc and isinstance(tc["function"], dict):
                    fn = tc["function"]
                    call_id = tc.get("id") or fn.get("id") or f"call_{len(input_items)}"
                    name = fn.get("name", "")
                    arguments = fn.get("arguments", "")
                else:
                    call_id = tc.get("id") or tc.get("call_id") or f"call_{len(input_items)}"
                    name = tc.get("name", fn.get("name", ""))
                    arguments = tc.get("arguments", fn.get("arguments", ""))
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments, sort_keys=True)
                if not isinstance(arguments, str):
                    arguments = str(arguments)
                input_items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                })
            continue
        if role == "tool":
            tool_call_id = m.get("tool_call_id") or m.get("call_id") or ""
            output = content if isinstance(content, str) else json.dumps(content, sort_keys=True) if content is not None else ""
            input_items.append({
                "type": "function_call_output",
                "call_id": tool_call_id,
                "output": str(output),
            })
            continue
        # normal message
        if isinstance(content, str):
            input_items.append({"type": "message", "role": role, "content": content})
        elif isinstance(content, list):
            text_parts: List[str] = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        # image_url -> keep as text placeholder for now
                        img = part.get("image_url", {})
                        url = img.get("url", "") if isinstance(img, dict) else str(img)
                        if url:
                            text_parts.append(f"[image: {url}]")
                elif isinstance(part, str):
                    text_parts.append(part)
            txt = "\n".join(text_parts)
            if role == "assistant":
                input_items.append({"type": "message", "role": role, "content": [{"type": "output_text", "text": txt}]})
            else:
                input_items.append({"type": "message", "role": role, "content": txt})
        elif content is not None:
            input_items.append({"type": "message", "role": role, "content": str(content)})
    openai_payload: Dict[str, Any] = {
        "model": payload.get("model"),
        "input": input_items,
        "stream": payload.get("stream", False),
    }
    if system_text.strip():
        openai_payload["instructions"] = system_text.strip()
    # map common params
    if "temperature" in payload:
        openai_payload["temperature"] = payload["temperature"]
    if "top_p" in payload:
        openai_payload["top_p"] = payload["top_p"]
    if "max_tokens" in payload:
        openai_payload["max_output_tokens"] = payload["max_tokens"]
    if "max_completion_tokens" in payload:
        openai_payload["max_output_tokens"] = payload["max_completion_tokens"]
    if "max_output_tokens" in payload:
        openai_payload["max_output_tokens"] = payload["max_output_tokens"]
    # tools: chat format {type:function, function:{name,description,parameters}} -> responses flat
    if "tools" in payload and isinstance(payload["tools"], list):
        chat_tools = payload["tools"]
        resp_tools: List[Dict[str, Any]] = []
        for t in chat_tools:
            if not isinstance(t, dict):
                continue
            if "function" in t and isinstance(t["function"], dict):
                fn = t["function"]
                resp_tools.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                })
            else:
                # already flat or unknown - pass through
                resp_tools.append(t)
        if resp_tools:
            openai_payload["tools"] = resp_tools
    if "tool_choice" in payload:
        openai_payload["tool_choice"] = payload["tool_choice"]
    if "parallel_tool_calls" in payload:
        openai_payload["parallel_tool_calls"] = payload["parallel_tool_calls"]
    if "reasoning_effort" in payload:
        openai_payload["reasoning"] = {"effort": payload["reasoning_effort"]}
    return openai_payload


def _resolve_reasoning_effort(payload: Dict[str, Any]) -> Optional[str]:
    """Resolve Anthropic reasoning/thinking config to an OpenAI Responses effort.

    Order: explicit ``reasoning.effort`` > ``thinking`` block > env default.
    ``max`` is clamped to ``high``. ``off``/``none``/empty returns ``None``
    (upstream opencode.ai/Console Go rejects the literal string ``off`` — it
    expects ``none`` or the field omitted entirely).

    Returns None when the resolved effort is "off" so the caller skips the
    ``reasoning`` field in the upstream payload.
    ``max`` is clamped to ``high`` (OpenCode rejects it). Returns None when no
    signal is present and no env override is set (provider default applies).
    """
    _ALLOWED = ("none", "minimal", "low", "medium", "high", "xhigh")
    _DISABLED = ("off", "none", "disabled", "")

    def _clamp(effort: Any) -> Optional[str]:
        e = str(effort).strip().lower()
        if e in _DISABLED:
            return None
        if e == "max":
            return "high"
        return e if e in _ALLOWED else "low"

    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort is not None:
            return _clamp(effort)
    thinking = payload.get("thinking")
    if isinstance(thinking, dict):
        # thinking.enabled=true + budget_tokens → medium; thinking absent → None
        if thinking.get("type") == "disabled" or thinking.get("enabled") is False:
            return None
        if thinking.get("enabled") is True or thinking.get("type") == "enabled":
            return "medium"
    env_override = os.getenv("CSMART_REASONING_EFFORT", "").strip().lower()
    if env_override:
        return _clamp(env_override)
    return None

__all__ = [
    "transform_anthropic_to_openai_chat",
    "transform_anthropic_to_openai_responses",
    "transform_openai_chat_to_responses",
    "_extract_system_text",
    "_convert_anthropic_tool_to_openai",
    "_convert_anthropic_tool_to_openai_responses",
    "_convert_anthropic_message_to_openai",
    "_convert_anthropic_message_to_openai_responses",
    "_resolve_reasoning_effort",
]
