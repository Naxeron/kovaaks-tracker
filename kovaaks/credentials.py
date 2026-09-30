"""Store login passwords in a supported native operating-system credential store.

Backend discovery is deliberately lazy: importing this module never opens a
keychain or prompts the user. Callers should perform operations off the GUI
thread because the operating system may ask the user to unlock its store.
"""

import importlib


SERVICE = "KovaaKsTracker"

_NATIVE_BACKENDS = {
    ("keyring.backends.macOS", "Keyring"),
    ("keyring.backends.Windows", "WinVaultKeyring"),
    ("keyring.backends.SecretService", "Keyring"),
    ("keyring.backends.kwallet", "DBusKeyring"),
    ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
}
_CHAINER_BACKEND = ("keyring.backends.chainer", "ChainerBackend")
_UNAVAILABLE_MESSAGE = (
    "Secure password storage is unavailable or locked. Unlock your system "
    "credential store and try again, or use this session only."
)
_READ_MESSAGE = "Could not access the saved password. Unlock your system credential store and try again."
_SAVE_MESSAGE = "Could not securely save the password. Unlock your system credential store and try again."
_DELETE_MESSAGE = "Could not remove the saved password. Unlock your system credential store and try again."


class CredentialStorageError(Exception):
    """A credential-store failure with a fixed, safe user-facing message."""


def _is_builtin(backend, allowed):
    """Accept exact built-in classes, excluding third-party subclasses."""
    backend_type = type(backend)
    identity = (backend_type.__module__, backend_type.__name__)
    if identity not in allowed:
        return False
    module = importlib.import_module(identity[0])
    return getattr(module, identity[1], None) is backend_type


def _get_backend():
    """Select one native backend without delegating to unsafe chain fallbacks.

    Keyring's default chainer can contain third-party plaintext stores. Choose
    its first supported native child directly so reads, writes, and deletions
    consistently use that store. A locked store is an error, never a reason to
    fall through to another backend or a local file.
    """
    try:
        keyring = importlib.import_module("keyring")
        selected = keyring.get_keyring()
        if _is_builtin(selected, {_CHAINER_BACKEND}):
            candidates = selected.backends
        else:
            candidates = (selected,)
        for candidate in candidates:
            if _is_builtin(candidate, _NATIVE_BACKENDS) and candidate.priority > 0:
                return candidate
    except Exception:
        # Backend exceptions can contain credentials or other private details.
        raise CredentialStorageError(_UNAVAILABLE_MESSAGE) from None
    raise CredentialStorageError(_UNAVAILABLE_MESSAGE)


def get_password(username):
    """Return the saved password or None, raising on an inaccessible store."""
    if not isinstance(username, str) or not username:
        return None
    backend = _get_backend()
    try:
        password = backend.get_password(SERVICE, username)
        if password is not None and not isinstance(password, str):
            raise CredentialStorageError(_READ_MESSAGE)
        return password
    except Exception:
        raise CredentialStorageError(_READ_MESSAGE) from None


def set_password(username, password):
    """Save and verify a password before a caller removes any legacy copy."""
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise CredentialStorageError("A username and password are required to save credentials.")
    backend = _get_backend()
    try:
        backend.set_password(SERVICE, username, password)
        if backend.get_password(SERVICE, username) != password:
            raise CredentialStorageError(_SAVE_MESSAGE)
    except Exception:
        raise CredentialStorageError(_SAVE_MESSAGE) from None


def delete_password(username):
    """Remove a saved password, succeeding when the entry is already absent."""
    if not isinstance(username, str) or not username:
        return
    backend = _get_backend()
    try:
        if backend.get_password(SERVICE, username) is None:
            return
        try:
            backend.delete_password(SERVICE, username)
        except Exception:
            # Another process may have removed the entry after our first read.
            # Do not suppress an error unless absence can be verified.
            if backend.get_password(SERVICE, username) is None:
                return
            raise
        if backend.get_password(SERVICE, username) is not None:
            raise CredentialStorageError(_DELETE_MESSAGE)
    except Exception:
        raise CredentialStorageError(_DELETE_MESSAGE) from None
