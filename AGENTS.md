# AI Assistant Guidelines for KovaaKs Tracker

Assist developing KovaaKs Tracker. Follow guidelines.

## 1. Testing is Mandatory
Implement or update automated tests for features, business logic, bugs.
* **Regression Tests**: Verify bug fix, prevent regression.
* **Unit/Integration Tests**: Cover core computations, edge cases, error handling.
* **Test Location**: Put Python tests under `tests/` directory, filename prefix `test_`.
* **Verification**: Run `pytest` before completion. Ensure tests pass. `pytest` only scans `tests/` via `pytest.ini` for headless, fast runs. Do not import `tkinter` or instantiate GUI widgets at module level in tests.
* **Test Isolation**: Ensure tests never read or write production files. Any tests interacting with caches, configs, or stats must patch paths (such as `SCORES_CACHE` and `CONFIG_PATH`) to temporary test directories (e.g. `tmp_path`) to prevent side-effects on production data.

## 2. Code Quality & Idiomatic Python
* Write clean Python. Prefer list comprehensions, generator expressions, built-ins over loops.
* Use safe type casting (`safe_int`, `safe_float` in `data_processing.py`). Prevent uncaught errors.
* Maintain docstrings, comments. Do not delete existing comments.

## 3. UI and Concurrency Safety
* Keep main GUI thread responsive. Run network requests, API queries, file parsing in background or daemon threads.
* Wrap background operations in `try-finally`. Prevent threads hanging or failing silently.
* Update UI state (status/progress bars, labels) on all execution paths.
* Avoid importing/instantiating heavy GUI toolkits (e.g. `tkinter`) for headless-friendly utilities like clipboard access. Prefer platform-specific native commands (`pbpaste`, `xclip`, `xsel`) or Windows APIs (`ctypes`) first, falling back to Tkinter only as a final backup.
* Under `pywebview`, threads can evaluate JS directly via `window.evaluate_js` without custom GUI thread-marshalling wrappers. Do not introduce redundant GUI-thread dispatching helpers.

## 4. Startup Performance & Caching
* **Deferred / Asynchronous Initialization:** In GUI applications, perform heavy initialization tasks (such as compressed cache loading, parsing local files, etc.) in a background thread to allow the GUI window to render instantly (<50ms). Use thread-safe locks/events (`threading.Event`) to synchronize API calls and block UI interaction until the background thread completes.
* **Test Environment Synchronization:** When running under unit tests (detected via `"pytest" in sys.modules`), bypass background loading and run initialization synchronously. This prevents race conditions with mocks and assertions in the test suite.
* **Incremental File Parsing:** Do not scan or read large lists of historical files from disk on every startup. Cache parsed file statistics in the main JSON/gzip cache. Compare the active directory listing to cached files and incrementally parse only newly added files.
* **Dynamic Timestamp Parsing:** Extract timestamps from file/log names when checking temporal conditions (such as runs in the last 24 hours) to avoid opening or reading the file contents.
* **Avoid Redundant Disk Saves**: Set a dirty flag when cache data is modified, and only perform disk writes (like slow gzip saves) if changes have actually occurred.
* **Atomic Save Safety**: When performing atomic file saves (e.g., writing to a `.tmp` file and using `os.replace`), use process-unique temporary filenames (e.g., appending the current process PID) to prevent concurrent processes or test runs from colluding and corrupting the temporary file.
* **Corrupt Load Safeguards**: If the cache file exists but fails to load on startup (due to temporary locks or corruption), set a flag to prevent the application from writing back or saving an empty cache over it, keeping the file contents intact for recovery.


## 5. API Data Quirks & Accuracy
* **Entry Counts**: Do NOT rely on the `counts.entries` field from the `/scenario/popular` KovaaKs API endpoint, as it often returns heavily inflated/inaccurate numbers (e.g., tracking total plays or bot spam). To get the actual unique player count for a scenario, you MUST query the `/leaderboard/scores/global` endpoint (via `get_accurate_entry_count`). Do not optimize away these fetches even if the popular endpoint provides a count.
* **Pagination Warning**: Because the `/scenario/popular` API endpoint natively sorts by these *inflated* counts, you must evaluate any pagination early-exit conditions (e.g., `max_on_page < entries_limit`) using the **original inflated numbers** *before* overwriting them with the accurate ones. Evaluating limits on the accurate counts will prematurely abort the pagination loop.
