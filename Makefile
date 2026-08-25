SHELL := /bin/sh

UV ?= uv

LOCAL_ENV_FILE := $(wildcard $(CURDIR)/.env.local)
ifneq ($(LOCAL_ENV_FILE),)
service doctor connect: export SOLERCIA_CONNECT_ENV_FILE ?= $(LOCAL_ENV_FILE)
endif

PYTEST := $(UV) run --locked --extra dev --extra oauth --extra service pytest
CLI := $(UV) run --locked --extra oauth --extra service solercia-connect
BUILD_OUT ?= dist

.PHONY: help setup setup-browser build-offline service check test test-live doctor connect

help: ## Show the supported local workflows.
	@printf '%s\n' \
	  'make setup' \
	  'make setup-browser' \
	  'make build-offline [BUILD_OUT=dist]' \
	  'make service' \
	  'make check' \
	  'make test' \
	  'make test-live TYPE=outlook ACCOUNT=user@example.com' \
	  'make doctor TYPE=outlook ACCOUNT=user@example.com' \
	  'make connect TYPE=outlook ACCOUNT=user@example.com [ALLOW_BROWSER_FALLBACK=1]'

setup: ## Install the locked core, development, OAuth, and service dependencies.
	@$(UV) sync --locked --extra dev --extra oauth --extra service

setup-browser: ## Install the optional browser package and Chromium binary explicitly.
	@$(UV) sync --locked --extra dev --extra oauth --extra service --extra browser
	@$(UV) run --locked --extra browser playwright install chromium

build-offline: ## Build sdist and wheel offline after the locked setup has completed.
	@mkdir -p "$(BUILD_OUT)"
	@$(UV) run --locked --offline --extra dev python -m build --no-isolation --outdir "$(BUILD_OUT)"

service: ## Start the loopback FastAPI service.
	@$(UV) run --locked --extra oauth --extra service solercia-connect-service

check: ## Run non-mutating static checks.
	@$(UV) run --locked --extra dev ruff format --check .
	@$(UV) run --locked --extra dev ruff check .
	@$(UV) run --locked --extra dev mypy src

test: ## Run the hermetic default test suite.
	@$(PYTEST) -m 'not live'

test-live: ## Run one explicitly opted-in provider contract suite.
	@test -n "$(TYPE)" || { printf '%s\n' 'TYPE is required'; exit 2; }
	@test -n "$(ACCOUNT)" || { printf '%s\n' 'ACCOUNT is required'; exit 2; }
	@SOLERCIA_CONNECT_LIVE=1 SOLERCIA_CONNECT_LIVE_TYPE="$(TYPE)" \
	  SOLERCIA_CONNECT_LIVE_ACCOUNT="$(ACCOUNT)" \
	  $(PYTEST) -m live tests/live

doctor: ## Inspect one provider/account readiness contract.
	@test -n "$(TYPE)" || { printf '%s\n' '{"schema":"solercia.connect/v1","ok":false,"operation":"make.doctor","status":"invalid_input","provider":null,"account":null,"route":null,"data":{},"receipt":null,"error":{"code":"invalid_input","message":"TYPE is required","retryable":false,"missing_configuration":[],"remediation":["Run make doctor TYPE=outlook ACCOUNT=user@example.com."],"cause_category":"input"}}'; exit 2; }
	@test -n "$(ACCOUNT)" || { printf '%s\n' '{"schema":"solercia.connect/v1","ok":false,"operation":"make.doctor","status":"invalid_input","provider":null,"account":null,"route":null,"data":{},"receipt":null,"error":{"code":"invalid_input","message":"ACCOUNT is required","retryable":false,"missing_configuration":[],"remediation":["Run make doctor TYPE=outlook ACCOUNT=user@example.com."],"cause_category":"input"}}'; exit 2; }
	@$(CLI) doctor "$(TYPE)" "$(ACCOUNT)"

connect: ## Start the canonical interactive connection flow.
	@test -n "$(TYPE)" || { printf '%s\n' '{"schema":"solercia.connect/v1","ok":false,"operation":"make.connect","status":"invalid_input","provider":null,"account":null,"route":null,"data":{},"receipt":null,"error":{"code":"invalid_input","message":"TYPE is required","retryable":false,"missing_configuration":[],"remediation":["Run make connect TYPE=outlook ACCOUNT=user@example.com."],"cause_category":"input"}}'; exit 2; }
	@test -n "$(ACCOUNT)" || { printf '%s\n' '{"schema":"solercia.connect/v1","ok":false,"operation":"make.connect","status":"invalid_input","provider":null,"account":null,"route":null,"data":{},"receipt":null,"error":{"code":"invalid_input","message":"ACCOUNT is required","retryable":false,"missing_configuration":[],"remediation":["Run make connect TYPE=outlook ACCOUNT=user@example.com."],"cause_category":"input"}}'; exit 2; }
	@if [ "$(ALLOW_BROWSER_FALLBACK)" = "1" ]; then \
	  $(CLI) connect "$(TYPE)" "$(ACCOUNT)" --allow-browser-fallback; \
	else \
	  $(CLI) connect "$(TYPE)" "$(ACCOUNT)"; \
	fi
