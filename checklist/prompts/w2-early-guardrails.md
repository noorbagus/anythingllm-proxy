# Prompt Refactor — `csmart/security/guardrails.py` (W2-early, PARALLEL lane T-B)

> **Paste ke terminal T-B (paralel bareng T-A `app/config` + T-C `proxy_streamer`).** Lane ini **bisa jalan sekarang** — depend T2/T4 DONE (445+269, no cycle).
> Workdir: `/Volumes/Xugab/LAB/Tria/anythingllm-proxy` Branch: `refactor/modularize` | Barrier: W2 butuh T-A+routing green

---

## Role

**SDLC Security Engineer — W2 T-B (PARALLEL)**
Workdir: `/Volumes/Xugab/LAB/Tria/anythingllm-proxy` | Branch: `refactor/modularize`
File: `csmart/security/guardrails.py:1` (~260 LOC, <700)
Source: `csmart_proxy.py:804-905` + `1019-1090` (+ `804 BLOCKED_*_PATTERNS`)
Depend DONE: `csmart/security/secrets.py:1` (445) + `csmart/logging/structured.py:1` (269) — `269+445=714` separate, `secrets` ↛ `logging` no cycle

---

## SDLC

### 1. REQ

Single responsibility **guardrails** — block credential/tool abuse sebelum forward. Wajib verbatim, never raises:

- Konstanta: `BLOCKED_PATH_PATTERNS:804` (list regex path credential — `~/.aws`, `.env.local`, `PrivateLink/.env`, `.git/config`, `id_rsa`, `credentials.json`, dll) + `BLOCKED_COMMAND_PATTERNS:820` (list regex command — `cat /etc/shadow`, `env | grep`, `printenv`, `aws configure`, `gcloud auth`, dll) — verbatim `os.getenv` override jika ada, else defaults
- `def check_security_guardrails(tool_name: str, tool_input: Any) -> Optional[str]:837` — return violation string atau `None`:
  - `tool_name in ("bash","execute_command","command","run_command")` → `tool_input["command"||"cmd"]` match `BLOCKED_COMMAND_PATTERNS` (re.IGNORECASE) → `f"command memuat akses credential sensitif (diblokir): {cmd[:120]}"`
  - candidates dari keys `("path","file_path","filepath","cwd","root","subpath")` + nested `("view","edit","read","glob")` dict → match `BLOCKED_PATH_PATTERNS` → violation
  - `sanitize_payload` helper: `text` encode `SANITIZE_TRUNCATE_BYTES:117` + `SANITIZE_TRUNCATE_LINES:118` truncation → `[snipped N lines]` — preserve `DLP_ALLOW:125` allowlist check via `from csmart.security.secrets import _shannon_entropy` jika dipakai
- `def sanitize_payload(body: Dict[str,Any]) -> None:905` — in-place walk `messages[].content` → `_walk_content` truncate + redact (verb `csmart_proxy.py:905-945`), no return, never raises. Keep `SANITIZE_TRUNCATE_*` via `from csmart.app.config import SANITIZE_TRUNCATE_BYTES/LINES` atau `os.getenv` fallback jika config belum green (isolasi: prefer import config, fallback `os.getenv`)
- Optional but keep if present: `def _mcp_sse_post(url, payload, timeout=25):1019` + `def _websearch_exa(query, max_results=5):1045` + `EXA_MCP_URL:1016` — stdlib only, tanpa API key, Exa hosted MCP `https://mcp.exa.ai/mcp` (override `EXA_MCP_URL` env) — keep jika `check_security_guardrails` tidak butuh, pindah ke `guardrails.py` sebagai websearch helper (atau defer ke W3 handlers jika lean, tapi keep <700 so include).

**Out of scope:** `SecretVault`/`_Rule`/`_shannon_entropy` internals → `secrets.py:1` (import, jangan duplikat). `align_prefix_3_region`/`route_model_tier` → `routing/model.py`.

### 2. DESIGN

```
csmart/security/secrets.py (SecretVault, _shannon_entropy, _b64url_key, _redact — no logging import)
        ▲ from csmart.security.secrets import ... (optional, hanya jika guardrails butuh entropy/allowlist check)
csmart/security/guardrails.py (pure check, inject secrets+logging via import, never raises)
        ▲ from csmart.logging.structured import _log (optional, structured GUARDRAIL_BLOCK event)
        │ from csmart.app.config import SANITIZE_TRUNCATE_*, DLP_ALLOW (fallback os.getenv if config lane not yet)
csmart/handlers/messages.py (W3) — caller: `err = check_security_guardrails(info["name"], tool_input); if err: block`
csmart/streaming/proxy_streamer.py (W2-early T-C) — no direct depend on guardrails (parallel via inject: streamer tidak import guardrails)
```

- Tidak ada cycle: `guardrails` tidak import `csmart.handlers`, `csmart.streaming.proxy_streamer`, `csmart.routing.model`.
- `secrets.py` tetap ↛ `logging`/`guardrails` — guardrails satu arah import secrets/logging.
- Keep stdlib: `re`, `os`, `json`, `pathlib` — no new dependency.

### 3. IMPL

```bash
sed -n '804,945p' csmart_proxy.py   # BLOCKED_* + check_security_guardrails + sanitize_payload
sed -n '1019,1090p' csmart_proxy.py # _mcp_sse_post + _websearch_exa + EXA_MCP_URL (+ store_ccr_payload skip — itu CCR, not guardrails)
grep -n "SANITIZE_TRUNCATE\|DLP_ALLOW\|BLOCKED_" csmart_proxy.py | head
```

Struktur `guardrails.py`:
```python
"""csmart.security.guardrails — check_security_guardrails + sanitize_payload (pure, never raises)."""
from __future__ import annotations
import os, re, json
from typing import Any, Dict, List, Optional
try:
    from csmart.app.config import SANITIZE_TRUNCATE_BYTES, SANITIZE_TRUNCATE_LINES, DLP_ALLOW
except ImportError:
    SANITIZE_TRUNCATE_BYTES = int(os.getenv("CSMART_SANITIZE_MAX_BYTES", "2048"))
    SANITIZE_TRUNCATE_LINES = int(os.getenv("CSMART_SANITIZE_MAX_LINES", "40"))
    DLP_ALLOW = [w for w in os.getenv("CSMART_DLP_ALLOW", "").split(",") if w]
try: from csmart.logging.structured import _log
except ImportError: _log = lambda *a, **kw: None
# optional: from csmart.security.secrets import _shannon_entropy (if needed for allowlist)

BLOCKED_PATH_PATTERNS = [...]     # 804 verbatim
BLOCKED_COMMAND_PATTERNS = [...]  # 820 verbatim
EXA_MCP_URL = os.getenv("EXA_MCP_URL", "https://mcp.exa.ai/mcp")  # 1016 if keeping websearch helper

def check_security_guardrails(tool_name: str, tool_input: Any) -> Optional[str]: ... # 837 verbatim
def sanitize_payload(body: Dict[str,Any]) -> None: ...  # 905 verbatim (in-place, truncate)
# optional keep: def _mcp_sse_post ... + def _websearch_exa ... if <700 budget
__all__ = ["BLOCKED_PATH_PATTERNS","BLOCKED_COMMAND_PATTERNS","check_security_guardrails","sanitize_payload","_mcp_sse_post","_websearch_exa","EXA_MCP_URL"]
```

- Pertahankan `re.search(pattern, cmd, re.IGNORECASE)` verbatim, `cmd[:120]` truncation, `Optional[str]` return.
- `wc -l` target ~260 (<700). Jika `_mcp_sse_post`+`_websearch_exa` bikin >300, tetap <700 — keep (budget 440 spare).

### 4. TEST

```bash
python3 -m py_compile csmart/security/guardrails.py && echo "OK"

# smoke block command
python3 -c "from csmart.security.guardrails import check_security_guardrails; print(check_security_guardrails('bash', {'command': 'cat ~/.aws/credentials'}))"
# expect non-None contains "credential sensitif"

# smoke block path
python3 -c "from csmart.security.guardrails import check_security_guardrails; print(check_security_guardrails('read', {'path': '/Volumes/Xugab/LAB/PrivateLink/.env.local'}))"
# expect non-None

# smoke allow
python3 -c "from csmart.security.guardrails import check_security_guardrails; assert check_security_guardrails('read', {'path': '/tmp/foo.txt'}) is None; print('allow PASS')"

# smoke sanitize in-place truncate
python3 -c "from csmart.security.guardrails import sanitize_payload; b={'messages':[{'role':'user','content':'x'*5000}]}; sanitize_payload(b); print(len(b['messages'][0]['content']) < 5000, 'sanitize PASS')"

# no cycle
python3 -c "import pathlib; s=pathlib.Path('csmart/security/guardrails.py').read_text(); assert 'csmart.handlers' not in s and 'csmart.streaming.proxy_streamer' not in s and 'csmart.routing.model' not in s; print('no cycle PASS')"

# optional websearch helper if kept (stdlib only, no network in hermetic — just signature)
python3 -c "from csmart.security.guardrails import _mcp_sse_post, _websearch_exa; print('websearch helpers present')"

wc -l csmart/security/guardrails.py  # <700
```

- Barrier W2: `python3 -m py_compile csmart/security/guardrails.py csmart/streaming/proxy_streamer.py csmart/app/config.py csmart/routing/*.py` green → W3 handlers.

### 5. DONE

- [ ] `wc -l` <700 + `py_compile` OK + 5 smoke PASS (block command/path, allow, sanitize, no cycle)
- [ ] `git add csmart/security/guardrails.py`
- [ ] Update `checklist/progress.md`: `guardrails.py` 0→~260 ✅, W2-early T-B — unblock `handlers/messages` (W3 caller `check_security_guardrails`)

## Env

- `CSMART_SANITIZE_MAX_BYTES/LINES`, `CSMART_DLP_ALLOW`, `EXA_MCP_URL` — via `csmart.app.config` if green else `os.getenv` fallback (jangan hardcode).

## Acceptance

- [ ] `guardrails.py` ~260 <700, `py_compile` OK, guardrails smoke green (block/allow/sanitize), no cycle → W3 `handlers/messages` consumer ready
