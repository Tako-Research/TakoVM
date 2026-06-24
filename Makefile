# Tako VM developer shortcuts.
#
# Quickest start from a fresh clone:
#   make bootstrap   # install (editable) + build executor image + start PostgreSQL
#   make doctor      # confirm the environment is ready
#   make server      # run the API server
#
# Run `make` or `make help` to list every target.

# Override the installer if you prefer uv: `make install PIP="uv pip"`
PIP ?= pip
EXECUTOR_IMAGE ?= code-executor:latest

.DEFAULT_GOAL := help
.PHONY: help bootstrap install executor doctor dev server test lint compose-up compose-down

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

bootstrap: install executor dev ## One-shot dev setup: install + build executor image + start PostgreSQL
	@echo ""
	@echo "Bootstrap complete. Run 'make doctor' to verify, then 'make server'."

install: ## Install the package in editable mode with dev + server extras
	$(PIP) install -e ".[dev,server]"

executor: ## Build the executor image the sandbox runs per job
	docker build -t $(EXECUTOR_IMAGE) -f docker/Dockerfile.executor .

doctor: ## Diagnose the local environment (Docker, image, DB, workspace, config)
	tako-vm doctor

dev: ## Start local PostgreSQL for development
	tako-vm dev up

server: ## Start the API server
	tako-vm server

test: ## Run the test suite (permissive mode; needs Docker + PostgreSQL)
	TAKO_VM_SECURITY_MODE=permissive pytest tests/ -v

lint: ## Lint and format with ruff
	ruff check --fix tako_vm/ tests/
	ruff format tako_vm/ tests/

compose-up: ## Run the full stack via Docker Compose (server + PostgreSQL + executor + socket proxy)
	docker compose up -d --build

compose-down: ## Stop the Docker Compose stack
	docker compose down
