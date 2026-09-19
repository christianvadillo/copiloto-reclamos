.PHONY: install test lint fmt demo serve worker

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
RUFF := $(VENV)/bin/ruff
COPILOTO := $(VENV)/bin/copiloto

install:
	python3 -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	$(RUFF) check src tests

fmt:
	$(RUFF) format src tests

demo:
	$(COPILOTO) demo

serve:
	$(COPILOTO) serve

worker:
	$(COPILOTO) worker
