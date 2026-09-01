"""csmart.security.secrets — DLP & bidirectional SecretVault.

Single responsibility secrets. Verbatim extract dari ``csmart_proxy.py`` 497-700
(``SECRET_REGEXES`` s.d. ``SecretVault``) + graphify community 49+9
(``_redact`` / ``_log`` / ``get_db`` / ``_shannon_entropy`` / ``_b64url_key``).

Expose: ``_Rule``, ``load_gitleaks_rules``, ``SecretVault``, redact helpers.
Jangan ambil guardrails (itu T-W2). ``_log`` tetap struktur JSON ter-redact
agar mudah di-tracking (JSONL per-hari di ``LOG_DIR``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

try:
    from cryptography.fernet import Fernet as _Fernet
except ImportError:  # pragma: no cover
    _Fernet = None

# ---------------------------------------------------------------------------
# Runtime config (verbatim dari csmart_proxy.py:91,125,133-140,392-393)
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("CSMART_DB", "csmart_state.db")
DLP_ALLOW = [w for w in os.getenv("CSMART_DLP_ALLOW", "").split(",") if w]
VAULT_PERSIST = os.getenv("CSMART_VAULT_PERSIST", "0") == "1"
VAULT_KEY = os.getenv("CSMART_VAULT_KEY", "")

# Mask style: "hash" (default, zero-info) -> placeholder __CSMART_SEC_<hash>__,
# tidak ada byte secret pun ikut ke upstream/log. "preserve" (opsional) ->
# prefix+suffix (mis. sk-ant...90ab); CAVEAT: 10 char ikut ke upstream, hanya
# dipakai kalau log tracking prefix-suffix memang dibutuhkan.
MASK_STYLE = os.getenv("CSMART_MASK_STYLE", "hash").strip().lower()
LOG_DIR = os.getenv("CSMART_LOG_DIR", str(Path.home() / ".csmart" / "logs"))
VERBOSE = os.getenv("CSMART_VERBOSE", "0") == "1"

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
# 1. DLP & BIDIRECTIONAL SECRET VAULT (verbatim 489-799, GITLEAKS_TOML path dipatch)
# =====================================================================
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


GITLEAKS_TOML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "gitleaks.toml")
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

__all__ = ["_Rule", "load_gitleaks_rules", "SecretVault", "_redact", "_b64url_key", "_shannon_entropy", "_secret_value", "SECRET_REGEXES", "GITLEAKS_PATTERNS", "vault"]
