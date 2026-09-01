# Prompt Refactor — `csmart/app/config.py` (W1 T1, PARALLEL lane)

> **Paste prompt ini ke terminal T1 (paralel, gated by W0).** 1 terminal = 1 lane. Jangan campur edit file lane lain.
> Worktree: `anythingllm-proxy` branch `refactor/modularize` (main). Sync `track-b` tidak perlu untuk file ini — source-of-truth `csmart_proxy.py` main `9a30983`.

---

## Role

**SDLC Foundation Engineer — W1 T1**
Workdir: `/Volumes/Xugab/LAB/Tria/anythingllm-proxy`
Branch: `refactor/modularize`
File target: `csmart/app/config.py:1` — **0 → ~260 LOC, wajib <700** (`wc -l` bukti)
Source-of-truth: `csmart_proxy.py:65-273` + `csmart_proxy.py:95-130` + `csmart_proxy.py:167-193` (lihat `grep -n` mapping di bawah)

---

## SDLC Principle (ikuti urut, jangan loncat)

### 1. REQ — Requirements (apa yang harus ada)

Single responsibility **config/env**: load env, expose constants & helpers. **Tidak boleh** import DB, streaming, secrets, guardrails — pure layer paling bawah (tanpa dependensi ke `csmart/*` lain). Wajib expose (tanpa cycle, pure):

- `def _load_gateway_env() -> None` — verbatim `csmart_proxy.py:65-76` (PrivateLink `.env` → `load_dotenv` dari `dotenv`, mirror `router/cli_dispatch.py:36`). Dipanggil sekali saat import modul.
- Konstanta proxy: `UPSTREAM_BASE_URL:81` (`ANTHROPIC_UPSTREAM_URL` || `UPSTREAM_BASE_URL` || `https://api.deepseek.com/anthropic`), `UPSTREAM_API_KEY:86` (`UPSTREAM_API_KEY` || `ANTHROPIC_AUTH_TOKEN`), `OPENAI_API_KEY:88`, `PROXY_HOST:89` (`CSMART_HOST` default `127.0.0.1`), `PROXY_PORT:90` (`CSMART_PORT` 8080 int), `DB_PATH:91` (`CSMART_DB`), `FLASH_MODEL:93`, `FLAGSHIP_MODEL:94` (`CSMART_FLASH_MODEL`/`CSMART_FLAGSHIP_MODEL`), `UPSTREAM_TIMEOUT:95` (`CSMART_UPSTREAM_TIMEOUT` 120 float)
- Token limits config: `MAX_TOKENS_FLOOR:96` (`CSMART_MIN_MAX_TOKENS` 4096), `MAX_TOKENS_CEIL:97` (`CSMART_MAX_MAX_TOKENS` 16384), `_MODEL_TOKEN_LIMITS:101` (list `{"keys": [...], "floor": ..., "ceil": ...}` untuk `deepseek-v4`/`muse-spark` + env override `CSMART_MAX_TOKENS_FLOOR_DEEPSEEK` etc.), `MAX_ROUNDS:113`, `SANITIZE_TRUNCATE_BYTES:117`, `SANITIZE_TRUNCATE_LINES:118`, `CCR_MIN_BYTES:121`, `CCR_PREVIEW_LINES:122`, `DLP_ALLOW:125`, `MOCK_MODE:130` — verbatim default + `os.getenv` fallback
- OpenAI routing config: `OPENAI_BASE_URL:167` (`CSMART_OPENAI_BASE_URL` || `OPENAI_BASE_URL` || `https://opencode.ai/zen/go/v1`, `.rstrip("/")`), `OPENAI_CHAT_COMPLETIONS_PATH:171` (`CSMART_OPENAI_CHAT_COMPLETIONS_PATH` || `/chat/completions`), `OPENAI_RESPONSES_PATH:174` (`CSMART_OPENAI_RESPONSES_PATH` || `/responses`), `OPENAI_MESSAGES_PATH:193` (`CSMART_OPENAI_MESSAGES_PATH` || `/messages`)
- Pattern lists: `OPENAI_MODEL_PATTERNS`, `OPENAI_RESPONSES_MODEL_PATTERNS:180` (`CSMART_RESPONSES_PATTERNS` default `grok-,gpt-5.6,muse-`), `ANTHROPIC_NATIVE_MODEL_PATTERNS:196` (`CSMART_ANTHROPIC_NATIVE_PATTERNS` / minimax-m*, qwen3.*) — verbatim `os.getenv(...).split(",")` filtering
- `def _load_openai_model_map() -> Dict[str, Dict[str,str]]` — verbatim `csmart_proxy.py:214-270` (parse `CSMART_OPENAI_MODEL_MAP` JSON env: Key=alias, value `{target, endpoint_type}` atau string shorthand, normalize `endpoint_type` ke `responses|messages|chat_completions`, fallback defaults). Plus `OPENAI_MODEL_MAP = _load_openai_model_map():273`

**Out of scope** (jangan masukkan ke `config.py`): `clean_openai_model_name`/`is_openai_model`/`resolve_openai_endpoint`/`route_model_tier` → `csmart/routing/model.py`; `clamp_max_tokens`/`_model_token_limits` → `csmart/routing/token_limits.py` (tapi konstanta `MAX_TOKENS_FLOOR/CEIL` + `_MODEL_TOKEN_LIMITS` tetap di `config.py` dan di-import oleh `token_limits.py`).

### 2. DESIGN — Module boundaries

```
csmart/app/config.py (pure, no csmart.* import)
        ▲
        │ imports
csmart/routing/token_limits.py  (from csmart.app.config import MAX_TOKENS_FLOOR, MAX_TOKENS_CEIL, _MODEL_TOKEN_LIMITS)
csmart/routing/model.py         (from csmart.app.config import OPENAI_BASE_URL, OPENAI_MODEL_MAP, *_PATTERNS)
csmart/handlers/*, transform/*, streaming/* (indirect via routing)
```

- Tidak ada import siklus: `config.py` **tidak** import `csmart.routing.*`, `csmart.security.*`, dll. Hanya stdlib + `os` + `json` + `dotenv` (opsional, try/except).
- `_load_gateway_env()` dipanggil di top-level modul (sebelum konstanta), mirror `csmart_proxy.py:76`.
- `OPENAI_BASE_URL` selalu `rstrip("/")` — guard double `/v1/v1` di `handlers/openai.py` (W3) bergantung ini.
- Semua konstanta dibaca sekali saat import (env snapshot), bukan per-request — konsisten dengan monolit.

### 3. IMPL — Implementation steps

1. Baca source verbatim (jangan rewrite logic):
   ```bash
   sed -n '65,76p' csmart_proxy.py      # _load_gateway_env
   sed -n '81,130p' csmart_proxy.py     # UPSTREAM_* + PROXY_* + *_MODEL + token limits header
   sed -n '167,210p' csmart_proxy.py    # OPENAI_BASE_URL + 3 PATH + 3 PATTERN lists
   sed -n '214,273p' csmart_proxy.py    # _load_openai_model_map + OPENAI_MODEL_MAP
   sed -n '950,954p' csmart_proxy.py    # _model_token_limits signature (untuk cek import, bukan untuk copy ke config)
   ```
2. Buat `csmart/app/config.py:1` — struktur:
   ```python
   """csmart.app.config — env & constants (pure, no DB/streaming dep)."""
   from __future__ import annotations
   import json, os
   try: from dotenv import load_dotenv
   except ImportError: load_dotenv = None

   def _load_gateway_env() -> None: ...  # verbatim 65-76
   _load_gateway_env()

   UPSTREAM_BASE_URL = ...  # 81-85
   UPSTREAM_API_KEY = ...   # 86
   OPENAI_API_KEY = ...     # 88
   PROXY_HOST = ...         # 89
   PROXY_PORT = ...         # 90
   DB_PATH = ...            # 91
   FLASH_MODEL = ...        # 93
   FLAGSHIP_MODEL = ...     # 94
   UPSTREAM_TIMEOUT = ...   # 95
   MAX_TOKENS_FLOOR = ...   # 96
   MAX_TOKENS_CEIL = ...    # 97
   _MODEL_TOKEN_LIMITS = [...]  # 101-115
   MAX_ROUNDS = ...         # 113
   SANITIZE_TRUNCATE_BYTES = ... # 117
   SANITIZE_TRUNCATE_LINES = ... # 118
   CCR_MIN_BYTES = ...      # 121
   CCR_PREVIEW_LINES = ...  # 122
   DLP_ALLOW = ...          # 125
   MOCK_MODE = ...          # 130
   OPENAI_BASE_URL = ...    # 167-170
   OPENAI_CHAT_COMPLETIONS_PATH = ... # 171-173
   OPENAI_RESPONSES_PATH = ...        # 174-178
   OPENAI_MESSAGES_PATH = ...         # 193
   OPENAI_MODEL_PATTERNS = [...]      # jika ada di monolit (cek grep)
   OPENAI_RESPONSES_MODEL_PATTERNS = [...] # 180-189
   ANTHROPIC_NATIVE_MODEL_PATTERNS = [...] # 196-205
   def _load_openai_model_map(): ...  # 214-270
   OPENAI_MODEL_MAP = _load_openai_model_map()  # 273
   __all__ = [...]  # export semua di atas
   ```
3. Pertahankan `os.getenv` key & default verbatim (jangan rename env var).
4. `wc -l csmart/app/config.py` harus ~220-280 (<700). Jika >300, cek duplikasi.
5. Pastikan `__init__.py` ada: `csmart/__init__.py`, `csmart/app/__init__.py` (sudah ada, 0B).

### 4. TEST — Verification (wajib PASS sebelum DONE)

```bash
# 1. Compile
python3 -m py_compile csmart/app/config.py && echo "OK"

# 2. Import smoke (pure, tanpa csmart lain)
python3 -c "from csmart.app.config import UPSTREAM_BASE_URL, OPENAI_BASE_URL, OPENAI_MODEL_MAP, OPENAI_CHAT_COMPLETIONS_PATH, OPENAI_RESPONSES_PATH, _MODEL_TOKEN_LIMITS, PROXY_HOST, FLASH_MODEL; print('UPSTREAM', UPSTREAM_BASE_URL); print('OPENAI', OPENAI_BASE_URL); print('MAP', len(OPENAI_MODEL_MAP)); print('LIMITS', _MODEL_TOKEN_LIMITS[:1])"

# 3. Env snapshot defaults (tanpa .env)
python3 -c "from csmart.app.config import OPENAI_BASE_URL; assert OPENAI_BASE_URL=='https://opencode.ai/zen/go/v1', OPENAI_BASE_URL; print('default OPENAI_BASE_URL PASS')"
python3 -c "from csmart.app.config import UPSTREAM_BASE_URL; assert UPSTREAM_BASE_URL, 'empty'; print('UPSTREAM_BASE_URL', UPSTREAM_BASE_URL)"

# 4. JSON env override (isolate subprocess agar tidak pollute)
CSMART_OPENAI_MODEL_MAP='{"my-alias":{"target":"real-model","endpoint_type":"responses"}}' python3 -c "from csmart.app.config import OPENAI_MODEL_MAP; assert 'my-alias' in OPENAI_MODEL_MAP, OPENAI_MODEL_MAP; assert OPENAI_MODEL_MAP['my-alias']['endpoint_type']=='responses'; print('OPENAI_MODEL_MAP JSON PASS', OPENAI_MODEL_MAP['my-alias'])"

# 5. Import cycle check — config tidak import routing/handlers
python3 -c "import ast, pathlib; src=pathlib.Path('csmart/app/config.py').read_text(); assert 'csmart.routing' not in src and 'csmart.security' not in src and 'csmart.streaming' not in src, 'cycle!'; print('no cycle PASS')"

# 6. LOC
wc -l csmart/app/config.py  # <700

# 7. Barrier W1 (setelah T2/T3/T4 sudah DONE synced dari track-b)
python3 -m py_compile csmart/app/config.py csmart/security/secrets.py csmart/logging/structured.py csmart/streaming/sse.py csmart/streaming/redactor.py && echo "W1 py_compile green"
```

- Jika `pytest -k config` ada, jalankan; jika belum ada test khusus, smoke di atas cukup untuk gate.
- Hermetic tidak perlu network — pure env.

### 5. DONE — Definition of Done untuk lane ini

- [ ] `wc -l csmart/app/config.py` <700 (bukti di commit msg atau `checklist/progress.md`)
- [ ] `py_compile` OK + 7 smoke PASS
- [ ] Tidak ada import ke `csmart.routing`/`csmart.security`/`csmart.streaming` (cek `grep -n "from csmart" csmart/app/config.py` harus kosong atau hanya `dotenv`)
- [ ] `git add csmart/app/config.py` (commit per lane atau tunggu barrier W1 — koordinasi di `checklist/progress.md`)
- [ ] Update `checklist/progress.md`: `csmart/app/config.py` 0 → ~260 LOC, status ✅, gate W1 T1
- [ ] Lane ini paralel — boleh jalan bareng T2/T3/T4, tapi barrier W1 baru green setelah semua T1–T4 `py_compile` green

---

## Env Penting (jangan commit secret)

- `OPENAI_BASE_URL` default `https://opencode.ai/zen/go/v1` (rstrip "/"), `UPSTREAM_BASE_URL` dari `ANTHROPIC_UPSTREAM_URL` || `UPSTREAM_BASE_URL` || fallback `https://api.deepseek.com/anthropic` — jangan commit `.env.local`
- `OPENAI_API_KEY` dari `PrivateLink/.env.local` — jangan commit secret, `DLP_ALLOW` dari `CSMART_DLP_ALLOW`
- `CSMART_OPENAI_MODEL_MAP` JSON env — key=alias, value `{target, endpoint_type}` atau string shorthand — tes via subprocess env override

## Acceptance Criteria

- [ ] File `csmart/app/config.py:1` berisi ~260 LOC, <700, `py_compile` OK
- [ ] Semua konstanta + `_load_gateway_env` + `_load_openai_model_map` + `OPENAI_MODEL_MAP` verbatim, env key & default preserved
- [ ] Tidak ada cycle, pure layer, barrier W1 siap untuk `routing/model.py` + `routing/token_limits.py` (depend on `config.py`)
