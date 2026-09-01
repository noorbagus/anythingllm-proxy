"""csmart.logging.structured — structured JSONL audit logging + sqlite.

Ekstrak dari ``csmart_proxy.py`` 276-520 (``_log``, ``get_db``, ``init_db``,
``_banner``) + ``_redact`` di-import dari ``csmart.security.secrets``
(jangan duplikasi — single source of truth). ``secrets`` tidak import
``logging`` (hindari cycle).

Thread-safe, never raises, redact credential by key, JSON-safe coercion,
trace_id via ContextVar, sqlite helper untuk ``csmart_state.db``.

Interface:
    _log(event, **fields)          — sync emit satu JSONL record (compat monolit)
    _redact(value)                 — re-export dari csmart.security.secrets
    _SENSITIVE_KEYS                — re-export dari csmart.security.secrets
    get_db() / init_db()           — sqlite helper (extract dari proxy 449-482)
    _banner()                      — startup banner (extract dari proxy 439-443)
    StructuredLogger               — class non-blocking queue (opsional)
    logger                         — singleton (``from any_proxy.logging.structured import logger``)
    set_trace_id / get_trace_id    — ContextVar helper
    log_structured(event, level, **fields) — alias dengan level
"""
from __future__ import annotations

import contextvars
import json
import os
import queue
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

# _redact single source — jangan duplikasi (secrets tidak import logging → no cycle)
from any_proxy.security.secrets import _SENSITIVE_KEYS, _redact  # noqa: E402

# ---------------------------------------------------------------------------
# Config (mirror csmart_proxy.py:91,392-393) — overridable via env
# ---------------------------------------------------------------------------
DB_PATH: str = os.getenv("CSMART_DB", "csmart_state.db")
LOG_DIR: str = os.getenv("CSMART_LOG_DIR", str(Path.home() / ".csmart" / "logs"))
VERBOSE: bool = os.getenv("CSMART_VERBOSE", "0") == "1"
_LOG_LEVEL: str = os.getenv("CSMART_LOG_LEVEL", "INFO")
_MAX_QUEUE: int = int(os.getenv("CSMART_LOG_QUEUE", "10000"))

_trace_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("csmart_trace_id", default=None)
_write_lock = threading.Lock()


def set_trace_id(trace_id: str | None) -> None:
    """Set trace_id untuk context saat ini (propagate via ContextVar)."""
    _trace_var.set(trace_id)


def get_trace_id() -> str | None:
    """Ambil trace_id dari context saat ini."""
    return _trace_var.get()


def _json_safe(value: object) -> object:
    """Coerce non-JSON-serializable values to str so writer never chokes."""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _build_record(event: str, level: str = "INFO", **fields: Any) -> dict[str, Any]:
    """Bangun record JSON terstruktur — dipanggil oleh _log & StructuredLogger."""
    rec: dict[str, Any] = {
        "event": event,
        "level": level,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    tid = _trace_var.get()
    if tid:
        rec["trace_id"] = tid
    for k, v in fields.items():
        rec[k] = _json_safe(v)
    return _redact(rec)


def _write_record(rec: dict[str, Any]) -> None:
    """Tulis satu record ke file harian atau stderr (thread-safe, never raises)."""
    try:
        line = json.dumps(rec, ensure_ascii=False)
        if LOG_DIR:
            # LOG_DIR == "" disables file logging (hermetic tests)
            Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
            path = Path(LOG_DIR) / f"session_{datetime.now().strftime('%Y%m%d')}.jsonl"
            with _write_lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        elif VERBOSE:
            with _write_lock:
                sys.stderr.write(line + "\n")
    except Exception:
        # logging must never break the proxy
        pass


def _log(event: str, **fields: Any) -> None:
    """Emit one redacted JSONL event. Never raises; never logs secrets.

    Compat dengan ``csmart_proxy.py:_log`` — signature sama persis.
    Level default INFO; override via ``level=`` kwarg.
    """
    try:
        level = str(fields.pop("level", "INFO"))
        rec = _build_record(event, level=level, **fields)
        _write_record(rec)
    except Exception:
        pass


def log_structured(event: str, level: str = "INFO", **fields: Any) -> None:
    """Alias eksplisit dengan level — untuk tracking pipeline."""
    _log(event, level=level, **fields)


# ---------------------------------------------------------------------------
# sqlite helpers — extract dari csmart_proxy.py:449-482 (verbatim)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# _banner — extract dari csmart_proxy.py:439-443
# ---------------------------------------------------------------------------
def _banner() -> None:
    # import lazy agar tidak hard-depend di test (env mungkin belum set)
    # fallback ke os.getenv bila proxy constants belum ada
    host = os.getenv("CSMART_HOST", "127.0.0.1")
    port = os.getenv("CSMART_PORT", "8080")
    upstream = os.getenv("ANTHROPIC_UPSTREAM_URL") or os.getenv("UPSTREAM_BASE_URL") or "https://api.deepseek.com/anthropic"
    flash = os.getenv("CSMART_FLASH_MODEL", "deepseek-chat")
    flagship = os.getenv("CSMART_FLAGSHIP_MODEL", "deepseek-reasoner")
    sys.stderr.write(
        f"[csmart] proxy http://{host}:{port} -> {upstream} "
        f"(flash={flash}, flagship={flagship})\n"
    )


# ---------------------------------------------------------------------------
# StructuredLogger — non-blocking queue + daemon writer (mirror router/logger.py)
# ---------------------------------------------------------------------------
class StructuredLogger:
    """Non-blocking structured logger backed by bounded queue + one daemon thread.

    Cocok untuk hot path streaming — ``log()`` tidak pernah blok proxy.
    Consumer: ``from any_proxy.logging.structured import logger``.
    """

    def __init__(
        self,
        log_dir: str | None = None,
        verbose: bool | None = None,
        max_queue: int | None = None,
        sink: TextIO | None = None,
    ) -> None:
        self.log_dir = LOG_DIR if log_dir is None else log_dir
        self.verbose = VERBOSE if verbose is None else verbose
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue or _MAX_QUEUE)
        self._sink = sink
        self._trace_var = _trace_var
        self._thread = threading.Thread(target=self._writer_loop, daemon=True, name="csmart-structured-logger")
        self._thread.start()

    def set_trace_id(self, trace_id: str | None) -> None:
        self._trace_var.set(trace_id)

    def log(self, event: str, level: str = "INFO", **fields: Any) -> None:
        try:
            rec = _build_record(event, level=level, **fields)
            try:
                self._queue.put_nowait(rec)
            except queue.Full:
                # drop oldest, enqueue newest — never block
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(rec)
                except queue.Full:
                    pass
        except Exception:
            pass

    # compat alias
    def _log(self, event: str, **fields: Any) -> None:
        level = str(fields.pop("level", "INFO"))
        self.log(event, level=level, **fields)

    def _writer_loop(self) -> None:
        while True:
            try:
                rec = self._queue.get()
                if self._sink is not None:
                    try:
                        self._sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        self._sink.flush()
                    except Exception:
                        pass
                else:
                    _write_record(rec)
            except Exception:
                continue

    @staticmethod
    def _json_safe(value: object) -> object:
        return _json_safe(value)


# Module singleton — W2/other consumers: ``from any_proxy.logging.structured import logger``
logger = StructuredLogger()

__all__ = [
    "_log",
    "_redact",
    "_SENSITIVE_KEYS",
    "LOG_DIR",
    "VERBOSE",
    "DB_PATH",
    "get_db",
    "init_db",
    "_banner",
    "StructuredLogger",
    "logger",
    "set_trace_id",
    "get_trace_id",
    "log_structured",
]
