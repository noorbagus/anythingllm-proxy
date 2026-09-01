"""Hermetic W3 tests — handlers/factory/shim behavioral parity with csmart_proxy.

Drives the FastAPI app via ``httpx.ASGITransport`` and replaces the upstream
with ``httpx.MockTransport`` (pola hemat-token-router). No live network.

Covers:
- /v1/messages OpenAI chat -> transformed SSE / JSON, non-streaming path
- /v1/chat/completions responses-routed -> transformed to /v1/responses
- /v1/responses passthrough
- /v1/models list
- passthrough double-/v1 guard (OPENAI_BASE_URL already ends in /v1)
- shim ``import csmart_proxy`` exposes ``app``
"""
from __future__ import annotations

import json

import httpx
import pytest

from any_proxy.app.factory import app
from any_proxy.app.config import OPENAI_BASE_URL, OPENAI_CHAT_COMPLETIONS_PATH, UPSTREAM_BASE_URL
from any_proxy.streaming import set_upstream_transport


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio_run(coro)


import asyncio


def asyncio_run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    """Return a factory that yields an ASGI client with the given MockTransport."""
    calls: list = []

    def _make(handler):
        transport = httpx.MockTransport(handler)
        set_upstream_transport(transport)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        return client

    yield _make
    set_upstream_transport(None)


def _chat_sse():
    return (
        "data: " + json.dumps({"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}) + "\n\n"
        "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
        "data: [DONE]\n\n"
    )


# ---------------------------------------------------------------------------
# /v1/messages — OpenAI chat_completions model
# ---------------------------------------------------------------------------

def test_messages_openai_chat_stream_transforms(monkeypatch, tmp_path):
    """glm-* model -> OPENAI_BASE_URL chat/completions, SSE -> Anthropic shape."""
    from any_proxy.app.config import OPENAI_MODEL_ALIASES

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, content=_chat_sse().encode("utf-8"),
                              headers={"Content-Type": "text/event-stream"})

    set_upstream_transport(httpx.MockTransport(handler))
    monkeypatch.setattr("any_proxy.handlers.messages.MOCK_MODE", False)

    async def _t():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/v1/messages", json={
                "model": "glm-5.3-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
                "stream": True,
            })
            return resp

    resp = _run(_t())
    assert resp.status_code == 200
    assert captured["url"].startswith(f"{OPENAI_BASE_URL}{OPENAI_CHAT_COMPLETIONS_PATH}")
    # transformed upstream body must be OpenAI-shaped (messages, not system)
    assert "messages" in captured["body"]
    body_text = resp.text
    assert "event: message_start" in body_text
    assert "event: message_delta" in body_text


def test_messages_openai_chat_nonstream_json(monkeypatch, tmp_path):
    """stream:false -> single JSON Anthropic-shaped response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chatcmpl-x",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        })

    set_upstream_transport(httpx.MockTransport(handler))
    monkeypatch.setattr("any_proxy.handlers.messages.MOCK_MODE", False)

    async def _t():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/v1/messages", json={
                "model": "glm-5.3-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            })

    resp = _run(_t())
    assert resp.status_code == 200
    j = resp.json()
    assert j["type"] == "message"
    assert "content" in j
    assert j["model"] == "glm-5.3-flash"  # no alias mapping for glm-*


def test_messages_anthropic_native_preserves_model(monkeypatch, tmp_path):
    """minimax-* -> OPENAI_BASE_URL messages path, model preserved, passthrough."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=(
            "event: message_start\ndata: {}\n\n".encode("utf-8")
        ), headers={"Content-Type": "text/event-stream"})

    set_upstream_transport(httpx.MockTransport(handler))
    monkeypatch.setattr("any_proxy.handlers.messages.MOCK_MODE", False)

    async def _t():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/v1/messages", json={
                "model": "minimax-m2",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            })

    resp = _run(_t())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


# ---------------------------------------------------------------------------
# /v1/chat/completions — responses-routed model -> /v1/responses
# ---------------------------------------------------------------------------

def test_chat_completions_responses_route(monkeypatch, tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"id": "resp_x", "object": "response"})

    set_upstream_transport(httpx.MockTransport(handler))

    async def _t():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/v1/chat/completions", json={
                "model": "muse-spark-1.2-contributor",
                "messages": [{"role": "user", "content": "hi"}],
            })

    resp = _run(_t())
    assert resp.status_code == 200
    assert captured["url"].endswith("/v1/responses")
    assert "input" in captured["body"]  # transformed to Responses shape


# ---------------------------------------------------------------------------
# /v1/responses — passthrough
# ---------------------------------------------------------------------------

def test_responses_passthrough(monkeypatch, tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "resp_x", "object": "response"})

    set_upstream_transport(httpx.MockTransport(handler))

    async def _t():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/v1/responses", json={
                "model": "grok-4.6",
                "input": "hi",
            })

    resp = _run(_t())
    assert resp.status_code == 200
    assert captured["url"].endswith("/v1/responses")


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------

def test_models_list(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"{}")

    set_upstream_transport(httpx.MockTransport(handler))

    async def _t():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/v1/models")

    resp = _run(_t())
    assert resp.status_code == 200
    j = resp.json()
    assert j["object"] == "list"
    ids = [m["id"] for m in j["data"]]
    assert "grok-4.6" in ids
    assert "glm-5.3" in ids


# ---------------------------------------------------------------------------
# passthrough — double-/v1 guard
# ---------------------------------------------------------------------------

def test_passthrough_openai_model_strips_v1(monkeypatch, tmp_path):
    """POST /v1/embeddings {model: glm-5.3-flash} -> OPENAI_BASE_URL/embeddings
    (NOT OPENAI_BASE_URL/v1/embeddings). OPENAI_BASE_URL already ends in /v1."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"object": "list", "data": []})

    set_upstream_transport(httpx.MockTransport(handler))

    async def _t():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/v1/embeddings", json={"model": "glm-5.3-flash", "input": "x"})

    resp = _run(_t())
    assert resp.status_code == 200
    assert captured["url"] == f"{OPENAI_BASE_URL}/embeddings"
    assert "//v1/v1" not in captured["url"]


def test_passthrough_non_openai_model_uses_upstream(monkeypatch, tmp_path):
    """Non-openai model hint -> UPSTREAM_BASE_URL + path (unchanged)."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"object": "list", "data": []})

    set_upstream_transport(httpx.MockTransport(handler))

    async def _t():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/v1/count_tokens", json={"model": "claude-3-5-sonnet", "x": 1})

    resp = _run(_t())
    assert resp.status_code == 200
    assert captured["url"].startswith(f"{UPSTREAM_BASE_URL}/v1/count_tokens")


# ---------------------------------------------------------------------------
# shim
# ---------------------------------------------------------------------------

def test_shim_exposes_app():
    import csmart_proxy

    assert csmart_proxy.app is app
    assert csmart_proxy.app.title == "csmart Local Context Optimizer"