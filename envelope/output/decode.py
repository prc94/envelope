"""Incremental per-stream decoding to text, with binary detection.

Decision order for each stream (made on the first bytes, see ``SNIFF_BYTES``):
1. byte-order mark (UTF-8, UTF-16 LE/BE);
2. UTF-16 byte pattern (many NULs at odd or even offsets);
3. binary (NULs or a high share of control bytes);
4. UTF-8, switching to the console code page (cmd / Windows PowerShell only)
   at the first invalid UTF-8 sequence.
Undecodable bytes become U+FFFD and are counted.
"""

from __future__ import annotations

import codecs
import sys

from envelope.output.binary import looks_binary, sniff_type

SNIFF_BYTES = 1024


def utf16_pattern(sample: bytes) -> str | None:
    """Return 'utf-16-le'/'utf-16-be' when the sample looks like BOM-less UTF-16 text."""
    n = len(sample) - len(sample) % 2
    if n < 4:
        return None
    pairs = n // 2
    odd_zero = sum(1 for i in range(1, n, 2) if sample[i] == 0)
    even_zero = sum(1 for i in range(0, n, 2) if sample[i] == 0)
    if odd_zero >= pairs * 0.4 and even_zero <= pairs * 0.05:
        return "utf-16-le"
    if even_zero >= pairs * 0.4 and odd_zero <= pairs * 0.05:
        return "utf-16-be"
    return None


def console_codepage() -> str:
    """The OEM code page cmd and Windows PowerShell use when they cannot emit UTF-8."""
    if sys.platform == "win32":
        import ctypes

        cp = int(ctypes.windll.kernel32.GetOEMCP())
        name = f"cp{cp}"
        try:
            codecs.lookup(name)
            return name
        except LookupError:
            pass
    return "cp437"


class StreamDecoder:
    def __init__(self, fallback_codepage: str | None = None) -> None:
        self.fallback = fallback_codepage
        self.encoding: str | None = None
        self.binary = False
        self.binary_type: str | None = None
        self.replaced = 0
        self.raw_bytes = 0
        self._pending = b""
        self._pending_since: float | None = None
        self._decoder: codecs.IncrementalDecoder | None = None
        self._switched = False

    # --- public API ------------------------------------------------------------

    def feed(self, data: bytes, *, now: float | None = None) -> str:
        self.raw_bytes += len(data)
        if self.binary:
            return ""
        if self._decoder is None:
            if not self._pending and now is not None:
                self._pending_since = now
            self._pending += data
            if not self._ready(self._pending):
                return ""
            data, self._pending = self._pending, b""
            self._decide(data)
            if self.binary:
                return ""
        return self._decode(data, final=False)

    def peek_pending(self) -> str:
        """Best-effort text of undecided bytes still buffered, for display only
        (does not commit to an encoding decision). Empty once decided."""
        if self._decoder is not None or not self._pending:
            return ""
        return self._pending.decode("utf-8", "replace")

    def maybe_decide_idle(self, now: float, idle_s: float = 0.2) -> str:
        """Force the pending encoding decision if bytes have sat unread for
        ``idle_s`` without reaching a newline or :data:`SNIFF_BYTES` (design B4:
        decide on the first newline, 1,024 bytes, or 200 ms without new bytes,
        whichever comes first). Returns any newly-decoded text."""
        if self._decoder is not None or not self._pending or self._pending_since is None:
            return ""
        if now - self._pending_since < idle_s:
            return ""
        data, self._pending = self._pending, b""
        self._decide(data)
        if self.binary:
            return ""
        return self._decode(data, final=False)

    def finish(self) -> str:
        if self._decoder is None:
            data, self._pending = self._pending, b""
            if not data:
                self.encoding = "utf-8"
                self._decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
                return ""
            self._decide(data)
            if self.binary:
                return ""
            return self._decode(data, final=True)
        if self.binary:
            return ""
        return self._decode(b"", final=True)

    # --- internals ---------------------------------------------------------------

    @staticmethod
    def _ready(pending: bytes) -> bool:
        """Decide early (a full line is enough) so stream interleaving is preserved."""
        if len(pending) >= SNIFF_BYTES:
            return True
        return b"\n" in pending and (len(pending) >= 4 or b"\x00" not in pending)

    def _decide(self, sample: bytes) -> None:
        if sample.startswith(codecs.BOM_UTF8):
            self.encoding = "utf-8-sig"
        elif sample.startswith(codecs.BOM_UTF16_LE) or sample.startswith(codecs.BOM_UTF16_BE):
            self.encoding = "utf-16"
        elif (pattern := utf16_pattern(sample[:SNIFF_BYTES])) is not None:
            self.encoding = pattern
        elif looks_binary(sample[:SNIFF_BYTES]):
            self.binary = True
            self.binary_type = sniff_type(sample)
            return
        else:
            self.encoding = "utf-8"
        self._decoder = codecs.getincrementaldecoder(self.encoding)("surrogateescape")

    def _decode(self, data: bytes, *, final: bool) -> str:
        assert self._decoder is not None
        if self._switched:
            text = self._decoder.decode(data, final)
        else:
            text = self._decoder.decode(data, final)
            if self.fallback and self.encoding == "utf-8" and _has_escapes(text):
                # Not UTF-8: reinterpret this chunk and everything after it in the code page.
                raw = text.encode("utf-8", "surrogateescape")
                self.encoding = self.fallback
                self._switched = True
                self._decoder = codecs.getincrementaldecoder(self.fallback)("replace")
                text = self._decoder.decode(raw, final)
        if _has_escapes(text):
            count = sum(1 for ch in text if "\udc80" <= ch <= "\udcff")
            self.replaced += count
            text = "".join("�" if "\udc80" <= ch <= "\udcff" else ch for ch in text)
        self.replaced += text.count("�") if self._switched else 0
        return text


def _has_escapes(text: str) -> bool:
    return any("\udc80" <= ch <= "\udcff" for ch in text)
