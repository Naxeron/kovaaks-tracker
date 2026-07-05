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
