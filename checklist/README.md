# Checklist Refactor `csmart_proxy.py` — 3676 LOC → 14 Modul (<700/file)

> **Goal:** Pecah God File `csmart_proxy.py:1` (3676 LOC, 127 nodes, degree 66, 8 community — graphify `e6b8aadd` vs HEAD `9a30983`) menjadi `csmart/` 14 modul, setiap file <700 LOC, dengan barrier SDLC tiap Wave.
> Issue: #1 `chore(refactor): modularize … — Track B 90%`
> Branch: `refactor/modularize` (+ worktree `../track-b` `feat/track-b-handlers` — sync barrier tiap Wave)
> Workdir: `/Volumes/Xugab/LAB/Tria/anythingllm-proxy` (main) + `/Volumes/Xugab/LAB/Tria/track-b` (T2/T3 source-of-truth, belum fully sync)

## Struktur Target (14 modul + 8 `__init__.py` + shim)

```
csmart/
  app/factory.py        (~280)  # create_app, lifespan, keepalive, _banner
  app/config.py         (~260)  # _load_gateway_env, _load_openai_model_map, clamp_max_tokens env
  routing/model.py      (~220)  # clean_openai_model_name, is_openai_model, resolve_openai_endpoint, route_model_tier, align_prefix_3_region
  routing/token_limits.py (~90) # _model_token_limits, clamp_max_tokens detail
  security/secrets.py   (445)   # _Rule, SecretVault, _redact, _shannon_entropy — DONE
  security/guardrails.py(~260)  # check_security_guardrails — W2
  logging/structured.py (225)   # _log, _redact, get_db, init_db, StructuredLogger — DONE
  transform/anthropic_to_openai.py (~500) # transform_anthropic_to_openai_chat/responses — W2
  transform/openai_to_anthropic.py (~450) # transform_openai_sse_to_anthropic + responses→chat JSON/SSE — W2 (reuse Issue Track-C)
  streaming/sse.py      (211)   # _parse_sse_data, _iter_sse_events, _sse_source, _format_event — DONE
  streaming/redactor.py (184)   # StreamingRedactor — DONE
  streaming/proxy_streamer.py (~480) # ProxyStreamer family — W2 (butuh sse+redactor+secrets+logging)
  handlers/messages.py  (~480)  # handle_messages slim orchestrator — W3 sequential
  handlers/openai.py    (~360+210) # handle_openai_chat/responses/models + passthrough (+ double-/v1 fix) — W3
csmart_proxy.py shim    (~30)   # from csmart.app.factory import app — W3 last
```

## Wave & Gate (SDLC — 4 terminal paralel ideal)

| Wave | Mode | Terminal | Modul | Dependensi | Gate |
|------|------|----------|-------|------------|------|
| **W0** | SEQUENTIAL | 1 | Track-B commit (`passthrough` double-`/v1` fix, 3361 LOC) | — | `py_compile` + MockTransport + `pytest -m "not live"` |
| **W1** | PARALLEL (4 lane) | T1 Foundation · T2 Security · T3 Streaming-Leaf · T4 Logging | `app/config`+`routing/*` · `security/secrets` · `streaming/sse+redactor` · `logging/structured` | — (tanpa cross-dep) | `py_compile` semua file W1 + import smoke |
| **W2** | PARALLEL (2 lane) | T-Transform · T-Stream/Security | `transform/*` · `security/guardrails`+`streaming/proxy_streamer` | W1 gate | `pytest -m "not live"` + MockTransport |
| **W3** | SEQUENTIAL | Orchestrator | `handlers/messages+openai` + `app/factory` + shim | W2 gate | live `curl` + `graphify update .` + `git log --stat` |

File detail per Wave: [`w0-gate.md`](w0-gate.md) · [`w1-foundation.md`](w1-foundation.md) · [`w2-integration.md`](w2-integration.md) · [`w3-final.md`](w3-final.md)
Progress live: [`progress.md`](progress.md) · Definition of Done: [`dod.md`](dod.md)
