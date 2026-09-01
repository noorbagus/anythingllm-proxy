# Prompt Refactor — `csmart/streaming/proxy_streamer.py` (W2-early, PARALLEL lane T-C)

> **Paste ke terminal T-C (paralel bareng T-A `app/config` + T-B `guardrails` + T1 routing setelah config green).** Lane ini **bisa jalan sekarang** — depend DONE leaf (sse+redactor+secrets+logging).
> Workdir: `/Volumes/Xugab/LAB/Tria/anythingllm-proxy` Branch: `refactor/modularize` | Barrier: W2 `pytest -m "not live"` sebelum W3 handlers

---

## Role

**SDLC Streaming Engineer — W2 T-C (PARALLEL, inject guardrails)**
Workdir: `/Volumes/Xugab/LAB/Tria/anythingllm-proxy` | Branch: `refactor/modularize`
File: `csmart/streaming/proxy_streamer.py:1` (~480 LOC, <700)
Source: `csmart_proxy.py:2622-2853` `ProxyStreamer` + `csmart_proxy.py:2343 StreamingRedactor` integrasi + call sites `2735 check_security_guardrails` (inject, bukan import guardrails)
Depend DONE: `streaming/sse.py:211` + `streaming/redactor.py:184` + `security/secrets.py:445` + `logging/structured.py:269` — no cycle (`secrets` ↛ `logging`)

---

## SDLC

### 1. REQ

Single responsibility **proxy_streamer** — stream upstream SSE, shadow tool_use lokal, redaction, vault masking. Wajib verbatim streaming semantics, preserve `client.send(stream=True)` + `aiter_bytes`:

- `class ProxyStreamer:2622`:
  - `__init__(method, url, headers, body)` → `self.method/url/headers/body`, `self.client_index=0`, `self.held_by_index: Dict[int, Dict]`, `self.pending: Dict[int, List[Tuple[event,payload]]]` — verbatim
  - `async def _stream_round(messages, max_tokens_ceil?)` — core loop: `httpx.AsyncClient` + `client.send(stream=True)` → `async for event_name, payload in _iter_sse_events(resp):` → handle `content_block_start` (tool_use `csmart_expand_symbol`/`csmart_websearch` vs passthrough) → `pending`/`held_by_index` → `_execute_held` → `yield _format_event` — preserve `StreamingRedactor` feed/flush per chunk + `SecretVault` mask/unmask path jika ada
  - `def _execute_held(block, tool_input) -> Dict` — dispatch `csmart_expand_symbol` → `get_ccr_payload(ref_id)` (from `csmart_proxy.py:2826` CCR store, inject via param atau `from csmart.logging.structured import get_db` helper) → `{"input": tool_input, "content": content}` atau error `ref_id tidak ditemukan`; else shadow `content=""`
  - `def _build_followup(messages, held) -> List[Dict]` — build `assistant_content` (tool_use) + `user_results` (tool_result) → `followup = messages + [assistant(content=assistant_content)] + [user(content=user_results)]` — verbatim `csmart_proxy.py:2836-2853`
  - `async def stream(...)` / `async def __call__` — entry: iterate `_stream_round`, yield SSE bytes, handle `keepalive` (`last_request_timestamp:2858`), `MOCK_STREAM`, `TOKEN_CLAMP` logging
- Integrasi leaf:
  - `from csmart.streaming.sse import _iter_sse_events, _format_event, _parse_sse_data` — inject, jangan reimplement
  - `from csmart.streaming.redactor import StreamingRedactor` — per-chunk `redactor.feed(chunk)`/`redactor.flush()`
  - `from csmart.security.secrets import SecretVault` (optional vault wrapper) — dual-vault probe `csmart_proxy:vault` via `set_redactor_logger` pattern jika ada
  - `from csmart.logging.structured import _log, get_db, init_db` — for `KEEPALIVE_PING`, `MOCK_STREAM`, `TOKEN_CLAMP` events + `get_ccr_payload` (via `get_db`)
- `guardrails` inject (agar T-B paralel tidak block): **jangan** `from csmart.security.guardrails import check_security_guardrails` di top-level — inject via `__init__(..., guardrail_fn=None)` atau `set_guardrail_fn(fn)` + fallback `try: from csmart.security.guardrails import check_security_guardrails` di `_execute_held` lazy import. Preserve `2735 err = check_security_guardrails(info["name"], tool_input)` semantics: if `err` → `block = {"content": err}` (secrets never reach client/upstream).

**Out of scope:** `check_security_guardrails` impl → `guardrails.py:1` (inject, jangan duplikat). `BLOCKED_*_PATTERNS` → guardrails. `align_prefix_3_region`/`route_model_tier` → `routing/model.py`.

### 2. DESIGN

```
DONE leaf:
  csmart/streaming/sse.py (211) ─┐
  csmart/streaming/redactor.py ─┤─► csmart/streaming/proxy_streamer.py (480) ◄─ inject guardrail_fn ─ csmart/security/guardrails.py (260, T-B PARALLEL)
  csmart/security/secrets.py ────┤          │ from csmart.logging.structured import _log, get_db (269 DONE)
  csmart/logging/structured.py ──┘          └─► csmart/handlers/messages.py (W3 orchestrator) + handlers/openai (W3)
```

- Tidak ada cycle: `proxy_streamer` tidak import `csmart.handlers`, `csmart.routing.model` (hanya `secrets`/`logging`/`sse`/`redactor`). Guardrails parallel via inject — no top-level `from csmart.security.guardrails import ...` hard dep.
- Keep streaming semantics: `client.send(stream=True)` + `aiter_bytes` chunked, `StreamingRedactor` per `content_block_delta`, `SecretVault` mask before yield, unmask on held execution.
- `keepalive` `2858 last_request_timestamp` — `time.time()` + `asyncio.create_task` ping `httpx.AsyncClient` keepalive — preserve but isolatable for hermetic test (inject `time_fn`/`client_factory`).

### 3. IMPL

```bash
sed -n '2622,2853p' csmart_proxy.py   # ProxyStreamer verbatim
sed -n '2343,2622p' csmart_proxy.py   # StreamingRedactor integration snippet if not fully in redactor.py
sed -n '2858,2950p' csmart_proxy.py   # keepalive + SERVER_START + _upstream_headers (jangan duplikat — _upstream_headers di routing/model or handlers)
grep -n "get_ccr_payload\|get_db\|_log.*KEEPALIVE\|_log.*MOCK_STREAM\|client.send" csmart_proxy.py | head -n 30
```

Struktur `proxy_streamer.py`:
```python
"""csmart.streaming.proxy_streamer — ProxyStreamer (SSE shadow + redaction + vault)."""
from __future__ import annotations
import asyncio, json, time, httpx
from typing import Any, Dict, List, Optional, Tuple
from csmart.streaming.sse import _iter_sse_events, _format_event, _parse_sse_data
from csmart.streaming.redactor import StreamingRedactor
from csmart.security.secrets import SecretVault  # optional vault wrapper
from csmart.logging.structured import _log, get_db  # for CCR + keepalive logging
# guardrails inject (paralel T-B):
try: from csmart.security.guardrails import check_security_guardrails as _guardrails_default
except ImportError: _guardrails_default = None

def get_ccr_payload(ref_id: str) -> Optional[str]: ...  # verbatim via get_db() SELECT context_blobs

class ProxyStreamer:
    def __init__(self, method: str, url: str, headers: Dict[str,str], body: Dict[str,Any], guardrail_fn=None, ...) -> None:
        self.guardrail_fn = guardrail_fn or _guardrails_default
        ...
    async def _stream_round(self, messages: List[Dict[str,Any]]) -> ...: ...  # verbatim + inject _iter_sse_events + StreamingRedactor
    def _execute_held(self, block: Dict[str,Any], tool_input: Any) -> Dict[str,Any]: ... # guardrail_fn dispatch
    def _build_followup(self, messages: List[Dict[str,Any]], held: List[Dict[str,Any]]) -> List[Dict[str,Any]]: ...
    async def stream(self, ...): ...  # entry + keepalive
    # optional set_guardrail_fn for T-B late binding
    def set_guardrail_fn(self, fn) -> None: self.guardrail_fn = fn

__all__ = ["ProxyStreamer","get_ccr_payload"]
```

- Preserve `held_by_index`/`pending`/`client_index` state machine verbatim — critical for tool_use shadowing correctness.
- `wc -l` target ~480 (<700). Jika keepalive+CCR helper bikin >500, tetap <700 — budget 220 spare.

### 4. TEST

```bash
python3 -m py_compile csmart/streaming/proxy_streamer.py && echo "OK"

# smoke construct + helper
python3 -c "from csmart.streaming.proxy_streamer import ProxyStreamer, get_ccr_payload; ps=ProxyStreamer('POST','http://upstream/v1/messages',{},{}); print('ProxyStreamer construct PASS', ps.method); print('get_ccr_payload', get_ccr_payload('ref_00000000'))"

# smoke inject guardrails (parallel T-B — verify no hard dep)
python3 -c "from csmart.streaming.proxy_streamer import ProxyStreamer; ps=ProxyStreamer('POST','http://x',{},{}); ps.set_guardrail_fn(lambda n,i: 'blocked' if 'aws' in str(i) else None); print(ps._execute_held({'id':'toolu_1','name':'bash'}, {'command':'cat ~/.aws/credentials'}))"

# smoke _build_followup
python3 -c "from csmart.streaming.proxy_streamer import ProxyStreamer; ps=ProxyStreamer('POST','http://x',{},{}); print(ps._build_followup([{'role':'user','content':'hi'}], [{'id':'toolu_1','name':'csmart_expand_symbol','input':{'ref_id':'ref_abcd'}}]))"

# no cycle
python3 -c "import pathlib; s=pathlib.Path('csmart/streaming/proxy_streamer.py').read_text(); assert 'csmart.handlers' not in s and 'csmart.routing.model' not in s; assert 'from csmart.security.guardrails import' not in s or 'guardrail_fn' in s; print('proxy_streamer no cycle/inject PASS')"

# barrier W2 hermetic (MockTransport upstream SSE passthrough, shadow csmart_expand_symbol)
# pytest subset if exists:
pytest -q -k "proxy_stream or streamer" 2>&1 | tail

wc -l csmart/streaming/proxy_streamer.py  # <700
```

- Hermetic `MockTransport` not required here — leaf `sse`+`redactor` already hermetic, streamer preserve `client.send(stream=True)` signature.

### 5. DONE

- [ ] `wc -l` <700 + `py_compile` OK + 4 smoke PASS (construct, guardrail inject, _build_followup, no cycle/inject)
- [ ] `git add csmart/streaming/proxy_streamer.py`
- [ ] Update `checklist/progress.md`: `proxy_streamer.py` 0→~480 ✅, W2-early T-C — unblock `handlers/messages` + `handlers/openai` (W3)
- [ ] Koordinasi T-B: setelah `guardrails.py` green, verify `ProxyStreamer.set_guardrail_fn(check_security_guardrails)` late binding PASS

## Env

- `CSMART_KEEPALIVE`, `CSMART_MOCK_RESPONSES`, `UPSTREAM_BASE_URL` — via `csmart.app.config` (import if available, else `os.getenv` fallback). Keepalive ping interval via `time.time()` + `asyncio`.

## Acceptance

- [ ] `proxy_streamer.py` ~480 <700, `py_compile` OK, guardrails inject pattern (no hard top-level import), streaming semantics preserve `client.send(stream=True)` + `StreamingRedactor` + `SecretVault` → W3 handlers consumer ready
