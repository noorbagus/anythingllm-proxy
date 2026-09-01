"""csmart.app.factory — FastAPI app assembly (W3).

Creates the FastAPI ``app`` singleton with lifespan + keepalive, and mounts the
handler routers from ``csmart.handlers``. One-way dependency: this module
imports handlers; handlers never import this module (they import the leaf
``app.keepalive`` state instead). Exposes ``app`` for AnythingLLM import and for
the ``csmart_proxy`` shim.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from any_proxy.app.keepalive import keepalive_worker
from any_proxy.handlers.messages import router as messages_router
from any_proxy.handlers.openai import router as openai_router
from any_proxy.logging.structured import _banner, _log, get_trace_id, init_db, set_trace_id
from any_proxy.security.secrets import _redact


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _log("SERVER_START", app_title=app.title)
    keepalive_task = asyncio.create_task(keepalive_worker())
    yield
    keepalive_task.cancel()


app = FastAPI(title="csmart Local Context Optimizer", lifespan=lifespan)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    # Propagate AnythingLLM/OpenCode X-Request-ID or generate one; store in ContextVar for JSON tracing.
    tid = request.headers.get("x-request-id") or request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    set_trace_id(tid)
    t0 = time.monotonic()
    _log("REQ_INGRESS", method=request.method, path=request.url.path, query=str(request.url.query), trace_id=tid)
    # Raw ingress capture — header + body (redacted, clipped) for AnythingLLM debugging, same trace_id.
    try:
        _raw_headers = {k.lower(): _redact(v) if k.lower() in ("authorization", "x-api-key", "x-goog-api-key") else v for k, v in request.headers.items()}
        _body_bytes = await request.body()
        _body_preview = _redact(_body_bytes[:2048].decode(errors="replace")) if _body_bytes else ""
        _log("REQ_RAW", headers=_raw_headers, body_len=len(_body_bytes), body_preview=_body_preview[:1200], trace_id=tid)
    except Exception as _e:
        _log("REQ_RAW_ERROR", error=str(_e)[:200], trace_id=tid)
    try:
        response = await call_next(request)
    except Exception as exc:
        _log("REQ_ERROR", error=str(exc)[:500], error_type=type(exc).__name__, latency_ms=int((time.monotonic() - t0) * 1000), trace_id=tid)
        raise
    latency = int((time.monotonic() - t0) * 1000)
    _log("REQ_EGRESS", status_code=response.status_code, latency_ms=latency, trace_id=tid)
    response.headers["X-Trace-Id"] = tid
    return response

app.include_router(messages_router)
app.include_router(openai_router)


__all__ = ["app", "lifespan"]
