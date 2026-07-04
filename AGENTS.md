# AI Assistant Guidelines for KovaaKs Tracker

You are assisting in developing the KovaaKs Tracker project. Follow these guidelines at all times.

## 1. Testing is Mandatory
Whenever you implement a new feature, modify business logic, or fix a bug, you **must** implement or update corresponding automated tests.
* **Regression Tests**: For bug fixes, write a test that verifies the failure condition is resolved and does not regress.
* **Unit/Integration Tests**: For features, write tests covering core computations, edge cases, and error handling.
* **Test Location**: Put python tests under the `tests/` directory with the filename prefix `test_`.
* **Verification**: Before completing any task, run the test suite via `pytest` to ensure all tests pass (no regressions allowed). Note that `pytest` is configured via `pytest.ini` to only scan the `tests/` directory. This prevents scratch/development files from accidentally loading Tkinter or launching visual GUI windows, keeping the test run headless, focus-uninterrupting, and extremely fast (completing in under 0.2 seconds). Avoid importing `tkinter` or instantiating GUI widgets at the module level in any test code.

## 2. Code Quality & Idiomatic Python
* Write idiomatic, clean Python code. Prefer list comprehensions, generator expressions, and Python built-ins over verbose loops.
* Use safe type casting helper functions (like `safe_int` and `safe_float` in `data_processing.py`) to prevent uncaught value/type errors.
* Maintain clean docstrings and comments. Do not delete existing comments unless they are obsolete or directly contradicted by the changes.

## 3. UI and Concurrency Safety
* Always keep the main GUI thread responsive. Perform network requests, API queries, or file parsing in background threads or daemon worker threads.
* Safely wrap background operations in `try-finally` structures and clean error-handling to prevent threads from hanging or silently failing.
* Ensure UI state variables (such as status bars, progress bars, and labels) are updated on all execution paths (including early returns, rate-limits, and exceptions).
