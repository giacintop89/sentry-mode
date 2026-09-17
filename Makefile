PYTHON ?= python3
BIN = .venv/bin

.PHONY: setup test lint format status camera-test audio-test pico pico-device pico-release
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
	$(BIN)/python firmware/pico/tools/mqtt_check.py --build-dir build/pico-host
	$(BIN)/python firmware/pico/tools/audio_check.py --build-dir build/pico-host
	$(BIN)/python firmware/pico/tools/link_check.py --build-dir build/pico-host
	$(BIN)/python firmware/pico/tools/pack_provisioning.py --build-dir build/pico-host --verify
# The board build. PICO_SDK_PATH is where the SDK was cloned; the UF2 lands in build/pico2w.
PICO_SDK_PATH ?= $(HOME)/.local/share/pico-sdk
PICO_BOARD ?= pico2_w
pico-device:
	cmake -S firmware/pico -B build/$(PICO_BOARD) -G Ninja -DSENTRY_PICO_TARGET=device \
		-DPICO_BOARD=$(PICO_BOARD) -DPICO_SDK_PATH=$(PICO_SDK_PATH)
	cmake --build build/$(PICO_BOARD)
	$(BIN)/python firmware/pico/tools/image_check.py --build-dir build/$(PICO_BOARD) \
		--provisioning .local/pico-provisioning.json
	@echo "flash: hold BOOTSEL, then copy build/$(PICO_BOARD)/sentry_firmware.uf2 onto $(if $(filter pico2 pico2_w,$(PICO_BOARD)),RP2350,RPI-RP2)"
# What goes out with an image: the commit, the SDK under it, the hash of the file, what the
# hub will let a node of that kind be asked for, and what this firmware still does not do.
# It runs the host suites on the way past, so a manifest that says they passed saw them.
pico-release: pico pico-device
	$(BIN)/python firmware/pico/tools/release_manifest.py --build-dir build/$(PICO_BOARD)
