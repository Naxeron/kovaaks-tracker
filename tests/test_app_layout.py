"""The launcher and packaged resources work outside the installation directory."""

from pathlib import Path
import runpy
from unittest.mock import Mock

import pytest

from kovaaks import app, logging_helpers


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_launcher_delegates_to_application(monkeypatch, tmp_path):
    main = Mock()
    monkeypatch.setattr(app, "main", main)
    monkeypatch.chdir(tmp_path)

    runpy.run_path(str(PROJECT_ROOT / "kovaaks_web.py"), run_name="__main__")

    main.assert_called_once_with()


def test_importing_launcher_does_not_start_application(monkeypatch):
    main = Mock()
    monkeypatch.setattr(app, "main", main)

    runpy.run_path(str(PROJECT_ROOT / "kovaaks_web.py"), run_name="launcher")

    main.assert_not_called()


def test_main_loads_packaged_web_assets_from_any_working_directory(monkeypatch, tmp_path):
    api = Mock()
    window = Mock()
    create_window = Mock(return_value=window)
    monkeypatch.setattr(app, "KovaaksAPI", Mock(return_value=api))
    monkeypatch.setattr(app.webview, "create_window", create_window)
    monkeypatch.setattr(app.webview, "start", Mock())
    monkeypatch.chdir(tmp_path)

    app.main()

    index = Path(create_window.call_args.args[1])
    assert index.is_absolute()
    assert index == PROJECT_ROOT / "kovaaks" / "web" / "index.html"
    assert index.is_file()
    assert (index.parent / "script.js").is_file()
    assert (index.parent / "style.css").is_file()
    api.set_window.assert_called_once_with(window)
    api.shutdown.assert_called_once_with()


def test_log_actions_use_configured_path_from_any_working_directory(monkeypatch, tmp_path):
    log_directory = tmp_path / "data"
    log_directory.mkdir()
    log_file = log_directory / "kovaaks.log"
    log_file.write_text("Application message\n", encoding="utf-8")
    unrelated_log = tmp_path / "kovaaks.log"
    unrelated_log.write_text("Unrelated file\n", encoding="utf-8")
    monkeypatch.setattr(logging_helpers, "LOG_FILE", str(log_file))
    monkeypatch.chdir(tmp_path)
    api = app.KovaaksAPI.__new__(app.KovaaksAPI)

    assert api.get_logs() == "Application message\n"
    assert api.clear_logs() is True
    assert log_file.read_text(encoding="utf-8") == ""
    assert unrelated_log.read_text(encoding="utf-8") == "Unrelated file\n"


@pytest.mark.parametrize("method,expected", [("get_logs", "No logs found."), ("clear_logs", False)])
def test_log_actions_handle_missing_configured_file(monkeypatch, tmp_path, method, expected):
    monkeypatch.setattr(logging_helpers, "LOG_FILE", str(tmp_path / "missing.log"))
    api = app.KovaaksAPI.__new__(app.KovaaksAPI)

    assert getattr(api, method)() == expected
