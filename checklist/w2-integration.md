# W2 — Integration (PARALEL 2 lane, butuh W1 gate green)

> **Mode:** PARALEL — 2 terminal, depend on W1. Barrier: `pytest -m "not live"` + MockTransport.
> **Gate masuk:** W1 `py_compile` + import smoke green

```
T-Transform ─┐
              ├─► BARRIER W2 ─► W3 (handlers sequential)
T-Stream/Sec ─┘
```

## T-Transform — `transform/anthropic_to_openai.py` + `transform/openai_to_anthropic.py`

- **Files:** `csmart/transform/anthropic_to_openai.py` (~500), `csmart/transform/openai_to_anthropic.py` (~450)
- **Source:**
  - `anthropic_to_openai`: `csmart_proxy.py:1228 transform_anthropic_to_openai_chat`, `1383 transform_anthropic_to_openai_responses`, `1415 _convert_anthropic_message_to_openai_responses`, `1516`, `1556 transform_openai_chat_to_responses`, `1690 _resolve_reasoning_effort` + helpers `_convert_anthropic_*`, `_extract_system_text`
  - `openai_to_anthropic`: `2506 transform_openai_responses_to_openai_chat_json`, `2600 _format_openai_chat_sse` (Issue Track-C reuse), `2707 transform_openai_responses_to_anthropic_json`, `2771 transform_openai_chat_to_anthropic_json` + SSE adapters `transform_openai_responses_sse_to_openai_chat_sse`
- **Depend:** `routing/model.py` (is_openai_model), `streaming/sse.py` (_parse_sse_data, _iter_sse_events)
- **REQ:** Pure transform, no DB, no app global, injectable routing. Pertahankan `cache_read_input_tokens`, `reasoning_content`, `tool_calls` handling.
- **DESIGN:** Tiap file expose transform pair + helper; `openai_to_anthropic` reuse `_format_event` dari `streaming/sse.py` bila perlu (atau keep local jika cycle).
- **IMPL:**
  - Verbatim extract, ganti `from csmart_proxy import ...` ke `from csmart.routing.model import ...` / `from csmart.streaming.sse import ...`
  - Keep <700/file (estimasi 500 + 450).
- **TEST:**
  ```bash
  python3 -m py_compile csmart/transform/*.py
  python3 -c "from csmart.transform.anthropic_to_openai import transform_anthropic_to_openai_chat; print('transform OK')"
  pytest tests/test_csmart_proxy.py -k "transform or anthropic or openai_chat" -q
  ```
- **DONE:** Required by `handlers/messages.py` (W3)

### Prompt T-Transform
```
Role: SDLC Transform Engineer — W2
Workdir: /Volumes/Xugab/LAB/Tria/anythingllm-proxy | Branch: refactor/modularize
Files: csmart/transform/anthropic_to_openai.py, csmart/transform/openai_to_anthropic.py
Depend: csmart/routing/model.py + csmart/streaming/sse.py (W1)

REQ: Extract transform_anthropic_to_openai_chat/responses + transform_openai_responses_to_openai_chat_json/_format_openai_chat_sse s.d. transform_openai_*_to_anthropic — keep cache_read_input_tokens, reasoning, tool_calls.
DESIGN: Pure, injectable, no DB/app global. Reuse sse utils.
IMPL: Verbatim csmart_proxy.py:1228,1383,1415,1516,1556,1690,2506,2600,2707,2771. Fix imports to csmart.*. <700/file.
TEST: py_compile + import smoke + pytest -k transform green.
DONE: Unblock handlers/messages (W3).
```

---

## T-Stream/Security — `security/guardrails.py` + `streaming/proxy_streamer.py`

- **Files:** `csmart/security/guardrails.py` (~260), `csmart/streaming/proxy_streamer.py` (~480)
- **Source:**
  - `guardrails`: `csmart_proxy.py:689 check_security_guardrails`, `Any 45` community, `_websearch_exa`, `_mcp_sse_post`
  - `proxy_streamer`: `csmart_proxy.py:2345 ProxyStreamer` (Any 45), `2897 ProxyStreamer`, `_stream_round`, `_execute_held`, `StreamingRedactor` integration, `SecretVault` caller wrapper
- **Depend:** `security/secrets.py` (SecretVault, _Rule), `logging/structured.py` (_log), `streaming/sse.py` (_iter_sse_events, _format_event, _parse_sse_data), `streaming/redactor.py` (StreamingRedactor)
- **REQ:** Guardrails never raises, streaming preserves `SecretVault` caller wrapper + `StreamingRedactor` + `SecretVault` unmask. `proxy_streamer` mode `openai_chat` reuse adapter Track-C (`transform_openai_responses_sse_to_openai_chat_sse`) bila upstream `responses` tapi downstream `chat_completions`.
- **DESIGN:** `guardrails.py` pure check, inject `SecretVault` instance; `proxy_streamer.py` owns `ProxyStreamer` family, inject `sse`+`redactor`+`secrets`+`logging` via constructor/setter — hindari import cycle ke `csmart_proxy`.
- **IMPL:**
  - Extract verbatim, replace `from csmart_proxy import ...` dengan `from csmart.security.secrets import SecretVault` etc.
  - Keep streaming logic `client.send(stream=True)` + `aiter_bytes` — jangan ubah (Issue B2).
  - `proxy_streamer.py` 0B → ~480, <700.
- **TEST:**
  ```bash
  python3 -m py_compile csmart/security/guardrails.py csmart/streaming/proxy_streamer.py
  python3 -c "from csmart.security.guardrails import check_security_guardrails; print('guardrails OK')"
  python3 -c "from csmart.streaming.proxy_streamer import ProxyStreamer; print('streamer OK')"
  pytest tests/test_csmart_proxy.py -k "guardrail or proxy_stream or stream" -q
  # MockTransport untuk ProxyStreamer jika ada
  ```
- **DONE:** Required by `handlers/messages.py` + `handlers/openai.py` (W3)

### Prompt T-Stream/Security
```
Role: SDLC Streaming+Security Engineer — W2
Workdir: /Volumes/Xugab/LAB/Tria/anythingllm-proxy | Branch: refactor/modularize
Files: csmart/security/guardrails.py, csmart/streaming/proxy_streamer.py
Depend: csmart/security/secrets.py + csmart/logging/structured.py + csmart/streaming/sse.py + csmart/streaming/redactor.py (W1 DONE) + graphify Any(45)

REQ: Extract check_security_guardrails + ProxyStreamer family — preserve StreamingRedactor+SecretVault wrapper, mode openai_chat adapter, never raises.
DESIGN: Guardrails inject SecretVault; ProxyStreamer inject sse/redactor/logging, keep client.send(stream=True).
IMPL: Verbatim csmart_proxy.py:689,2345,2897 + _websearch_exa/_mcp_sse_post. Fix imports, avoid cycle. <700/file.
TEST: py_compile + import smoke + pytest -k guardrail/proxy_stream + MockTransport.
DONE: Unblock handlers + factory (W3).
```

---

## Barrier W2

```bash
python3 -m py_compile csmart/transform/*.py csmart/security/guardrails.py csmart/streaming/proxy_streamer.py
wc -l csmart/transform/*.py csmart/security/guardrails.py csmart/streaming/proxy_streamer.py  # all <700
pytest -m "not live" -q  # wajib green sebelum W3
# Optional MockTransport sama seperti W0 — verify ProxyStreamer openai_chat mode
# git add csmart/transform csmart/security/guardrails.py csmart/streaming/proxy_streamer.py
```
