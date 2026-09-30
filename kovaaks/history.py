"""Compact history mappings with shared timestamps and copy-on-write counts."""

from collections.abc import ItemsView, MutableMapping, ValuesView
from copy import deepcopy
import struct
import threading
from weakref import WeakValueDictionary


_COUNT = struct.Struct("<q")
_MIN_COUNT = -(1 << 63)
_MAX_COUNT = (1 << 63) - 1
_AXES = WeakValueDictionary()
_AXES_LOCK = threading.Lock()


class _TimestampAxis:
    """One immutable timestamp ordering and lookup index shared by many rows."""

    __slots__ = ("timestamps", "index", "__weakref__")

    def __init__(self, timestamps):
        self.timestamps = timestamps
        self.index = {stamp: index for index, stamp in enumerate(timestamps)}


def _axis_for(timestamps):
    timestamps = tuple(timestamps)
    with _AXES_LOCK:
        axis = _AXES.get(timestamps)
        if axis is None:
            axis = _TimestampAxis(timestamps)
            _AXES[timestamps] = axis
        return axis


def _can_pack_count(value):
    # Preserve booleans, floats, oversized integers, and malformed values exactly
    # instead of silently changing their type or truncating them during packing.
    return type(value) is int and _MIN_COUNT <= value <= _MAX_COUNT


class _HistoryItemsView(ItemsView):
    def __iter__(self):
        history = self._mapping
        if history._fallback is not None:
            yield from history._fallback.items()
        else:
            yield from zip(history._axis.timestamps,
                           (value[0] for value in struct.iter_unpack("<q", history._counts)))


class _HistoryValuesView(ValuesView):
    def __iter__(self):
        history = self._mapping
        if history._fallback is not None:
            yield from history._fallback.values()
        else:
            yield from (value[0] for value in struct.iter_unpack("<q", history._counts))


class CompactHistory(MutableMapping):
    """Expose a normal mutable mapping while storing integer counts in bytes.

    Histories with the same timestamp ordering share one axis and its index.
    Counts use signed 64-bit little-endian storage. Mutations replace immutable
    buffers, so a deep-copied snapshot shares those buffers safely until either
    mapping changes. Unsupported keys or values use an ordinary dict fallback
    without data loss. Callers synchronize concurrent access to each mapping.

    ``timestamps`` and ``packed_counts`` expose the immutable compact encoding;
    ``is_compact`` is False for fallback mappings, whose packed_counts is None.
    """

    __slots__ = ("_axis", "_counts", "_fallback", "__weakref__")

    def __init__(self, mapping=(), **kwargs):
        if isinstance(mapping, CompactHistory) and not kwargs:
            self._axis = mapping._axis
            self._counts = mapping._counts
            self._fallback = mapping._fallback.copy() if mapping._fallback is not None else None
            return
        data = dict(mapping, **kwargs)
        self._initialize(tuple(data), tuple(data.values()))

    def _initialize(self, timestamps, counts):
        if (all(type(stamp) is str for stamp in timestamps)
                and len(set(timestamps)) == len(timestamps)
                and all(_can_pack_count(value) for value in counts)):
            self._axis = _axis_for(timestamps)
            self._counts = struct.pack(f"<{len(counts)}q", *counts)
            self._fallback = None
        else:
            self._axis = None
            self._counts = b""
            self._fallback = dict(zip(timestamps, counts))

    @classmethod
    def from_counts(cls, timestamps, counts):
        """Build from parallel arrays, rejecting a mismatched sample count."""
        timestamps, counts = tuple(timestamps), tuple(counts)
        if len(timestamps) != len(counts):
            raise ValueError("History timestamps and counts must have equal lengths")
        result = cls.__new__(cls)
        result._initialize(timestamps, counts)
        return result

    @classmethod
    def from_packed(cls, timestamps, packed_counts):
        """Restore immutable count bytes without allocating individual integers."""
        timestamps = tuple(timestamps)
        if (any(type(stamp) is not str for stamp in timestamps)
                or len(set(timestamps)) != len(timestamps)):
            raise ValueError("Packed history timestamps must be unique strings")
        if not isinstance(packed_counts, (bytes, bytearray, memoryview)):
            raise ValueError("Packed history counts must be bytes")
        packed_counts = bytes(packed_counts)
        if len(packed_counts) != _COUNT.size * len(timestamps):
            raise ValueError("Packed history counts must contain one int64 per timestamp")
        result = cls.__new__(cls)
        result._axis = _axis_for(timestamps)
        result._counts = packed_counts
        result._fallback = None
        return result

    @property
    def is_compact(self):
        return self._fallback is None

    @property
    def timestamps(self):
        return self._axis.timestamps if self.is_compact else tuple(self._fallback)

    @property
    def packed_counts(self):
        return self._counts if self.is_compact else None

    def __getitem__(self, key):
        if self._fallback is not None:
            return self._fallback[key]
        index = self._axis.index[key]
        return _COUNT.unpack_from(self._counts, index * _COUNT.size)[0]

    def __setitem__(self, key, value):
        if self._fallback is not None:
            self._fallback[key] = value
            return
        if type(key) is not str or not _can_pack_count(value):
            data = dict(self.items())
            data[key] = value
            self._fallback, self._axis, self._counts = data, None, b""
            return
        index = self._axis.index.get(key)
        packed = _COUNT.pack(value)
        if index is None:
            self._axis = _axis_for((*self._axis.timestamps, key))
            self._counts += packed
        else:
            offset = index * _COUNT.size
            if self._counts[offset:offset + _COUNT.size] != packed:
                self._counts = self._counts[:offset] + packed + self._counts[offset + _COUNT.size:]

    def __delitem__(self, key):
        if self._fallback is not None:
            del self._fallback[key]
            return
        index = self._axis.index[key]
        timestamps = self._axis.timestamps
        self._axis = _axis_for(timestamps[:index] + timestamps[index + 1:])
        offset = index * _COUNT.size
        self._counts = self._counts[:offset] + self._counts[offset + _COUNT.size:]

    def __iter__(self):
        return iter(self._axis.timestamps if self.is_compact else self._fallback)

    def __len__(self):
        return len(self._axis.timestamps if self.is_compact else self._fallback)

    def items(self):
        return _HistoryItemsView(self)

    def values(self):
        return _HistoryValuesView(self)

    def update(self, *args, **kwargs):
        """Merge a batch with one packing pass instead of rebuilding per sample."""
        incoming = dict(*args, **kwargs)
        if not incoming:
            return
        if self._fallback is not None:
            self._fallback.update(incoming)
            return
        if all(type(key) is str and _can_pack_count(value)
               and key in self._axis.index and self[key] == value
               for key, value in incoming.items()):
            return
        data = dict(self.items())
        data.update(incoming)
        self._initialize(tuple(data), tuple(data.values()))

    def copy(self):
        """Return an independent mapping sharing only immutable compact data."""
        return type(self)(self)

    def __copy__(self):
        return self.copy()

    def __deepcopy__(self, memo):
        result = type(self).__new__(type(self))
        memo[id(self)] = result
        result._axis, result._counts = self._axis, self._counts
        result._fallback = deepcopy(self._fallback, memo) if self._fallback is not None else None
        return result

    def __repr__(self):
        return f"{type(self).__name__}({dict(self.items())!r})"
