# TaskPilot — the commands you actually need.
#
#   make install   # one-time local setup
#   make up        # docker compose (api + web + db + mockshop)
#   make test      # backend tests + frontend unit tests
#   make eval      # the 15-task evaluation

SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/Scripts/python
PIP := $(VENV)/Scripts/pip
PYTEST := $(VENV)/Scripts/pytest
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help install install-backend install-frontend up down logs build \
        test test-backend test-frontend test-e2e coverage eval eval-quick lint fmt \
        typecheck mockshop dev dev-api dev-web clean precommit ci

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: install-backend install-frontend ## Install backend + frontend deps

install-backend: ## Create the venv and install backend deps
	python -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r backend/requirements-dev.txt
	$(PY) -m playwright install chromium

install-frontend: ## Install frontend deps
	cd frontend && npm install

up: ## Start the whole stack with Docker
	$(COMPOSE) up --build

down: ## Stop the stack
	$(COMPOSE) down

logs: ## Tail container logs
	$(COMPOSE) logs -f --tail=100

build: ## Build images without starting
	$(COMPOSE) build

mockshop: ## Run MockShop locally on :9000
	cd demo-apps/mockshop && ../../$(PY) app.py

test: test-backend test-frontend ## Run every test suite

test-backend: ## pytest (unit + e2e, no external services)
	cd backend && ../$(PYTEST) tests/ -q

test-frontend: ## vitest
	cd frontend && npm run test

# Coverage is measured across the whole suite, e2e included: the browser layer
# is only exercised by the e2e tests, so excluding them understates app.tools.
coverage: ## Run the suite with coverage and enforce the 80% gate
	cd backend && ../$(PYTEST) tests/ -q --cov=app --cov-report=term-missing --cov-report=xml
	cd backend && ../$(PY) scripts/coverage_gate.py --xml coverage.xml \
		--package app.agent:80 --package app.tools:80 --total 75

test-e2e: ## Playwright e2e (frontend)
	cd frontend && npm run e2e

eval: ## Run the 15-task evaluation and write results
	cd backend && ../$(PY) -m eval.run --reset-each

eval-quick: ## First 3 eval tasks only (fast smoke)
	cd backend && ../$(PY) -m eval.run --tasks 3 --reset-each

migrate: ## Apply database migrations (alembic upgrade head)
	cd backend && ../$(VENV)/Scripts/alembic upgrade head

migrate-autogen: ## Autogenerate a migration after changing models
	cd backend && ../$(VENV)/Scripts/alembic revision --autogenerate -m "describe change"

lint: ## ruff + eslint
	cd backend && ../$(VENV)/Scripts/ruff check app eval tests scripts
	cd frontend && npm run lint

fmt: ## black + prettier
	cd backend && ../$(VENV)/Scripts/black app eval tests
	cd frontend && npx prettier --write "**/*.{ts,tsx,css,json}"

typecheck: ## mypy + tsc
	cd backend && ../$(VENV)/Scripts/mypy app
	cd frontend && npm run typecheck

precommit: ## Run the pre-commit hooks over the tree
	.venv/Scripts/pre-commit run --all-files || pre-commit run --all-files

ci: lint typecheck test ## What GitHub Actions runs

dev-api: ## uvicorn with reload
	cd backend && ../$(VENV)/Scripts/uvicorn app.main:get_app --factory --reload --port 8000

dev-web: ## next dev
	cd frontend && npm run dev

clean: ## Remove caches and build output
	rm -rf frontend/.next frontend/node_modules/.cache backend/.pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
