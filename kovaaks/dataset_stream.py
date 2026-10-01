"""Bounded gzip/JSON decoding for the public scenario release datasets."""

from collections.abc import Sequence
from functools import partial
import gzip
import io
import json
import struct

from .catalog import freeze_catalog, freeze_json


_CHUNK_SIZE = 64 * 1024
_MAX_VALUE_CHARS = 8 * 1024 * 1024
_MAX_FIELD_KEYS = 512
_WHITESPACE = " \t\r\n"


class PackedCounts(Sequence):
    """Keep a positional history row compact before its timestamps are known.

    Sorted release JSON places the history before the timestamps. Integer rows
    use immutable int64 bytes plus a null bitmap; unusual legacy values keep
    their original types in a tuple. The normal sequence interface lets existing
    history merging retain its first-non-null duplicate timestamp semantics.
    """

    __slots__ = ("_packed", "_missing", "_fallback")

    def __init__(self, values):
        self._fallback = None
        if all(value is None or type(value) is int and -(1 << 63) <= value < (1 << 63)
               for value in values):
            missing = bytearray((len(values) + 7) // 8)
            for index, value in enumerate(values):
                if value is None:
                    missing[index // 8] |= 1 << (index % 8)
            self._missing = bytes(missing) if any(missing) else b""
            self._packed = struct.pack(f"<{len(values)}q", *(0 if value is None else value for value in values))
        else:
            self._packed = self._missing = b""
            self._fallback = tuple(values)

    def __len__(self):
        return len(self._fallback) if self._fallback is not None else len(self._packed) // 8

    def __iter__(self):
        if self._fallback is not None:
            yield from self._fallback
        elif not self._missing:
            yield from (item[0] for item in struct.iter_unpack("<q", self._packed))
        else:
            for index, (value,) in enumerate(struct.iter_unpack("<q", self._packed)):
                yield None if self._missing[index // 8] & (1 << (index % 8)) else value

    def __getitem__(self, index):
        if isinstance(index, slice):
            return list(self)[index]
        if self._fallback is not None:
            return self._fallback[index]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError("history sample index out of range")
        if self._missing and self._missing[index // 8] & (1 << (index % 8)):
            return None
        return struct.unpack_from("<q", self._packed, index * 8)[0]

    def __eq__(self, other):
        if not isinstance(other, Sequence):
            return NotImplemented
        return len(self) == len(other) and all(left == right for left, right in zip(self, other))


class _ChunkReader(io.RawIOBase):
    """Adapt streamed HTTP chunks to gzip without collecting the response body."""

    def __init__(self, chunks, check_cancel):
        self._chunks = iter(chunks)
        self._chunk = memoryview(b"")
        self._check_cancel = check_cancel

    def readable(self):
        return True

    def readinto(self, target):
        self._check_cancel()
        while not self._chunk:
            try:
                self._chunk = memoryview(next(self._chunks))
            except StopIteration:
                return 0
            self._check_cancel()
        size = min(len(target), len(self._chunk))
        target[:size] = self._chunk[:size]
        self._chunk = self._chunk[size:]
        return size


class _JsonStream:
    """Decode individual JSON values without accumulating the enclosing document."""

    def __init__(self, stream, check_cancel, chunk_size=_CHUNK_SIZE):
        self._stream = stream
        self._check_cancel = check_cancel
        self._chunk_size = chunk_size
        self._buffer = ""
        self._position = 0
        self._eof = False
        self._field_keys = {}
        # A bound method would keep the reader and its final buffers in a cycle.
        self._decoder = json.JSONDecoder(object_pairs_hook=partial(self._object, self._field_keys))

    @staticmethod
    def _object(field_keys, pairs):
        """Restore field-key sharing across separately decoded scenario rows.

        JSONDecoder's internal key memo is cleared after every raw_decode call.
        Keep a bounded pool for common schema fields, including nested objects;
        arbitrary additional fields still retain their original contents.
        """
        result = {}
        for key, value in pairs:
            shared = field_keys.get(key)
            if shared is not None:
                key = shared
            elif len(field_keys) < _MAX_FIELD_KEYS:
                field_keys[key] = key
            result[key] = value
        return result

    def _fill(self):
        self._check_cancel()
        self._buffer = self._buffer[self._position:]
        self._position = 0
        text = self._stream.read(self._chunk_size)
        self._buffer += text
        self._eof = not text

    def peek(self):
        while True:
            while self._position < len(self._buffer):
                char = self._buffer[self._position]
                if char not in _WHITESPACE:
                    return char
                self._position += 1
            if self._eof:
                return ""
            self._fill()

    def expect(self, char):
        if self.peek() != char:
            raise ValueError(f"Expected JSON delimiter {char!r}")
        self._position += 1

    def value(self):
        self._check_cancel()
        if not self.peek():
            raise ValueError("Unexpected end of JSON")
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buffer, self._position)
            except json.JSONDecodeError:
                if self._eof:
                    raise
            else:
                # A chunk ending in '1' may still be the start of '123' or
                # '1e3'. Wait for a delimiter (also ':' when decoding a key).
                if end < len(self._buffer):
                    if self._buffer[end] in _WHITESPACE + ",]}:":
                        self._position = end
                        return value
                    if self._eof:
                        raise ValueError("Invalid character after JSON value")
                elif self._eof:
                    self._position = end
                    return value
            if len(self._buffer) - self._position > _MAX_VALUE_CHARS:
                raise ValueError("Dataset JSON item is too large")
            self._fill()

    def object_items(self, read_value=None):
        self.expect("{")
        if self.peek() == "}":
            self._position += 1
            return
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            self.expect(":")
            yield key, self.value() if read_value is None else read_value(key)
            if self.peek() == "}":
                self._position += 1
                return
            self.expect(",")

    def array_items(self):
        self.expect("[")
        if self.peek() == "]":
            self._position += 1
            return
        while True:
            yield self.value()
            if self.peek() == "]":
                self._position += 1
                return
            self.expect(",")

    def finish(self):
        # Read through gzip EOF so a truncated footer or CRC failure cannot
        # turn an otherwise valid JSON prefix into a successful download.
        if self.peek():
            raise ValueError("Unexpected content after dataset JSON")


def read_dataset_response(response, filename, check_cancel=lambda: None):
    """Validate a complete streamed asset before exposing any of its contents."""
    chunks = response.iter_content(chunk_size=_CHUNK_SIZE)
    with _ChunkReader(chunks, check_cancel) as raw:
        with io.BufferedReader(raw, buffer_size=_CHUNK_SIZE) as buffered:
            with gzip.GzipFile(fileobj=buffered) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8") as text:
                    reader = _JsonStream(text, check_cancel)
                    if filename == "scenarios.json.gz":
                        data = []
                        for item in reader.array_items():
                            if (not isinstance(item, dict) or not item.get("leaderboardId")
                                    or not isinstance(item.get("counts"), dict)):
                                raise ValueError("Expected a scenario list")
                            # Freeze one row at a time so publication does not
                            # temporarily duplicate an entire decoded catalog.
                            data.append(freeze_json(item))
                    else:
                        def read_value(key):
                            if key != "history":
                                return reader.value()
                            history = {}
                            for lid, counts in reader.object_items():
                                if not isinstance(counts, list):
                                    raise ValueError("Expected a history count array")
                                history[lid] = PackedCounts(counts)
                            return history

                        data = dict(reader.object_items(read_value))
                        stamps, history = data.get("timestamps"), data.get("history")
                        if (not isinstance(stamps, list) or not all(isinstance(stamp, str) for stamp in stamps)
                                or not isinstance(history, dict)
                                or any(len(counts) != len(stamps) for counts in history.values())):
                            raise ValueError("Expected timestamps and history in the dataset")
                    reader.finish()
                    return freeze_catalog(data) if filename == "scenarios.json.gz" else data
