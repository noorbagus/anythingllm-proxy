# Prompt Refactor — `csmart/routing/model.py` + `csmart/routing/token_limits.py` (W1 T1 remainder, PARALLEL after config)

> **Paste ke terminal T1-b (atau lanjut T1 `app/config` setelah config green).** Wajib **sequence after `csmart/app/config.py`** — butuh import `OPENAI_MODEL_MAP`, `_MODEL_TOKEN_LIMITS`, `MAX_TOKENS_*`, `OPENAI_*_PATTERNS`.
> Workdir: `/Volumes/Xugab/LAB/Tria/anythingllm-proxy` Branch: `refactor/modularize` | Gate: W1 barrier (bareng secrets/logging/streaming DONE)

---

## Role

**SDLC Routing Engineer — W1 T1**
Workdir: `/Volumes/Xugab/LAB/Tria/anythingllm-proxy` | Branch: `refactor/modularize`
Files: `csmart/routing/model.py:1` (~220 LOC, <700) + `csmart/routing/token_limits.py:1` (~90 LOC, <700)
Source: `csmart_proxy.py:276-336` + `csmart_proxy.py:1128-1250` + `csmart_proxy.py:947-985` | Depend: `csmart/app/config.py:1` (W1 T1, ~260) — pure layer atas config

---

## SDLC

### 1. REQ

**`routing/model.py`** — pure routing/alias, no DB, no streaming, no secrets:
- `def _model_matches_alias(model, alias):276` — verbatim (wildcard `*`, trailing `-`, `startswith`/`in`)
- `def clean_openai_model_name(model_name):287` — verbatim (strip prefix/alias cleaning)
- `def is_openai_model(model_name):297` — verbatim (cek `OPENAI_MODEL_MAP` alias + `OPENAI_MODEL_PATTERNS` + `OPENAI_RESPONSES_MODEL_PATTERNS`; tapi **delegasi** ke config patterns, jangan duplikat list)
- `def is_anthropic_native_model(model_name):` — verbatim (cek `ANTHROPIC_NATIVE_MODEL_PATTERNS`, precedence over `is_openai_model`)
- `def resolve_openai_endpoint(model):314` — verbatim Track A (loop `OPENAI_MODEL_MAP` → `endpoint_type` normalize `responses|messages|chat_completions`, fallback pattern checks + `"response" in cleaned`)
- `def detect_openai_endpoint_type(model_name):` — thin wrapper `resolve_openai_endpoint` (messages→chat_completions, else passthrough) — preserve call sites
- `def _openai_upstream_headers(request):336` — prefer `Authorization` header else `OPENAI_API_KEY` (jangan ubah `_upstream_headers` existing)
- `def route_model_tier(payload, session_key):1189` + `def align_prefix_3_region(payload):1128` — verbatim heuristic router + 3-region prefix aligner (graphify community 52 + 47 `handle_messages`)

**`routing/token_limits.py`** — clamp detail:
- `def _model_token_limits(model_name):947` → `(floor, ceil)` via `from csmart.app.config import _MODEL_TOKEN_LIMITS, MAX_TOKENS_FLOOR/CEIL` (jangan duplikat `_MODEL_TOKEN_LIMITS`)
- `def clamp_max_tokens(body):956` — verbatim (read `body["max_tokens"]`/`body["model"]`, resolve floor/ceil via `_model_token_limits`, action `lowered_to_ceil|raised_to_floor|kept`, `_log("TOKEN_CLAMP",…)` → inject logger via `from csmart.logging.structured import _log` atau optional no-log fallback)

**Out of scope:** `OPENAI_BASE_URL`, `OPENAI_MODEL_MAP`, `_MODEL_TOKEN_LIMITS` constants → `csmart/app/config.py` (import, jangan redefine). `sanitize_payload:905` → `csmart/handlers/messages.py` (W3).

### 2. DESIGN

```
csmart/app/config.py (constants, no csmart.* import)
        ▲
        │ from csmart.app.config import OPENAI_MODEL_MAP, OPENAI_*_PATTERNS, _MODEL_TOKEN_LIMITS, MAX_TOKENS_*, OPENAI_API_KEY
csmart/routing/model.py (pure, injectable)
csmart/routing/token_limits.py (from csmart.app.config import _MODEL_TOKEN_LIMITS... + from csmart.logging.structured import _log)
        ▲
csmart/transform/*, csmart/handlers/*, csmart/streaming/proxy_streamer
```

- `routing/model.py` **tidak** import `csmart.logging`/`csmart.security` (keep pure, testable hermetic). `token_limits.py` boleh import `_log` tapi wrap `try/except` agar clamp never raises even if log fails.
- `resolve_openai_endpoint` adalah single source of truth untuk W2 `transform/openai_to_anthropic` + W3 `handlers/openai` passthrough — jaga signature `resolve_openai_endpoint(model: str) -> str` verbatim.

### 3. IMPL

```bash
sed -n '276,360p' csmart_proxy.py   # _model_matches_alias, clean_openai_model_name, is_openai_model, resolve_openai_endpoint, _openai_upstream_headers, is_anthropic_native, detect_openai_endpoint_type
sed -n '1128,1290p' csmart_proxy.py # align_prefix_3_region + route_model_tier
sed -n '947,985p' csmart_proxy.py   # _model_token_limits + clamp_max_tokens
grep -n "ANTHROPIC_NATIVE_MODEL_PATTERNS\|OPENAI_MODEL_PATTERNS\|OPENAI_RESPONSES_MODEL_PATTERNS" csmart_proxy.py | head
```

Struktur `model.py`:
```python
"""csmart.routing.model — model matching, endpoint routing, tier/prefix."""
from __future__ import annotations
import os, re
from typing import Any, Dict
from csmart.app.config import (
    OPENAI_MODEL_MAP, OPENAI_BASE_URL, OPENAI_API_KEY,
    OPENAI_MODEL_PATTERNS, OPENAI_RESPONSES_MODEL_PATTERNS, ANTHROPIC_NATIVE_MODEL_PATTERNS,
)
# + local re patterns if any, then def _model_matches_alias … detect_openai_endpoint_type, align_prefix_3_region, route_model_tier
__all__ = ["_model_matches_alias","clean_openai_model_name","is_openai_model","is_anthropic_native_model","resolve_openai_endpoint","detect_openai_endpoint_type","_openai_upstream_headers","align_prefix_3_region","route_model_tier"]
```

Struktur `token_limits.py`:
```python
"""csmart.routing.token_limits — clamp_max_tokens + _model_token_limits."""
from __future__ import annotations
from typing import Any, Dict, Tuple
from csmart.app.config import _MODEL_TOKEN_LIMITS, MAX_TOKENS_FLOOR, MAX_TOKENS_CEIL
try: from csmart.logging.structured import _log
except ImportError: _log = lambda *a, **kw: None
def _model_token_limits(model_name: str) -> Tuple[int,int]: ...
def clamp_max_tokens(body: Dict[str,Any]) -> Dict[str,Any]: ...
__all__ = ["_model_token_limits","clamp_max_tokens"]
```

- Pertahankan env key/defaults via config import (jangan re-read `os.getenv`).
- `wc -l` target: `model.py` ~220, `token_limits.py` ~90 (<700 each).

### 4. TEST

```bash
# compile (butuh config green dulu)
python3 -m py_compile csmart/app/config.py && python3 -m py_compile csmart/routing/model.py csmart/routing/token_limits.py && echo "OK"

# smoke model
python3 -c "from csmart.routing.model import is_openai_model, is_anthropic_native_model, resolve_openai_endpoint, clean_openai_model_name; assert is_openai_model('glm-5.3-flash'); assert resolve_openai_endpoint('muse-spark-1.2-contributor')=='responses'; assert is_anthropic_native_model('minimax-m2'); print('routing/model PASS')"

# smoke tier/prefix
python3 -c "from csmart.routing.model import route_model_tier, align_prefix_3_region; print(route_model_tier({'model':'deepseek-chat','messages':[{'role':'user','content':'hi'}]}, 'sess')); print(align_prefix_3_region({'messages':[{'role':'user','content':'hello world'}]}))"

# smoke clamp
python3 -c "from csmart.routing.token_limits import clamp_max_tokens; print(clamp_max_tokens({'model':'deepseek-chat','max_tokens':100})); print(clamp_max_tokens({'model':'deepseek-chat','max_tokens':999999})); print(clamp_max_tokens({'model':'muse-spark','max_tokens':1}))"

# no cycle
python3 -c "import pathlib; src=pathlib.Path('csmart/routing/model.py').read_text(); assert 'csmart.security' not in src and 'csmart.streaming' not in src; print('model no cycle PASS')"
python3 -c "import pathlib; s=pathlib.Path('csmart/routing/token_limits.py').read_text(); assert 'csmart.handlers' not in s; print('token_limits no cycle PASS')"

wc -l csmart/routing/model.py csmart/routing/token_limits.py  # <700 each
```

- Barrier W1: `python3 -m py_compile csmart/app/config.py csmart/routing/*.py csmart/security/secrets.py csmart/logging/structured.py csmart/streaming/*.py` green.

### 5. DONE

- [ ] `wc -l` both <700 + `py_compile` OK + 5 smoke PASS
- [ ] `git add csmart/routing/model.py csmart/routing/token_limits.py`
- [ ] Update `checklist/progress.md`: routing pair 0→~310 LOC ✅, W1 T1 lane complete → unblock W2 `transform/*`

## Env

- `CSMART_OPENAI_MODEL_MAP` JSON, `CSMART_RESPONSES_PATTERNS` (`grok-,gpt-5.6,muse-`), `CSMART_ANTHROPIC_NATIVE_PATTERNS` — semua via `csmart.app.config` (jangan re-parse).

## Acceptance

- [ ] `routing/model.py` ~220 + `token_limits.py` ~90, <700, `py_compile` OK, no cycle, routing smoke green → W2 `transform` unblocked
