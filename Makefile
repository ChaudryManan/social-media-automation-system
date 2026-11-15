VENV := .venv
VENV_STAMP := $(VENV)/.installed

# BOOTSTRAP_PYTHON creates the venv. Override it if Python 3.13 is installed
# under another name, e.g. `make test BOOTSTRAP_PYTHON=python3`.
ifeq ($(OS),Windows_NT)
VENV_PYTHON := $(VENV)/Scripts/python.exe
BOOTSTRAP_PYTHON ?= py -3.13
else
VENV_PYTHON := $(VENV)/bin/python
BOOTSTRAP_PYTHON ?= python3.13
endif

.PHONY: dev test lint format migrate downgrade migration seed venv

$(VENV_STAMP): pyproject.toml
	$(BOOTSTRAP_PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install -e ".[dev]"
	$(VENV_PYTHON) -c "from pathlib import Path; Path('$(VENV_STAMP)').touch()"

venv: $(VENV_STAMP)

dev: venv
	docker compose up --build

test: venv
	$(VENV_PYTHON) -m pytest

lint: venv
	$(VENV_PYTHON) -m ruff check backend tests

format: venv
	$(VENV_PYTHON) -m ruff format backend tests

migrate: venv
	$(VENV_PYTHON) -m alembic upgrade head

downgrade: venv
	$(VENV_PYTHON) -m alembic downgrade -1

migration: venv
	$(VENV_PYTHON) -m alembic revision --autogenerate -m "$(m)"

seed: venv
	$(VENV_PYTHON) -m app.services.seed
