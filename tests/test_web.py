"""
Tests for Web UI API and play launcher.
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks_web import KovaaksAPI


class SyncThread:
    def __init__(self, target, args=(), kwargs=None, daemon=True):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
    def start(self):
        self.target(*self.args, **self.kwargs)


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_play_scenario(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()

    with patch("kovaaks.api.is_scenario_zombie", return_value=False), \
         patch("kovaaks.api.is_scenario_downloaded", return_value=True), \
         patch("threading.Thread", SyncThread), \
         patch("webbrowser.open") as mock_webbrowser_open:
        res = api.play_scenario("1wall 6targets TE")
        assert res is True
        mock_webbrowser_open.assert_called_once_with(
            "steam://run/824270/?action=jump-to-scenario;name=1wall%206targets%20TE"
        )


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_play_scenario_handles_exception(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()

    with patch("kovaaks.api.is_scenario_zombie", return_value=False), \
         patch("kovaaks.api.is_scenario_downloaded", return_value=True), \
         patch("threading.Thread", SyncThread), \
         patch("webbrowser.open", side_effect=Exception("Webbrowser error")):
        res = api.play_scenario("Pasu Voltaic Easy")
        assert res is True


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
def test_update_status(mock_polling, mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    mock_window = MagicMock()
    api.set_window(mock_window)

    api.update_status("Hello status")
    mock_window.evaluate_js.assert_called_with('if(window.setStatus) window.setStatus("Hello status")')


@patch("kovaaks_web.save_scores_cache")
@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("os.path.exists")
@patch("os.listdir")
@patch("threading.Thread")
def test_start_stats_polling(mock_thread, mock_listdir, mock_exists, mock_load_cache, mock_load_config, mock_save):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000, "stats_dir": "/fake/stats"}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}
    mock_exists.return_value = True
    mock_listdir.return_value = [
        "1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv",
        "some_other_file.txt",
    ]

    api = KovaaksAPI()
    mock_thread.reset_mock()
    api._start_stats_polling()

    assert "1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv" in api._known_stat_files
    mock_thread.assert_called_once()
    mock_save.assert_called()


@patch("kovaaks_web.save_scores_cache")
@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("os.path.exists")
@patch("os.listdir")
@patch("threading.Thread")
def test_start_stats_polling_with_new_files(
    mock_thread, mock_listdir, mock_exists, mock_load_cache, mock_load_config, mock_save
):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000, "stats_dir": "/fake/stats"}
    mock_load_cache.return_value = {
        "scenarios": [],
        "scores": {},
        "entry_history": {},
        "known_stat_files": ["1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv"]
    }
    mock_exists.return_value = True
    # Start with only the known file during initialization
    mock_listdir.return_value = [
        "1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv",
        "some_other_file.txt",
    ]

    api = KovaaksAPI()
    
    # Simulate a new file being added to the directory after initialization
    mock_listdir.return_value = [
        "1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv",
        "1w6ts Reload - Challenge - 2026.05.10-13.00.00 Stats.csv",
        "some_other_file.txt",
    ]
    
    mock_thread.reset_mock()
    mock_save.reset_mock()
    
    api._start_stats_polling()

    assert "1w6ts Reload - Challenge - 2026.05.10-13.00.00 Stats.csv" in api._known_stat_files
    assert "1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv" in api._known_stat_files
    
    # 1 for poll loop, 1 for _handle_new_stats_files
    assert mock_thread.call_count == 2
    mock_save.assert_called()



@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
@patch("time.sleep")
def test_handle_new_stats_files(mock_sleep, mock_polling, mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    mock_window = MagicMock()
    api.set_window(mock_window)

    api._handle_new_stats_files("/fake/stats", ["1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv"])
    mock_window.evaluate_js.assert_any_call("if(window.fetchData) window.fetchData()")
    mock_window.evaluate_js.assert_any_call('if (window.onLocalScoreDetected) window.onLocalScoreDetected("1w6ts Reload")')


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
@patch("time.sleep")
@patch("kovaaks.api.kovaaks_login")
@patch("kovaaks.api.kovaaks_get_friends_scores")
@patch("kovaaks.data_processing.parse_leaderboard_entries")
@patch("kovaaks_web.save_scores_cache")
def test_handle_new_stats_files_with_fetch(
    mock_save_cache, mock_parse, mock_get_scores, mock_login, mock_sleep,
    mock_polling, mock_load_cache, mock_load_config
):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000, "password": "password123"}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}
    mock_login.return_value = "fake_jwt"
    mock_get_scores.return_value = {"fake_data": True}
    mock_parse.return_value = ({"score": "123.4"}, [])

    api = KovaaksAPI()
    mock_window = MagicMock()
    api.set_window(mock_window)
    
    api._scenario_info = {"lid-1": {"name": "1w6ts Reload", "entries": 1000}}

    from unittest.mock import mock_open
    with patch("builtins.open", mock_open(read_data="Score:,123.4\n")):
        api._handle_new_stats_files("/fake/stats", ["1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv"])
        
        mock_login.assert_called_once_with("testuser", "password123")
        mock_save_cache.assert_called_once()
        mock_window.evaluate_js.assert_any_call("if(window.fetchData) window.fetchData()")


def test_style_css_selection():
    css_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web",
        "style.css"
    )
    assert os.path.exists(css_path)
    with open(css_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Verify user-select is explicitly enabled on .log-panel and #log-content
    assert ".log-panel" in content
    assert "#log-content" in content
    
    import re
    # Match `.log-panel { ... user-select: text; ... }`
    # and `#log-content { ... user-select: text; ... }`
    log_panel_block = re.search(r"\.log-panel\s*\{[^}]*\}", content)
    log_content_block = re.search(r"#log-content\s*\{[^}]*\}", content)
    
    assert log_panel_block is not None, "Could not find .log-panel rule in style.css"
    assert log_content_block is not None, "Could not find #log-content rule in style.css"
    
    assert "user-select: text" in log_panel_block.group(0)
    assert "-webkit-user-select: text" in log_panel_block.group(0)
    
    assert "user-select: text" in log_content_block.group(0)
    assert "-webkit-user-select: text" in log_content_block.group(0)


def test_web_ui_optimizations():
    # Verify style.css contains hardware acceleration properties on .table-container
    css_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web",
        "style.css"
    )
    assert os.path.exists(css_path)
    with open(css_path, "r", encoding="utf-8") as f:
        content_css = f.read()

    import re
    table_container_block = re.search(r"\.table-container\s*\{[^}]*\}", content_css)
    assert table_container_block is not None, "Could not find .table-container rule in style.css"
    
    assert "will-change: transform" in table_container_block.group(0)
    assert "transform: translate3d(0, 0, 0)" in table_container_block.group(0)
    assert "backface-visibility: hidden" in table_container_block.group(0)

    # Verify script.js contains throttled scroll listeners and smooth scroll interpolators
    js_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web",
        "script.js"
    )
    assert os.path.exists(js_path)
    with open(js_path, "r", encoding="utf-8") as f:
        content_js = f.read()

    assert "requestAnimationFrame" in content_js
    assert "lastScrollCheck" in content_js
    assert "resetScrollInterpolation" in content_js
    assert "targetScrollTop" in content_js
    assert "isAnimatingScroll" in content_js
    assert "animateScroll" in content_js

    # Verify kovaaks_web.py sets the environment variable and checks sys.argv for --gui
    py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "kovaaks_web.py"
    )
    assert os.path.exists(py_path)
    with open(py_path, "r", encoding="utf-8") as f:
        content_py = f.read()

    assert "WEBKIT_DISABLE_DMABUF_RENDERER" in content_py
    assert "--gui" in content_py
    assert "sys.argv" in content_py


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
@patch("time.sleep")
def test_autoplay_notification_fires_before_table_refresh(
    mock_sleep, mock_polling, mock_load_cache, mock_load_config
):
    """onLocalScoreDetected must fire before fetchData to minimize autoplay latency."""
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    mock_window = MagicMock()
    api.set_window(mock_window)

    api._handle_new_stats_files("/fake/stats", ["1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv"])

    calls = [str(c) for c in mock_window.evaluate_js.call_args_list]
    autoplay_idx = None
    fetch_idx = None
    for i, c in enumerate(calls):
        if "onLocalScoreDetected" in c and autoplay_idx is None:
            autoplay_idx = i
        if "fetchData" in c and fetch_idx is None:
            fetch_idx = i

    assert autoplay_idx is not None, "onLocalScoreDetected was not called"
    assert fetch_idx is not None, "fetchData was not called"
    assert autoplay_idx < fetch_idx, (
        f"onLocalScoreDetected (call #{autoplay_idx}) must fire before "
        f"fetchData (call #{fetch_idx}) for minimum autoplay latency"
    )


@patch("kovaaks_web.save_scores_cache")
@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
@patch("time.sleep")
@patch("kovaaks.api.kovaaks_login")
@patch("kovaaks.api.kovaaks_get_friends_scores")
@patch("kovaaks.data_processing.parse_leaderboard_entries")
def test_handle_new_stats_files_retries_when_user_entry_none(
    mock_parse, mock_get_scores, mock_login, mock_sleep,
    mock_polling, mock_load_cache, mock_load_config, mock_save_cache
):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000, "password": "password123"}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}
    mock_login.return_value = "fake_jwt"
    mock_get_scores.return_value = {"fake_data": True}
    
    # First attempt: parse returns user_entry = None (simulating API lag)
    # Second attempt: parse returns valid user_entry
    mock_parse.side_effect = [
        (None, []),
        ({"score": "123.4"}, []),
    ]

    api = KovaaksAPI()
    mock_window = MagicMock()
    api.set_window(mock_window)
    
    api._scenario_info = {"lid-1": {"name": "1w6ts Reload", "entries": 1000}}

    from unittest.mock import mock_open
    with patch("builtins.open", mock_open(read_data="Score:,123.4\n")):
        api._handle_new_stats_files("/fake/stats", ["1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv"])
        
        # Verify it logged in
        mock_login.assert_called_once_with("testuser", "password123")
        # Verify kovaaks_get_friends_scores was called twice (initial + 1 retry)
        assert mock_get_scores.call_count == 2
        # Verify cache saved successfully
        mock_save_cache.assert_called_once()


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_play_scenario_zombie_notifies_frontend(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {
        "scenarios": [],
        "scores": {},
        "entry_history": {},
        "zombies": ["pasuvoltaiceasy"]
    }

    api = KovaaksAPI()
    mock_window = MagicMock()
    api.set_window(mock_window)

    res = api.play_scenario("Pasu Voltaic Easy")
    assert res is True

    # Verify frontend is notified about the zombie scenario
    calls = [c[0][0] for c in mock_window.evaluate_js.call_args_list]
    assert any("onZombieDetected" in call and "Pasu Voltaic Easy" in call for call in calls)


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_play_scenario_bg_zombie_notifies_frontend(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {
        "scenarios": [],
        "scores": {},
        "entry_history": {},
        "zombies": []
    }

    api = KovaaksAPI()
    mock_window = MagicMock()
    api.set_window(mock_window)

    # Mock is_scenario_zombie to return True (simulating a discovered zombie in the bg check)
    with patch("kovaaks.api.is_scenario_zombie", return_value=True), \
         patch("threading.Thread", SyncThread), \
         patch("webbrowser.open") as mock_webbrowser_open, \
         patch("kovaaks_web.save_scores_cache") as mock_save:
        res = api.play_scenario("Pasu Voltaic Easy")
        assert res is True
        mock_webbrowser_open.assert_called_once()
        
        # Verify frontend was notified that the zombie was detected
        calls = [c[0][0] for c in mock_window.evaluate_js.call_args_list]
        assert any("onZombieDetected" in call and "Pasu Voltaic Easy" in call for call in calls)
        assert any("fetchData" in call for call in calls)


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_zombie_scenarios_included_in_rebuild_data(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {
        "scenarios": [
            {
                "leaderboardId": "lid-zombie",
                "scenarioName": "Zombie Scenario",
                "counts": {"entries": 1500},
                "scenario": {"aimType": "Tracking"}
            },
            {
                "leaderboardId": "lid-normal",
                "scenarioName": "Normal Scenario",
                "counts": {"entries": 1200},
                "scenario": {"aimType": "Tracking"}
            }
        ],
        "scores": {},
        "entry_history": {},
        "zombies": ["zombiescenario"]
    }

    api = KovaaksAPI()
    
    data = api.get_data(min_entries=1000, show_hidden=False)
    
    assert "zombies" in data
    assert "Zombie Scenario" in data["zombies"]
    
    row_names = [r[0] for r in data["rows"]]
    assert "Zombie Scenario" in row_names
    assert "Normal Scenario" in row_names


def test_log_panel_ui_elements():
    # Verify index.html contains log-context-menu and its items
    html_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web",
        "index.html"
    )
    assert os.path.exists(html_path)
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    
    assert 'id="log-context-menu"' in html_content
    assert 'id="menu-log-copy"' in html_content
    assert 'id="menu-log-selectall"' in html_content

    # Verify script.js contains the context menu logic, mousedown state tracking and the isSelectingLog helper
    js_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web",
        "script.js"
    )
    assert os.path.exists(js_path)
    with open(js_path, "r", encoding="utf-8") as f:
        js_content = f.read()

    assert "isMouseDownOnLogs" in js_content
    assert "isSelectingLog" in js_content
    assert "logContextMenu" in js_content
    assert "menu-log-copy" in js_content
    assert "menu-log-selectall" in js_content


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_get_clipboard_linux(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    
    with patch("sys.platform", "linux"), \
         patch("subprocess.check_output", return_value="pasted_text_linux") as mock_check_output:
        val = api.get_clipboard()
        assert val == "pasted_text_linux"
        mock_check_output.assert_called_with(["xclip", "-selection", "clipboard", "-o"], text=True)


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_get_clipboard_darwin(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    
    with patch("sys.platform", "darwin"), \
         patch("subprocess.check_output", return_value="pasted_text_mac") as mock_check_output:
        val = api.get_clipboard()
        assert val == "pasted_text_mac"
        mock_check_output.assert_called_once_with(["pbpaste"], text=True)


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_get_clipboard_windows(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    
    mock_ctypes = MagicMock()
    mock_ctypes.windll.user32.OpenClipboard.return_value = True
    mock_ctypes.windll.user32.GetClipboardData.return_value = 12345
    mock_ctypes.windll.kernel32.GlobalLock.return_value = 67890
    mock_ctypes.c_wchar_p.return_value.value = "pasted_text_windows"
    mock_ctypes.windll.kernel32.GlobalUnlock.return_value = True
    mock_ctypes.windll.user32.CloseClipboard.return_value = True
    
    with patch("sys.platform", "win32"), \
         patch.dict("sys.modules", {"ctypes": mock_ctypes}):
        val = api.get_clipboard()
        assert val == "pasted_text_windows"


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_get_clipboard_tkinter_fallback(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    
    mock_tk = MagicMock()
    mock_root = MagicMock()
    mock_tk.Tk.return_value = mock_root
    mock_root.clipboard_get.return_value = "pasted_text_tkinter"
    
    with patch("sys.platform", "unknown_os"), \
         patch.dict("sys.modules", {"tkinter": mock_tk}):
        val = api.get_clipboard()
        assert val == "pasted_text_tkinter"
        mock_tk.Tk.assert_called_once()
        mock_root.clipboard_get.assert_called_once()
        mock_root.destroy.assert_called_once()


def test_login_modal_enter_and_password_toggle():
    # Verify index.html contains show password toggles
    html_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web",
        "index.html"
    )
    assert os.path.exists(html_path)
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    assert 'id="login-show-password"' in html_content
    assert 'id="settings-show-password"' in html_content

    # Verify script.js contains the keydown/enter key and toggle change listener logics
    js_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web",
        "script.js"
    )
    assert os.path.exists(js_path)
    with open(js_path, "r", encoding="utf-8") as f:
        js_content = f.read()

    assert "login-show-password" in js_content
    assert "settings-show-password" in js_content
    assert "triggerLoginSubmit" in js_content
    assert "Enter" in js_content


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
def test_fetch_all_stats_prevents_overlapping_fetches(mock_polling, mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    
    # Simulate fetch already in progress
    api._fetch_in_progress = True
    
    # Try calling fetch_all_stats
    with patch("threading.Thread") as mock_thread:
        res = api.fetch_all_stats(silent=True)
        assert res is False
        mock_thread.assert_not_called()

    # Reset in progress
    api._fetch_in_progress = False
    with patch("threading.Thread") as mock_thread:
        res = api.fetch_all_stats(silent=True)
        assert res is True
        mock_thread.assert_called_once()


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
def test_rebuild_data_and_finish_passes_silent(mock_polling, mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    mock_window = MagicMock()
    api.set_window(mock_window)
    
    api._rebuild_data_and_finish(errors=0, silent=True)
    mock_window.evaluate_js.assert_called_with("fetchData(true)")
    
    mock_window.reset_mock()
    api._rebuild_data_and_finish(errors=0, silent=False)
    mock_window.evaluate_js.assert_called_with("fetchData(false)")


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
def test_is_fetch_in_progress_and_cancel_fetch(mock_polling, mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    assert api.is_fetch_in_progress() is False
    
    api._fetch_in_progress = True
    assert api.is_fetch_in_progress() is True
    
    # Cancel fetch when in progress should return True
    assert api.cancel_fetch() is True
    assert api._fetch_cancelled is True
    
    # Cancel fetch when not in progress should return False
    api._fetch_in_progress = False
    assert api.cancel_fetch() is False


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
def test_fetch_all_stats_sets_progress_flag_synchronously(mock_polling, mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    assert api.is_fetch_in_progress() is False
    
    with patch("threading.Thread") as mock_thread:
        res = api.fetch_all_stats(silent=True)
        assert res is True
        assert api.is_fetch_in_progress() is True
        mock_thread.assert_called_once()


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
@patch("kovaaks_web.KovaaksAPI._start_stats_polling")
def test_rebuild_data_and_cancelled_passes_silent(mock_polling, mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()
    mock_window = MagicMock()
    api.set_window(mock_window)
    
    api._rebuild_data_and_cancelled(silent=True)
    mock_window.evaluate_js.assert_called_with("fetchData(true)")
    
    mock_window.reset_mock()
    api._rebuild_data_and_cancelled(silent=False)
    mock_window.evaluate_js.assert_called_with("fetchData(false)")


def test_auto_refresh_timer_prevents_redundant_resets():
    js_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web",
        "script.js"
    )
    assert os.path.exists(js_path)
    with open(js_path, "r", encoding="utf-8") as f:
        js_content = f.read()

    # The tracker object should exist
    assert "currentAutoRefreshState" in js_content
    # The setupAutoRefresh function should check if the active state equals the config values
    assert "currentAutoRefreshState.enabled === enabled" in js_content
    assert "currentAutoRefreshState.interval === interval" in js_content




