PYTHON ?= python3
BIN = .venv/bin

.PHONY: setup test lint format status camera-test audio-test pico
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
pico:
	cmake -S firmware/pico -B build/pico-host -G Ninja
	cmake --build build/pico-host
	ctest --test-dir build/pico-host --output-on-failure
	$(BIN)/python firmware/pico/tools/check_against_contracts.py --build-dir build/pico-host
