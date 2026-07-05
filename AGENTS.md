# AI Assistant Guidelines for KovaaKs Tracker

Assist developing KovaaKs Tracker. Follow guidelines.

## 1. Testing is Mandatory
Implement or update automated tests for features, business logic, bugs.
* **Regression Tests**: Verify bug fix, prevent regression.
* **Unit/Integration Tests**: Cover core computations, edge cases, error handling.
* **Test Location**: Put Python tests under `tests/` directory, filename prefix `test_`.
* **Verification**: Run `pytest` before completion. Ensure tests pass. `pytest` only scans `tests/` via `pytest.ini` for headless, fast runs. Do not import `tkinter` or instantiate GUI widgets at module level in tests.

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
* **Avoid Redundant Disk Saves:** Set a dirty flag when cache data is modified, and only perform disk writes (like slow gzip saves) if changes have actually occurred.

