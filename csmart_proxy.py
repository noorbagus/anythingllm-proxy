#!/usr/bin/env python3
"""csmart v3 — standalone local optimizer proxy (single file).

Pipeline: sanitizer -> DLP/secret-vault -> guardrails -> reversible CCR ->
3-region prefix aligner -> heuristic model router -> SSE streaming -> keepalive.

Prioritas: sanitasi key/credential agar TIDAK bocor ke upstream, ke disk, maupun
ke log. Standalone — tidak mengimpor ``router/``; pola ditiru dari v2.1.0
(``shadow_loop``, ``sse_stream``, ``safe_path``, ``logger``).

Cara pakai:
    export UPSTREAM_BASE_URL="https://api.deepseek.com/anthropic"
    export UPSTREAM_API_KEY="sk-..."        # atau ANTHROPIC_AUTH_TOKEN dari PrivateLink .env
    python3.14 csmart_proxy.py
    export ANTHROPIC_BASE_URL="http://127.0.0.1:8080"; export ANTHROPIC_API_KEY="dummy"
    claude
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.request
import uuid
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:
    from cryptography.fernet import Fernet as _Fernet
except ImportError:  # pragma: no cover
    _Fernet = None


# =====================================================================
# ENV LOADING (mirror router/cli_dispatch.py:36-37,130-131)
# =====================================================================
def _load_gateway_env() -> None:
    """Load the PrivateLink gateway env files so ANTHROPIC_AUTH_TOKEN is found
    even when only the proxy script is started (no prior env export)."""
    for path in (
        "/Volumes/Xugab/LAB/PrivateLink/credentials/.env",
        "/Volumes/Xugab/LAB/PrivateLink/.env.local",
    ):
        if load_dotenv is not None and os.path.exists(path):
            load_dotenv(path, override=False)


_load_gateway_env()

# =====================================================================
# CONFIGURATION & CONSTANTS
# =====================================================================
UPSTREAM_BASE_URL = (
    os.getenv("ANTHROPIC_UPSTREAM_URL")
    or os.getenv("UPSTREAM_BASE_URL")
    or "https://api.deepseek.com/anthropic"
)
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN", "")
# OpenAI API key is separate (for OpenAI-native endpoints)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PROXY_HOST = os.getenv("CSMART_HOST", "127.0.0.1")
PROXY_PORT = int(os.getenv("CSMART_PORT", "8080"))
DB_PATH = os.getenv("CSMART_DB", "csmart_state.db")

FLASH_MODEL = os.getenv("CSMART_FLASH_MODEL", "deepseek-chat")
FLAGSHIP_MODEL = os.getenv("CSMART_FLAGSHIP_MODEL", "deepseek-reasoner")
UPSTREAM_TIMEOUT = float(os.getenv("CSMART_UPSTREAM_TIMEOUT", "120"))
MAX_TOKENS_FLOOR = int(os.getenv("CSMART_MIN_MAX_TOKENS", "4096"))
MAX_TOKENS_CEIL = int(os.getenv("CSMART_MAX_MAX_TOKENS", "16384"))
# Batas floor/cap per-model (match by substring, urut dari paling spesifik).
# Cap = batas output model agar tidak terpotong; floor = minimal agar reasoning
# tidak memakan seluruh budget. Nilai env override untuk tiap model bila diisi.
_MODEL_TOKEN_LIMITS: List[Dict[str, Any]] = [
    {
        "keys": ["deepseek-v4", "deepseek-v4-flash"],
        "floor": int(os.getenv("CSMART_MAX_TOKENS_FLOOR_DEEPSEEK", "8192")),
        "ceil": int(os.getenv("CSMART_MAX_TOKENS_CEIL_DEEPSEEK", "32768")),
    },
    {
        "keys": ["muse-spark", "muse-"],
        "floor": int(os.getenv("CSMART_MAX_TOKENS_FLOOR_MUSE", "8192")),
        "ceil": int(os.getenv("CSMART_MAX_TOKENS_CEIL_MUSE", "131072")),
    },
]
MAX_ROUNDS = int(os.getenv("CSMART_MAX_SHADOW_ROUNDS", "5"))

# Sanitizer (noise reduction)
ANSI_ESCAPE_REGEX = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SANITIZE_TRUNCATE_BYTES = int(os.getenv("CSMART_SANITIZE_MAX_BYTES", "2048"))
SANITIZE_TRUNCATE_LINES = int(os.getenv("CSMART_SANITIZE_MAX_LINES", "40"))

# Reversible CCR
CCR_MIN_BYTES = int(os.getenv("CSMART_CCR_MIN_BYTES", "3072"))
CCR_PREVIEW_LINES = int(os.getenv("CSMART_CCR_PREVIEW_LINES", "10"))

# DLP
DLP_ALLOW = [w for w in os.getenv("CSMART_DLP_ALLOW", "").split(",") if w]

# Mock mode — skip upstream call, return canned Anthropic response. Diagnostic
# only: helps isolate "is it the upstream format that's rejected" vs "is it the
# proxy transform that's broken" vs "is it Claude Code's renderer".
MOCK_MODE = os.getenv("CSMART_MOCK_RESPONSES", "0") == "1"

# Secret vault at-rest
VAULT_PERSIST = os.getenv("CSMART_VAULT_PERSIST", "0") == "1"
VAULT_KEY = os.getenv("CSMART_VAULT_KEY", "")

# Mask style: "hash" (default, zero-info) -> placeholder __CSMART_SEC_<hash>__,
# tidak ada byte secret pun ikut ke upstream/log. "preserve" (opsional) ->
# prefix+suffix (mis. sk-ant...90ab); CAVEAT: 10 char ikut ke upstream, hanya
# dipakai kalau log tracking prefix-suffix memang dibutuhkan.
MASK_STYLE = os.getenv("CSMART_MASK_STYLE", "hash").strip().lower()

# Keepalive (jaga KV-cache TTL provider, biasanya 5 menit)
KEEPALIVE_TICK = int(os.getenv("CSMART_KEEPALIVE_TICK", "30"))
KEEPALIVE_WINDOW_START = int(os.getenv("CSMART_KEEPALIVE_WINDOW_START", "270"))
KEEPALIVE_WINDOW_END = int(os.getenv("CSMART_KEEPALIVE_WINDOW_END", "300"))

# Heuristic router triggers (flagship tier)
_COMPLEX_TRIGGERS = [
    t.strip()
    for t in os.getenv(
        "CSMART_ROUTE_FLAGSHIP_KEYWORDS",
        "architecture,refactor whole system,security audit,database migration,redesign,multi-file refactor",
    ).split(",")
    if t.strip()
]

# OpenAI-native model detection and endpoints
OPENAI_MODEL_PATTERNS = [
    t.strip()
    for t in os.getenv(
        "CSMART_OPENAI_PATTERNS",
        "gpt-,o1-,o3-,muse-,text-,davinci-,curie-,deepseek-,"
        "glm-,kimi-,longcat-,mimo-,hy3,hy4-,grok-",
    ).split(",")
    if t.strip()
]
OPENAI_BASE_URL = os.getenv(
    "CSMART_OPENAI_BASE_URL",
    os.getenv("OPENAI_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
)
OPENAI_CHAT_COMPLETIONS_PATH = os.getenv(
    "CSMART_OPENAI_CHAT_PATH", "/chat/completions"
)
OPENAI_RESPONSES_PATH = os.getenv(
    "CSMART_OPENAI_RESPONSES_PATH", "/responses"
)
# Model families served by the OpenAI Responses endpoint (/responses) per the
# OpenCode Go model table. Everything else OpenAI (glm-,kimi-,longcat-,
# deepseek-,mimo-,hy3-,hy4-,o1-,o3-,text-...) defaults to chat completions.
# ⚠️ JANGAN tambahkan "opencode-" di sini (atau di OPENAI_MODEL_PATTERNS): itu
# org prefix "opencode-go/<id>", bukan model family — semua model OpenCode Go
# bakal ke-hijack ke /responses (lihat kasus opencode-go/hy3 → 502).
OPENAI_RESPONSES_MODEL_PATTERNS = [
    t.strip()
    for t in os.getenv(
        "CSMART_RESPONSES_PATTERNS",
        "grok-,gpt-5.6,muse-",
    ).split(",")
    if t.strip()
]
# Anthropic-compatible /messages endpoint on the same OpenCode Go base
# ({OPENAI_BASE_URL}/messages, @ai-sdk/anthropic).
OPENAI_MESSAGES_PATH = os.getenv("CSMART_OPENAI_MESSAGES_PATH", "/messages")

# Model families served by that Anthropic-compatible /messages endpoint
# (minimax-m*, qwen3.*). These speak Anthropic-native protocol — NO OpenAI
# protocol transform, client model name preserved verbatim, raw SSE passthrough.
ANTHROPIC_NATIVE_MODEL_PATTERNS = [
    t.strip()
    for t in os.getenv(
        "CSMART_ANTHROPIC_NATIVE_PATTERNS",
        "minimax-,qwen3",
    ).split(",")
    if t.strip()
]

# Model-id aliases applied on the OpenAI path only. OpenCode Go doesn't serve
# DeepSeek's real API ids (deepseek-chat / deepseek-reasoner — those belong to
# the DeepSeek upstream passthrough), so map them to OpenCode Go's v4 ids so the
# documented FLASH/FLAGSHIP defaults keep working when routed to OpenCode Go.
OPENAI_MODEL_ALIASES: Dict[str, str] = {
    os.getenv("CSMART_ALIAS_DEEPSEEK_CHAT", "deepseek-chat"): os.getenv(
        "CSMART_ALIAS_DEEPSEEK_CHAT_TO", "deepseek-v4-flash"
    ),
    os.getenv("CSMART_ALIAS_DEEPSEEK_REASONER", "deepseek-reasoner"): os.getenv(
        "CSMART_ALIAS_DEEPSEEK_REASONER_TO", "deepseek-v4-pro"
    ),
}

# System Prompt Steering for OpenAI-native models
# Instructs model to follow Claude Code tool use format exactly
SYSTEM_STEERING_PROMPT = """You are a helpful coding assistant in a terminal chat. Follow EXACTLY the Claude Code tool use format.

TOP PRIORITY — RESPOND TO WHAT WAS ACTUALLY ASKED:
- If the user only greets you (halo, hi, hai, hello) or makes small talk, reply naturally in 1-2 short sentences. DO NOT analyze, DO NOT run commands, DO NOT create files, DO NOT propose plans.
- NEVER invent or fabricate a task, document, plan, or file that the user did not ask for.
- NEVER create files, folders, or run shell commands unless the user explicitly asks you to.
- NEVER guess or assume the working directory; use the real current directory. Do not fabricate paths.
- NEVER explore, list, or read files/directories unless the user explicitly asks. In particular, do NOT read graphify output (e.g. graphify-out/graph.json, graphify-out/*.md, graphify-merged/*), do NOT run graphify, and do NOT run `ls`, `find`, or `cat` just to "understand the codebase" — unless the user directly requests it.
- Read the user's last message literally and do exactly that, nothing more.
- Do NOT add unsolicited notes, caveats, or guesses about content the user did not send (e.g. do not claim the user attached an image, diagram, or task that is not in the message). If the message is only a greeting, reply with ONLY the greeting and nothing else.

TOOL USE FORMAT (when you ARE asked to take action):
- You MUST format tool calls as a JSON array in the tool_use block.
- You MUST NOT add extra preamble, explanations, or thinking outside the content block.
- You MUST use the provided tool definitions when the user asks to take action.
- You MUST follow the input_schema exactly when generating tool_use calls.
- You MUST NEVER imitate terminal UI or status indicators: no "✻", "Crunched for Ns", "done <time>", "…", spinners, checkmarks, or fake progress text.
- Always respond directly to the user's message in natural language.
"""

# Logging — default ke ~/.csmart/logs supaya observability fail-safe (konsisten
# dengan router/logger.py). CSMART_LOG_DIR tetap bisa override. Nilai tetap di-redact.
LOG_DIR = os.getenv("CSMART_LOG_DIR", str(Path.home() / ".csmart" / "logs"))
VERBOSE = os.getenv("CSMART_VERBOSE", "0") == "1"

# =====================================================================
# REDACTED LOGGING — key tidak pernah masuk log
# =====================================================================
_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "x-api-key",
    "token",
    "password",
    "secret",
    "real_secret",
    "client_secret",
    "access_key",
    "private_key",
}


def _redact(value: Any) -> Any:
    """Blank sensitive values by key name (never prints credentials)."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _log(event: str, **fields: Any) -> None:
    """Emit one redacted JSONL event. Never raises; never logs secrets."""
    try:
        rec = _redact({"event": event, "ts": datetime.now(timezone.utc).isoformat(), **fields})
        if LOG_DIR:
            Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
            path = Path(LOG_DIR) / f"session_{datetime.now().strftime('%Y%m%d')}.jsonl"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        elif VERBOSE:
            sys.stderr.write(json.dumps(rec) + "\n")
    except Exception:  # logging must never break the proxy
        pass


def _banner() -> None:
    sys.stderr.write(
        f"[csmart] proxy http://{PROXY_HOST}:{PROXY_PORT} -> {UPSTREAM_BASE_URL} "
        f"(flash={FLASH_MODEL}, flagship={FLAGSHIP_MODEL})\n"
    )


# =====================================================================
# DATABASE INITIALIZATION
# =====================================================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS context_blobs (
                ref_id TEXT PRIMARY KEY,
                payload_type TEXT,
                raw_content TEXT,
                token_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS secret_vault (
                mask_id TEXT PRIMARY KEY,
                real_secret TEXT,          -- NULL kecuali CSMART_VAULT_PERSIST=1 (terenkripsi)
                pattern_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()


# =====================================================================
# 1. DLP & BIDIRECTIONAL SECRET VAULT
# =====================================================================
# Gitleaks-inspired high-precision patterns: (regex, label). Group 1, bila ada,
# adalah nilai secret yang akan di-mask (bukan pembungkus assignment).
SECRET_REGEXES: List[Tuple[str, str]] = [
    (r"(?i)\bsk-ant-[A-Za-z0-9_-]{20,}", "anthropic_key"),
    (r"(?i)\bsk_live_[A-Za-z0-9]{16,}", "stripe_live"),
    (r"(?i)\bsk_test_[A-Za-z0-9]{16,}", "stripe_test"),
    (r"(?i)\bsk-[A-Za-z0-9_-]{20,}", "openai_key"),
    (r"(?i)\bghp_[A-Za-z0-9]{36}", "github_token"),
    (r"(?i)\bgithub_pat_[A-Za-z0-9_]{20,}", "github_pat"),
    (r"(?i)\bglpat-[A-Za-z0-9_-]{20,}", "gitlab_token"),
    (r"(?i)\bxox[baprs]-[A-Za-z0-9-]{10,}", "slack_token"),
    (r"(?i)\bAIza[0-9A-Za-z_-]{20,}", "gcp_api_key"),
    (r"(?i)\bya29\.[0-9A-Za-z_-]+", "google_oauth"),
    (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "aws_access_key"),
    (r"\bey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}", "jwt_token"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private_key"),
    (r"(?i)\Brpk_[A-Za-z0-9]{16,}", "rpk"),
    (r"(?i)\bnvapi-[A-Za-z0-9_-]{20,}", "nvidia_token"),
    (r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", "bearer_token"),
    (
        r"(?i)\b(?:password|passwd|secret|token|api_key|apikey|access_key|client_secret|private_key)\s*[:=]\s*[\"']?([^\"'\s\n,;]+)",
        "generic_secret",
    ),
]
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX_RE = re.compile(r"[0-9a-fA-F]{28,}")


# =====================================================================
# 1b. GITLEAKS RULESET (vendored config/gitleaks.toml, ~200 rule).
# Regex+keyword prefilter diadopsi AGGRESIF (tanpa entropy gate, sesuai
# filosofi csmart): apapun format yang match langsung di-mask. Kebalikan
# gitleaks binary (entropy-gated) yang justru loloskan key low-entropy.
# =====================================================================

@dataclass
class _Rule:
    """Satu deteksi secret: regex + prefilter/precision gates."""
    regex: Any  # re.Pattern
    ptype: str
    keywords: Tuple[str, ...] = ()
    allow_regexes: Tuple[Any, ...] = ()  # per-rule allowlist (gitleaks [[rules.allowlists]])
    stopwords: Tuple[str, ...] = ()
    secret_group: int = 0  # 0 = pakai group 1 (atau full match)


def _compile_allow_regexes(allowlists: Optional[List[Any]]) -> Tuple[Any, ...]:
    pats: List[Any] = []
    for al in allowlists or []:
        for rx in al.get("regexes") or []:
            try:
                pats.append(re.compile(rx))
            except Exception:
                continue
    return tuple(pats)


def _rule_stopwords(allowlists: Optional[List[Any]]) -> Tuple[str, ...]:
    words: List[str] = []
    for al in allowlists or []:
        for w in al.get("stopwords") or []:
            if isinstance(w, str) and w:
                words.append(w.lower())
    return tuple(words)


def load_gitleaks_rules(path: str) -> Tuple[_Rule, ...]:
    """Load ~200 gitleaks patterns dari config/gitleaks.toml.

    Soft-fail: file hilang / tomli tidak ada / regex tak tercompile -> di-skip,
    proxy tetap jalan dengan SECRET_REGEXES bawaan.
    """
    rules, _allowed = _load_gitleaks_config(path)
    return rules


def _load_gitleaks_config(path: str) -> Tuple[Tuple[_Rule, ...], Tuple[Any, ...]]:
    """Return (rules setara _Rule, global allowlist regexes)."""
    if tomllib is None or not os.path.exists(path):
        return (), ()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        _log("GITLEAKS_RULES", error=str(exc), path=path)
        return (), ()
    rules: List[_Rule] = []
    skipped = 0
    for r in data.get("rules") or []:
        rx = r.get("regex", "")
        if not rx:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)  # nested-set regex (upstream gitleaks), masih valid
                pat = re.compile(rx)
        except Exception:
            skipped += 1
            continue
        rid = r.get("id") or r.get("description") or "unknown"
        rules.append(
            _Rule(
                regex=pat,
                ptype=f"gitleaks:{rid}",
                keywords=tuple(k.lower() for k in (r.get("keywords") or []) if isinstance(k, str)),
                allow_regexes=_compile_allow_regexes(r.get("allowlists")),
                stopwords=_rule_stopwords(r.get("allowlists")),
                secret_group=int(r.get("secretGroup") or 0),
            )
        )
    if rules:
        _log("GITLEAKS_RULES", loaded=len(rules), skipped=skipped, path=os.path.basename(path))
    global_allow = _compile_allow_regexes([data.get("allowlist") or {}]) if isinstance(data.get("allowlist"), dict) else ()
    return tuple(rules), global_allow


GITLEAKS_TOML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "gitleaks.toml")
_GITLEAKS_RULES, GLOBAL_ALLOW_PATTERNS = _load_gitleaks_config(GITLEAKS_TOML)
GITLEAKS_PATTERNS: Tuple[_Rule, ...] = _GITLEAKS_RULES

_ALL_PATTERNS: Tuple[_Rule, ...] = tuple(
    _Rule(regex=re.compile(rx, re.IGNORECASE if "(?i)" in rx else 0), ptype=pt, keywords=())
    for rx, pt in SECRET_REGEXES
) + GITLEAKS_PATTERNS


def _secret_value(match: "re.Match[str]", rule: _Rule) -> Optional[str]:
    """Ambil nilai secret dari match (hormati secretGroup, lalu group 1, lalu full)."""
    g = match.group(rule.secret_group) if rule.secret_group and match.re.groups >= rule.secret_group else None
    if isinstance(g, str) and g:
        return g
    groups = match.groups()
    if groups and groups[0]:
        return groups[0]
    return match.group(0)


def _shannon_entropy(s: str) -> float:
    """Shannon entropy in bits per character (base 2)."""
    if not s:
        return 0.0
    length = len(s)
    counts: Dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = 0.0
    for cnt in counts.values():
        p = cnt / length
        entropy -= p * math.log2(p)
    return entropy


def _b64url_key(key: str) -> bytes:
    """Derive a Fernet-compatible URL-safe b64 key (32 bytes) from an env key."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecretVault:
    """Two-tier masking + bidirectional restore.

    At-rest (default): real secrets live ONLY in process memory — tabel
    ``secret_vault.real_secret`` tetap NULL. Persist terenkripsi (Fernet)
    opsional via ``CSMART_VAULT_PERSIST=1`` + ``CSMART_VAULT_KEY``.
    """

    def __init__(self) -> None:
        self.mem_cache: Dict[str, str] = {}   # mask_id -> real_secret
        self.reverse_cache: Dict[str, str] = {}  # real_secret -> mask_id
        self.display_map: Dict[str, str] = {}  # display (prefix...suffix) -> real_secret
        self._fernet: Any = None
        if VAULT_PERSIST:
            if _Fernet is None or not VAULT_KEY:
                _log(
                    "VAULT_CONFIG",
                    error="CSMART_VAULT_PERSIST=1 but cryptography/CSMART_VAULT_KEY missing; falling back to in-memory",
                )
            else:
                self._fernet = _Fernet(_b64url_key(VAULT_KEY))
        self._load_persisted()

    def _load_persisted(self) -> None:
        if self._fernet is None:
            return
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT mask_id, real_secret, pattern_type FROM secret_vault WHERE real_secret IS NOT NULL"
                ).fetchall()
            for row in rows:
                mask_id = row["mask_id"]
                secret = self._fernet.decrypt(row["real_secret"].encode("utf-8")).decode("utf-8")
                self.mem_cache[mask_id] = secret
                self.reverse_cache[secret] = mask_id
                display = self._display(secret)
                if display is None or display == mask_id:
                    continue
                existing = self.display_map.get(display)
                if existing is None or existing == secret:
                    self.display_map[display] = secret
        except Exception as exc:  # stale key / corrupt row -> ignore
            _log("VAULT_LOAD", error=str(exc))

    def _display(self, secret: str) -> Optional[str]:
        """Prefix+suffix mask utk log tracking (prefix 6 + '...' + suffix 4).

        None saat secret terlalu pendek (<=10) supaya mask tidak sama dengan
        aslinya -> fallback ke hash placeholder.
        """
        if MASK_STYLE != "preserve" or len(secret) < 11:
            return None
        return f"{secret[:6]}...{secret[-4:]}"

    def get_or_create_mask(self, secret: str, pattern_type: str) -> str:
        existing = self.reverse_cache.get(secret)
        if existing:
            return self._display(d) if MASK_STYLE == "preserve" and (d := self.mem_cache[existing]) else existing
        hash_id = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]
        mask_id = f"__CSMART_SEC_{hash_id}__"
        display = self._display(secret) or mask_id
        self.mem_cache[mask_id] = secret
        self.reverse_cache[secret] = mask_id
        if display != mask_id:
            existing_disp = self.display_map.get(display)
            if existing_disp is None or existing_disp == secret:
                self.display_map[display] = secret
            else:  # collision prefix+suffix antar secret -> pakai hash biar unik
                display = mask_id
        try:
            with get_db() as conn:
                if self._fernet is not None:
                    enc = self._fernet.encrypt(secret.encode("utf-8")).decode("utf-8")
                    conn.execute(
                        "INSERT OR REPLACE INTO secret_vault (mask_id, real_secret, pattern_type) VALUES (?, ?, ?)",
                        (mask_id, enc, pattern_type),
                    )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO secret_vault (mask_id, real_secret, pattern_type) VALUES (?, NULL, ?)",
                        (mask_id, pattern_type),
                    )
                conn.commit()
        except Exception as exc:
            _log("VAULT_PUT", error=str(exc))
        _log("SECRET_MASKED", mask=display, pattern=pattern_type, len=len(secret))
        return display

    def mask_text(self, text: str) -> str:
        """Two-tier masking: high-precision regex, then selective entropy pass."""
        if not text:
            return text
        # Tier 1: known secret formats (builtin + gitleaks pattern set).
        text_lower = text.lower()
        for rule in _ALL_PATTERNS:
            if rule.keywords and not any(k in text_lower for k in rule.keywords):
                continue
            for match in rule.regex.finditer(text):
                val = _secret_value(match, rule)
                if not isinstance(val, str) or len(val) < 8:
                    continue
                if rule.allow_regexes and any(a.search(val) for a in rule.allow_regexes):
                    continue
                if rule.stopwords and any(s in val.lower() for s in rule.stopwords):
                    continue
                if GLOBAL_ALLOW_PATTERNS and any(a.search(val) for a in GLOBAL_ALLOW_PATTERNS):
                    continue
                if val.startswith("__CSMART_"):  # jangan re-mask placeholder sendiri
                    continue
                text = text.replace(val, self.get_or_create_mask(val, rule.ptype))
        # Tier 2: entropy safety net for unknown-but-likely-secret tokens.
        for word in text.split():
            clean = word.strip("\"'()[]{}<>,;:")
            if self._looks_like_secret(clean):
                text = text.replace(clean, self.get_or_create_mask(clean, "high_entropy"))
        return text

    def unmask_text(self, text: str) -> str:
        """Restore secrets on the client-bound path ONLY (never sent upstream)."""
        if not text:
            return text
        for display, real in list(self.display_map.items()):
            text = text.replace(display, real)
        for mask_id, real in list(self.mem_cache.items()):
            text = text.replace(mask_id, real)
        return text

    def _looks_like_secret(self, token: str) -> bool:
        """Conservative heuristic so legit code (hashes, paths, UUIDs) is not masked."""
        if len(token) <= 28 or _shannon_entropy(token) <= 4.5:
            return False
        if token.startswith(("__CSMART_", "ref_", "sha256:", "0x", "http", "www.")):
            return False
        if "/" in token:  # path-like
            return False
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", token):
            return False
        if _UUID_RE.fullmatch(token) or _HEX_RE.fullmatch(token):
            return False
        if re.fullmatch(r"[A-Za-z_]+[A-Za-z_0-9]*", token) and not any(c.isdigit() for c in token):
            return False  # plain identifier (camel/snake) tanpa digit
        if not any(c.isupper() for c in token):
            return False
        has_digit = any(c.isdigit() for c in token)
        has_sep = "-" in token or "_" in token or "." in token
        if not (has_digit or has_sep):
            return False
        for allow in DLP_ALLOW:
            if allow and allow in token:
                return False
        return True


vault = SecretVault()

# =====================================================================
# 2. SANITIZER & GUARDRAIL
# =====================================================================
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


def _model_token_limits(model_name: str) -> Tuple[int, int]:
    """Resolve (floor, ceil) for a model name. Falls back to global default."""
    lower = (model_name or "").lower()
    for entry in _MODEL_TOKEN_LIMITS:
        if any(k in lower for k in entry["keys"]):
            return entry["floor"], entry["ceil"]
    return MAX_TOKENS_FLOOR, MAX_TOKENS_CEIL


def clamp_max_tokens(body: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp client max_tokens to [floor, ceil]. Emits a structured JSON log
    (TOKEN_CLAMP) tracking original, applied, floor, ceil, and action so
    max-token and reasoning-budget behavior can be traced per model."""
    mt = body.get("max_tokens")
    if not isinstance(mt, int):
        _log("TOKEN_CLAMP", model=body.get("model", ""), requested=mt,
             applied=mt, floor=None, ceil=None, action="skip_non_int")
        return body
    floor, ceil = _model_token_limits(body.get("model", ""))
    if mt < floor:
        applied = floor
        action = "raised_to_floor"
    elif mt > ceil:
        applied = ceil
        action = "lowered_to_ceil"
    else:
        applied = mt
        action = "kept"
    body["max_tokens"] = applied
    _log("TOKEN_CLAMP", model=body.get("model", ""), requested=mt,
         applied=applied, floor=floor, ceil=ceil, action=action)
    return body


# =====================================================================
# 3. REVERSIBLE CONTEXT STORAGE (CCR)
# =====================================================================
EXPAND_TOOL_SCHEMA: Dict[str, Any] = {
    "name": "csmart_expand_symbol",
    "description": "Mengambil isi payload/file utuh yang sebelumnya dipadatkan oleh proxy csmart CCR.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ref_id": {"type": "string", "description": "ID referensi context, e.g. 'ref_8a1f4b2c'"}
        },
        "required": ["ref_id"],
    },
}

# Tool web search yang DIEKSEKUSI PROXY (bukan tool WebSearch bawaan Claude Code,
# yang tidak di-expose ke model pihak ketiga). Proxy jalan di host dengan akses
# internet penuh -> hasil di-feed balik sebagai tool_result ke model.
WEBSEARCH_TOOL_SCHEMA: Dict[str, Any] = {
    "name": "csmart_websearch",
    "description": "Cari informasi terkini dari internet (dieksekusi oleh proxy). Query harus ringkas dan spesifik, dalam bahasa Inggris bila mungkin.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query untuk dicari di web."}
        },
        "required": ["query"],
    },
}


# Backend web search opencode (`OPENCODE_ENABLE_EXA=1`) -> Exa hosted MCP.
# Public endpoint, TANPA API key (tapi free-tier rate-limited). Ganti backend
# lama DuckDuckGo (flaky/timeout). Override env `EXA_MCP_URL` untuk custom,
# mis. `https://mcp.exa.ai/mcp?exaApiKey=...` agar bebas rate limit.
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


def store_ccr_payload(payload_type: str, content: str) -> Tuple[str, str]:
    """Persist a large payload to SQLite, return (ref_id, compact stub)."""
    ref_id = f"ref_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:8]}"
    token_est = len(content) // 4
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO context_blobs (ref_id, payload_type, raw_content, token_count) VALUES (?, ?, ?, ?)",
                (ref_id, payload_type, content, token_est),
            )
            conn.commit()
    except Exception as exc:
        _log("CCR_PUT", error=str(exc))
    lines = content.splitlines()
    preview = "\n".join(lines[:CCR_PREVIEW_LINES]) if len(lines) > CCR_PREVIEW_LINES else content
    stub = (
        f"{preview}\n\n"
        f"[CSMART CCR: konten penuh ({token_est} tokens) tersimpan di {ref_id}. "
        f"Gunakan tool 'csmart_expand_symbol' dengan ref_id='{ref_id}' bila perlu isi lengkap.]"
    )
    return ref_id, stub


def get_ccr_payload(ref_id: str) -> Optional[str]:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT raw_content FROM context_blobs WHERE ref_id = ?", (ref_id,)
            ).fetchone()
        if row:
            return row["raw_content"]
    except Exception:
        pass
    return None


# =====================================================================
# 4. 3-REGION PREFIX ALIGNER
# =====================================================================
def align_prefix_3_region(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Sort tools deterministically, register expand tool, stamp cache marker on
    the last immutable-prefix block. Deterministic for byte-identical cache."""
    system_prompts = payload.get("system", [])
    tools = list(payload.get("tools", []))
    messages = payload.get("messages", [])

    names = [t.get("name") for t in tools if isinstance(t, dict)]
    if "csmart_expand_symbol" not in names:
        tools.append(EXPAND_TOOL_SCHEMA)
    if "csmart_websearch" not in names:
        tools.append(WEBSEARCH_TOOL_SCHEMA)
    tools = sorted(tools, key=lambda t: t.get("name", ""))

    if tools:
        for t in tools:
            if isinstance(t, dict):
                t.pop("cache_control", None)
        if isinstance(tools[-1], dict):
            tools[-1]["cache_control"] = {"type": "ephemeral"}
    elif isinstance(system_prompts, list) and system_prompts:
        for s in system_prompts:
            if isinstance(s, dict):
                s.pop("cache_control", None)
        if isinstance(system_prompts[-1], dict):
            system_prompts[-1]["cache_control"] = {"type": "ephemeral"}

    payload["system"] = system_prompts
    payload["tools"] = tools
    payload["messages"] = messages
    return payload


# =====================================================================
# 5. HEURISTIC MODEL ROUTER (Flash vs Flagship, pinned per session)
# =====================================================================
_session_model: Dict[str, Tuple[str, float]] = {}


def _extract_last_text(payload: Dict[str, Any]) -> str:
    messages = payload.get("messages", [])
    if not messages:
        return ""
    last = messages[-1]
    content = last.get("content", "")
    if isinstance(content, str):
        return content
    parts: List[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("text", "input_text") and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "tool_result":
                c = block.get("content")
                if isinstance(c, str):
                    parts.append(c)
    return "\n".join(parts)


def route_model_tier(payload: Dict[str, Any], session_key: str) -> str:
    """Pick a model for this request. Pinned per session (cache stability)."""
    now = time.time()
    cached = _session_model.get(session_key)
    if cached and now - cached[1] < 3600:
        return cached[0]
    text = _extract_last_text(payload).lower()
    model = FLAGSHIP_MODEL if any(t in text for t in _COMPLEX_TRIGGERS) else FLASH_MODEL
    _session_model[session_key] = (model, now)
    return model


# =====================================================================
# 5.1 OPENAI NATIVE MODEL SUPPORT — Protocol Transformation
# =====================================================================


def clean_openai_model_name(model_name: str) -> str:
    """Clean model name by removing organization prefix like "opencode-go/".
    Preserve original for detection but clean for upstream request."""
    # Strip everything before last slash if present (e.g. "opencode-go/muse" → "muse")
    if "/" in model_name:
        return model_name.rsplit("/", 1)[-1]
    return model_name


def is_openai_model(model_name: str) -> bool:
    """Detect if model is OpenAI-native (requires protocol transformation)."""
    lower_name = model_name.lower()
    return any(pattern.lower() in lower_name for pattern in OPENAI_MODEL_PATTERNS)


def is_anthropic_native_model(model_name: str) -> bool:
    """Detect models served by the Anthropic-compatible /messages endpoint
    (OpenCode Go: minimax-m*, qwen3.*). These are Anthropic-native — no OpenAI
    protocol transform, model name preserved verbatim, raw SSE passthrough.
    Takes precedence over ``is_openai_model`` (a model is one or the other)."""
    lower_name = model_name.lower()
    return any(pattern.lower() in lower_name for pattern in ANTHROPIC_NATIVE_MODEL_PATTERNS)


def detect_openai_endpoint_type(model_name: str) -> str:
    """Detect which OpenAI endpoint to use (chat_completions or responses)."""
    lower_name = model_name.lower()
    # Responses API families (per OpenCode Go model table): grok-*, gpt-5.6-*,
    # muse-*, opencode-*. Everything else OpenAI (glm-,kimi-,longcat-,deepseek-,
    # mimo-,hy3-,hy4-,text-...) is chat completions.
    if any(pattern.lower() in lower_name for pattern in OPENAI_RESPONSES_MODEL_PATTERNS):
        return "responses"
    if "response" in lower_name or "responses" in lower_name:
        return "responses"
    # Default to chat completions for most OpenAI models
    return "chat_completions"


def _extract_system_text(system: Any) -> str:
    """Extract concatenated system text from Anthropic system format (str or list)."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return " ".join(
            block.get("text", "") for block in system if isinstance(block, dict)
        )
    return str(system)


def _convert_anthropic_tool_to_openai(anthropic_tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Anthropic tool format (input_schema) → OpenAI Chat Completions tool format.

    Chat Completions nests everything under ``function``:
      {"type":"function","function":{"name":...,"parameters":...}}
    """
    return {
        "type": "function",
        "function": {
            "name": anthropic_tool["name"],
            "description": anthropic_tool.get("description", ""),
            "parameters": anthropic_tool.get("input_schema", {}),
        },
    }


def _convert_anthropic_tool_to_openai_responses(anthropic_tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Anthropic tool format → OpenAI Responses API tool format.

    Responses API puts ``name`` at the TOP level (flat), NOT nested under
    ``function``. Sending Chat Completions format here causes upstream 400:
    "tools[0] missing required field name".
    """
    return {
        "type": "function",
        "name": anthropic_tool["name"],
        "description": anthropic_tool.get("description", ""),
        "parameters": anthropic_tool.get("input_schema", {}),
    }


def _convert_anthropic_message_to_openai(anth_msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert Anthropic message → LIST of OpenAI Chat Completions messages.

    Returns a list because a turn may expand to multiple OpenAI messages:
    an assistant turn with text + tool_use becomes ONE assistant message holding
    both ``content`` and ``tool_calls``, while each ``tool_result`` becomes its
    own standalone ``role:"tool"`` message (OpenAI requires tool results as a
    separate message, not merged into the user turn).
    """
    role = anth_msg.get("role", "user")
    content = anth_msg.get("content", "")

    # Anthropic content is either str or list[blocks]
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": str(content)}]

    messages: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            if txt:
                text_parts.append(txt)
        elif btype == "thinking":
            # K2b: preserve chain-of-thought from history. DeepSeek chat path
            # carries CoT via reasoning_content on the assistant message (not a
            # separate field); OpenAI modern models use thinking_blocks. We emit
            # reasoning_content so the round-trip does not lose the thinking.
            txt = block.get("thinking", "")
            if txt:
                reasoning_parts.append(txt)
        elif btype == "tool_use":
            # OpenAI embeds tool calls into the assistant message itself
            tool_calls.append({
                "id": block.get("id") or f"call_{len(tool_calls)}",
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), sort_keys=True),
                },
            })
        elif btype == "tool_result":
            # tool_result is its own role:"tool" message in OpenAI chat format
            tool_use_id = block.get("tool_use_id", "")
            raw = block.get("content", "")
            if isinstance(raw, list):
                raw = "".join(
                    p.get("text", "") for p in raw if isinstance(p, dict)
                )
            messages.append({
                "role": "tool",
                "tool_call_id": tool_use_id,
                "content": str(raw),
            })

    # Emit the role message (assistant may carry both text and tool_calls).
    # System messages that appear mid-conversation are kept as-is; OpenAI
    # accepts a system message anywhere in the array. A user turn containing
    # ONLY tool_result blocks produces just role:"tool" messages and no empty
    # user stub (a bare {"role":"user","content":""} corrupts the sequence).
    if role == "system":
        return [{"role": "system", "content": "".join(text_parts)}]

    if tool_calls or text_parts:
        msg: Dict[str, Any] = {"role": role, "content": "".join(text_parts)}
        if reasoning_parts and role == "assistant":
            msg["reasoning_content"] = "\n".join(reasoning_parts)
        if tool_calls:
            msg["tool_calls"] = tool_calls
        messages.insert(0, msg)
    elif reasoning_parts and role == "assistant":
        # Assistant turn with ONLY a thinking block — still preserve the CoT.
        messages.insert(0, {
            "role": "assistant",
            "content": "",
            "reasoning_content": "\n".join(reasoning_parts),
        })

    return messages


def transform_anthropic_to_openai_chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Transform Anthropic Messages API payload → OpenAI Chat Completions API payload."""
    # Extract system prompt
    system_text = _extract_system_text(payload.get("system", ""))

    # Convert all messages
    messages: List[Dict[str, Any]] = []

    # Add system message first if non-empty
    if system_text.strip():
        messages.append({"role": "system", "content": system_text})

    # Add conversation messages
    for anth_msg in payload.get("messages", []):
        if isinstance(anth_msg, dict):
            messages.extend(_convert_anthropic_message_to_openai(anth_msg))

    # Build OpenAI payload
    openai_payload: Dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
        "stream": True,
    }

    # Copy optional parameters if present
    if "max_tokens" in payload:
        openai_payload["max_tokens"] = payload["max_tokens"]
    if "temperature" in payload:
        openai_payload["temperature"] = payload["temperature"]
    if "top_p" in payload:
        openai_payload["top_p"] = payload["top_p"]

    # Cap reasoning/chain-of-thought on the chat path (deepseek). Reasoning-model
    # CoT can burn tens of thousands of tokens on a trivial prompt (observed: 2.8k
    # thinking tokens + hallucinated tangents like "helm" for a bare greeting).
    # reasoning_effort is honored by the upstream; effort "low" trims CoT length and
    # wandering. Toggle via CSMART_REASONING_EFFORT (default low; "" disables).
    _effort = os.getenv("CSMART_REASONING_EFFORT", "low").strip()
    if _effort:
        openai_payload["reasoning_effort"] = _effort

    # Convert tools if present
    anthropic_tools = payload.get("tools", [])
    if anthropic_tools:
        openai_tools = [
            _convert_anthropic_tool_to_openai(tool) for tool in anthropic_tools
        ]
        openai_payload["tools"] = openai_tools
        # Enable parallel tool calls by default (Anthropic-like behavior)
        openai_payload["parallel_tool_calls"] = True

    return openai_payload


def _convert_anthropic_message_to_openai_responses(
    anth_msg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Convert Anthropic message → LIST of OpenAI Responses API input items.

    Returns a list because an assistant turn with tool_use must become separate
    ``message`` + ``function_call`` items in the Responses ``input`` array — the
    chat-completions ``tool_calls`` field is rejected by ``/v1/responses``.
    Likewise ``tool_result`` becomes a standalone ``function_call_output`` item.
    """
    role = anth_msg.get("role", "user")
    content = anth_msg.get("content", "")

    if isinstance(content, str):
        return [{"type": "message", "role": role, "content": content}]

    # Block format: [{"type":"text","text":...}] or tool_use/tool_result
    items: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    reasoning_parts: List[str] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            if txt:
                text_parts.append(txt)
        elif btype == "thinking" and role == "assistant":
            # K2b: preserve chain-of-thought from history as a Responses API
            # "reasoning" input item (summary list). Keeps prompt cache + CoT
            # intact across turns.
            txt = block.get("thinking", "")
            if txt:
                reasoning_parts.append(txt)
        elif btype == "tool_use":
            items.append({
                "type": "function_call",
                "call_id": block.get("id") or f"call_{len(items)}",
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {}), sort_keys=True),
            })
        elif btype == "tool_result":
            # tool_result carries the output of a prior tool_use -> function_call_output
            tool_use_id = block.get("tool_use_id", "")
            raw = block.get("content", "")
            if isinstance(raw, list):
                # Text parts join verbatim; non-text parts (dicts) become JSON
                # so the Responses function_call_output.output stays valid JSON.
                parts: List[str] = []
                for p in raw:
                    if isinstance(p, dict):
                        if p.get("type") == "text":
                            parts.append(p.get("text", ""))
                        else:
                            try:
                                parts.append(json.dumps(p, ensure_ascii=False))
                            except (TypeError, ValueError):  # non-serializable block
                                parts.append(str(p))
                    else:
                        parts.append(str(p))
                raw = "".join(parts)
            elif isinstance(raw, dict):
                try:
                    raw = json.dumps(raw, ensure_ascii=False)
                except (TypeError, ValueError):
                    raw = str(raw)
            items.append({
                "type": "function_call_output",
                "call_id": tool_use_id,
                "output": str(raw),
            })

    # Emit text FIRST so item order mirrors the Anthropic content order
    # (text block precedes tool blocks in the original turn). K2b: assistant
    # thinking is emitted as a "reasoning" item at the very front (thinking
    # precedes text/tool blocks in an Anthropic assistant turn).
    if text_parts:
        text = "\n".join(text_parts)
        if role == "assistant":
            items.insert(0, {
                "type": "message",
                "role": role,
                "content": [{"type": "output_text", "text": text}],
            })
        else:
            items.insert(0, {"type": "message", "role": role, "content": text})
    if reasoning_parts and role == "assistant":
        reasoning_items = [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": t}]}
            for t in reasoning_parts
        ]
        items[0:0] = reasoning_items

    # Preserve the original turn even when both text and tools are empty.
    if not items:
        items.append({"type": "message", "role": role, "content": ""})
    return items


def transform_anthropic_to_openai_responses(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Transform Anthropic Messages API payload → OpenAI Responses API payload.

    Matching OpenCode Go / OpenAI Responses API format.
    """
    system_text = _extract_system_text(payload.get("system", ""))
    input_items: List[Dict[str, Any]] = []
    for m in payload.get("messages", []):
        # Each Anthropic message flattens to 1..N Responses items (message +
        # separate function_call / function_call_output items).
        input_items.extend(_convert_anthropic_message_to_openai_responses(m))
    openai_payload: Dict[str, Any] = {
        "model": payload.get("model"),
        "instructions": system_text,
        "input": input_items,
        "stream": True,
    }
    if "max_tokens" in payload:
        openai_payload["max_output_tokens"] = payload["max_tokens"]
    if "temperature" in payload:
        openai_payload["temperature"] = payload["temperature"]
    if "top_p" in payload:
        openai_payload["top_p"] = payload["top_p"]
    # Copy tools if present (Anthropic tools → OpenAI Responses tool format:
    # flat with ``name`` at top level, NOT nested under ``function``).
    anthropic_tools = payload.get("tools", [])
    if anthropic_tools:
        openai_tools = [
            _convert_anthropic_tool_to_openai_responses(tool) for tool in anthropic_tools
        ]
        openai_payload["tools"] = openai_tools
        openai_payload["parallel_tool_calls"] = True
    # Map Anthropic reasoning/thinking -> Responses API reasoning effort.
    # OpenCode gateway only accepts: off / minimal / low / medium / high (rejects "max").
    effort = _resolve_reasoning_effort(payload)
    if effort is not None:
        openai_payload["reasoning"] = {"effort": effort}
    return openai_payload


def _resolve_reasoning_effort(payload: Dict[str, Any]) -> Optional[str]:
    """Resolve Anthropic reasoning/thinking config to an OpenAI Responses effort.

    Order: explicit ``reasoning.effort`` > ``thinking`` block > env default.
    ``max`` is clamped to ``high``. ``off``/``none``/empty returns ``None``
    (upstream opencode.ai/Console Go rejects the literal string ``off`` — it
    expects ``none`` or the field omitted entirely).

    Returns None when the resolved effort is "off" so the caller skips the
    ``reasoning`` field in the upstream payload.
    ``max`` is clamped to ``high`` (OpenCode rejects it). Returns None when no
    signal is present and no env override is set (provider default applies).
    """
    _ALLOWED = ("none", "minimal", "low", "medium", "high", "xhigh")
    _DISABLED = ("off", "none", "disabled", "")

    def _clamp(effort: Any) -> Optional[str]:
        e = str(effort).strip().lower()
        if e in _DISABLED:
            return None
        if e == "max":
            return "high"
        return e if e in _ALLOWED else "low"

    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort is not None:
            return _clamp(effort)
    thinking = payload.get("thinking")
    if isinstance(thinking, dict):
        # thinking.enabled=true + budget_tokens → medium; thinking absent → None
        if thinking.get("type") == "disabled" or thinking.get("enabled") is False:
            return None
        if thinking.get("enabled") is True or thinking.get("type") == "enabled":
            return "medium"
    env_override = os.getenv("CSMART_REASONING_EFFORT", "").strip().lower()
    if env_override:
        return _clamp(env_override)
    return None


async def transform_openai_sse_to_anthropic(
    sse_events: AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None],
) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    """Transform OpenAI Chat Completions SSE stream → Anthropic Messages SSE stream.

    OpenAI: data: {"choices": [{"delta": {"content": "..."}}]}
    Anthropic: event: content_block_delta\ndata: {"type": "content_block_delta", "delta": {"text": "..."}}\n\n
    """
    # Track if we've sent message_start yet
    sent_message_start = False
    # Track whether the upstream stream emitted a terminal event (message_stop
    # via [DONE] or finish_reason). If the upstream dies without one (connection
    # drop / timeout / provider forgot the marker), we fall back to an explicit
    # incomplete-stream error so the client is never left hanging.
    saw_terminal_event = False
    events_processed = 0
    text_emitted = 0
    # Reasoning (deepseek delta.reasoning_content) -> Anthropic thinking block.
    # Thinking occupies index 0; the visible answer shifts to index 1.
    reasoning_open = False
    text_index = 0
    text_started = False
    emit_reasoning = os.getenv("CSMART_EMIT_REASONING", "1") == "1"
    # Streaming tool_use blocks: Anthropic requires each block to be opened with
    # content_block_start (id+name), streamed via input_json_delta, then closed
    # with content_block_stop. Map OpenAI tool index -> Anthropic block index.
    openai_tool_index_to_block = {}  # openai delta index -> anthropic block index
    open_tool_blocks = []            # anthropic block indices currently open (in order)
    # OpenAI chat usage.prompt_tokens_details.cached_tokens -> cache_read_input_tokens.
    # Populated from the final usage chunk when present; only emitted when > 0.
    cache_read_tokens = 0

    def _stop_reason(fr: Any) -> str:
        # Map OpenAI finish_reason -> Anthropic stop_reason.
        return "max_tokens" if fr == "length" else ("tool_use" if fr == "tool_calls" else "end_turn")

    def _msg_delta(stop: str) -> Dict[str, Any]:
        usage = {"input_tokens": 0, "output_tokens": text_emitted}
        # Emit cache_read_input_tokens only when a real value was captured (> 0).
        if cache_read_tokens > 0:
            usage["cache_read_input_tokens"] = cache_read_tokens
        return {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": usage,
        }

    def _msg_stop_usage() -> Dict[str, Any]:
        usage = {"input_tokens": 0, "output_tokens": text_emitted}
        if cache_read_tokens > 0:
            usage["cache_read_input_tokens"] = cache_read_tokens
        return usage

    _log("OPENAI_SSE_TRANSFORM_START", status="started")

    async for _, openai_event in sse_events:
        events_processed += 1
        # Capture prompt caching usage from the final chunk (OpenAI sends usage
        # non-null on the last chunk). prompt_tokens_details.cached_tokens -> cache_read.
        oai_usage = openai_event.get("usage")
        if isinstance(oai_usage, dict):
            ptd = (oai_usage.get("prompt_tokens_details") or {})
            if isinstance(ptd, dict):
                ct = ptd.get("cached_tokens")
                if isinstance(ct, (int, float)) and ct > 0:
                    cache_read_tokens = int(ct)
        # OpenAI sends [DONE] at end of stream (marked as sentinel dict)
        if openai_event.get("__openai_done"):
            if reasoning_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
            if text_started:
                yield "content_block_stop", {"type": "content_block_stop", "index": text_index}
            for block_idx in open_tool_blocks:
                yield "content_block_stop", {"type": "content_block_stop", "index": block_idx}
            open_tool_blocks.clear()
            _log("OPENAI_SSE_TRANSFORM_DONE", events_processed=events_processed, text_emitted=text_emitted)
            yield "message_delta", _msg_delta("end_turn")
            yield "message_stop", {"type": "message_stop", "usage": _msg_stop_usage()}
            saw_terminal_event = True
            break

        choices = openai_event.get("choices", [])
        if not choices:
            _log("OPENAI_SSE_SKIP", reason="no_choices")
            continue

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # Send message_start on first chunk
        if not sent_message_start:
            yield "message_start", {
                "type": "message_start",
                "message": {
                    "id": f"msg_{uuid.uuid4().hex[:24]}",
                    "type": "message",
                    "role": "assistant",
                    "model": _active_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
            sent_message_start = True

        # Handle reasoning_content (deepseek chain-of-thought) -> thinking block
        reasoning = delta.get("reasoning_content")
        if emit_reasoning and reasoning and not reasoning_open:
            # First reasoning chunk opens a thinking block at index 0.
            # LiteLLM emits signature:"" for non-signable providers (deepseek).
            yield "content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            }
            reasoning_open = True
            text_index = 1
        if reasoning_open and reasoning:
            yield "content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": reasoning},
            }

        # Handle text content delta
        if "content" in delta and delta["content"] is not None:
            text = delta["content"]
            if reasoning_open:
                # Thinking block finished; visible answer lives at index 1.
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
                reasoning_open = False
            if text:
                if not text_started:
                    # Each block must be opened with content_block_start before deltas.
                    yield "content_block_start", {
                        "type": "content_block_start",
                        "index": text_index,
                        "content_block": {"type": "text", "text": ""},
                    }
                    text_started = True
                text_emitted += len(text)
                yield "content_block_delta", {
                    "type": "content_block_delta",
                    "index": text_index,
                    "delta": {"type": "text_delta", "text": text},
                }

        # Handle tool call delta (streaming tool calls)
        if "tool_calls" in delta and delta["tool_calls"] is not None:
            if reasoning_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
                reasoning_open = False
            for tool_call_delta in delta["tool_calls"]:
                oai_index = tool_call_delta.get("index", 0)
                fn = tool_call_delta.get("function", {})
                name = fn.get("name")
                args = fn.get("arguments", "")
                tool_id = tool_call_delta.get("id")

                # First chunk of a tool call carries id + name -> open the block.
                if oai_index not in openai_tool_index_to_block:
                    block_idx = text_index + 1 + len(open_tool_blocks)  # continuous indexing
                    openai_tool_index_to_block[oai_index] = block_idx
                    open_tool_blocks.append(block_idx)
                    yield "content_block_start", {
                        "type": "content_block_start",
                        "index": block_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id or "",
                            "name": name or "",
                            "input": {},
                        },
                    }
                block_idx = openai_tool_index_to_block[oai_index]

                # Arguments stream in subsequent chunks -> emit deltas.
                if args:
                    yield "content_block_delta", {
                        "type": "content_block_delta",
                        "index": block_idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args,
                        },
                    }

        # End of stream
        if finish_reason is not None:
            if reasoning_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
            if text_started:
                yield "content_block_stop", {"type": "content_block_stop", "index": text_index}
            for block_idx in open_tool_blocks:
                yield "content_block_stop", {"type": "content_block_stop", "index": block_idx}
            open_tool_blocks.clear()
            yield "message_delta", _msg_delta(_stop_reason(finish_reason))
            yield "message_stop", {"type": "message_stop", "usage": _msg_stop_usage()}
            saw_terminal_event = True
            break

    # Upstream ended without a terminal event (connection drop / timeout).
    # Never leave the client hanging: emit a spec-compliant incomplete error.
    if not saw_terminal_event:
        _log("OPENAI_SSE_TRANSFORM_INCOMPLETE", events_processed=events_processed, text_emitted=text_emitted)
        yield "error", {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": ("csmart: provider stream ended before message_stop; "
                            "response incomplete, content may be truncated"),
            },
        }


async def transform_openai_responses_sse_to_anthropic(
    sse_events: AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None],
) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    """Transform OpenAI Responses API SSE stream → Anthropic Messages SSE stream.

    OpenCode / OpenAI Responses event types (confirmed live):
      response.created                     -> message_start
      response.output_item.added (message) -> message_start (if not yet)
      response.output_text.delta           -> content_block_delta (text)  <-- primary text stream
      response.output_item.added (fn_call)-> content_block_start (tool_use)
      response.function_call_arguments.delta -> content_block_delta (input_json)
      response.output_item.done (fn_call)  -> content_block_stop (tool_use)
      response.output_item.done (message)  -> content_block_stop (text, safety)
      response.completed                   -> message_stop
      ping / other metadata                -> skipped
    """
    # State for full Anthropic Messages SSE shape (id, model, usage). Captured
    # incrementally: response.created provides id/model, response.completed
    # provides final usage + stop_reason. Without these, Claude Code's strict
    # parser drops the entire stream (issue #4, gaps X-1 + X-2).
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    model_name = ""
    input_tokens = 0
    output_tokens = 0
    stop_reason = "end_turn"
    sent_message_start = False
    text_block_started = False
    text_emitted = 0
    tool_index = 0  # running index for tool_use content blocks
    tool_args_streamed = False  # whether partial args were streamed for current tool
    counts: Dict[str, int] = {}  # per upstream event type, for observability
    # K2b: Responses API reasoning (chain-of-thought) -> Anthropic thinking block.
    # Thinking lives at a high index (1000) to avoid colliding with text (0) and
    # tool blocks (1..N); it is opened on the first reasoning delta and closed on
    # .done / text / tool appearance.
    thinking_block_open = False
    thinking_index = 1000
    # K3: Responses usage.input_tokens_details.cached_tokens -> cache_read_input_tokens.
    # Captured on response.completed; only emitted when > 0.
    cache_read_tokens = 0

    def _ms_payload() -> Dict[str, Any]:
        """Full Anthropic message_start payload with id/model/usage."""
        return {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model_name,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        }

    def _md_payload() -> Dict[str, Any]:
        """Anthropic message_delta payload (stop_reason + final usage)."""
        usage = {
            "output_tokens": output_tokens,
            "input_tokens": input_tokens,
        }
        if cache_read_tokens > 0:
            usage["cache_read_input_tokens"] = cache_read_tokens
        return {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": usage,
        }

    _log("OPENAI_RESPONSES_SSE_TRANSFORM", status="started")

    async for event_name, openai_event in sse_events:
        event_type = event_name
        if event_type is None:
            continue
        counts[event_type] = counts.get(event_type, 0) + 1

        # ---- upstream failure: never swallow it (would yield an empty 200) ---
        if event_type == "error":
            err = openai_event.get("error", {})
            if not isinstance(err, dict):
                err = {}
            # Log only the error class — never the raw upstream message body
            # (it can echo request fragments / tool results). Redaction by key
            # name does NOT cover values, so a verbatim body would leak.
            _log("UPSTREAM_ERROR",
                 status_code=err.get("status_code"),
                 error_type=str(err.get("type", ""))[:80] if isinstance(err, dict) else "")
            yield "error", {
                "type": "error",
                "error": {
                    "type": "upstream_error",
                    "status_code": err.get("status_code"),
                    "message": f"csmart upstream error: {err.get('message', 'upstream rejected request')}",
                },
            }
            break

        # ---- lifecycle ---------------------------------------------------
        if event_type == "response.created":
            resp_info = openai_event.get("response", {}) or {}
            if isinstance(resp_info, dict):
                rid = resp_info.get("id")
                if isinstance(rid, str) and rid:
                    msg_id = f"msg_{rid[:24]}"
                rmodel = resp_info.get("model")
                if isinstance(rmodel, str) and rmodel:
                    model_name = rmodel
            yield "message_start", _ms_payload()
            sent_message_start = True
            continue

        if event_type == "response.completed":
            if thinking_block_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                thinking_block_open = False
            if text_block_started:
                yield "content_block_stop", {"type": "content_block_stop", "index": 0}
            resp_info = openai_event.get("response", {}) or {}
            status = "completed"
            if isinstance(resp_info, dict):
                status = resp_info.get("status", "completed") or "completed"
                # Capture final usage (overrides placeholder 0s from message_start)
                usage = resp_info.get("usage", {}) or {}
                if isinstance(usage, dict):
                    input_tokens = int(usage.get("input_tokens", 0) or 0)
                    output_tokens = int(usage.get("output_tokens", 0) or 0)
                    # K3: Responses usage.input_tokens_details.cached_tokens -> cache_read
                    itd = usage.get("input_tokens_details") or {}
                    if isinstance(itd, dict):
                        ct = itd.get("cached_tokens")
                        if isinstance(ct, (int, float)) and ct > 0:
                            cache_read_tokens = int(ct)
                # Determine stop_reason: tool_use if last item is function_call;
                # otherwise map incomplete_details.reason to Anthropic stop_reason.
                output_items = resp_info.get("output", []) or []
                if isinstance(output_items, list) and output_items:
                    last = output_items[-1]
                    if isinstance(last, dict) and last.get("type") == "function_call":
                        stop_reason = "tool_use"
                if status != "completed":
                    inc = resp_info.get("incomplete_details", {}) or {}
                    reason = inc.get("reason", "") if isinstance(inc, dict) else ""
                    if reason == "max_output_tokens":
                        stop_reason = "max_tokens"
                    elif reason == "content_filter":
                        stop_reason = "refusal"
            if status != "completed" and text_emitted == 0 and tool_index == 0:
                # Upstream finished without text AND with a failure/incomplete status.
                # Never swallow it (would look like an empty "done" to the client).
                _log("UPSTREAM_ERROR", status_code=None,
                     message=f"upstream response status={status}, no text emitted")
                yield "error", {
                    "type": "error",
                    "error": {"type": "upstream_incomplete",
                              "message": f"csmart upstream: response status={status}, no text",
                              "status_code": None},
                }
                break  # error is terminal — do not emit completion events after it
            # X-2 fix: emit message_delta (stop_reason + final usage) BEFORE message_stop
            yield "message_delta", _md_payload()
            _stop_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
            if cache_read_tokens > 0:
                _stop_usage["cache_read_input_tokens"] = cache_read_tokens
            yield "message_stop", {"type": "message_stop", "usage": _stop_usage}
            _log("OPENAI_RESPONSES_SSE_TRANSFORM", status="completed", text_emitted=text_emitted,
                 upstream_status=status, events=counts, stop_reason=stop_reason,
                 input_tokens=input_tokens, output_tokens=output_tokens)
            break

        # ---- text streaming (PRIMARY source of assistant text) ------------
        if event_type == "response.output_text.delta":
            if not sent_message_start:
                yield "message_start", _ms_payload()
                sent_message_start = True
            if thinking_block_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                thinking_block_open = False
            if not text_block_started:
                yield "content_block_start", {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
                text_block_started = True
            # Responses API: delta is a STRING ("delta": "text..."), not {"text": ...}.
            delta = openai_event.get("delta", "")
            text = delta.get("text", "") if isinstance(delta, dict) else delta
            if text:
                text_emitted += len(text)
                yield "content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                }
            continue

        # ---- final text (no-delta providers) ------------------------------
        if event_type == "response.output_text.done":
            # Some providers emit only the final full text (field may be
            # {"text": ...} or {"delta": ...}) without any .delta events.
            # Skip if text was already streamed (avoids double-emit with the
            # .delta handler or the output_item.done safety path).
            if text_emitted == 0:
                delta = openai_event.get("delta", "")
                text = delta.get("text", "") if isinstance(delta, dict) else delta
                if not text:
                    text = openai_event.get("text", "")
                    if isinstance(text, dict):
                        text = text.get("text", "")
                if text:
                    if not sent_message_start:
                        yield "message_start", _ms_payload()
                        sent_message_start = True
                    if thinking_block_open:
                        yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                        thinking_block_open = False
                    if not text_block_started:
                        yield "content_block_start", {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        }
                        text_block_started = True
                    text_emitted += len(text)
                    yield "content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    }
            continue

        # ---- tool call start ---------------------------------------------
        if event_type == "response.output_item.added":
            item = openai_event.get("item", {})
            if item.get("type") == "function_call":
                if thinking_block_open:
                    yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                    thinking_block_open = False
                tool_index += 1
                tool_args_streamed = False
                yield "content_block_start", {
                    "type": "content_block_start",
                    "index": tool_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": item.get("call_id") or item.get("id") or f"call_{tool_index}",
                        "name": item.get("name", ""),
                        "input": {},
                    },
                }
            elif item.get("type") == "message" and not sent_message_start:
                yield "message_start", _ms_payload()
                sent_message_start = True
            continue

        # ---- tool call args streaming ------------------------------------
        if event_type == "response.function_call_arguments.delta":
            args = openai_event.get("delta", "")
            if args:
                tool_args_streamed = True
                yield "content_block_delta", {
                    "type": "content_block_delta",
                    "index": tool_index,
                    "delta": {"type": "input_json_delta", "partial_json": args},
                }
            continue

        # ---- tool call args final (no-delta providers) --------------------
        if event_type == "response.function_call_arguments.done":
            # Some providers emit ONLY the full final arguments string (in
            # {"delta": ...}) without any prior .delta events. If nothing was
            # streamed for the current tool, emit the full string now so the
            # tool_use input is not left as {}.
            if not tool_args_streamed:
                args = openai_event.get("delta", "")
                if isinstance(args, dict):
                    args = args.get("arguments") or args.get("delta") or ""
                if not args:
                    args = openai_event.get("arguments", "")
                if args:
                    tool_args_streamed = True  # guard a duplicate/late .done
                    yield "content_block_delta", {
                        "type": "content_block_delta",
                        "index": tool_index,
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    }
            continue

        # ---- finalization events ------------------------------------------
        if event_type == "response.output_item.done":
            item = openai_event.get("item", {})
            if item.get("type") == "function_call":
                # C1b: some providers deliver a function call's arguments ONLY in
                # the final output_item.done item.arguments — no function_call_
                # arguments.delta/.done events fire for that tool at all (seen
                # live with muse-spark parallel tool calls). Backfill so the
                # tool_use input is not left as {}. Guarded by tool_args_streamed
                # to avoid double-emitting when args already streamed.
                if not tool_args_streamed:
                    args = item.get("arguments", "")
                    if isinstance(args, dict):
                        try:
                            args = json.dumps(args, sort_keys=True)
                        except (TypeError, ValueError):
                            args = str(args)
                    if args:
                        tool_args_streamed = True
                        yield "content_block_delta", {
                            "type": "content_block_delta",
                            "index": tool_index,
                            "delta": {"type": "input_json_delta", "partial_json": args},
                        }
                yield "content_block_stop", {"type": "content_block_stop", "index": tool_index}
            elif item.get("type") == "message":
                # Safety net: if streaming deltas never fired (some providers only
                # send final content), emit the full text now.
                if not text_block_started:
                    content = item.get("content", [])
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            txt = part.get("text", "")
                            if txt:
                                if not sent_message_start:
                                    yield "message_start", _ms_payload()
                                    sent_message_start = True
                                yield "content_block_start", {
                                    "type": "content_block_start",
                                    "index": 0,
                                    "content_block": {"type": "text", "text": ""},
                                }
                                text_block_started = True
                                text_emitted += len(txt)
                                yield "content_block_delta", {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {"type": "text_delta", "text": txt},
                                }
            continue

        # ---- reasoning / thinking stream (Responses API) ------------------
        # response.reasoning_summary_text.delta / response.reasoning_text.delta
        # carry chain-of-thought fragments -> Anthropic thinking block at the
        # anti-collision index. Closed on .done, or when text/tool appears.
        if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            if not sent_message_start:
                yield "message_start", _ms_payload()
                sent_message_start = True
            if not thinking_block_open:
                yield "content_block_start", {
                    "type": "content_block_start",
                    "index": thinking_index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                }
                thinking_block_open = True
            delta = openai_event.get("delta", "")
            text = delta.get("text", "") if isinstance(delta, dict) else delta
            if text:
                yield "content_block_delta", {
                    "type": "content_block_delta",
                    "index": thinking_index,
                    "delta": {"type": "thinking_delta", "thinking": text},
                }
            continue

        if event_type in ("response.reasoning_summary_text.done", "response.reasoning_text.done"):
            if thinking_block_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": thinking_index}
                thinking_block_open = False
            continue

        # ---- everything else (ping, response.in_progress, content_part.*) skipped
        continue


# =====================================================================
# 6. STREAMING REDACTOR (split-marker-safe unmask)
# =====================================================================
_MARKER_RE = re.compile(r"__CSMART_SEC_[0-9a-f]{8}__")
_REDACTOR_TAIL = 64


class StreamingRedactor:
    """Unmask markers on the client-bound path without splitting them at chunk
    boundaries. A split marker stays masked (errs safe), never leaks a secret."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> str:
        combined = self._buf + chunk
        cut = max(0, len(combined) - _REDACTOR_TAIL)
        emit = combined[:cut]
        rest = combined[cut:]
        start = emit.rfind("__CSMART_SEC_")
        if start != -1 and not _MARKER_RE.search(emit[start:]):
            emit, rest = emit[:start], emit[start:] + rest
        self._buf = rest
        return vault.unmask_text(emit)

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return vault.unmask_text(out)


# =====================================================================
# 7. SSE SOURCE + SHADOW-STREAMER (expand + guardrail interception)
# =====================================================================
def _parse_sse_data(data_lines: List[str]) -> Dict[str, Any]:
    raw = "\n".join(data_lines)
    raw_stripped = raw.strip()
    # Special case for OpenAI end-of-stream marker
    if raw_stripped == "[DONE]":
        # Use a sentinel dict that transform_openai_sse_to_anthropic recognizes
        return {"__openai_done": True}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    return {"type": "error", "error": {"type": "invalid_payload", "message": raw[:200]}}


async def _iter_sse_events(
    resp: httpx.Response,
) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    data_lines: List[str] = []
    event_name: Optional[str] = None
    async for raw_line in resp.aiter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                yield event_name, _parse_sse_data(data_lines)
                data_lines = []
                event_name = None
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if data_lines:
        yield event_name, _parse_sse_data(data_lines)


# Test seam (hermetic): inject ``httpx.MockTransport`` via this attr — pola yang
# sama dengan ``router/dispatcher._UPSTREAM_TRANSPORT``. ``None`` = jaringan asli.
_UPSTREAM_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


async def _sse_source(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    """Open the upstream stream, canonical-serialize the body, yield SSE events."""
    payload_bytes = json.dumps(body, sort_keys=True).encode("utf-8")
    async with httpx.AsyncClient(transport=_UPSTREAM_TRANSPORT, timeout=UPSTREAM_TIMEOUT) as client:
        async with client.stream(method, url, headers=headers, content=payload_bytes) as resp:
            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")[:400]
                yield "error", {
                    "type": "error",
                    "error": {"type": "upstream_error", "status_code": resp.status_code, "message": err_body},
                }
                return
            async for event_name, payload in _iter_sse_events(resp):
                yield event_name, payload


def transform_openai_responses_to_anthropic_json(
    payload: Dict[str, Any], model: str = ""
) -> Dict[str, Any]:
    """Transform OpenAI Responses API JSON response → Anthropic Messages JSON response.

    Used for the non-streaming path (Claude Code retry with stream:false).
    OpenAI Responses output items:
      {"type":"message","role":"assistant","content":[{"type":"output_text","text":...}]}
      {"type":"function_call","id":...,"name":...,"arguments":"{...}"}
    """
    content: List[Dict[str, Any]] = []
    stop_reason = "end_turn"

    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for part in item.get("content", []) or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    txt = part.get("text", "")
                    if txt:
                        content.append({"type": "text", "text": txt})
        elif itype == "reasoning":
            # Responses API: reasoning summary is a list of parts, each with "text".
            for part in (item.get("summary") or []):
                if isinstance(part, dict) and part.get("text"):
                    content.append({
                        "type": "thinking",
                        "thinking": part.get("text"),
                        "signature": None,
                    })
        elif itype == "function_call":
            content.append({
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id") or f"call_{len(content)}",
                "name": item.get("name", ""),
                "input": _safe_json_loads(item.get("arguments", "")),
            })
            stop_reason = "tool_use"

    usage = payload.get("usage", {}) or {}
    anthropic_usage = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }
    # K3: OpenAI usage.input_tokens_details.cached_tokens -> cache_read_input_tokens
    itd = usage.get("input_tokens_details") or {}
    if isinstance(itd, dict):
        ct = itd.get("cached_tokens")
        if ct:
            anthropic_usage["cache_read_input_tokens"] = int(ct)
    return {
        "id": f"msg_{payload.get('id', 'resp')[:8]}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model or payload.get("model", ""),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


def transform_openai_chat_to_anthropic_json(
    payload: Dict[str, Any], model: str = ""
) -> Dict[str, Any]:
    """Transform OpenAI Chat Completions JSON response → Anthropic Messages JSON response."""
    content: List[Dict[str, Any]] = []
    stop_reason = "end_turn"
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    # Thinking block must precede text in Anthropic content
    rc = msg.get("reasoning_content")
    if isinstance(rc, str) and rc.strip():
        content.append({"type": "thinking", "thinking": rc, "signature": None})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {}) or {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or f"call_{len(content)}",
            "name": fn.get("name", ""),
            "input": _safe_json_loads(fn.get("arguments", "")),
        })
        stop_reason = "tool_use"
    usage = payload.get("usage", {}) or {}
    anthropic_usage = {
        "input_tokens": int(usage.get("prompt_tokens", 0)),
        "output_tokens": int(usage.get("completion_tokens", 0)),
    }
    # K3: OpenAI usage.prompt_tokens_details.cached_tokens -> cache_read_input_tokens
    ptd = usage.get("prompt_tokens_details") or {}
    if isinstance(ptd, dict):
        ct = ptd.get("cached_tokens")
        if ct:
            anthropic_usage["cache_read_input_tokens"] = int(ct)
    return {
        "id": f"msg_{payload.get('id', 'chat')[:8]}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model or payload.get("model", ""),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


def _safe_json_loads(raw: Any) -> Any:
    """Parse JSON string, fall back to {} on failure."""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _format_event(event_name: Optional[str], payload: Dict[str, Any]) -> bytes:
    etype = str(payload.get("type") or event_name or "message")
    return f"event: {etype}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


# =====================================================================
# MOCK MODE — bypass upstream, return canned Anthropic Messages response
# =====================================================================
def _mock_anthropic_json(model_name: str = "") -> Dict[str, Any]:
    """Canned non-stream Anthropic Messages JSON (mock mode)."""
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": "[MOCK] Non-stream response dari csmart. Format spec-compliant Anthropic. "
                    "Kalau Claude Code render ini, masalah ada di upstream (format ditolak).",
        }],
        "model": model_name or "claude-3-5-sonnet-20241022",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 28},
    }


async def _mock_anthropic_stream(model_name: str = "") -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    """Canned spec-compliant Anthropic Messages SSE stream (mock mode).

    Text-only at index 0 (no thinking). If this still fails to render, Claude
    Code's renderer is rejecting something else entirely.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    model = model_name or "claude-3-5-sonnet-20241022"
    text = ("[MOCK] Halo dari csmart — upstream di-skip. Text-only stream "
            "(no thinking block) untuk isolasi: kalau ini render, masalah thinking; "
            "kalau tidak, masalah lebih dalam.")
    yield "message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
    }
    yield "content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    yield "content_block_delta", {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }
    yield "content_block_stop", {"type": "content_block_stop", "index": 0}
    yield "message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 42, "input_tokens": 10},
    }
    # message_stop with usage (litellm pattern) — some strict clients read usage here
    yield "message_stop", {"type": "message_stop", "usage": {"input_tokens": 10, "output_tokens": 42}}


class ProxyStreamer:
    """Stream upstream SSE to the client, shadowing tool_use locally:
    - ``csmart_expand_symbol`` -> expand from CCR (reversible compression).
    - guardrail violation      -> blocked result (secrets never reach client/upstream).
    Other tool_use streams through unchanged (client executes it)."""

    def __init__(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Dict[str, Any],
    ) -> None:
        self.method = method
        self.url = url
        self.headers = headers
        self.body = body
        self.round = 1
        self.client_index = 0
        self._pending_held: List[Dict[str, Any]] = []

    async def run(self) -> AsyncGenerator[bytes, None]:
        for _ in range(MAX_ROUNDS):
            messages = self.body.get("messages", [])
            self._pending_held = []
            async for chunk in self._stream_round(messages):
                yield chunk
            held = self._pending_held
            if not held:
                return
            self.body = {**self.body, "messages": self._build_followup(messages, held)}
        yield _format_event(
            "error",
            {"type": "error", "error": {"type": "max_shadow_rounds", "message": "csmart: too many shadow rounds"}},
        )

    async def _stream_round(
        self, messages: List[Dict[str, Any]]
    ) -> AsyncGenerator[bytes, None]:
        held_indices: set[int] = set()
        held_by_index: Dict[int, Dict[str, Any]] = {}
        pending: Dict[int, List[Tuple[Optional[str], Dict[str, Any]]]] = {}
        client_index_map: Dict[int, int] = {}
        buffered_end: List[Tuple[Optional[str], Dict[str, Any]]] = []

        async for event_name, payload in _sse_source(
            self.method, self.url, self.headers, {**self.body, "messages": messages}
        ):
            etype = payload.get("type", "")

            if etype == "message_start":
                if self.round == 1:
                    yield _format_event(event_name, payload)
                continue

            if etype in ("message_delta", "message_stop"):
                buffered_end.append((event_name, payload))
                continue

            if etype == "content_block_start":
                index = payload.get("index")
                if not isinstance(index, int):
                    yield _format_event(event_name, payload)
                    continue
                cb = payload.get("content_block", {})
                if isinstance(cb, dict) and cb.get("type") == "tool_use":
                    pending[index] = [(event_name, payload)]
                    held_by_index[index] = {
                        "index": index,
                        "id": cb.get("id"),
                        "name": cb.get("name", ""),
                        "input_parts": [],
                    }
                    base_input = cb.get("input")
                    if isinstance(base_input, dict) and base_input:
                        held_by_index[index]["input_parts"].append(json.dumps(base_input))
                    continue
                new_index = self.client_index
                self.client_index += 1
                client_index_map[index] = new_index
                p = dict(payload)
                p["index"] = new_index
                yield _format_event(event_name, p)
                continue

            if etype == "content_block_delta":
                index = payload.get("index")
                if not isinstance(index, int):
                    yield _format_event(event_name, payload)
                    continue
                if index in pending:
                    pending[index].append((event_name, payload))
                    delta = payload.get("delta", {})
                    if isinstance(delta, dict) and isinstance(delta.get("partial_json"), str):
                        held_by_index[index]["input_parts"].append(delta["partial_json"])
                    continue
                new_index = client_index_map.get(index)
                if new_index is None:
                    continue
                p = dict(payload)
                p["index"] = new_index
                yield _format_event(event_name, p)
                continue

            if etype == "content_block_stop":
                index = payload.get("index")
                if not isinstance(index, int):
                    yield _format_event(event_name, payload)
                    continue
                if index in pending:
                    pending[index].append((event_name, payload))
                    info = held_by_index[index]
                    tool_input = self._join_input(info["input_parts"])
                    err = check_security_guardrails(info["name"], tool_input)
                    if info["name"] in ("csmart_expand_symbol", "csmart_websearch") or err:
                        if err:
                            info["blocked_reason"] = err
                        held_indices.add(index)
                        continue
                    # Normal tool_use -> replay buffered block to the client.
                    new_index = self.client_index
                    self.client_index += 1
                    client_index_map[index] = new_index
                    for en, pl in pending[index]:
                        p = dict(pl)
                        p["index"] = new_index
                        yield _format_event(en, p)
                    continue
                new_index = client_index_map.get(index)
                if new_index is None:
                    continue
                p = dict(payload)
                p["index"] = new_index
                yield _format_event(event_name, p)
                continue

            if etype == "ping":
                yield _format_event(event_name, payload)
                continue

            if etype == "error":
                yield _format_event(event_name, payload)
                return

            yield _format_event(event_name, payload)

        self.round += 1

        if held_indices:
            # Expand/guardrail resolution is synchronous (SQLite read + dict
            # build) — no gather needed.
            self._pending_held = [
                self._execute_held(held_by_index[i]) for i in sorted(held_indices)
            ]
            return

        for event_name, payload in buffered_end:
            yield _format_event(event_name, payload)

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _join_input(parts: List[str]) -> Dict[str, Any]:
        raw = "".join(parts)
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"_partial_json": raw}

    @staticmethod
    def _execute_held(block: Dict[str, Any]) -> Dict[str, Any]:
        tool_input = ProxyStreamer._join_input(block["input_parts"])
        if block.get("blocked_reason"):
            return {
                **block,
                "input": tool_input,
                "content": (
                    f"[CSMART SECURITY BLOCKED] {block['blocked_reason']}. "
                    "Eksekusi dicegat oleh proxy — jangan ulangi; gunakan tool lain."
                ),
            }
        if block["name"] == "csmart_websearch":
            query = (tool_input or {}).get("query")
            if not query:
                return {
                    **block,
                    "input": tool_input,
                    "content": "ERROR: csmart_websearch memerlukan argumen 'query'.",
                }
            content = _websearch_exa(str(query))
            return {**block, "input": tool_input, "content": content}
        if block["name"] == "csmart_expand_symbol":
            ref_id = (tool_input or {}).get("ref_id")
            if not ref_id:
                return {
                    **block,
                    "input": tool_input,
                    "content": "ERROR: csmart_expand_symbol memerlukan argumen string 'ref_id'.",
                }
            content = get_ccr_payload(str(ref_id))
            if content is None:
                return {
                    **block,
                    "input": tool_input,
                    "content": f"ERROR: ref_id {ref_id!r} tidak ditemukan di context store.",
                }
            return {**block, "input": tool_input, "content": content}
        return {**block, "input": tool_input, "content": ""}

    def _build_followup(
        self, messages: List[Dict[str, Any]], held: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        assistant_content: List[Dict[str, Any]] = []
        user_results: List[Dict[str, Any]] = []
        for block in held:
            assistant_content.append(
                {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input", {})}
            )
            user_results.append(
                {"type": "tool_result", "tool_use_id": block["id"], "content": block.get("content", "")}
            )
        followup = list(messages)
        if assistant_content:
            followup.append({"role": "assistant", "content": assistant_content})
        followup.append({"role": "user", "content": user_results})
        return followup


# =====================================================================
# 8. BACKGROUND KEEPALIVE (jaga KV-cache TTL provider)
# =====================================================================
last_request_timestamp: float = time.time()
last_keepalive_ok: float = time.monotonic()
_prefix_snapshot: Optional[Dict[str, Any]] = None
_active_model: str = FLASH_MODEL


async def keepalive_worker() -> None:
    global last_request_timestamp, last_keepalive_ok
    while True:
        await asyncio.sleep(KEEPALIVE_TICK)
        elapsed = time.time() - last_request_timestamp
        if not (KEEPALIVE_WINDOW_START <= elapsed < KEEPALIVE_WINDOW_END):
            continue
        if not _prefix_snapshot or not UPSTREAM_API_KEY:
            continue
        now_mono = time.monotonic()
        if now_mono - last_keepalive_ok < 45:  # jangan spam saat retry
            continue
        payload: Dict[str, Any] = {
            "model": _active_model,
            "max_tokens": 1,
            "system": _prefix_snapshot.get("system", []),
            "tools": _prefix_snapshot.get("tools", []),
            "messages": [{"role": "user", "content": "ping"}],
        }
        headers = {
            "Authorization": f"Bearer {UPSTREAM_API_KEY}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        try:
            async with httpx.AsyncClient(transport=_UPSTREAM_TRANSPORT, timeout=10.0) as client:
                resp = await client.post(
                    f"{UPSTREAM_BASE_URL}/v1/messages",
                    headers=headers,
                    content=json.dumps(payload, sort_keys=True).encode("utf-8"),
                )
            if resp.status_code < 400:
                last_request_timestamp = time.time()
                last_keepalive_ok = time.monotonic()
                _log("KEEPALIVE_PING", status_code=resp.status_code, model=_active_model)
        except Exception:
            pass


# =====================================================================
# 9. FASTAPI APP & ROUTES
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _log("SERVER_START", app_title=app.title)
    keepalive_task = asyncio.create_task(keepalive_worker())
    yield
    keepalive_task.cancel()


app = FastAPI(title="csmart Local Context Optimizer", lifespan=lifespan)


def _upstream_headers(request: Request) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {UPSTREAM_API_KEY}",
        "x-api-key": UPSTREAM_API_KEY,  # Anthropic /v1/messages requires x-api-key, not Bearer
        "Content-Type": "application/json",
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
    }


@app.post("/v1/messages", response_model=None)
async def handle_messages(request: Request) -> StreamingResponse | JSONResponse:
    global last_request_timestamp, _prefix_snapshot, _active_model
    last_request_timestamp = time.time()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    session_key = request.headers.get("x-csmart-session") or "default"
    if not UPSTREAM_API_KEY:
        _log("UPSTREAM_KEY_MISSING", warning=True)

    body = clamp_max_tokens(body)
    sanitize_payload(body)

    # -------------------------------------------------------------------------
    # Step 1: Detect OpenAI models BEFORE model tier routing overrides name
    # OpenAI detection is based on original model name from client request
    # -------------------------------------------------------------------------
    original_model = body.get("model", "")
    # Anthropic-native models (minimax-/qwen3) take precedence: they are served
    # by OpenCode Go's Anthropic-compatible /messages — not OpenAI protocol.
    is_anthropic_native = is_anthropic_native_model(original_model)
    is_openai = (not is_anthropic_native) and is_openai_model(original_model)
    endpoint_type = detect_openai_endpoint_type(original_model) if is_openai else "anthropic"
    cleaned_model = clean_openai_model_name(original_model)
    if is_openai:
        cleaned_model = OPENAI_MODEL_ALIASES.get(cleaned_model, cleaned_model)

    _log("OPENAI_DETECTION",
        original_model=original_model,
        cleaned_model=cleaned_model,
        is_openai=is_openai,
        is_anthropic_native=is_anthropic_native,
        endpoint_type=endpoint_type
    )

    # -------------------------------------------------------------------------
    # Step 2: Heuristic model tier routing (flash vs flagship)
    # -------------------------------------------------------------------------
    routed_model = route_model_tier(body, session_key)
    _active_model = routed_model
    if is_anthropic_native:
        # OpenCode Go Anthropic-native: preserve the client model (minimax-m*/qwen3.*).
        # The tier router would rewrite it to deepseek-chat → 401 upstream.
        body["model"] = cleaned_model
    else:
        body["model"] = routed_model

    # -------------------------------------------------------------------------
    # Step 3: System Prompt Steering for OpenAI-native models
    # Inject before 3-region alignment so steering is part of the immutable prefix
    # -------------------------------------------------------------------------
    if is_openai:
        # Inject steering prompt into system (based on original detection).
        # PREPEND (first block) so the model reads it before the huge Claude Code
        # agent prompt — appended steering was drowned and the model fabricated
        # tasks on a bare greeting. First position = highest emphasis.
        steering_block = {"type": "text", "text": SYSTEM_STEERING_PROMPT}
        current_system = body.get("system", "")
        if isinstance(current_system, str):
            # Convert string to list format and prepend
            if current_system.strip():
                body["system"] = [
                    steering_block,
                    {"type": "text", "text": current_system},
                ]
            else:
                body["system"] = [steering_block]
        elif isinstance(current_system, list):
            # Already list format, prepend
            body["system"] = [steering_block] + current_system
        else:
            # Fallback: convert to string and prepend
            body["system"] = f"{SYSTEM_STEERING_PROMPT}\n\n{_extract_system_text(current_system)}"

    # -------------------------------------------------------------------------
    # 3-region prefix alignment (includes steering now for cache stability)
    # -------------------------------------------------------------------------
    body = align_prefix_3_region(body)
    _prefix_snapshot = {"system": body.get("system", []), "tools": body.get("tools", [])}

    _system_text = _extract_system_text(body.get("system", []))
    _log(
        "INBOUND_REQUEST",
        model=routed_model,
        session=session_key,
        messages=len(body.get("messages", [])),
        system_chars=len(_system_text),
        tools_count=len(body.get("tools", [])),
        is_openai=is_openai,
    )

    if is_openai:
        # OpenAI endpoints don't need anthropic-version header
        # Use separate OPENAI_API_KEY for OpenAI-native endpoints
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        # Select endpoint and transform request
        if endpoint_type == "chat_completions":
            upstream_url = f"{OPENAI_BASE_URL}{OPENAI_CHAT_COMPLETIONS_PATH}"
            transformed_body = transform_anthropic_to_openai_chat(body)
            transformed_body["model"] = cleaned_model
        elif endpoint_type == "responses":
            upstream_url = f"{OPENAI_BASE_URL}{OPENAI_RESPONSES_PATH}"
            transformed_body = transform_anthropic_to_openai_responses(body)
            transformed_body["model"] = cleaned_model
        else:
            upstream_url = f"{OPENAI_BASE_URL}{OPENAI_CHAT_COMPLETIONS_PATH}"
            transformed_body = transform_anthropic_to_openai_chat(body)
            transformed_body["model"] = cleaned_model

        _log("OPENAI_REQUEST_TRANSFORM",
            upstream_url=upstream_url,
            endpoint_type=endpoint_type,
            input_model=original_model,
            output_model=cleaned_model,
            input_messages=len(body.get("messages", [])),
            output_messages=(
                len(transformed_body.get("messages", []))
                if endpoint_type == "chat_completions"
                else len(transformed_body.get("input", []))
            )
        )
    else:
        if is_anthropic_native:
            # OpenCode Go Anthropic-compatible /messages: passthrough with the
            # client model preserved; Anthropic endpoints need x-api-key (K7).
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "x-api-key": OPENAI_API_KEY,
                "Content-Type": "application/json",
                "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
            }
            upstream_url = f"{OPENAI_BASE_URL}{OPENAI_MESSAGES_PATH}"
        else:
            # DeepSeek Anthropic-native endpoint includes anthropic-version header
            headers = _upstream_headers(request)
            upstream_url = f"{UPSTREAM_BASE_URL}/v1/messages"
        transformed_body = body

    # Debug: dump the exact upstream request once per env flag (CSMART_DUMP_BODY=1)
    if os.getenv("CSMART_DUMP_BODY") == "1":
        try:
            with open("/tmp/csmart-body-dump.json", "w") as _f:
                json.dump({"url": upstream_url, "body": transformed_body}, _f, indent=2)
            _log("BODY_DUMPED", path="/tmp/csmart-body-dump.json", url=upstream_url)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Step 3: Response transformation (OpenAI -> Anthropic format)
    # -------------------------------------------------------------------------
    async def generator() -> AsyncGenerator[bytes, None]:
        redactor = StreamingRedactor()

        if MOCK_MODE:
            _log("MOCK_STREAM", endpoint_type=endpoint_type, model=cleaned_model,
                 response_model=original_model)
            async for _event_name, anthropic_event in _mock_anthropic_stream(original_model):
                event_bytes = _format_event(_event_name, anthropic_event)
                out = redactor.feed(event_bytes.decode("utf-8", errors="replace"))
                if out:
                    yield out.encode("utf-8")
            final = redactor.flush()
            if final:
                yield final.encode("utf-8")
            return

        if is_openai and endpoint_type == "chat_completions":
            # For OpenAI chat completions: transform SSE format
            async for _event_name, anthropic_event in transform_openai_sse_to_anthropic(
                _sse_source("POST", upstream_url, headers, transformed_body)
            ):
                event_bytes = _format_event(_event_name, anthropic_event)
                out = redactor.feed(event_bytes.decode("utf-8", errors="replace"))
                if out:
                    yield out.encode("utf-8")
            final = redactor.flush()
            if final:
                yield final.encode("utf-8")
        elif is_openai and endpoint_type == "responses":
            # For OpenAI Responses API: special transform SSE format
            async for _event_name, anthropic_event in transform_openai_responses_sse_to_anthropic(
                _sse_source("POST", upstream_url, headers, transformed_body)
            ):
                event_bytes = _format_event(_event_name, anthropic_event)
                out = redactor.feed(event_bytes.decode("utf-8", errors="replace"))
                if out:
                    yield out.encode("utf-8")
            final = redactor.flush()
            if final:
                yield final.encode("utf-8")
        else:
            # Anthropic native: use existing ProxyStreamer which handles CCR/shadowing
            streamer = ProxyStreamer("POST", upstream_url, headers, transformed_body)
            async for chunk in streamer.run():
                out = redactor.feed(chunk.decode("utf-8", errors="replace"))
                if out:
                    yield out.encode("utf-8")
            final = redactor.flush()
            if final:
                yield final.encode("utf-8")

    # -------------------------------------------------------------------------
    # Step 4: Non-streaming path (Claude Code retries with stream:false when the
    # streaming attempt returns no events). Return a proper JSON response —
    # NOT an event-stream — otherwise Claude Code reports "malformed response".
    # -------------------------------------------------------------------------
    if body.get("stream") is False:
        _log("NON_STREAMING_REQUEST", endpoint_type=endpoint_type, model=cleaned_model)
        if MOCK_MODE:
            _log("MOCK_NON_STREAM", endpoint_type=endpoint_type, model=cleaned_model,
                 response_model=original_model)
            return JSONResponse(_mock_anthropic_json(original_model))
        try:
            transformed_body["stream"] = False
            payload_bytes = json.dumps(transformed_body, sort_keys=True).encode("utf-8")
            async with httpx.AsyncClient(
                transport=_UPSTREAM_TRANSPORT, timeout=UPSTREAM_TIMEOUT
            ) as client:
                resp = await client.post(upstream_url, headers=headers, content=payload_bytes)
                resp_json = resp.json()
            if resp.status_code >= 400:
                _log("NON_STREAMING_UPSTREAM_ERROR", status=resp.status_code)
                raise HTTPException(status_code=502, detail=f"Upstream error: {resp.status_code}")

            if is_openai and endpoint_type == "responses":
                anthropic_json = transform_openai_responses_to_anthropic_json(resp_json, cleaned_model)
            elif is_openai and endpoint_type == "chat_completions":
                anthropic_json = transform_openai_chat_to_anthropic_json(resp_json, cleaned_model)
            else:
                anthropic_json = resp_json
            _log("NON_STREAMING_RESPONSE", content_blocks=len(anthropic_json.get("content", [])))
            return JSONResponse(anthropic_json)
        except HTTPException:
            raise
        except Exception as exc:
            _log("NON_STREAMING_ERROR", error=str(exc)[:200])
            raise HTTPException(status_code=502, detail=f"Non-streaming upstream failed: {exc}")

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_PASSTHROUGH_HEADERS = {"anthropic-version"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def passthrough(request: Request, path: str) -> StreamingResponse:
    """Forward non-messages endpoints (e.g. GET /v1/models, /v1/messages/count_tokens)."""
    target = f"{UPSTREAM_BASE_URL}/{path}"
    body_bytes = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    headers: Dict[str, str] = {"Authorization": f"Bearer {UPSTREAM_API_KEY}"}
    for name, val in request.headers.items():
        if name.lower() in _PASSTHROUGH_HEADERS:
            headers[name.lower()] = val
    if body_bytes:
        headers.setdefault("Content-Type", "application/json")

    async def proxy_gen() -> AsyncGenerator[bytes, None]:
        async with httpx.AsyncClient(transport=_UPSTREAM_TRANSPORT, timeout=UPSTREAM_TIMEOUT) as client:
            req = client.build_request(request.method, target, headers=headers, content=body_bytes)
            resp = await client.send(req, stream=True)
            async for chunk in resp.aiter_bytes():
                yield chunk

    return StreamingResponse(proxy_gen(), media_type="application/json")


if __name__ == "__main__":
    import uvicorn

    _banner()
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="warning")
