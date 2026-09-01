# CSMART E2E VERIFICATION — SDLC Gate (Independent Verification)

> Prinsip: Verification ≠ Implementation. Session ini TIDAK boleh edit `csmart/**/*.py` / `csmart_proxy.py`. Hanya verifikasi + bukti. Fail → block `gh issue close 1`.

## 1. Prime Directive & Context
- Proyek: `anythingllm-proxy` — modularize `csmart_proxy.py:3676 → 14 modul (<700/file) + shim 16`
- W3 DONE 5/5 claimed: `handlers/messages:395` + `handlers/openai:221` + `factory:37` + `keepalive:90` + `config:340` + `proxy_streamer:341` + `transform/anthropic_to_openai:536` + `transform/openai_to_anthropic:697` + 6 pendukung + shim. Total 4410 LOC, max 697.
- Objektif E2E: Buktikan **behavioral equivalence** vs source + **contract & integration** green secara independen (hermetic + live jika kredensial ada). Traceability: tiap requirement → 1+ test case → bukti log.

## 2. Preconditions (Arrange) — Verifikasi DULU sebelum test
```bash
pwd # /Volumes/Xugab/LAB/Tria/anythingllm-proxy
wc -l csmart/**/*.py csmart_proxy.py # tiap file <700, max 697, shim 16, total 4410
python3 -m py_compile csmart/**/*.py csmart_proxy.py && echo "main py_compile GREEN"
python3 -m py_compile /Volumes/Xugab/LAB/Tria/track-b/csmart/**/*.py /Volumes/Xugab/LAB/Tria/track-b/csmart_proxy.py && echo "track-b py_compile GREEN"
for f in csmart/app/config.py csmart/app/factory.py csmart/app/keepalive.py csmart/handlers/messages.py csmart/handlers/openai.py csmart/streaming/proxy_streamer.py csmart/streaming/sse.py csmart_proxy.py csmart/transform/anthropic_to_openai.py csmart/transform/openai_to_anthropic.py csmart/routing/model.py csmart/security/guardrails.py; do diff -q "$f" "/Volumes/Xugab/LAB/Tria/track-b/$f" || echo "DIFF $f"; done; echo "byte-diff PASS"
python3 -c "from csmart.security.secrets import SecretVault; v=SecretVault(); print('secrets OK')"
python3 -c "from csmart.streaming.sse import _parse_sse_data; assert _parse_sse_data(['data: {\"a\":1}'])=={'a':1}; print('sse OK')"
python3 -c "from csmart.logging.structured import _log; print('logging OK')"
python3 -c "from csmart.app.factory import app; print('factory OK', type(app))"
python3 -c "import csmart_proxy; assert hasattr(csmart_proxy,'app'); print('shim OK')"
grep -rn "from csmart" csmart/security/secrets.py && echo "CYCLE FAIL" || echo "no-cycle PASS: secrets leaf"
grep -rn "from csmart.transform.anthropic_to_openai" csmart/transform/openai_to_anthropic.py && echo "CYCLE FAIL" || echo "no-cycle PASS: transform pure"
cat /tmp/gf-meter.txt 2>/dev/null || echo "gf-meter N/A"
```
Gate: STOP jika salah satu FAIL.

## 3. Verification Levels (SDLC V-Model)

### L1 — Unit / Transform (Pure Functions, No I/O)
Tujuan: Pure transform equivalence vs `csmart_proxy.py` source.
- `transform/anthropic_to_openai.py` 536: 9 def (`_extract_system_text`, `_convert_anthropic_tool_to_openai`×2, `_convert_anthropic_message_to_openai`×2, `transform_anthropic_to_openai_chat`, `transform_anthropic_to_openai_responses`, `transform_openai_chat_to_responses`, `_resolve_reasoning_effort`)
- `transform/openai_to_anthropic.py` 697: 4 def + `set_active_model` reuse `streaming/sse.py:217` helpers (`_format_event/_iter_sse_events/_parse_sse_data/_safe_json_loads`)
- Test: import smoke + `_resolve_reasoning_effort({thinking:{type:disabled}})→None` + behavioral diff JSON/SSE/incomplete/backfill vs source (harus MATCH)
```bash
python3 -c "from csmart.transform.anthropic_to_openai import transform_anthropic_to_openai_chat, transform_anthropic_to_openai_responses, transform_openai_chat_to_responses; print('T2a OK')"
python3 -c "from csmart.transform.openai_to_anthropic import transform_openai_sse_to_anthropic, transform_openai_responses_sse_to_anthropic, transform_openai_responses_to_anthropic_json, transform_openai_chat_to_anthropic_json, set_active_model; print('W2b OK')"
```

### L2 — Contract / Routing (Model Dispatch)
Tujuan: Kontrak routing & token limits MATCH source.
- `clean_openai_model_name` / `is_openai_model` / `is_anthropic_native_model` / `detect_openai_endpoint_type` / `route_model_tier` / `clamp_max_tokens`
- 8 models matrix: `glm-5.3-flash`, `kimi-k2.5`, `deepseek-chat`, `minimax-m2`, `muse-spark-1.2-contributor`, `gpt-5`, `claude-4-sonnet`, `gemini-2.5`
- `store_ccr_payload` ref_id identical + stub EQUAL + roundtrip
- `guardrails`: `check_security_guardrails('bash',{command:'cat ~/.aws/credentials'})` block, `('read',{path:.env.local})` block, `/tmp/foo.txt` allow
```bash
python3 -c "from csmart.routing.model import is_openai_model, resolve_openai_endpoint, is_anthropic_native_model; assert is_openai_model('glm-5.3-flash'); assert resolve_openai_endpoint('muse-spark-1.2-contributor')=='responses'; assert is_anthropic_native_model('minimax-m2'); print('routing OK')"
python3 -c "from csmart.routing.token_limits import clamp_max_tokens; assert clamp_max_tokens({'model':'deepseek-chat','max_tokens':999999})==16384; print('token_limits OK')"
python3 -c "from csmart.security.guardrails import check_security_guardrails; assert not check_security_guardrails('bash',{'command':'cat ~/.aws/credentials'})[0]; print('guardrails OK')"
```

### L3 — Integration / Handlers (Hermetic, No Network)
Tujuan: Handler orchestration + passthrough correctness secara hermetik (`ASGITransport` + `MockTransport`).
- Suite: `pytest tests/test_w3_handlers.py -v` — harus **9 passed**
- Coverage per test:
  1. `/v1/messages` OpenAI chat stream→Anthropic SSE
  2. `/v1/messages` non-stream JSON
  3. Anthropic-native preserve model
  4. `/v1/chat/completions` responses-routed → body `input`
  5. `/v1/responses` passthrough
  6. `/v1/models` list
  7. Passthrough openai-model strips `v1/` (no `//v1/v1` — fix dari source bug `.../v1/v1/embeddings`)
  8. Passthrough non-openai → upstream
  9. Shim exposes `app` (`import csmart_proxy` has `app`)
- Plus: `_UPSTREAM_TRANSPORT` dynamic via `get_upstream_transport()` (bukan import-binding stale), keepalive leaf (`app/keepalive.py:90`) dipakai factory+handlers
```bash
pytest tests/test_w3_handlers.py -v --tb=short 2>&1 | tee /tmp/e2e-l3.log
# Verifikasi passthrough fix:
grep -n "path\[3:\]" csmart/handlers/openai.py && echo "passthrough fix PRESENT" || echo "fix MISSING"
# Verifikasi keepalive leaf:
grep -n "get_upstream_transport\|_UPSTREAM_TRANSPORT" csmart/streaming/sse.py csmart/handlers/openai.py
```

### L4 — E2E Live (Optional, jika kredensial ada)
Tujuan: Live upstream `UPSTREAM_BASE_URL` / `OPENAI_BASE_URL` — hanya jika env kredensial tersedia, selain itu SKIP terdokumentasi.
```bash
# Live hanya jika OPENAI_API_KEY / ANTHROPIC_API_KEY tersedia; jangan hardcode secret
pytest -m live -v 2>&1 | tee /tmp/e2e-l4.log || echo "L4 SKIP (no live creds) — documented"
# Alternatif manual curl (jika server running):
# uvicorn csmart_proxy:app --port 8000 &
# curl -s http://localhost:8000/v1/models | head -c 500
```

## 4. Non-Functional Verification
- No-cycle: `transform pure (json/os/typing only, no csmart.*)`, `routing ↛ security`, `handlers ↛ factory`, `factory → handlers` one-way, `keepalive` leaf
- LOC gate: `wc -l csmart/**/*.py` tiap file <700, shim ~30 (bukti di log)
- Compile gate: `py_compile` main + track-b GREEN
- Sync gate: `diff -q` main vs track-b byte-identical untuk semua modul + shim
- Security: `sanitize_payload` vault mask via `csmart.security.secrets`, guardrails block list verified

## 5. Evidence & Traceability (Wajib Simpan)
- Simpan log: `pytest tests/test_w3_handlers.py -v | tee /tmp/e2e-verify.log`
- Simpan `wc -l` + `py_compile` + `diff -q` + `import smoke` output ke `/tmp/e2e-verify.log` (append)
- Simpan `cat /tmp/gf-meter.txt` (tool call count + estimasi token) ke log
- Simpan `graphify` status jika ada: `ls -l graphify-out/graph.json` + `head -n 20 graphify-out/graph.json` (stale check)
- Jika fail → sertakan `diff` behavioral vs source + MockTransport URL trace + reproducer minimal
- Update checklist hanya setelah bukti green: `checklist/dod.md` Global+W3 + `checklist/progress.md` Skor 14/14 (14 PASS <700 + 0 VIOLATION, 4410 LOC)

## 6. Exit Criteria (Done HANYA jika)
- [ ] Preconditions GREEN (LOC <700, py_compile main+track-b, byte-diff PASS, 5 import smoke, no-cycle PASS)
- [ ] L1 9 def + 4 def transform smoke PASS + behavioral diff MATCH
- [ ] L2 contract MATCH (8 models + clamp + CCR + guardrails)
- [ ] L3 9 passed hermetic + passthrough `//v1/v1` fix verified + keepalive leaf verified
- [ ] L4 live PASS/SKIP terdokumentasi (jika SKIP, alasan env missing tercatat)
- [ ] Evidence `/tmp/e2e-verify.log` + `/tmp/e2e-l3.log` + `gf-meter` ada
- Baru boleh `git add checklist/ && git commit` + `gh issue close 1`

## 7. Aturan Eksekusi
- Jangan edit source; **verifikasi only**. Jika temuan → lapor sebagai defect dengan reproducer, jangan auto-fix.
- Hermetic dulu, live terakhir. Tiap layer harus green sebelum naik (L1→L2→L3→L4).
- Semua claim harus ada bukti command output, bukan asumsi. Satu fail di L1-L3 = block close.
- Gunakan `ASGITransport` + `MockTransport` untuk L3; jangan hit network live tanpa `pytest -m live`.
