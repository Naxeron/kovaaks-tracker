"""
Tests for Web UI API and play launcher.
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks_web import KovaaksAPI


@patch("kovaaks_web.load_config")
@patch("kovaaks_web.load_scores_cache")
def test_play_scenario(mock_load_cache, mock_load_config):
    mock_load_config.return_value = {"username": "testuser", "min_entries": 1000}
    mock_load_cache.return_value = {"scenarios": [], "scores": {}, "entry_history": {}}

    api = KovaaksAPI()

    with patch("webbrowser.open") as mock_webbrowser_open:
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

    with patch("webbrowser.open", side_effect=Exception("Webbrowser error")):
        res = api.play_scenario("Pasu Voltaic Easy")
        assert res is False


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


@patch("kovaaks.cache.save_scores_cache")
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


@patch("kovaaks.cache.save_scores_cache")
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
    mock_listdir.return_value = [
        "1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv",
        "1w6ts Reload - Challenge - 2026.05.10-13.00.00 Stats.csv",
        "some_other_file.txt",
    ]

    api = KovaaksAPI()
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
@patch("kovaaks.cache.save_scores_cache")
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



    



