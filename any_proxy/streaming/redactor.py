"""csmart.streaming.redactor — StreamingRedactor isolasi (no DB).

Ekstrak dari ``csmart_proxy.py:2480-2510`` (``_MARKER_RE``, ``_REDACTOR_TAIL``,
``StreamingRedactor``) + ``csmart_proxy.py:645`` (``SecretVault.unmask_text``).

Pure — tidak sentuh DB, tidak import proxy langsung. Unmask di-inject via
callback (hapus dependensi ``vault`` global); fallback ke identity agar hermetic
& pure. Structured JSONL log via ``csmart.logging.structured`` agar trackable.

Interface:
    StreamingRedactor(unmask_fn=None, log_callback=None)
        feed(chunk: str) -> str   — emit prefix aman, tahan tail 64 char
        flush() -> str            — emit sisa buffer

Invariant: split marker ``__CSMART_SEC_<8hex>__`` yang terpotong di boundary
chunk tidak pernah bocor sebagian; tahan sampai lengkap lalu unmask atomik.

Graphify community 37 — streaming/SSE path.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Structured log — injectable, no hard dep pada csmart_proxy._log
# ---------------------------------------------------------------------------
try:  # pragma: no cover
    from any_proxy.logging.structured import _log as _redactor_log  # type: ignore
except ImportError:
    def _redactor_log(event: str, **fields: object) -> None:  # type: ignore[no-redef]
        pass

_log_callback: Optional[Callable[..., None]] = None


def set_redactor_logger(callback: Optional[Callable[..., None]]) -> None:
    """Inject custom structured logger untuk redactor (hapus dep _log langsung)."""
    global _log_callback
    _log_callback = callback


def _emit(ev: str, level: str = "INFO", **fields: object) -> None:
    try:
        if _log_callback is not None:
            _log_callback(ev, level=level, **fields)
        else:
            _redactor_log(ev, level=level, **fields)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Marker constants — mirror csmart_proxy.py:2480-2481
# ---------------------------------------------------------------------------
_MARKER_RE = re.compile(r"__CSMART_SEC_[0-9a-f]{8}__")
_REDACTOR_TAIL = 64


def _default_unmask(text: str) -> str:
    """Fallback unmask — coba semua vault instance yang ada, else identity.

    Pure isolasi: tidak import any_proxy_proxy di top-level agar tidak circular.
    Dipanggil per feed/flush; lazy import agar hermetic tests tidak butuh vault.
    Probe semua vault yang mungkin terisi (proxy + security) — ambil yang
    menghasilkan perubahan; jika tak ada yang ubah, identity.
    """
    # probe semua vault candidates, return yang pertama yang mengubah text
    for _mod, _attr in (
        ("csmart.security.secrets", "vault"),
        ("csmart_proxy", "vault"),
    ):
        try:
            import importlib

            mod = importlib.import_module(_mod)
            vault = getattr(mod, _attr, None)
            if vault is not None and hasattr(vault, "unmask_text"):
                out = vault.unmask_text(text)  # type: ignore[no-any-return]
                if out != text:
                    return out
        except ImportError:
            continue
        except Exception:
            continue
    # no vault changed it — try first non-identity as fallback, else identity
    for _mod, _attr in (
        ("csmart.security.secrets", "vault"),
        ("csmart_proxy", "vault"),
    ):
        try:
            import importlib

            mod = importlib.import_module(_mod)
            vault = getattr(mod, _attr, None)
            if vault is not None and hasattr(vault, "unmask_text"):
                return vault.unmask_text(text)  # type: ignore[no-any-return]
        except ImportError:
            continue
        except Exception:
            continue
    return text


class StreamingRedactor:
    """Unmask marker di client-bound path tanpa split di boundary chunk.

    Split marker stays masked (errs safe), never leaks secret — tahan tail
    64 char + partial ``__CSMART_SEC_`` prefix sampai chunk berikutnya lengkap.
    Pure — no DB. ``unmask_fn`` injectable agar hermetic & hapus dep ``_log``/vault.

    Args:
        unmask_fn: ``(str) -> str`` custom unmask (default: lazy vault lookup).
        log_callback: optional structured log callback (``(event, **fields)``).

    Example:
        red = StreamingRedactor()
        out = red.feed(chunk)
        tail = red.flush()
    """

    def __init__(
        self,
        unmask_fn: Optional[Callable[[str], str]] = None,
        log_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        self._buf = ""
        self._unmask: Callable[[str], str] = unmask_fn or _default_unmask
        self._log_cb = log_callback
        # stats untuk structured tracking
        self._chunks = 0
        self._bytes_in = 0

    def feed(self, chunk: str) -> str:
        """Feed satu chunk, return prefix aman yang sudah di-unmask.

        Tail 64 char ditahan untuk hindari split marker. Jika emit berakhir
        dengan partial ``__CSMART_SEC_`` tanpa match regex lengkap, tahan juga.
        """
        self._chunks += 1
        self._bytes_in += len(chunk)
        combined = self._buf + chunk
        cut = max(0, len(combined) - _REDACTOR_TAIL)
        emit = combined[:cut]
        rest = combined[cut:]
        start = emit.rfind("__CSMART_SEC_")
        if start != -1 and not _MARKER_RE.search(emit[start:]):
            # partial marker at boundary — tahan, jangan emit setengah
            emit, rest = emit[:start], emit[start:] + rest
            _emit("REDACTOR_HOLD_PARTIAL", level="DEBUG", held=len(rest), chunks=self._chunks)
        self._buf = rest
        out = self._unmask(emit)
        if emit != out:
            _emit("REDACTOR_UNMASK", level="DEBUG", emit_len=len(emit), out_len=len(out), chunks=self._chunks)
        return out

    def flush(self) -> str:
        """Flush sisa buffer (akhir stream) — unmask & kosongkan."""
        out, self._buf = self._buf, ""
        result = self._unmask(out)
        _emit(
            "REDACTOR_FLUSH",
            level="INFO",
            chunks=self._chunks,
            bytes_in=self._bytes_in,
            flushed=len(result),
            had_partial=bool(out),
        )
        return result

    # -- introspection untuk tests/monitoring --------------------------------
    @property
    def buffered(self) -> int:
        """Panjang buffer yang masih ditahan (untuk hermetic assert)."""
        return len(self._buf)


__all__ = [
    "StreamingRedactor",
    "_MARKER_RE",
    "_REDACTOR_TAIL",
    "set_redactor_logger",
]
