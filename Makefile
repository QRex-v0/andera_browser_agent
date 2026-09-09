PYTHON ?= python3
VENV ?= .venv

.PHONY: setup

setup:
	@if [ ! -x "$(VENV)/bin/python" ]; then $(PYTHON) -m venv "$(VENV)"; fi
	"$(VENV)/bin/python" -m pip install --upgrade pip
	"$(VENV)/bin/python" -m pip install -r requirements.txt
	"$(VENV)/bin/python" -m pip install -e ".[dev,browser]"
	"$(VENV)/bin/python" -m playwright install chromium
