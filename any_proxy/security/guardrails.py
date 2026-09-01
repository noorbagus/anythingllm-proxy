"""csmart.security.guardrails — tool-call guardrails + payload sanitize (pure, never raises).

Extracted verbatim from csmart_proxy.py (804-945, 1016-1090). One-way deps:
config/structured/secrets. No cycle: guardrails does not import handlers/streaming/routing.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from any_proxy.app.config import ANSI_ESCAPE_REGEX, DLP_ALLOW, SANITIZE_TRUNCATE_BYTES, SANITIZE_TRUNCATE_LINES
except ImportError:  # pragma: no cover
    ANSI_ESCAPE_REGEX = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    SANITIZE_TRUNCATE_BYTES = int(os.getenv("CSMART_SANITIZE_MAX_BYTES", "2048"))
    SANITIZE_TRUNCATE_LINES = int(os.getenv("CSMART_SANITIZE_MAX_LINES", "40"))
    DLP_ALLOW = [w for w in os.getenv("CSMART_DLP_ALLOW", "").split(",") if w]

try:
    from any_proxy.logging.structured import _log
except ImportError:  # pragma: no cover
    def _log(event: str, **fields: Any) -> None:  # type: ignore[misc]
        return None

from any_proxy.security.secrets import vault  # noqa: E402  (one-way: secrets ↛ guardrails)

BLOCKED_PATH_PATTERNS = [
    r"\.env(?:\..+)?$",                        # .env, .env.local (akhir path)
    r"id_(?:rsa|ed25519|dsa|ecdsa)(?:\.pub)?",
    r"\.pem$",
    r"\.p12$",
    r"\.pfx$",
    r"\.key$",
    r"\.git/config$",
    r"credentials\.(?:json|csv)$",
    r"(?:service[_\-]account)[\w\-]*\.json$",
    r"client_secret[\w\-]*\.json$",
    r"\.kube/config$",
    r"[\\/]\.ssh[\\/]",
    r"[\\/]\.aws[\\/]",
    r"[\\/]\.config[\\/]gcloud[\\/]",
]
BLOCKED_COMMAND_PATTERNS = [
    r"^\s*(?:printenv|env|export\s+-p)\b",
    r"security\s+find-generic-password",
    r"aws\s+configure\s+(?:get|list)",
    r"gcloud\s+auth\s+",
    r"(?:cat|less|more|head|tail|sed|awk|grep|base64|strings)\s+.*(?:\.env|id_rsa|id_ed25519|\.pem|\.key|credentials)",
    r"(?:source|\.)\s+[~/]?[\w/]*\.env\b",
]
# Hanya mask pattern glob (bukan path literal) yang menunjuk file secret.
_BLOCKED_GLOB_MARKERS = (".env", "id_rsa", "id_ed25519", ".pem", ".key", "credentials")


def _canonicalize_path(p: str) -> str:
    """Expand ~ and resolve symlinks/.. so pattern checks cannot be bypassed."""
    return os.path.realpath(os.path.expanduser(p))


def check_security_guardrails(tool_name: str, tool_input: Any) -> Optional[str]:
    """Return a violation message if *tool_input* touches credentials, else None."""
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool_name in ("bash", "execute_command", "command", "run_command"):
        cmd = str(tool_input.get("command") or tool_input.get("cmd") or "")
        for pattern in BLOCKED_COMMAND_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return f"command memuat akses credential sensitif (diblokir): {cmd[:120]}"
    candidates: List[str] = []
    for key in ("path", "file_path", "filepath", "cwd", "root", "subpath"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            candidates.append(val)
    for key in ("view", "edit", "read", "glob"):
        sub = tool_input.get(key)
        if not isinstance(sub, dict):
            continue
        for k2 in ("file_path", "path", "pattern"):
            val = sub.get(k2)
            if not isinstance(val, str) or not val:
                continue
            if k2 == "pattern" and not any(m in val for m in _BLOCKED_GLOB_MARKERS):
                continue  # glob umum (mis. "**/*.py") bukan file secret
            candidates.append(val)
    for path in candidates:
        canon = _canonicalize_path(path)
        for pattern in BLOCKED_PATH_PATTERNS:
            if re.search(pattern, canon, re.IGNORECASE):
                return f"akses file '{path}' dicegat (kandungan credential sensitif)"
    return None


def sanitize_raw_logs(text: str) -> str:
    """Strip ANSI escapes and head-tail truncate logs > 2KB."""
    if not isinstance(text, str) or not text:
        return text
    text = ANSI_ESCAPE_REGEX.sub("", text)
    if len(text.encode("utf-8")) > SANITIZE_TRUNCATE_BYTES:
        lines = text.splitlines()
        if len(lines) > SANITIZE_TRUNCATE_LINES:
            head = "\n".join(lines[:20])
            tail = "\n".join(lines[-20:])
            snipped = len(lines) - (SANITIZE_TRUNCATE_LINES)
            text = f"{head}\n\n... [CSMART SNIPPED {snipped} LINES] ...\n\n{tail}"
    return text


def _mask_dict(value: Dict[str, Any]) -> Dict[str, Any]:
    """Mask every string leaf of a (small) nested dict — e.g. tool_use.input."""
    return {
        k: (_mask_dict(v) if isinstance(v, dict) else (vault.mask_text(v) if isinstance(v, str) else v))
        for k, v in value.items()
    }


def _mask_text_block(value: Any) -> Any:
    """Sanitize + mask a text block (dict with 'text' / string)."""
    if isinstance(value, dict):
        out = dict(value)
        if isinstance(out.get("text"), str):
            out["text"] = vault.mask_text(sanitize_raw_logs(out["text"]))
        return out
    if isinstance(value, str):
        return vault.mask_text(sanitize_raw_logs(value))
    return value


def sanitize_payload(body: Dict[str, Any]) -> None:
    """In-place: sanitize + mask system and message content, block tool_use.input."""

    def _walk_content(content: Any) -> Any:
        if isinstance(content, str):
            return vault.mask_text(sanitize_raw_logs(content))
        if isinstance(content, list):
            out: List[Any] = []
            for block in content:
                if not isinstance(block, dict):
                    out.append(block)
                    continue
                b = dict(block)
                btype = b.get("type")
                if btype in ("text", "input_text", "output_text"):
                    b = _mask_text_block(b)
                elif btype == "tool_result":
                    c = b.get("content")
                    if isinstance(c, str):
                        b["content"] = vault.mask_text(sanitize_raw_logs(c))
                    elif isinstance(c, list):
                        b["content"] = [_mask_text_block(x) for x in c]
                elif btype == "tool_use":
                    inp = b.get("input")
                    if isinstance(inp, dict):
                        b["input"] = _mask_dict(inp)
                out.append(b)
            return out
        return content

    sysval = body.get("system")
    if isinstance(sysval, str):
        body["system"] = vault.mask_text(sanitize_raw_logs(sysval))
    elif isinstance(sysval, list):
        body["system"] = [_mask_text_block(s) for s in sysval]
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict):
                msg["content"] = _walk_content(msg.get("content"))


EXA_MCP_URL = os.getenv("EXA_MCP_URL", "https://mcp.exa.ai/mcp")


def _mcp_sse_post(url: str, payload: Dict[str, Any], timeout: int = 25) -> Dict[str, Any]:
    """POST JSON-RPC ke MCP server (SSE transport), return parsed result dict.

    Response berbentuk SSE: `event: message\\ndata: {jsonrpc result}`. Baris
    `data: ` berisi payload JSON-RPC lengkap (result/error).
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2024-11-05",
            # Exa MCP memblok UA default urllib (Python-urllib/3.x) -> 403.
            "User-Agent": "Mozilla/5.0 (compatible; csmart-proxy/1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise ValueError("MCP SSE response tidak mengandung event 'data'")


def _websearch_exa(query: str, max_results: int = 5) -> str:
    """Sync web search via Exa hosted MCP (backend yang sama dipakai opencode).

    Stdlib only — tanpa dependency baru & tanpa API key. Exa MCP mengembalikan
    konten bersih per hasil (Title/URL/Published/Highlights) siap di-feed balik
    sebagai tool_result ke model.
    """
    try:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {"query": query, "numResults": max_results},
            },
        }
        data = _mcp_sse_post(EXA_MCP_URL, payload)
        result = data.get("result", {})
        content = result.get("content", []) or []
        blocks: List[str] = [
            item["text"].strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ]
        if not blocks:
            return f"[csmart_websearch] Tidak ada hasil untuk query {query!r}."
        joined = "\n\n".join(blocks)
        # Exa free-tier kadang balas pesan rate-limit/error alih-alih hasil ->
        # surface sebagai ERROR, bukan "hasil", biar model tahu backend kena limit.
        if "rate limit" in joined.lower() or "exaApiKey" in joined.lower():
            return (
                f"[csmart_websearch] ERROR: Exa MCP rate-limited. "
                f"Set env EXA_MCP_URL dengan Exa API key (mcp.exa.ai/mcp?exaApiKey=...) "
                f"untuk query {query!r}."
            )
        return (
            f"[csmart_websearch] {len(blocks)} hasil (Exa MCP) untuk {query!r}:\n"
            + joined
        )
    except Exception as exc:
        return f"[csmart_websearch] ERROR: {exc}"


__all__ = [
    "BLOCKED_PATH_PATTERNS",
    "BLOCKED_COMMAND_PATTERNS",
    "check_security_guardrails",
    "sanitize_raw_logs",
    "sanitize_payload",
    "EXA_MCP_URL",
    "_mcp_sse_post",
    "_websearch_exa",
]
