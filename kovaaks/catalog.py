"""Immutable JSON containers for scenario catalogs shared by cache snapshots.

Catalogs are replaced as a whole after a download. Freezing their nested
containers once lets every checkpoint share metadata without sharing mutable
scores, histories, or local statistics. List/dict subclasses retain normal
JSON encoding, equality, indexing, and iteration behavior.
"""


def _immutable(*args, **kwargs):
    raise TypeError("Scenario catalog is immutable; replace the catalog instead")


class FrozenDict(dict):
    """A recursively frozen JSON object, safe to share across deep copies."""

    __slots__ = ()

    def __new__(cls, values=()):
        result = dict.__new__(cls)
        values = values if isinstance(values, dict) else dict(values)
        dict.__init__(result, ((key, freeze_json(value)) for key, value in values.items()))
        return result

    def __init__(self, values=()):
        # Construction happens in __new__; a repeated __init__ cannot mutate it.
        pass

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    __setitem__ = __delitem__ = __ior__ = _immutable
    clear = pop = popitem = setdefault = update = _immutable


class FrozenList(list):
    """A recursively frozen JSON array, retaining ordinary list compatibility."""

    __slots__ = ()

    def __new__(cls, values=()):
        result = list.__new__(cls)
        list.__init__(result, (freeze_json(value) for value in values))
        return result

    def __init__(self, values=()):
        # Construction happens in __new__; a repeated __init__ cannot mutate it.
        pass

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    __setitem__ = __delitem__ = __iadd__ = __imul__ = _immutable
    append = clear = extend = insert = pop = remove = reverse = sort = _immutable


def freeze_json(value):
    """Detach every mutable JSON container, reusing previously frozen values."""
    if isinstance(value, (FrozenDict, FrozenList)):
        return value
    if isinstance(value, dict):
        return FrozenDict(value)
    if isinstance(value, list):
        return FrozenList(value)
    if isinstance(value, tuple):
        return tuple(freeze_json(item) for item in value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError(f"Unsupported scenario catalog value: {type(value).__name__}")


def freeze_catalog(scenarios):
    """Freeze a full scenario list before publishing it to application readers."""
    if not isinstance(scenarios, list):
        raise ValueError("Scenario catalog must be a list")
    return freeze_json(scenarios)
