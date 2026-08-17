.PHONY: setup run test docker docker-run

PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python
STREAMLIT := $(VENV)/bin/streamlit

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@test -f .env || cp .env.example .env
	@echo ""
	@echo "Ready. Optional: edit .env and set XAI_API_KEY for ad-heavy URL extract."
	@echo "Then:  make run"

run:
	$(STREAMLIT) run app.py --server.headless true

test:
	$(PIP) install -q pytest
	$(PY) -m pytest tests/ -q

docker:
	docker build -t recipe-tables:local .

docker-run:
	docker run --rm -p 8501:8501 \
	  -e XAI_API_KEY \
	  -v "$$(pwd)/data/library:/app/data/library" \
	  recipe-tables:local
