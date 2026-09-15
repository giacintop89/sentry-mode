PYTHON ?= python3
BIN = .venv/bin

.PHONY: setup test lint format status camera-test audio-test
setup:
	$(PYTHON) -m venv .venv
	$(BIN)/python -m pip install -e '.[dev]'
test:
	$(BIN)/pytest tests/unit
lint:
	$(BIN)/ruff check .
format:
	$(BIN)/ruff format .
status:
	$(BIN)/sentry-mode status
camera-test:
	$(BIN)/sentry-mode camera test
audio-test:
	$(BIN)/sentry-mode audio test-output
