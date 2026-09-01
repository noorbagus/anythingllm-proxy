# Graph Report - anythingllm-proxy  (2026-09-01)

## Corpus Check
- 39 files · ~28,264 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 399 nodes · 682 edges · 23 communities (22 shown, 1 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS · INFERRED: 2 edges (avg confidence: 0.95)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `353d3258`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- messages.py
- Definition of Done
- openai.py
- secrets.py
- handle_messages
- test_w3_handlers.py
- proxy_streamer.py
- sse.py
- sanitize_payload
- redactor.py
- opencode.json
- _build_record
- SDLC Principle (ikuti urut, jangan loncat)
- SDLC
- SDLC
- SDLC
- W1 — Foundation (PARALEL 4 lane, setelah W0 gate)
- app/__init__.py

## God Nodes (most connected - your core abstractions)
1. `_log()` - 28 edges
2. `handle_messages()` - 27 edges
3. `set_upstream_transport()` - 14 edges
4. `ProxyStreamer` - 12 edges
5. `handle_openai_chat()` - 11 edges
6. `get_upstream_transport()` - 10 edges
7. `_sse_source()` - 10 edges
8. `_run()` - 10 edges
9. `handle_openai_responses()` - 9 edges
10. `handle_models()` - 9 edges

## Surprising Connections (you probably didn't know these)
- `handle_messages()` --uses--> `ProxyStreamer`  [INFERRED]
  csmart/handlers/messages.py → csmart/streaming/proxy_streamer.py
- `handle_messages()` --uses--> `StreamingRedactor`  [INFERRED]
  csmart/handlers/messages.py → csmart/streaming/redactor.py
- `client_factory()` --calls--> `set_upstream_transport()`  [EXTRACTED]
  tests/test_w3_handlers.py → csmart/streaming/sse.py
- `test_chat_completions_responses_route()` --calls--> `set_upstream_transport()`  [EXTRACTED]
  tests/test_w3_handlers.py → csmart/streaming/sse.py
- `test_messages_anthropic_native_preserves_model()` --calls--> `set_upstream_transport()`  [EXTRACTED]
  tests/test_w3_handlers.py → csmart/streaming/sse.py

## Import Cycles
- None detected.

## Communities (23 total, 1 thin omitted)

### Community 0 - "messages.py"
Cohesion: 0.06
Nodes (49): _load_gateway_env(), csmart.app.config — pure env layer (verbatim from csmart_proxy.py:65-273). REQ:…, Load the PrivateLink gateway env files so ANTHROPIC_AUTH_TOKEN is found even…, lifespan(), csmart.app.factory — FastAPI app assembly (W3). Creates the FastAPI ``app``…, keepalive_worker(), Any, csmart.app.keepalive — shared mutable keepalive/prefix state (W3 leaf). Holds… (+41 more)

### Community 1 - "Definition of Done"
Cohesion: 0.05
Nodes (35): Commit & Issue, Definition of Done, Global, W0 Gate — Track-B (blocker), W1 Gate — Foundation (DONE 7/7 — T2+T3+T4+T1 complete, 9/14 modul), W2 Gate — Integration (DONE 4/5 — T-B+T-C+T2a 536 PASS + W2b 697 PASS, barrier W2 green), W3 Gate — Handlers + Factory + Shim (DONE 5/5), Detail Verifikasi T2/T3/T4 + W1 T1 + W2 B/C (2026-09-01, main after sync → track-b after sync) (+27 more)

### Community 2 - "openai.py"
Cohesion: 0.10
Nodes (35): api_route, csmart.handlers — route handlers (W3). Re-exports the 5 FastAPI handlers:…, _mock_anthropic_json(), _mock_anthropic_stream(), Any, Canned non-stream Anthropic Messages JSON (mock mode)., Canned spec-compliant Anthropic Messages SSE stream (mock mode). Text-only at…, handle_models() (+27 more)

### Community 3 - "secrets.py"
Cohesion: 0.09
Nodes (29): _b64url_key(), _compile_allow_regexes(), get_db(), init_db(), _load_gitleaks_config(), load_gitleaks_rules(), _log(), Any (+21 more)

### Community 4 - "handle_messages"
Cohesion: 0.11
Nodes (28): handle_messages(), JSONResponse, post, Request, StreamingResponse, _upstream_headers(), is_anthropic_native_model(), Detect models served by the Anthropic-compatible /messages endpoint (OpenCode… (+20 more)

### Community 5 - "test_w3_handlers.py"
Cohesion: 0.14
Nodes (22): AsyncBaseTransport, Override transport global (untuk mock/hermetic tests)., set_upstream_transport(), fixture, asyncio_run(), client_factory(), Hermetic W3 tests — handlers/factory/shim behavioral parity with csmart_proxy.…, stream:false -> single JSON Anthropic-shaped response. (+14 more)

### Community 6 - "proxy_streamer.py"
Cohesion: 0.16
Nodes (15): get_db(), Connection, get_ccr_payload(), get_db(), _log(), ProxyStreamer, Any, csmart.streaming.proxy_streamer — ProxyStreamer (SSE shadow + CCR +… (+7 more)

### Community 7 - "sse.py"
Cohesion: 0.18
Nodes (19): csmart.streaming — streaming leaf (W1). Pure SSE utils + StreamingRedactor…, _emit(), _format_openai_chat_sse(), _iter_sse_events(), _parse_sse_data(), Any, csmart.streaming.sse — pure SSE utils (no DB). Ekstrak dari…, Parse httpx streaming response into ``(event_name, payload)`` tuples. Pure —… (+11 more)

### Community 8 - "sanitize_payload"
Cohesion: 0.12
Nodes (18): _canonicalize_path(), check_security_guardrails(), _log(), _mask_dict(), _mask_text_block(), _mcp_sse_post(), Any, Mask every string leaf of a (small) nested dict — e.g. tool_use.input. (+10 more)

### Community 9 - "redactor.py"
Cohesion: 0.14
Nodes (12): _default_unmask(), _emit(), csmart.streaming.redactor — StreamingRedactor isolasi (no DB). Ekstrak dari…, Unmask marker di client-bound path tanpa split di boundary chunk. Split marker…, Feed satu chunk, return prefix aman yang sudah di-unmask. Tail 64 char ditahan…, Flush sisa buffer (akhir stream) — unmask & kosongkan., Panjang buffer yang masih ditahan (untuk hermetic assert)., Inject custom structured logger untuk redactor (hapus dep _log langsung). (+4 more)

### Community 10 - "opencode.json"
Cohesion: 0.13
Nodes (14): model, options, apiKey, baseURL, timeout, permission, bash, edit (+6 more)

### Community 11 - "_build_record"
Cohesion: 0.19
Nodes (8): _build_record(), Any, Non-blocking structured logger backed by bounded queue + one daemon thread.…, Bangun record JSON terstruktur — dipanggil oleh _log & StructuredLogger., Tulis satu record ke file harian atau stderr (thread-safe, never raises)., StructuredLogger, _write_record(), TextIO

### Community 12 - "SDLC Principle (ikuti urut, jangan loncat)"
Cohesion: 0.18
Nodes (10): 1. REQ — Requirements (apa yang harus ada), 2. DESIGN — Module boundaries, 3. IMPL — Implementation steps, 4. TEST — Verification (wajib PASS sebelum DONE), 5. DONE — Definition of Done untuk lane ini, Acceptance Criteria, Env Penting (jangan commit secret), Prompt Refactor — `csmart/app/config.py` (W1 T1, PARALLEL lane) (+2 more)

### Community 13 - "SDLC"
Cohesion: 0.18
Nodes (10): 1. REQ, 2. DESIGN, 3. IMPL, 4. TEST, 5. DONE, Acceptance, Env, Prompt Refactor — `csmart/routing/model.py` + `csmart/routing/token_limits.py` (W1 T1 remainder, PARALLEL after config) (+2 more)

### Community 14 - "SDLC"
Cohesion: 0.18
Nodes (10): 1. REQ, 2. DESIGN, 3. IMPL, 4. TEST, 5. DONE, Acceptance, Env, Prompt Refactor — `csmart/security/guardrails.py` (W2-early, PARALLEL lane T-B) (+2 more)

### Community 15 - "SDLC"
Cohesion: 0.18
Nodes (10): 1. REQ, 2. DESIGN, 3. IMPL, 4. TEST, 5. DONE, Acceptance, Env, Prompt Refactor — `csmart/streaming/proxy_streamer.py` (W2-early, PARALLEL lane T-C) (+2 more)

### Community 16 - "W1 — Foundation (PARALEL 4 lane, setelah W0 gate)"
Cohesion: 0.18
Nodes (10): Barrier W1, Prompt T1, Prompt T2 (selesai — untuk arsip), Prompt T3 (selesai — untuk arsip), Prompt T4 (selesai — untuk arsip, T4 update 225→269), T1 — Foundation: `app/config` + `routing/*`, T2 — Security: `security/secrets.py`, T3 — Streaming Leaf: `streaming/sse.py` + `streaming/redactor.py` (+2 more)

## Knowledge Gaps
- **74 isolated node(s):** `$schema`, `model`, `small_model`, `apiKey`, `baseURL` (+69 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `_log()` connect `messages.py` to `openai.py`, `handle_messages`, `proxy_streamer.py`, `sse.py`, `redactor.py`, `_build_record`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Why does `handle_messages()` connect `handle_messages` to `messages.py`, `openai.py`, `proxy_streamer.py`, `sanitize_payload`, `redactor.py`?**
  _High betweenness centrality (0.036) - this node is a cross-community bridge._
- **Why does `ProxyStreamer` connect `proxy_streamer.py` to `messages.py`, `handle_messages`?**
  _High betweenness centrality (0.032) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `handle_messages()` (e.g. with `ProxyStreamer` and `StreamingRedactor`) actually correct?**
  _`handle_messages()` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `$schema`, `model`, `small_model` to the rest of the system?**
  _74 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `messages.py` be split into smaller, more focused modules?**
  _Cohesion score 0.06253652834599649 - nodes in this community are weakly interconnected._
- **Should `Definition of Done` be split into smaller, more focused modules?**
  _Cohesion score 0.04878048780487805 - nodes in this community are weakly interconnected._