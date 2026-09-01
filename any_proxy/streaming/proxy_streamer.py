"""csmart.streaming.proxy_streamer — ProxyStreamer (SSE shadow + CCR + guardrails).

Stream upstream SSE to the client, shadowing tool_use locally:
- ``csmart_expand_symbol`` -> expand from CCR (reversible compression).
- guardrail violation      -> blocked result (secrets never reach client/upstream).
Other tool_use streams through unchanged (client executes it).

Deps (one-way): streaming.sse (_sse_source/_format_event), logging.structured
(get_db/_log), app.config (MAX_ROUNDS), security.guardrails (SOFT import,
injectable via guardrail_fn so T-C does not block parallel T-B).
No cycle: this module imports no handler/routing.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from any_proxy.streaming.sse import _format_event, _sse_source

try:
    from any_proxy.app.config import CCR_PREVIEW_LINES, MAX_ROUNDS
except ImportError:  # pragma: no cover
    MAX_ROUNDS = int(os.getenv("CSMART_MAX_SHADOW_ROUNDS", "5"))
    CCR_PREVIEW_LINES = int(os.getenv("CSMART_CCR_PREVIEW_LINES", "10"))

try:
    from any_proxy.logging.structured import _log, get_db
except ImportError:  # pragma: no cover
    def _log(event: str, **fields: Any) -> None:  # type: ignore[misc]
        return None

    def get_db():  # type: ignore[misc]
        raise NotImplementedError("get_db unavailable")

# Soft import — injectable via guardrail_fn so this module stays buildable
# while T-B (security/guardrails.py) is still in flight.
try:
    from any_proxy.security.guardrails import check_security_guardrails as _default_guardrails
except ImportError:  # pragma: no cover
    _default_guardrails = None  # type: ignore[assignment]


def store_ccr_payload(payload_type: str, content: str) -> Tuple[str, str]:
    """Persist a large payload to SQLite, return (ref_id, compact stub)."""
    ref_id = f"ref_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:8]}"
    token_est = len(content) // 4
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO context_blobs (ref_id, payload_type, raw_content, token_count) VALUES (?, ?, ?, ?)",
                (ref_id, payload_type, content, token_est),
            )
            conn.commit()
    except Exception as exc:
        _log("CCR_PUT", error=str(exc))
    lines = content.splitlines()
    preview = "\n".join(lines[:CCR_PREVIEW_LINES]) if len(lines) > CCR_PREVIEW_LINES else content
    stub = (
        f"{preview}\n\n"
        f"[CSMART CCR: konten penuh ({token_est} tokens) tersimpan di {ref_id}. "
        f"Gunakan tool 'csmart_expand_symbol' dengan ref_id='{ref_id}' bila perlu isi lengkap.]"
    )
    return ref_id, stub


def get_ccr_payload(ref_id: str) -> Optional[str]:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT raw_content FROM context_blobs WHERE ref_id = ?", (ref_id,)
            ).fetchone()
        if row:
            return row["raw_content"]
    except Exception:
        pass
    return None


class ProxyStreamer:
    """Stream upstream SSE to the client, shadowing tool_use locally:
    - ``csmart_expand_symbol`` -> expand from CCR (reversible compression).
    - guardrail violation      -> blocked result (secrets never reach client/upstream).
    Other tool_use streams through unchanged (client executes it)."""

    def __init__(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Dict[str, Any],
        guardrail_fn: Optional[Any] = None,
        websearch_fn: Optional[Any] = None,
    ) -> None:
        self.method = method
        self.url = url
        self.headers = headers
        self.body = body
        self.round = 1
        self.client_index = 0
        self._pending_held: List[Dict[str, Any]] = []
        self._guardrail_fn = guardrail_fn if guardrail_fn is not None else _default_guardrails
        self._websearch_fn = websearch_fn

    def set_guardrail_fn(self, fn: Optional[Any]) -> None:
        """Late-bind guardrail function (e.g. after T-B lands)."""
        self._guardrail_fn = fn

    def set_websearch_fn(self, fn: Optional[Any]) -> None:
        """Late-bind web search executor (normally _websearch_exa from guardrails)."""
        self._websearch_fn = fn

    async def run(self) -> AsyncGenerator[bytes, None]:
        for _ in range(MAX_ROUNDS):
            messages = self.body.get("messages", [])
            self._pending_held = []
            async for chunk in self._stream_round(messages):
                yield chunk
            held = self._pending_held
            if not held:
                return
            self.body = {**self.body, "messages": self._build_followup(messages, held)}
        yield _format_event(
            "error",
            {"type": "error", "error": {"type": "max_shadow_rounds", "message": "csmart: too many shadow rounds"}},
        )

    async def _stream_round(
        self, messages: List[Dict[str, Any]]
    ) -> AsyncGenerator[bytes, None]:
        held_indices: set[int] = set()
        held_by_index: Dict[int, Dict[str, Any]] = {}
        pending: Dict[int, List[Tuple[Optional[str], Dict[str, Any]]]] = {}
        client_index_map: Dict[int, int] = {}
        buffered_end: List[Tuple[Optional[str], Dict[str, Any]]] = []

        async for event_name, payload in _sse_source(
            self.method, self.url, self.headers, {**self.body, "messages": messages}
        ):
            etype = payload.get("type", "")

            if etype == "message_start":
                if self.round == 1:
                    yield _format_event(event_name, payload)
                continue

            if etype in ("message_delta", "message_stop"):
                buffered_end.append((event_name, payload))
                continue

            if etype == "content_block_start":
                index = payload.get("index")
                if not isinstance(index, int):
                    yield _format_event(event_name, payload)
                    continue
                cb = payload.get("content_block", {})
                if isinstance(cb, dict) and cb.get("type") == "tool_use":
                    pending[index] = [(event_name, payload)]
                    held_by_index[index] = {
                        "index": index,
                        "id": cb.get("id"),
                        "name": cb.get("name", ""),
                        "input_parts": [],
                    }
                    base_input = cb.get("input")
                    if isinstance(base_input, dict) and base_input:
                        held_by_index[index]["input_parts"].append(json.dumps(base_input))
                    continue
                new_index = self.client_index
                self.client_index += 1
                client_index_map[index] = new_index
                p = dict(payload)
                p["index"] = new_index
                yield _format_event(event_name, p)
                continue

            if etype == "content_block_delta":
                index = payload.get("index")
                if not isinstance(index, int):
                    yield _format_event(event_name, payload)
                    continue
                if index in pending:
                    pending[index].append((event_name, payload))
                    delta = payload.get("delta", {})
                    if isinstance(delta, dict) and isinstance(delta.get("partial_json"), str):
                        held_by_index[index]["input_parts"].append(delta["partial_json"])
                    continue
                new_index = client_index_map.get(index)
                if new_index is None:
                    continue
                p = dict(payload)
                p["index"] = new_index
                yield _format_event(event_name, p)
                continue

            if etype == "content_block_stop":
                index = payload.get("index")
                if not isinstance(index, int):
                    yield _format_event(event_name, payload)
                    continue
                if index in pending:
                    pending[index].append((event_name, payload))
                    info = held_by_index[index]
                    tool_input = self._join_input(info["input_parts"])
                    err = self._guardrail_fn(info["name"], tool_input) if self._guardrail_fn else None
                    if info["name"] in ("csmart_expand_symbol", "csmart_websearch") or err:
                        if err:
                            info["blocked_reason"] = err
                        held_indices.add(index)
                        continue
                    # Normal tool_use -> replay buffered block to the client.
                    new_index = self.client_index
                    self.client_index += 1
                    client_index_map[index] = new_index
                    for en, pl in pending[index]:
                        p = dict(pl)
                        p["index"] = new_index
                        yield _format_event(en, p)
                    continue
                new_index = client_index_map.get(index)
                if new_index is None:
                    continue
                p = dict(payload)
                p["index"] = new_index
                yield _format_event(event_name, p)
                continue

            if etype == "ping":
                yield _format_event(event_name, payload)
                continue

            if etype == "error":
                yield _format_event(event_name, payload)
                return

            yield _format_event(event_name, payload)

        self.round += 1

        if held_indices:
            # Expand/guardrail resolution is synchronous (SQLite read + dict
            # build) — no gather needed.
            self._pending_held = [
                self._execute_held(held_by_index[i]) for i in sorted(held_indices)
            ]
            return

        for event_name, payload in buffered_end:
            yield _format_event(event_name, payload)

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _join_input(parts: List[str]) -> Dict[str, Any]:
        raw = "".join(parts)
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"_partial_json": raw}

    def _execute_held(self, block: Dict[str, Any], tool_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if tool_input is None:
            tool_input = ProxyStreamer._join_input(block.get("input_parts", []))
        blocked_reason = block.get("blocked_reason")
        if not blocked_reason and self._guardrail_fn and block.get("name") not in (
            "csmart_expand_symbol", "csmart_websearch"
        ):
            blocked_reason = self._guardrail_fn(block.get("name", ""), tool_input)
        if blocked_reason:
            return {
                **block,
                "input": tool_input,
                "content": (
                    f"[CSMART SECURITY BLOCKED] {blocked_reason}. "
                    "Eksekusi dicegat oleh proxy — jangan ulangi; gunakan tool lain."
                ),
            }
        if block["name"] == "csmart_websearch":
            query = (tool_input or {}).get("query")
            if not query:
                return {
                    **block,
                    "input": tool_input,
                    "content": "ERROR: csmart_websearch memerlukan argumen 'query'.",
                }
            if self._websearch_fn is None:
                return {
                    **block,
                    "input": tool_input,
                    "content": "ERROR: csmart_websearch tidak aktif (websearch_fn belum di-set).",
                }
            content = self._websearch_fn(str(query))
            return {**block, "input": tool_input, "content": content}
        if block["name"] == "csmart_expand_symbol":
            ref_id = (tool_input or {}).get("ref_id")
            if not ref_id:
                return {
                    **block,
                    "input": tool_input,
                    "content": "ERROR: csmart_expand_symbol memerlukan argumen string 'ref_id'.",
                }
            content = get_ccr_payload(str(ref_id))
            if content is None:
                return {
                    **block,
                    "input": tool_input,
                    "content": f"ERROR: ref_id {ref_id!r} tidak ditemukan di context store.",
                }
            return {**block, "input": tool_input, "content": content}
        return {**block, "input": tool_input, "content": ""}

    def _build_followup(
        self, messages: List[Dict[str, Any]], held: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        assistant_content: List[Dict[str, Any]] = []
        user_results: List[Dict[str, Any]] = []
        for block in held:
            assistant_content.append(
                {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input", {})}
            )
            user_results.append(
                {"type": "tool_result", "tool_use_id": block["id"], "content": block.get("content", "")}
            )
        followup = list(messages)
        if assistant_content:
            followup.append({"role": "assistant", "content": assistant_content})
        followup.append({"role": "user", "content": user_results})
        return followup


__all__ = [
    "ProxyStreamer",
    "store_ccr_payload",
    "get_ccr_payload",
]
