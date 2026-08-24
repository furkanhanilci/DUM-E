"""Just enough GGUF to read the metadata that decides whether a serve will work.

Not a GGUF library. The file is sixteen gigabytes and the only thing needed
from it is a handful of key-value pairs at the front — chiefly the chat
template, whose `raise_exception` guards make every request fail before a token
is produced.

Reading it properly rather than grepping for a substring matters: an earlier
version scanned a 4 MiB window, found nothing, and reported the template clean.
The template was there, 11 guards and all, just past the window.
"""
from __future__ import annotations

import struct
from pathlib import Path

MAGIC = b"GGUF"

# GGUF value type tags.
UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL, STRING, ARRAY, \
    UINT64, INT64, FLOAT64 = range(13)

_FIXED = {UINT8: ("<B", 1), INT8: ("<b", 1), UINT16: ("<H", 2), INT16: ("<h", 2),
          UINT32: ("<I", 4), INT32: ("<i", 4), FLOAT32: ("<f", 4),
          BOOL: ("<?", 1), UINT64: ("<Q", 8), INT64: ("<q", 8),
          FLOAT64: ("<d", 8)}


class GGUFError(RuntimeError):
    """The file is not GGUF, or its metadata could not be read."""


class _Reader:
    def __init__(self, handle):
        self.handle = handle

    def take(self, count: int) -> bytes:
        data = self.handle.read(count)
        if len(data) != count:
            raise GGUFError("metadata ended early")
        return data

    def scalar(self, kind: int):
        if kind in _FIXED:
            fmt, size = _FIXED[kind]
            return struct.unpack(fmt, self.take(size))[0]
        if kind == STRING:
            length = struct.unpack("<Q", self.take(8))[0]
            # A string longer than the whole metadata region is a parse error,
            # not a very long string.
            if length > 64 * 1024 * 1024:
                raise GGUFError(f"implausible string length {length}")
            return self.take(length).decode("utf-8", "replace")
        raise GGUFError(f"unsupported value type {kind}")

    def value(self, kind: int):
        if kind != ARRAY:
            return self.scalar(kind)
        element_kind, count = struct.unpack("<IQ", self.take(12))
        if count > 5_000_000:
            raise GGUFError(f"implausible array length {count}")
        # Long token lists are skipped rather than materialised; nothing here
        # needs them and a 150k-entry vocabulary is not worth the memory.
        if count > 4096:
            for _ in range(count):
                self.value(element_kind)
            return f"<array of {count} skipped>"
        return [self.value(element_kind) for _ in range(count)]


def metadata(path: Path | str, keys: tuple[str, ...] | None = None) -> dict:
    """Read the key-value header. `keys` limits what is kept, not what is parsed."""
    path = Path(path)
    with path.open("rb") as fh:
        reader = _Reader(fh)
        if reader.take(4) != MAGIC:
            raise GGUFError(f"{path} is not a GGUF file")
        version, _tensor_count, kv_count = struct.unpack("<IQQ", reader.take(20))
        if version not in (2, 3):
            raise GGUFError(f"unsupported GGUF version {version}")
        out: dict = {"_version": version, "_kv_count": kv_count}
        for _ in range(kv_count):
            key = reader.scalar(STRING)
            kind = struct.unpack("<I", reader.take(4))[0]
            value = reader.value(kind)
            if keys is None or key in keys:
                out[key] = value
        return out


def chat_template(path: Path | str) -> str | None:
    data = metadata(path, keys=("tokenizer.chat_template",))
    return data.get("tokenizer.chat_template")
