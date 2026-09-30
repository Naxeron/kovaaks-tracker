"""Credential storage tests use only fake backends, never a system keychain."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import traceback

import pytest

from kovaaks import credentials


class MemoryBackend:
    """Minimal fake implementing the keyring password API."""

    priority = 5

    def __init__(self):
        self.passwords = {}
        self.calls = []

    def get_password(self, service, username):
        self.calls.append(("get", service, username))
        return self.passwords.get((service, username))

    def set_password(self, service, username, password):
        self.calls.append(("set", service, username))
        self.passwords[service, username] = password

    def delete_password(self, service, username):
        self.calls.append(("delete", service, username))
        del self.passwords[service, username]


@pytest.fixture
def backend(monkeypatch):
    fake = MemoryBackend()
    monkeypatch.setattr(credentials, "_get_backend", lambda: fake)
    return fake


@pytest.fixture
def fake_keyring(monkeypatch):
    """Install a fake import boundary including canonical class identities."""
    modules = {}
    selection = SimpleNamespace(backend=None)
    modules["keyring"] = SimpleNamespace(get_keyring=lambda: selection.backend)

    def fake_import(name):
        if name not in modules:
            raise ImportError("No real credential-store imports are allowed")
        return modules[name]

    monkeypatch.setattr(credentials.importlib, "import_module", fake_import)

    def make_backend(module_name, class_name, **attributes):
        backend_type = type(class_name, (MemoryBackend,), {
            "__module__": module_name, **attributes,
        })
        module = modules.setdefault(module_name, SimpleNamespace())
        setattr(module, class_name, backend_type)
        return backend_type()

    return selection, modules, make_backend


def test_import_does_not_initialize_keyring(monkeypatch):
    def forbidden_import(name):
        pytest.fail("Importing credentials must not initialize a credential store")

    monkeypatch.setattr(credentials.importlib, "import_module", forbidden_import)
    spec = importlib.util.spec_from_file_location(
        "isolated_credentials", Path(credentials.__file__),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SERVICE == "KovaaKsTracker"


@pytest.mark.parametrize("module_name,class_name", sorted(credentials._NATIVE_BACKENDS))
def test_accepts_supported_native_backends(fake_keyring, module_name, class_name):
    selection, _, make_backend = fake_keyring
    selection.backend = make_backend(module_name, class_name)
    assert credentials._get_backend() is selection.backend


@pytest.mark.parametrize("module_name,class_name", [
    ("keyring.backends.null", "Keyring"),
    ("keyring.backends.fail", "Keyring"),
    ("keyrings.alt.file", "PlaintextKeyring"),
    ("keyrings.alt.file", "EncryptedKeyring"),
    ("custom_backend", "Keyring"),
])
def test_rejects_unsupported_backends_even_with_high_priority(fake_keyring, module_name, class_name):
    selection, _, make_backend = fake_keyring
    selection.backend = make_backend(module_name, class_name, priority=100)
    with pytest.raises(credentials.CredentialStorageError):
        credentials._get_backend()
    assert selection.backend.calls == []


def test_rejects_subclass_of_native_backend(fake_keyring):
    selection, _, make_backend = fake_keyring
    native = make_backend("keyring.backends.SecretService", "Keyring")
    selection.backend = type("CustomKeyring", (type(native),), {})()
    with pytest.raises(credentials.CredentialStorageError):
        credentials._get_backend()


def test_rejects_class_impersonating_a_builtin_name(fake_keyring):
    selection, _, make_backend = fake_keyring
    make_backend("keyring.backends.SecretService", "Keyring")
    selection.backend = type("Keyring", (MemoryBackend,), {
        "__module__": "keyring.backends.SecretService",
    })()
    with pytest.raises(credentials.CredentialStorageError):
        credentials._get_backend()


def test_chainer_selects_native_child_and_never_uses_plaintext(fake_keyring):
    selection, _, make_backend = fake_keyring
    plaintext = make_backend("keyrings.alt.file", "PlaintextKeyring", priority=100)
    native = make_backend("keyring.backends.SecretService", "Keyring")
    other_native = make_backend("keyring.backends.kwallet", "DBusKeyring")
    selection.backend = make_backend(
        "keyring.backends.chainer", "ChainerBackend",
        backends=[plaintext, native, other_native],
    )

    credentials.set_password("user", "test-password")
    assert credentials.get_password("user") == "test-password"
    credentials.delete_password("user")

    assert plaintext.calls == other_native.calls == selection.backend.calls == []
    assert native.passwords == {}


def test_chainer_without_native_children_is_rejected(fake_keyring):
    selection, _, make_backend = fake_keyring
    selection.backend = make_backend(
        "keyring.backends.chainer", "ChainerBackend",
        backends=[make_backend("keyrings.alt.file", "PlaintextKeyring")],
    )
    with pytest.raises(credentials.CredentialStorageError):
        credentials._get_backend()


def test_missing_keyring_library_is_safe(fake_keyring):
    _, modules, _ = fake_keyring
    del modules["keyring"]
    with pytest.raises(credentials.CredentialStorageError) as error:
        credentials.get_password("user")
    assert "unavailable or locked" in str(error.value)


def test_backend_discovery_error_is_safe(fake_keyring):
    selection, _, make_backend = fake_keyring

    def locked_priority(self):
        raise RuntimeError("secret-discovery-details")

    selection.backend = make_backend(
        "keyring.backends.SecretService", "Keyring", priority=property(locked_priority),
    )
    with pytest.raises(credentials.CredentialStorageError) as error:
        credentials._get_backend()
    assert "secret-discovery-details" not in "".join(traceback.format_exception(error.value))


def test_roundtrip_verifies_write_and_isolates_usernames(backend):
    credentials.set_password("first", " secret with spaces ")
    credentials.set_password("second", "other-password")
    assert backend.calls[:2] == [
        ("set", "KovaaKsTracker", "first"), ("get", "KovaaKsTracker", "first"),
    ]
    assert credentials.get_password("first") == " secret with spaces "
    assert credentials.get_password("second") == "other-password"
    assert credentials.get_password("missing") is None
    credentials.delete_password("first")
    assert credentials.get_password("first") is None
    assert credentials.get_password("second") == "other-password"


def test_silent_write_failure_does_not_report_success(backend, monkeypatch):
    monkeypatch.setattr(backend, "set_password", lambda *args: None)
    with pytest.raises(credentials.CredentialStorageError):
        credentials.set_password("user", "password")


@pytest.mark.parametrize("operation,backend_method", [
    (lambda: credentials.get_password("user"), "get_password"),
    (lambda: credentials.set_password("user", "password"), "set_password"),
    (lambda: credentials.set_password("user", "password"), "get_password"),
    (lambda: credentials.delete_password("user"), "delete_password"),
])
def test_backend_errors_never_expose_secret_details(backend, monkeypatch, caplog, operation, backend_method):
    backend.passwords[credentials.SERVICE, "user"] = "password"

    def fail(*args):
        raise RuntimeError("backend-error-including-plaintext-password")

    monkeypatch.setattr(backend, backend_method, fail)
    with pytest.raises(credentials.CredentialStorageError) as error:
        operation()

    rendered = "".join(traceback.format_exception(error.value))
    assert "backend-error-including-plaintext-password" not in str(error.value)
    assert "backend-error-including-plaintext-password" not in rendered
    assert "backend-error-including-plaintext-password" not in caplog.text


def test_invalid_backend_read_is_rejected(backend):
    backend.passwords[credentials.SERVICE, "user"] = object()
    with pytest.raises(credentials.CredentialStorageError):
        credentials.get_password("user")


def test_delete_missing_password_is_idempotent(backend):
    credentials.delete_password("missing")
    credentials.delete_password("missing")
    assert all(call[0] == "get" for call in backend.calls)


def test_delete_silent_failure_does_not_report_success(backend, monkeypatch):
    backend.passwords[credentials.SERVICE, "user"] = "password"
    monkeypatch.setattr(backend, "delete_password", lambda *args: None)
    with pytest.raises(credentials.CredentialStorageError):
        credentials.delete_password("user")


def test_delete_race_succeeds_only_after_confirming_absence(backend, monkeypatch):
    backend.passwords[credentials.SERVICE, "user"] = "password"

    def removed_elsewhere(service, username):
        del backend.passwords[service, username]
        raise RuntimeError("entry no longer exists")

    monkeypatch.setattr(backend, "delete_password", removed_elsewhere)
    credentials.delete_password("user")
    assert credentials.get_password("user") is None


@pytest.mark.parametrize("username,password", [
    ("", "password"), (None, "password"), ("user", ""), ("user", None),
])
def test_invalid_credentials_do_not_touch_backend(backend, username, password):
    with pytest.raises(credentials.CredentialStorageError):
        credentials.set_password(username, password)
    assert backend.calls == []


def test_empty_username_does_not_access_store(backend):
    assert credentials.get_password("") is None
    credentials.delete_password("")
    assert backend.calls == []
