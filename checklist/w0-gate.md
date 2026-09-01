# W0 — Gate 0: Track-B Commit (SEQUENTIAL, blocker W1–W3)

> **Mode:** SEQUENTIAL — 1 terminal, wajib green sebelum Wave lain jalan.
> **Branch:** `feat/track-b-handlers` worktree `/Volumes/Xugab/LAB/Tria/track-b` (3361 LOC, `MM csmart_proxy.py` belum commit)
> **Issue #1 §B0–B4** — Spec Prompt 2, edit region 2650–2936

## REQ

- `csmart_proxy.py` track-b sudah inject: `transform_openai_chat_to_responses:1557`, `handle_openai_chat:3188`, `handle_openai_responses:3238`, `handle_models:3270`, `passthrough:3314` (routing `v1/` → `OPENAI_BASE_URL` vs `UPSTREAM_BASE_URL`, guard anti `//v1/v1`)
- Bug PYC stale: `passthrough` guard ada tapi `import csmart_proxy` kadang resolve ke `anythingllm-proxy/csmart_proxy.py` bukan `track-b/csmart_proxy.py` → verifikasi harus `workdir=track-b` + `importlib.util.spec_from_file_location`

## DESIGN

- `OPENAI_BASE_URL = https://opencode.ai/zen/go/v1` (sudah `/v1`), `UPSTREAM_BASE_URL = https://opencode.ai/zen/go` (tanpa `/v1`)
- Guard: `if target_base == OPENAI_BASE_URL and path.startswith("v1/")` → strip `v1/` atau `f"{OPENAI_BASE_URL}/{path[3:]}"`

## IMPL Checklist

- [ ] `rm -rf /Volumes/Xugab/LAB/Tria/track-b/__pycache__`
- [ ] Verifikasi source: `grep -n "target_base == OPENAI_BASE_URL" /Volumes/Xugab/LAB/Tria/track-b/csmart_proxy.py` ada
- [ ] `python3 -m py_compile /Volumes/Xugab/LAB/Tria/track-b/csmart_proxy.py` → OK
- [ ] Fresh import check:
  ```bash
  python3 -c "import sys, importlib.util; s=importlib.util.spec_from_file_location('csmart_proxy','/Volumes/Xugab/LAB/Tria/track-b/csmart_proxy.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.passthrough.__code__.co_firstlineno)"
  ```

## TEST — Gate (semua wajib PASS)

- [ ] **MockTransport hermetic** (tanpa network, di `track-b`):
  - `GET /v1/models` → `{object:"list", data:[{id, object:"model"}]}` gabungan `OPENAI_MODEL_MAP` + upstream
  - `POST /v1/chat/completions` `glm-5.3-flash` (`stream:false`) → forward `OPENAI_BASE_URL/chat/completions`
  - `POST /v1/chat/completions` `muse-spark-1.2-contributor` → `transform_openai_chat_to_responses` → body ada `"input"`
  - `POST /v1/responses` → passthrough `OPENAI_BASE_URL/responses`
  - `POST /v1/embeddings` model openai → `OPENAI_BASE_URL` (cek URL **tanpa** `//v1/v1/`), tanpa model → `UPSTREAM_BASE_URL`
  - Auth: `_openai_upstream_headers()` forward `Bearer` else `OPENAI_API_KEY`
- [ ] `pytest -m "not live"` atau minimal `pytest tests/test_csmart_proxy_openai.py -k models` → green
- [ ] Live (opsional sebelum commit):
  ```bash
  workdir=/Volumes/Xugab/LAB/Tria/track-b python3 csmart_proxy.py &
  curl -H "Authorization: Bearer dummy" http://127.0.0.1:8080/v1/models | jq
  ```

## DONE

- [ ] `git -C /Volumes/Xugab/LAB/Tria/track-b add csmart_proxy.py && git commit -m "feat(track-b): OpenAI HTTP handlers (chat/responses/models + passthrough routing)"`
- [ ] `git -C /Volumes/Xugab/LAB/Tria/track-b log --stat -1` verify
- [ ] Jika `main` bergerak: `git -C /Volumes/Xugab/LAB/Tria/track-b rebase origin/main`
- [ ] Sync gate artefak ke `main` jika perlu (factory/routing placeholders tetap 0 LOC sampai W1)

## Prompt Terminal W0

```
Role: SDLC Gatekeeper — W0 Track-B
Workdir: /Volumes/Xugab/LAB/Tria/track-b
Branch: feat/track-b-handlers
Files: csmart_proxy.py:1557,3188,3238,3270,3314

REQ: Selesaikan B1–B4 Issue #1 (double-/v1, PYC stale, MockTransport, pytest)
DESIGN: Guard passthrough anti //v1/v1, keep proxy_gen client.send(stream=True)
IMPL: rm __pycache__, verify guard, py_compile, importlib smoke
TEST: B2 hermetic 6 case + pytest + live curl
DONE: git add+commit feat(track-b) + rebase check. Jangan ubah transform internals di luar region 2650–2936.
```
