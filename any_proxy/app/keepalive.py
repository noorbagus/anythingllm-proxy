"""csmart.app.keepalive — shared mutable keepalive/prefix state (W3 leaf).

Holds the module-level state that both ``factory.py`` (keepalive_worker) and
``handlers/messages.py`` (request touch / prefix snapshot / active model) read
and write. Isolated in a leaf module so ``factory`` and ``handlers`` can both
depend on it without creating an import cycle (handlers must not import
factory, and factory imports handlers for routers).

Deps (one-way): app.config (FLASH_MODEL/KEEPALIVE_*), logging.structured (_log),
streaming.sse (_UPSTREAM_TRANSPORT), httpx.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional

import httpx

from any_proxy.app.config import (
    FLASH_MODEL,
    KEEPALIVE_TICK,
    KEEPALIVE_WINDOW_END,
    KEEPALIVE_WINDOW_START,
    UPSTREAM_API_KEY,
    UPSTREAM_BASE_URL,
)
from any_proxy.logging.structured import _log
from any_proxy.streaming.sse import get_upstream_transport

last_request_timestamp: float = time.time()
last_keepalive_ok: float = time.monotonic()
_prefix_snapshot: Optional[Dict[str, Any]] = None
_active_model: str = FLASH_MODEL


def touch_request() -> None:
    global last_request_timestamp
    last_request_timestamp = time.time()


def set_prefix_snapshot(snapshot: Optional[Dict[str, Any]]) -> None:
    global _prefix_snapshot
    _prefix_snapshot = snapshot


def set_active_model(model: str) -> None:
    global _active_model
    _active_model = model


async def keepalive_worker() -> None:
    """Keep the KV-cache TTL alive on the provider with tiny pings."""
    global last_request_timestamp, last_keepalive_ok
    while True:
        await asyncio.sleep(KEEPALIVE_TICK)
        elapsed = time.time() - last_request_timestamp
        if not (KEEPALIVE_WINDOW_START <= elapsed < KEEPALIVE_WINDOW_END):
            continue
        if not _prefix_snapshot or not UPSTREAM_API_KEY:
            continue
        now_mono = time.monotonic()
        if now_mono - last_keepalive_ok < 45:  # jangan spam saat retry
            continue
        payload: Dict[str, Any] = {
            "model": _active_model,
            "max_tokens": 1,
            "system": _prefix_snapshot.get("system", []),
            "tools": _prefix_snapshot.get("tools", []),
            "messages": [{"role": "user", "content": "ping"}],
        }
        headers = {
            "Authorization": f"Bearer {UPSTREAM_API_KEY}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        try:
            async with httpx.AsyncClient(transport=get_upstream_transport(), timeout=10.0) as client:
                resp = await client.post(
                    f"{UPSTREAM_BASE_URL}/v1/messages",
                    headers=headers,
                    content=json.dumps(payload, sort_keys=True).encode("utf-8"),
                )
            if resp.status_code < 400:
                last_request_timestamp = time.time()
                last_keepalive_ok = time.monotonic()
                _log("KEEPALIVE_PING", status_code=resp.status_code, model=_active_model)
        except Exception:
            pass
