"""Binary output detection and file-type sniffing."""

from __future__ import annotations

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"%PDF-", "PDF"),
    (b"PK\x03\x04", "ZIP"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"\x7fELF", "ELF"),
    (b"MZ", "Windows executable"),
    (b"\xcf\xfa\xed\xfe", "Mach-O"),
    (b"\xca\xfe\xba\xbe", "Mach-O/Java class"),
    (b"SQLite format 3\x00", "SQLite"),
    (b"\x00asm", "WebAssembly"),
    (b"OggS", "Ogg"),
    (b"RIFF", "RIFF (WAV/AVI/WebP)"),
    (b"ID3", "MP3"),
)

_TEXT_CONTROLS = frozenset(b"\t\n\r\f\b\x1b\x07\x0b")


def sniff_type(data: bytes) -> str | None:
    for signature, name in _SIGNATURES:
        if data.startswith(signature):
            return name
    if len(data) > 262 and data[257:262] == b"ustar":
        return "tar"
    return None


def looks_binary(sample: bytes) -> bool:
    """NUL bytes, a known binary signature, or >30% non-text control bytes."""
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    kind = sniff_type(sample)
    if kind is not None and kind not in ("MP3",):
        return True
    controls = sum(1 for b in sample if b < 0x20 and b not in _TEXT_CONTROLS)
    return controls > len(sample) * 0.3
