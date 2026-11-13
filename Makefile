VENV := .venv
VENV_STAMP := $(VENV)/.installed

ifeq ($(OS),Windows_NT)
VENV_PYTHON := $(VENV)/Scripts/python.exe
else
VENV_PYTHON := $(VENV)/bin/python
endif

.PHONY: dev test lint format migrate seed venv

$(VENV_STAMP): pyproject.toml
	py -3.13 -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e ".[dev]"
	$(VENV_PYTHON) -c "from pathlib import Path; Path('$(VENV_STAMP)').touch()"

venv: $(VENV_STAMP)

dev: venv
	@echo "B1-02 will add the API application and Docker Compose development stack."

test: venv
	$(VENV_PYTHON) -m pytest

lint: venv
	$(VENV_PYTHON) -m ruff check backend tests

format: venv
	$(VENV_PYTHON) -m ruff format backend tests

migrate: venv
	@echo "B1-04 will add Alembic migrations."

seed: venv
	@echo "B1-06 will add the idempotent admin seed command."
