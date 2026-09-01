# W3 — Handlers + Factory + Shim (SEQUENTIAL — Orchestrator)

> **Mode:** SEQUENTIAL — 1 terminal orchestrator, butuh W2 gate green. Tidak paralel dengan W2.
> **Gate masuk:** W2 `pytest -m "not live"` green

## REQ

- `csmart_proxy.py:3240 handle_messages:2651` (degree 21, 18 nodes, community 47) — slim orchestrator <500 LOC (dari ~800)
- `csmart_proxy.py:3500 handle_openai_chat`, `3546 handle_openai_responses`, `3578 handle_models` (~360) + `3622 passthrough` (~210) — gabung `csmart/handlers/openai.py`, preserve `passthrough` double-`/v1` guard dari W0
- `csmart_proxy.py` lifespan/keepalive/`_banner` + `create_app()` — `csmart/app/factory.py` (~280)
- Shim backward compat: `csmart_proxy.py` 3676 → ~30 baris `from csmart.app.factory import app`

## DESIGN

- `handlers/messages.py` — orchestrator tipis: `sanitize_payload` → `route_model_tier` → `check_security_guardrails` → `transform_anthropic_to_openai_*` / `resolve_openai_endpoint` → `ProxyStreamer` → `_log`. Delegasi ke modul, no God logic.
- `handlers/openai.py` — 3 handler OpenAI + passthrough. Reuse `transform_openai_chat_to_responses` (dari `transform/anthropic_to_openai.py`), `transform_openai_responses_to_openai_chat_*` (dari `transform/openai_to_anthropic.py`), `resolve_openai_endpoint`/`is_openai_model` (routing), `_openai_upstream_headers` (app/config), `ProxyStreamer` (streaming).
- `app/factory.py` — `def create_app(): FastAPI()` + `lifespan`, `keepalive`, `_banner`, mount routers `handlers/*`. Expose `app` singleton.
- `csmart_proxy.py` shim — untuk AnythingLLM `import csmart_proxy:app` tetap jalan:
  ```python
  """shim — use csmart.app.factory:app"""
  from csmart.app.factory import app
  __all__ = ["app"]
  ```
  Jika AnythingLLM butuh simbol lain (e.g. `handle_messages`), re-export di shim atau update importer.

## IMPL Checklist

- [ ] Extract `handle_messages` community 47 — pindah `align_prefix_3_region`, `clean_openai_model_name` sudah di `routing/model.py` → import saja, jangan duplikat.
- [ ] `handlers/openai.py` — merge 4 route, keep `@app.post("/v1/chat/completions")` etc. tapi di `factory.py` via `APIRouter` atau mount langsung. Auth: `_openai_upstream_headers(request)` fallback `OPENAI_API_KEY`.
- [ ] `passthrough` — reuse guard `if target_base == OPENAI_BASE_URL and path.startswith("v1/")` → `f"{OPENAI_BASE_URL}/{path[3:]}"` (W0 verified), inject `workdir=track-b` test sudah green.
- [ ] `app/factory.py` — pindah `csmart_proxy.py:65 _load_gateway_env`, `439 _banner`, `lifespan` keepalive, `get_db`/`init_db` now di `logging/structured.py` → import.
- [ ] `wc -l` semua file <700 (messages ~480, openai ~570, factory ~280).
- [ ] Update `csmart/__init__.py` re-export bila perlu, `__all__`.

## TEST — Final Gate

```bash
# 1. Compile 14 modul
python3 -m py_compile csmart/app/factory.py csmart/app/config.py csmart/routing/*.py csmart/security/*.py csmart/logging/structured.py csmart/transform/*.py csmart/streaming/*.py csmart/handlers/*.py

# 2. Import smoke
python3 -c "from csmart.app.factory import app; print('app OK', app.title)"
python3 -c "import csmart_proxy; print('shim OK', csmart_proxy.app)"

# 3. Unit + hermetic
pytest -m "not live" -q                          # green
pytest tests/test_csmart_proxy_openai.py -k models -q  # green

# 4. Live (opsional, butuh OPENAI_API_KEY)
uvicorn csmart.app.factory:app --port 8080 &
curl -H "Authorization: Bearer dummy" http://127.0.0.1:8080/v1/models | jq
curl -X POST http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"muse-spark-1.2-contributor","messages":[{"role":"user","content":"hi"}]}' | head

# 5. Graph + loc
wc -l csmart/**/*.py csmart_proxy.py
# wajib tiap file <700
graphify update . 2>&1 | tail -n 20
cat graphify-out/GRAPH_REPORT.md | head -n 60  # cek God degree 66 turun
```

## DONE

- [ ] `git add csmart/ csmart_proxy.py && git commit -m "refactor: modularize csmart_proxy 3676->14 modules (<700/file) + shim"`
- [ ] `git log --stat -1` verify 14 file + shim
- [ ] `gh issue close 1` bila AC B1–B4 + DoD green
- [ ] Sync `track-b` worktree: `git -C /Volumes/Xugab/LAB/Tria/track-b rebase origin/main` atau cherry-pick `csmart/` ke `track-b` jika track-b masih aktif

## Prompt Terminal W3 — Orchestrator (SEQUENTIAL, after W2 gate)

```
Role: SDLC Integrator — Orchestrator (W3 SEQUENTIAL)
Workdir: /Volumes/Xugab/LAB/Tria/anythingllm-proxy | Branch: refactor/modularize
Files: csmart/handlers/messages.py, csmart/handlers/openai.py, csmart/app/factory.py, csmart_proxy.py (shim)
Depend: W1+W2 green — transform/* + guardrails + proxy_streamer + routing/model + streaming/sse + secrets + logging

REQ: Slim handle_messages (degree 21→orchestrator <500) + handle_openai_chat/responses/models + passthrough double-/v1 fix + create_app/lifespan.
DESIGN: messages delegasi ke transform/routing/guardrails/streaming; openai.py gabung 4 route + auth OPENAI_BASE_URL vs UPSTREAM_BASE_URL; factory create_app+lifespan; shim from csmart.app.factory import app.
IMPL: Extract last, update cross-module imports, wc -l <700, no cycle. Reuse W0 passthrough guard verified.
TEST: py_compile 14 file + pytest -m "not live" + live curl 8080 + graphify update . + git log --stat
DONE: git add+commit refactor: modularize 3676->14 + shim + gh issue close 1. Workdir main, sync track-b jika perlu.
```
