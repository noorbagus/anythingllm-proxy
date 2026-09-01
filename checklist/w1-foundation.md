# W1 — Foundation (PARALEL 4 lane, setelah W0 gate)

> **Mode:** PARALEL — 4 terminal independen, tanpa cross-dependensi. Barrier: `py_compile` + import smoke semua file W1.
> **Gate masuk:** W0 green (Track-B committed)

```
T1 Foundation ─┐
T2 Security    ─┤─► BARRIER W1 ─► W2
T3 Streaming   ─┤
T4 Logging     ─┘
```

## T1 — Foundation: `app/config` + `routing/*`

- **Files:** `csmart/app/config.py` (~260), `csmart/routing/model.py` (~220), `csmart/routing/token_limits.py` (~90)
- **Source:** `csmart_proxy.py:65 _load_gateway_env`, `214 _load_openai_model_map`, `276 _model_matches_alias`, `287 clean_openai_model_name`, `297 is_openai_model`, `314 resolve_openai_endpoint`, `1041 route_model_tier` (graphify community 52), `clamp_max_tokens` + `_model_token_limits`
- **REQ:** Pure config/routing, no DB, no streaming, no secrets. Env: `OPENAI_BASE_URL` default `https://opencode.ai/zen/go/v1`, `UPSTREAM_BASE_URL` via `.env.local`
- **DESIGN:** `app/config.py` owns `OPENAI_MODEL_MAP` + env loading; `routing/model.py` owns matching/routing; `routing/token_limits.py` owns clamping. `routing/model.py` re-export `is_openai_model` untuk dipakai handlers.
- **IMPL:**
  - Extract verbatim, hilangkan global `app`/`vault` dep, inject via param bila perlu.
  - `wc -l` masing-masing <700 (estimasi T1 total ~570).
- **TEST:**
  ```bash
  python3 -m py_compile csmart/app/config.py csmart/routing/model.py csmart/routing/token_limits.py
  python3 -c "from csmart.routing.model import is_openai_model, resolve_openai_endpoint; assert is_openai_model('glm-5.3-flash'); assert resolve_openai_endpoint('muse-spark-1.2-contributor')=='responses'"
  ```
- **DONE:** `git add csmart/app/config.py csmart/routing/*.py` (commit per lane atau tunggu barrier)

### Prompt T1
```
Role: SDLC Foundation Engineer — W1 T1
Workdir: /Volumes/Xugab/LAB/Tria/anythingllm-proxy | Branch: refactor/modularize
Files: csmart/app/config.py, csmart/routing/model.py, csmart/routing/token_limits.py

REQ: Extract _load_gateway_env, _load_openai_model_map, is_openai_model, resolve_openai_endpoint, route_model_tier, clamp_max_tokens — no DB/streaming dep.
DESIGN: config owns env+model_map, routing/model owns matching, token_limits owns clamping. Pure, injectable.
IMPL: Verbatim csmart_proxy.py:65,214,276,287,297,314,1041 + clamp_max_tokens. Keep <700/file, no import cycle.
TEST: py_compile + is_openai_model/resolve_openai_endpoint smoke. Gate barrier W1.
DONE: Ready for W2 transform (butuh routing/model).
```

---

## T2 — Security: `security/secrets.py`

- **Files:** `csmart/security/secrets.py` (445) — ✅ **DONE**, synced `track-b → main`
- **Source:** `csmart_proxy.py:91,125,133-140,392-393,398-436,449-481,489-799` — `SECRET_REGEXES`, `_Rule`, `SecretVault:82`, `_shannon_entropy`, `_b64url_key`, `GITLEAKS_TOML` → `../../config/gitleaks.toml`
- **REQ:** Single responsibility secrets, expose `_Rule`, `load_gitleaks_rules`, `SecretVault`, `_redact/_b64url_key/_shannon_entropy/_secret_value` — tanpa `check_security_guardrails` (W2)
- **DESIGN:** Tanpa `_log` cyclic — `set_*_logger` inject fallback `csmart.logging.structured:_log`
- **TEST (done):** `py_compile OK`, `SecretVault smoke OK`, `pytest -k secret 12 passed`
- **DONE:** Lane selesai, jadi dependency W2 `guardrails` + `proxy_streamer`

### Prompt T2 (selesai — untuk arsip)
```
Role: SDLC Security Engineer — W1 T2 (DONE)
Files: csmart/security/secrets.py (445 LOC)
DESIGN: Single responsibility, DLP bidirectional vault, entropy threshold preserved.
IMPL: Verbatim extract + GITLEAKS_TOML patch. _log via injectable.
TEST: py_compile + SecretVault + pytest -k secret green. Sync track-b→main done.
```

---

## T3 — Streaming Leaf: `streaming/sse.py` + `streaming/redactor.py`

- **Files:** `csmart/streaming/sse.py` (211), `csmart/streaming/redactor.py` (184), `csmart/streaming/__init__.py` (33) — ✅ **DONE**, synced `track-b → main`
- **Source:** `csmart_proxy.py:2510 _parse_sse_data + 2526 _iter_sse_events + 2552 _sse_source + 2693 _format_event + 2466 _format_openai_chat_sse` (sse); `2480 _MARKER_RE + 2484 StreamingRedactor` (redactor)
- **REQ:** Pure leaf, no DB, injectable transport/timeout, `_parse_sse_data` dual form `['data: {"a":1}']` + `['{"a":1}']` + `[DONE]`
- **DESIGN:** `set_sse_logger`/`set_redactor_logger` inject fallback `csmart.logging.structured:_log`; redactor `unmask_fn` inject dual-vault probe
- **TEST (done):** `py_compile OK` ×2, `_parse_sse_data SPEC PASS`, MockTransport (200+trailing+500), StreamingRedactor split-safe ALL PASS
- **DONE:** Dependency W2 `proxy_streamer` + `transform`

### Prompt T3 (selesai — untuk arsip)
```
Role: SDLC Streaming Engineer — W1 T3 (DONE)
Files: csmart/streaming/sse.py, csmart/streaming/redactor.py, csmart/logging/structured.py
DESIGN: Pure SSE utils + StreamingRedactor isolasi, injectable logger/vault.
IMPL: Extract verbatim + logger inject + dual-form _parse_sse_data + REDACTOR_* events.
TEST: py_compile + hermetic MockTransport + split-safe PASS. Sync track-b→main done.
```

---

## T4 — Logging: `logging/structured.py`

- **Files:** `csmart/logging/structured.py` (**269**, was 225 T3) — ✅ **DONE T4 update**, synced `track-b → main`
- **Source:** `csmart_proxy.py:276-520` (`_log:424`, `get_db:449`, `init_db:455`, `_banner:439`) + `_redact`/`_SENSITIVE_KEYS` — graphify community 49+37 — **T4 change:** `_redact` tidak lagi duplikat lokal `structured.py:56`, kini `from csmart.security.secrets import _SENSITIVE_KEYS, _redact` (`secrets` ↛ `logging` → no cycle, grep verified)
- **REQ:** Isolasi structured JSONL audit + sqlite helper (`get_db`/`init_db` untuk `csmart_state.db` + `context_blobs`/`secret_vault`) + `_banner` (lazy `os.getenv`), thread-safe, never raises, redact by key, `ContextVar trace_id`, `LOG_DIR`/`VERBOSE`/`DB_PATH` env. DONT: `structured.py:269` + `secrets.py:445` terpisah (combined 714, tidak digabung >700)
- **DESIGN:** `_json_safe`, `_build_record(event, level, trace_id, ...)`, `_write_record` JSONL harian, `_log(event, **fields)` compat, `StructuredLogger` queue+daemon + singleton `logger`, `DB_PATH` + `get_db()`/`init_db()` verbatim proxy 449-482, `_banner()` lazy, `set_trace_id`/`get_trace_id`
- **TEST (done T4):** `py_compile OK` ×2, `CSMART_LOG_DIR=/tmp/csmart-test-logs python3 -c "from csmart.logging.structured import _log; _log('test',x=1)"` → PASS (`session_20260901.jsonl` created), `_redact({'api_key':'x'})→{[REDACTED]}` PASS, `get_db`/`init_db`/`_banner` PASS, `secrets` no-cycle PASS
- **DONE:** Dependency W2 semua lane

### Prompt T4 (selesai — untuk arsip, T4 update 225→269)
```
Role: SDLC Logging Engineer — W1 T4 (DONE, T4 update)
Files: csmart/logging/structured.py (269 LOC, was 225)
Source: csmart_proxy.py:276-520 (_log:424, get_db:449, init_db:455, _banner:439) + _redact via from csmart.security.secrets import _SENSITIVE_KEYS, _redact — no cycle
DESIGN: Thread-safe JSONL + sqlite helper + _banner lazy, ContextVar trace_id, _json_safe, compat _log + StructuredLogger queue. DONT: 269+445 separate (<700 each).
TEST: py_compile ×2 + CSMART_LOG_DIR _log → session_*.jsonl + _redact [REDACTED] + get_db/init_db/_banner PASS + secrets no-cycle PASS.
```

---

## Barrier W1

```bash
wc -l csmart/app/config.py csmart/routing/*.py csmart/security/secrets.py csmart/logging/structured.py csmart/streaming/*.py
python3 -m py_compile csmart/app/config.py csmart/routing/model.py csmart/routing/token_limits.py csmart/security/secrets.py csmart/logging/structured.py csmart/streaming/sse.py csmart/streaming/redactor.py
python3 -c "from csmart.security.secrets import SecretVault; from csmart.streaming.sse import _parse_sse_data; from csmart.logging.structured import _log; print('W1 imports OK')"
# Jika gate green → git add csmart/app csmart/routing csmart/security/secrets.py csmart/logging csmart/streaming/sse.py csmart/streaming/redactor.py
```
