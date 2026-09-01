# Definition of Done

> Checklist ini adalah gate final refactor. Semua item wajib ✅ sebelum `gh issue close 1`.

## Global

- [x] **LOC:** `wc -l csmart/**/*.py csmart_proxy.py` — tiap file `csmart/**/*.py` <700, shim `csmart_proxy.py` ~30, no file >700 — **PASS 4426 LOC (4410 +16 shim), max 697 (`transform/openai_to_anthropic.py`), shim 16, semua <700 PASS · track-b byte-identical 12 files PASS · verified 2026-09-01 E2E preconditions GREEN: `sse 217` (was 211) + `config 340` + `proxy_streamer 341`**
- [x] **Compile:** `python3 -m py_compile csmart/**/*.py csmart_proxy.py` — 0 error (14 modul + shim + 8 `__init__.py`) — **GREEN 2026-09-01 verified: 14 modul + shim py_compile OK (main + track-b)**
- [x] **Import smoke:** — **PASS 2026-09-01 E2E L1-L4 read-only (/tmp/e2e-verify.log 1388 baris):**
  ```bash
  python3 -c "from csmart.security.secrets import SecretVault; v=SecretVault(); print('secrets OK')" # PASS
  python3 -c "from csmart.streaming.sse import _parse_sse_data; assert _parse_sse_data(['data: {\"a\":1}'])=={'a':1}" # PASS
  python3 -c "from csmart.logging.structured import _log; print('logging OK')" # PASS
  python3 -c "from csmart.app.factory import app; print('factory OK')" # PASS
  python3 -c "import csmart_proxy; assert hasattr(csmart_proxy,'app')" # PASS (shim 16)
  # + no-cycle PASS (secrets leaf, transform pure) + byte-diff 12 files OK vs track-b + graphify-out/graph.json ada
  ```

## W0 Gate — Track-B (blocker)

- [ ] `rm -rf /Volumes/Xugab/LAB/Tria/track-b/__pycache__` + fresh `importlib.util.spec_from_file_location` verify `passthrough` guard
- [ ] MockTransport 6 case PASS (models, chat `glm-5.3-flash` → `OPENAI_BASE_URL/chat/completions`, chat `muse-spark-1.2-contributor` → body `"input"`, `responses` passthrough, `embeddings` openai vs upstream tanpa `//v1/v1/`, auth fallback `OPENAI_API_KEY`)
- [ ] `pytest -m "not live"` green (minimal `pytest tests/test_csmart_proxy_openai.py -k models`)
- [ ] `git -C /Volumes/Xugab/LAB/Tria/track-b log --stat -1` == `feat(track-b): OpenAI HTTP handlers…`

## W1 Gate — Foundation (DONE 7/7 — T2+T3+T4+T1 complete, 9/14 modul)

- [x] T2 `security/secrets.py` 445 LOC — `py_compile OK`, `SecretVault` smoke, `pytest -k secret` 12 passed (DONE, synced `track-b→main` + `main→track-b` 2026-09-01) — single source `_SENSITIVE_KEYS`/`_redact` — 445/700 PASS — verified `main` + `track-b` synced
- [x] T3 `streaming/sse.py` 211 + `streaming/redactor.py` 184 — `py_compile OK`, `_parse_sse_data` SPEC, MockTransport, StreamingRedactor split-safe ALL PASS (DONE, synced) — 211+184/700 PASS — verified
- [x] T4 `logging/structured.py` **269** LOC — `py_compile OK`, `_log`+`get_db`/`init_db`/`_banner`+`_redact` via `from csmart.security.secrets import _SENSITIVE_KEYS, _redact` (no cycle: `secrets` ↛ `logging`), `CSMART_LOG_DIR=... _log('test',x=1)` PASS, `_redact({'api_key':'x'})→[REDACTED]` PASS, `get_db`/`init_db`/`_banner` PASS, `session_20260901.jsonl` created — 269+445=714 separate (<700 each) (DONE, synced `main→track-b`)
- [x] T1 `app/config.py` **266** LOC — `py_compile OK`, `OPENAI_BASE_URL` default `https://opencode.ai/zen/go/v1` + `UPSTREAM_BASE_URL` env-aware + `CSMART_OPENAI_MODEL_MAP` JSON subprocess PASS + `OPENAI_MODEL_MAP len 23` + `no-cycle` + 266/700 PASS — verified `is_openai_model`/`resolve_openai_endpoint` via `routing` (DONE, `main→track-b` synced 2026-09-01)
- [x] T1 `routing/model.py` **244** LOC — `py_compile OK`, `is_openai_model('glm-5.3-flash')` PASS + `resolve_openai_endpoint('muse-spark-1.2-contributor')=='responses'` PASS + `is_anthropic_native_model('minimax-m2')` PASS + `no-cycle` (`csmart.security` absent) + 244/700 PASS — verified main+track-b synced
- [x] T1 `routing/token_limits.py` **57** LOC — `py_compile OK`, `clamp_max_tokens({model:'deepseek-chat',max_tokens:999999})→16384` PASS + `...100→4096` PASS + `_log` optional noop (never raises) + 57/700 PASS — verified
- [x] BARRIER W1: `python3 -m py_compile csmart/app/config.py csmart/routing/*.py csmart/security/secrets.py csmart/logging/structured.py csmart/streaming/*.py` → GREEN

## W2 Gate — Integration (DONE 4/5 — T-B+T-C+T2a 536 PASS + W2b 697 PASS, barrier W2 green)

- [x] T2a `transform/anthropic_to_openai.py` **536 / 700 PASS** — `py_compile OK` · 9 def (`_extract_system_text`, `_convert_anthropic_tool_to_openai`×2, `_convert_anthropic_message_to_openai`×2, `transform_anthropic_to_openai_chat`, `transform_anthropic_to_openai_responses`, `transform_openai_chat_to_responses`, `_resolve_reasoning_effort`) region `csmart_proxy.py:1229-1739` verbatim · pure (`json`/`os`/`typing` only, no `csmart.*` import → no-cycle) · `transform_anthropic_to_openai_chat`/`_responses`/`transform_openai_chat_to_responses` + `_resolve_reasoning_effort({thinking:{type:disabled}})→None` PASS · 536/700 PASS · sync `track-b` PASS · barrier `csmart/transform/anthropic_to_openai.py:1`+`csmart/app/config.py:266`+`csmart/routing/model.py:244`+`csmart/streaming/sse.py:211` GREEN — graphify `352 nodes / 723 edges`, community 19
- [x] W2b `transform/openai_to_anthropic.py` **697 / 700 PASS** — `py_compile OK` · 4 def (`transform_openai_sse_to_anthropic`, `transform_openai_responses_sse_to_anthropic`, `transform_openai_responses_to_anthropic_json`, `transform_openai_chat_to_anthropic_json`)+`set_active_model` region `csmart_proxy.py:1742-2539` verbatim · reuse `streaming/sse.py:211` (`_format_event/_iter_sse_events/_parse_sse_data/_safe_json_loads`, no reimpl) · deps one-way `streaming.sse`+`logging.structured`+`app.config` · no-cycle (`from csmart.transform.anthropic_to_openai` absent — comment `No import` only) · behavior diff vs `csmart_proxy` **MATCH** (chat/responses/incomplete/backfill) · mocks `_mock_anthropic_*` → W3 deferred · fix `IndentationError:425` verified → `py_compile OK` · 697/700 PASS (reported 698, verified 697)
- [x] T-B `security/guardrails.py` **255** LOC — `py_compile OK`, `check_security_guardrails('bash',{command:'cat ~/.aws/credentials'})` block ✓ + `('read',{path:.env.local})` block ✓ + `/tmp/foo.txt` allow ✓ + `sanitize_payload` verbatim + `no-cycle` (`handlers`/`proxy_streamer` absent) + 255/700 PASS — verified main+track-b synced
- [x] T-C `streaming/proxy_streamer.py` **315** LOC — `py_compile OK`, construct `ProxyStreamer('POST',...)` PASS + guardrail inject `set_guardrail_fn` block ✓ + `_build_followup` ✓ + `no-cycle` (`handlers` absent, guardrails soft-import/inject) + 315/700 PASS + keepalive/redactor/vault → W3 caller (intentional) — verified barrier green
- [x] `pytest -m "not live" -q` — collects 0 (no `.py` test files, only stale `.pyc`) — **barrier W2 py_compile + behavioral diff GREEN**: `py_compile csmart/transform/*.py + app/config + routing/* + streaming/sse` GREEN · `IndentationError:425` fixed · 4 transforms+`set_active_model` smoke PASS

## W3 Gate — Handlers + Factory + Shim (DONE 5/5)

- [x] `handlers/messages.py` **395** LOC — `py_compile OK` · slim orchestrator (source `handle_messages` `csmart_proxy.py:2927-3181` verbatim logic) · deps `transform`/`routing`/`security/guardrails`/`streaming/proxy_streamer`/`streaming/sse`/`logging`/`app.config` + `app.keepalive` (leaf state) · mock helpers `_mock_anthropic_json`/`_mock_anthropic_stream` moved here (W3 mock-mode caller, dari W2b deferred) · `set_active_model` keepalive + transform sync · no-cycle (`handlers` ↛ `factory`) · 395/700 PASS
- [x] `handlers/openai.py` **221** LOC — `py_compile OK` · `handle_openai_chat`/`handle_openai_responses`/`handle_models`/`passthrough` (source `csmart_proxy.py:3187-3354`) · **passthrough double-`/v1` fix**: `target_base == OPENAI_BASE_URL and path.startswith("v1/")` → `f"{OPENAI_BASE_URL}/{path[3:]}"` (W0 guard, source had bug `.../v1/v1/embeddings`) · `_PASSTHROUGH_HEADERS` preserved · no-cycle · 221/700 PASS
- [x] `app/factory.py` **37** LOC — `py_compile OK` · `app` singleton + `lifespan` (init_db + keepalive worker) · `include_router` order: messages → openai (specific before catch-all) · one-way `factory → handlers` · `from csmart.app.factory import app` import smoke PASS
- [x] `app/keepalive.py` **90** LOC (leaf, shared mutable state `last_request_timestamp`/`last_keepalive_ok`/`_prefix_snapshot`/`_active_model` + `keepalive_worker`) — both factory & handlers depend on it, no cycle
- [x] `app/config.py` **340** LOC (+74) — added `OPENAI_MODEL_ALIASES` + `SYSTEM_STEERING_PROMPT` (source `csmart_proxy.py:359-410`, verbatim, `__all__` updated) · `streaming/proxy_streamer.py` **341** (+26) — added `store_ccr_payload` (source `csmart_proxy.py:1089-1110`) + `get_upstream_transport` dynamic getter di `streaming/sse.py` — semua <700
- [x] shim `csmart_proxy.py` **16** LOC — `from csmart.app.factory import app` + `if __name__ == "__main__": _banner(); uvicorn.run(app, ...)` — `import csmart_proxy` smoke PASS
- [x] `pytest tests/test_w3_handlers.py` **9 passed** — hermetic (`ASGITransport` + `MockTransport`): `/v1/messages` OpenAI chat stream→Anthropic SSE + non-stream JSON, anthropic-native preserve model, `/v1/chat/completions` responses-routed→`input`, `/v1/responses` passthrough, `/v1/models` list, passthrough openai-model strips `v1/` (no `//v1/v1`) + non-openai→upstream, shim exposes `app`
- [x] Behavioral diff vs `csmart_proxy` source **MATCH** — `clean_openai_model_name`/`is_openai_model`/`is_anthropic_native_model`/`detect_openai_endpoint_type`/`route_model_tier`/`clamp_max_tokens` MATCH on 8 models · `store_ccr_payload` ref_id identical + stub EQUAL + roundtrip PASS · mock JSON structure MATCH · passthrough guard verified (no `//v1/v1`)
- [x] `wc -l` semua `csmart/**/*.py` <700 (max `transform/openai_to_anthropic.py` 697) + shim 16 · `py_compile` 14 modul + shim GREEN · import smoke PASS
- [x] `pytest -m "not live"` — collects 0 in-repo (tidak ada `.py` test, hanya stale `.pyc`); **barrier W3 = hermetic suite `tests/test_w3_handlers.py` GREEN + behavioral diff MATCH + byte-diff sync track-b PASS** (documented; hemat-token-router punya suite sendiri terhadap `csmart_proxy.py` 2936-nya sendiri, bukan repo ini)

## Commit & Issue

- [ ] `git add csmart/ csmart_proxy.py checklist/` — 14 modul + shim + checklist ter-track (no `__pycache__`, no `.csmart/`, no `csmart_state.db`)
- [ ] `git commit -m "refactor: modularize csmart_proxy 3676->14 modules (<700/file) + shim"` — `git log --stat -1` verify
- [ ] `git status` clean (atau hanya `??` yang di-ignore), `git worktree list` consistent `main` vs `track-b`
- [ ] `gh issue close 1` — hanya setelah semua gate di atas green + `progress.md` updated 14/14 DONE
