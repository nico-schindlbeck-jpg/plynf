# Plinth — top-level Makefile
# Usage: make <target>
# Run `make help` for a quick reference.

SHELL := /bin/bash

# Pick a python interpreter. Prefer 3.11+; fall back to python3.
PYTHON := $(shell command -v python3.11 || command -v python3.12 || command -v python3.13 || command -v python3)
VENV := .venv
VENV_BIN := $(VENV)/bin
PIP := $(VENV_BIN)/pip
PY := $(VENV_BIN)/python

PLINTH_DATA_DIR ?= /tmp/plinth-data
PLINTH_WORKSPACE_PORT ?= 7421
PLINTH_GATEWAY_PORT ?= 7422
PLINTH_MOCK_MCP_PORT ?= 7423
PLINTH_DASHBOARD_PORT ?= 7424
PLINTH_IDENTITY_PORT ?= 7425
PLINTH_GITHUB_MCP_PORT ?= 7426
PLINTH_SLACK_MCP_PORT ?= 7427
PLINTH_LINEAR_MCP_PORT ?= 7428
PLINTH_NOTION_MCP_PORT ?= 7429
PLINTH_GOOGLE_MCP_PORT ?= 7430

LOG_DIR := /tmp/plinth-logs
PID_DIR := /tmp/plinth-pids

.PHONY: help install install-services install-sdk install-examples install-mock install-dashboard install-identity install-github-mcp install-slack-mcp install-linear-mcp install-notion-mcp install-google-mcp install-bench \
        test test-workspace test-gateway test-sdk test-mock test-dashboard test-identity test-github-mcp test-slack-mcp test-linear-mcp test-notion-mcp test-google-mcp test-ts test-bench \
        serve serve-workspace serve-gateway serve-mock serve-dashboard serve-identity serve-github-mcp serve-slack-mcp serve-linear-mcp serve-notion-mcp serve-google-mcp stop healthcheck \
        demo demo-handoff demo-resume demo-triage \
        bench bench-quick bench-compare \
        clean clean-data lint format ci tree

help:  ## Show this help message
	@echo "Plinth — make targets"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Quickstart: make install && make test && make serve && make demo"

# ───────── install ─────────

$(VENV)/bin/activate:
	@echo "→ Creating venv at $(VENV) using $(PYTHON)"
	@$(PYTHON) -m venv $(VENV)
	@$(PIP) install --upgrade pip wheel >/dev/null
	@touch $@

install: $(VENV)/bin/activate install-services install-mock install-sdk install-dashboard install-identity install-github-mcp install-slack-mcp install-linear-mcp install-notion-mcp install-google-mcp install-examples  ## Install everything
	@echo ""
	@echo "✔ Plinth installed. Try: make test, make serve, make demo"

install-services: $(VENV)/bin/activate  ## Install workspace + gateway services
	@echo "→ Installing workspace service"
	@$(PIP) install -e "./services/workspace[dev]" >/dev/null
	@echo "→ Installing gateway service"
	@$(PIP) install -e "./services/gateway[dev]" >/dev/null

install-mock: $(VENV)/bin/activate  ## Install mock-mcp server
	@echo "→ Installing mock-mcp server"
	@if [ -d ./mock-mcp-server ]; then \
		$(PIP) install -e "./mock-mcp-server[dev]" >/dev/null; \
	else \
		echo "  (mock-mcp-server not built yet — skipping)"; \
	fi

install-sdk: $(VENV)/bin/activate  ## Install Python SDK
	@echo "→ Installing Python SDK (plinth)"
	@$(PIP) install -e "./sdk/python[dev]" >/dev/null

install-dashboard: $(VENV)/bin/activate  ## Install dashboard service
	@echo "→ Installing dashboard service"
	@if [ -d ./services/dashboard ]; then \
		$(PIP) install -e "./services/dashboard[dev]" >/dev/null; \
	else \
		echo "  (dashboard not built yet — skipping)"; \
	fi

install-identity: $(VENV)/bin/activate  ## Install identity service
	@echo "→ Installing identity service"
	@if [ -d ./services/identity ]; then \
		$(PIP) install -e "./services/identity[dev]" >/dev/null; \
	else \
		echo "  (identity not built yet — skipping)"; \
	fi

install-github-mcp: $(VENV)/bin/activate  ## Install GitHub MCP server
	@echo "→ Installing GitHub MCP server"
	@if [ -d ./mcp-servers/github ]; then \
		$(PIP) install -e "./mcp-servers/github[dev]" >/dev/null; \
	else \
		echo "  (github-mcp not built yet — skipping)"; \
	fi

install-slack-mcp: $(VENV)/bin/activate  ## Install Slack MCP server
	@echo "→ Installing Slack MCP server"
	@if [ -d ./mcp-servers/slack ]; then \
		$(PIP) install -e "./mcp-servers/slack[dev]" >/dev/null; \
	else \
		echo "  (slack-mcp not built yet — skipping)"; \
	fi

install-linear-mcp: $(VENV)/bin/activate  ## Install Linear MCP server
	@echo "→ Installing Linear MCP server"
	@if [ -d ./mcp-servers/linear ]; then \
		$(PIP) install -e "./mcp-servers/linear[dev]" >/dev/null; \
	else \
		echo "  (linear-mcp not built yet — skipping)"; \
	fi

install-notion-mcp: $(VENV)/bin/activate  ## Install Notion MCP server
	@echo "→ Installing Notion MCP server"
	@if [ -d ./mcp-servers/notion ]; then \
		$(PIP) install -e "./mcp-servers/notion[dev]" >/dev/null; \
	else \
		echo "  (notion-mcp not built yet — skipping)"; \
	fi

install-google-mcp: $(VENV)/bin/activate  ## Install Google Workspace MCP server
	@echo "→ Installing Google Workspace MCP server"
	@if [ -d ./mcp-servers/google-workspace ]; then \
		$(PIP) install -e "./mcp-servers/google-workspace[dev]" >/dev/null; \
	else \
		echo "  (google-workspace-mcp not built yet — skipping)"; \
	fi

install-examples: $(VENV)/bin/activate  ## Install example agents
	@echo "→ Installing research-agent example"
	@$(PIP) install -e "./examples/01-research-agent" >/dev/null
	@if [ -f ./examples/02-multi-agent-handoff/pyproject.toml ]; then \
		echo "→ Installing multi-agent-handoff example"; \
		$(PIP) install -e "./examples/02-multi-agent-handoff" >/dev/null; \
	fi
	@if [ -f ./examples/03-resumable-workflow/pyproject.toml ]; then \
		echo "→ Installing resumable-workflow example"; \
		$(PIP) install -e "./examples/03-resumable-workflow" >/dev/null; \
	fi
	@if [ -f ./examples/04-github-issue-triage/pyproject.toml ]; then \
		echo "→ Installing github-issue-triage example"; \
		$(PIP) install -e "./examples/04-github-issue-triage" >/dev/null; \
	fi

# ───────── tests ─────────

test: test-workspace test-gateway test-sdk test-mock test-dashboard test-identity test-github-mcp test-slack-mcp test-linear-mcp test-notion-mcp test-google-mcp  ## Run all Python test suites
	@echo ""
	@echo "✔ All test suites passed"

test-workspace:  ## Run workspace service tests
	@echo "→ Workspace service tests"
	@cd services/workspace && $(abspath $(VENV_BIN))/pytest -q --cov=plinth_workspace --cov-report=term-missing:skip-covered

test-gateway:  ## Run gateway service tests
	@echo "→ Gateway service tests"
	@cd services/gateway && $(abspath $(VENV_BIN))/pytest -q --cov=plinth_gateway --cov-report=term-missing:skip-covered

test-sdk:  ## Run Python SDK tests
	@echo "→ Python SDK tests"
	@cd sdk/python && $(abspath $(VENV_BIN))/pytest -q --cov=plinth --cov-report=term-missing:skip-covered

test-mock:  ## Run mock-mcp server tests
	@echo "→ Mock MCP server tests"
	@if [ -d ./mock-mcp-server ]; then \
		cd mock-mcp-server && $(abspath $(VENV_BIN))/pytest -q --cov=mock_mcp --cov-report=term-missing:skip-covered; \
	else \
		echo "  (mock-mcp-server not built yet — skipping)"; \
	fi

test-dashboard:  ## Run dashboard service tests
	@echo "→ Dashboard service tests"
	@if [ -d ./services/dashboard ]; then \
		cd services/dashboard && $(abspath $(VENV_BIN))/pytest -q --cov=plinth_dashboard --cov-report=term-missing:skip-covered; \
	else \
		echo "  (dashboard not built yet — skipping)"; \
	fi

test-identity:  ## Run identity service tests
	@echo "→ Identity service tests"
	@if [ -d ./services/identity ]; then \
		cd services/identity && $(abspath $(VENV_BIN))/pytest -q --cov=plinth_identity --cov-report=term-missing:skip-covered; \
	else \
		echo "  (identity not built yet — skipping)"; \
	fi

test-github-mcp:  ## Run GitHub MCP server tests
	@echo "→ GitHub MCP tests"
	@if [ -d ./mcp-servers/github ]; then \
		cd mcp-servers/github && $(abspath $(VENV_BIN))/pytest -q --cov=github_mcp --cov-report=term-missing:skip-covered; \
	else \
		echo "  (github-mcp not built yet — skipping)"; \
	fi

test-slack-mcp:  ## Run Slack MCP server tests
	@echo "→ Slack MCP tests"
	@if [ -d ./mcp-servers/slack ]; then \
		cd mcp-servers/slack && $(abspath $(VENV_BIN))/pytest -q --cov=slack_mcp --cov-report=term-missing:skip-covered; \
	else \
		echo "  (slack-mcp not built yet — skipping)"; \
	fi

test-linear-mcp:  ## Run Linear MCP server tests
	@echo "→ Linear MCP tests"
	@if [ -d ./mcp-servers/linear ]; then \
		cd mcp-servers/linear && $(abspath $(VENV_BIN))/pytest -q --cov=linear_mcp --cov-report=term-missing:skip-covered; \
	else \
		echo "  (linear-mcp not built yet — skipping)"; \
	fi

test-notion-mcp:  ## Run Notion MCP server tests
	@echo "→ Notion MCP tests"
	@if [ -d ./mcp-servers/notion ]; then \
		cd mcp-servers/notion && $(abspath $(VENV_BIN))/pytest -q --cov=notion_mcp --cov-report=term-missing:skip-covered; \
	else \
		echo "  (notion-mcp not built yet — skipping)"; \
	fi

test-google-mcp:  ## Run Google Workspace MCP server tests
	@echo "→ Google Workspace MCP tests"
	@if [ -d ./mcp-servers/google-workspace ]; then \
		cd mcp-servers/google-workspace && $(abspath $(VENV_BIN))/pytest -q --cov=google_workspace_mcp --cov-report=term-missing:skip-covered; \
	else \
		echo "  (google-workspace-mcp not built yet — skipping)"; \
	fi

test-ts:  ## Run TypeScript SDK tests (requires npm)
	@echo "→ TypeScript SDK tests"
	@cd sdk/typescript && npm install --silent && npm run build && npm test

# ───────── lint / format ─────────

lint:  ## Lint all Python code
	@echo "→ Ruff"
	@$(VENV_BIN)/ruff check services sdk/python mock-mcp-server examples/01-research-agent || true

format:  ## Format all Python code
	@echo "→ Black"
	@$(VENV_BIN)/black services sdk/python mock-mcp-server examples/01-research-agent || true
	@echo "→ Ruff fix"
	@$(VENV_BIN)/ruff check --fix services sdk/python mock-mcp-server examples/01-research-agent || true

# ───────── serve ─────────

$(LOG_DIR):
	@mkdir -p $(LOG_DIR)

$(PID_DIR):
	@mkdir -p $(PID_DIR)

serve: $(LOG_DIR) $(PID_DIR) serve-workspace serve-gateway serve-mock serve-dashboard serve-identity serve-github-mcp serve-slack-mcp serve-linear-mcp serve-notion-mcp serve-google-mcp  ## Start all services in the background
	@echo ""
	@echo "✔ Services started:"
	@echo "  • Workspace        : http://localhost:$(PLINTH_WORKSPACE_PORT)/healthz   (logs: $(LOG_DIR)/workspace.log)"
	@echo "  • Gateway          : http://localhost:$(PLINTH_GATEWAY_PORT)/healthz     (logs: $(LOG_DIR)/gateway.log)"
	@echo "  • Mock MCP         : http://localhost:$(PLINTH_MOCK_MCP_PORT)/healthz    (logs: $(LOG_DIR)/mock-mcp.log)"
	@echo "  • Dashboard        : http://localhost:$(PLINTH_DASHBOARD_PORT)/          (logs: $(LOG_DIR)/dashboard.log)"
	@echo "  • Identity         : http://localhost:$(PLINTH_IDENTITY_PORT)/healthz    (logs: $(LOG_DIR)/identity.log)"
	@echo "  • GitHub MCP       : http://localhost:$(PLINTH_GITHUB_MCP_PORT)/healthz  (logs: $(LOG_DIR)/github-mcp.log)"
	@echo "  • Slack MCP        : http://localhost:$(PLINTH_SLACK_MCP_PORT)/healthz   (logs: $(LOG_DIR)/slack-mcp.log)"
	@echo "  • Linear MCP       : http://localhost:$(PLINTH_LINEAR_MCP_PORT)/healthz  (logs: $(LOG_DIR)/linear-mcp.log)"
	@echo "  • Notion MCP       : http://localhost:$(PLINTH_NOTION_MCP_PORT)/healthz  (logs: $(LOG_DIR)/notion-mcp.log)"
	@echo "  • Google Wrkspc MCP: http://localhost:$(PLINTH_GOOGLE_MCP_PORT)/healthz  (logs: $(LOG_DIR)/google-workspace-mcp.log)"
	@echo ""
	@echo "Stop with: make stop"

serve-workspace: $(LOG_DIR) $(PID_DIR)  ## Start workspace service
	@if [ -f $(PID_DIR)/workspace.pid ] && kill -0 $$(cat $(PID_DIR)/workspace.pid) 2>/dev/null; then \
		echo "  • Workspace already running (pid $$(cat $(PID_DIR)/workspace.pid))"; \
	else \
		mkdir -p $(PLINTH_DATA_DIR); \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/workspace.pid $(LOG_DIR)/workspace.log \
			PLINTH_DATA_DIR=$(PLINTH_DATA_DIR) PLINTH_WORKSPACE_PORT=$(PLINTH_WORKSPACE_PORT) \
			-- $(PY) -m plinth_workspace); \
		echo "  • Workspace started (pid $$pid)"; \
	fi

serve-gateway: $(LOG_DIR) $(PID_DIR)  ## Start gateway service
	@if [ -f $(PID_DIR)/gateway.pid ] && kill -0 $$(cat $(PID_DIR)/gateway.pid) 2>/dev/null; then \
		echo "  • Gateway already running (pid $$(cat $(PID_DIR)/gateway.pid))"; \
	else \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/gateway.pid $(LOG_DIR)/gateway.log \
			PLINTH_DATA_DIR=$(PLINTH_DATA_DIR) PLINTH_GATEWAY_PORT=$(PLINTH_GATEWAY_PORT) \
			-- $(PY) -m plinth_gateway); \
		echo "  • Gateway started (pid $$pid)"; \
	fi

serve-mock: $(LOG_DIR) $(PID_DIR)  ## Start mock-mcp server
	@if [ ! -d ./mock-mcp-server ]; then \
		echo "  • mock-mcp-server not built — skipping"; \
		exit 0; \
	fi
	@if [ -f $(PID_DIR)/mock-mcp.pid ] && kill -0 $$(cat $(PID_DIR)/mock-mcp.pid) 2>/dev/null; then \
		echo "  • Mock MCP already running (pid $$(cat $(PID_DIR)/mock-mcp.pid))"; \
	else \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/mock-mcp.pid $(LOG_DIR)/mock-mcp.log \
			PLINTH_MOCK_PORT=$(PLINTH_MOCK_MCP_PORT) \
			-- $(PY) -m mock_mcp); \
		echo "  • Mock MCP started (pid $$pid)"; \
	fi

serve-dashboard: $(LOG_DIR) $(PID_DIR)  ## Start dashboard service
	@if [ ! -d ./services/dashboard ]; then \
		echo "  • dashboard not built — skipping"; \
		exit 0; \
	fi
	@if [ -f $(PID_DIR)/dashboard.pid ] && kill -0 $$(cat $(PID_DIR)/dashboard.pid) 2>/dev/null; then \
		echo "  • Dashboard already running (pid $$(cat $(PID_DIR)/dashboard.pid))"; \
	else \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/dashboard.pid $(LOG_DIR)/dashboard.log \
			PLINTH_DASHBOARD_PORT=$(PLINTH_DASHBOARD_PORT) \
			PLINTH_DASHBOARD_WORKSPACE_URL=http://localhost:$(PLINTH_WORKSPACE_PORT) \
			PLINTH_DASHBOARD_GATEWAY_URL=http://localhost:$(PLINTH_GATEWAY_PORT) \
			PLINTH_DASHBOARD_MOCK_MCP_URL=http://localhost:$(PLINTH_MOCK_MCP_PORT) \
			-- $(PY) -m plinth_dashboard); \
		echo "  • Dashboard started (pid $$pid)"; \
	fi

serve-identity: $(LOG_DIR) $(PID_DIR)  ## Start identity service
	@if [ ! -d ./services/identity ]; then \
		echo "  • identity not built — skipping"; \
		exit 0; \
	fi
	@if [ -f $(PID_DIR)/identity.pid ] && kill -0 $$(cat $(PID_DIR)/identity.pid) 2>/dev/null; then \
		echo "  • Identity already running (pid $$(cat $(PID_DIR)/identity.pid))"; \
	else \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/identity.pid $(LOG_DIR)/identity.log \
			PLINTH_IDENTITY_PORT=$(PLINTH_IDENTITY_PORT) \
			PLINTH_IDENTITY_DATA_DIR=$(PLINTH_DATA_DIR) \
			-- $(PY) -m plinth_identity); \
		echo "  • Identity started (pid $$pid)"; \
	fi

serve-github-mcp: $(LOG_DIR) $(PID_DIR)  ## Start GitHub MCP server
	@if [ ! -d ./mcp-servers/github ]; then \
		echo "  • github-mcp not built — skipping"; \
		exit 0; \
	fi
	@if [ -f $(PID_DIR)/github-mcp.pid ] && kill -0 $$(cat $(PID_DIR)/github-mcp.pid) 2>/dev/null; then \
		echo "  • GitHub MCP already running (pid $$(cat $(PID_DIR)/github-mcp.pid))"; \
	else \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/github-mcp.pid $(LOG_DIR)/github-mcp.log \
			PLINTH_GITHUB_MCP_PORT=$(PLINTH_GITHUB_MCP_PORT) \
			-- $(PY) -m github_mcp); \
		echo "  • GitHub MCP started (pid $$pid)"; \
	fi

serve-slack-mcp: $(LOG_DIR) $(PID_DIR)  ## Start Slack MCP server
	@if [ ! -d ./mcp-servers/slack ]; then \
		echo "  • slack-mcp not built — skipping"; \
		exit 0; \
	fi
	@if [ -f $(PID_DIR)/slack-mcp.pid ] && kill -0 $$(cat $(PID_DIR)/slack-mcp.pid) 2>/dev/null; then \
		echo "  • Slack MCP already running (pid $$(cat $(PID_DIR)/slack-mcp.pid))"; \
	else \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/slack-mcp.pid $(LOG_DIR)/slack-mcp.log \
			PLINTH_SLACK_MCP_PORT=$(PLINTH_SLACK_MCP_PORT) \
			-- $(PY) -m slack_mcp); \
		echo "  • Slack MCP started (pid $$pid)"; \
	fi

serve-linear-mcp: $(LOG_DIR) $(PID_DIR)  ## Start Linear MCP server
	@if [ ! -d ./mcp-servers/linear ]; then \
		echo "  • linear-mcp not built — skipping"; \
		exit 0; \
	fi
	@if [ -f $(PID_DIR)/linear-mcp.pid ] && kill -0 $$(cat $(PID_DIR)/linear-mcp.pid) 2>/dev/null; then \
		echo "  • Linear MCP already running (pid $$(cat $(PID_DIR)/linear-mcp.pid))"; \
	else \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/linear-mcp.pid $(LOG_DIR)/linear-mcp.log \
			PLINTH_LINEAR_MCP_PORT=$(PLINTH_LINEAR_MCP_PORT) \
			-- $(PY) -m linear_mcp); \
		echo "  • Linear MCP started (pid $$pid)"; \
	fi

serve-notion-mcp: $(LOG_DIR) $(PID_DIR)  ## Start Notion MCP server
	@if [ ! -d ./mcp-servers/notion ]; then \
		echo "  • notion-mcp not built — skipping"; \
		exit 0; \
	fi
	@if [ -f $(PID_DIR)/notion-mcp.pid ] && kill -0 $$(cat $(PID_DIR)/notion-mcp.pid) 2>/dev/null; then \
		echo "  • Notion MCP already running (pid $$(cat $(PID_DIR)/notion-mcp.pid))"; \
	else \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/notion-mcp.pid $(LOG_DIR)/notion-mcp.log \
			PLINTH_NOTION_MCP_PORT=$(PLINTH_NOTION_MCP_PORT) \
			-- $(PY) -m notion_mcp); \
		echo "  • Notion MCP started (pid $$pid)"; \
	fi

serve-google-mcp: $(LOG_DIR) $(PID_DIR)  ## Start Google Workspace MCP server
	@if [ ! -d ./mcp-servers/google-workspace ]; then \
		echo "  • google-workspace-mcp not built — skipping"; \
		exit 0; \
	fi
	@if [ -f $(PID_DIR)/google-workspace-mcp.pid ] && kill -0 $$(cat $(PID_DIR)/google-workspace-mcp.pid) 2>/dev/null; then \
		echo "  • Google Workspace MCP already running (pid $$(cat $(PID_DIR)/google-workspace-mcp.pid))"; \
	else \
		pid=$$($(PY) scripts/_spawn.py \
			$(PID_DIR)/google-workspace-mcp.pid $(LOG_DIR)/google-workspace-mcp.log \
			PLINTH_GOOGLE_MCP_PORT=$(PLINTH_GOOGLE_MCP_PORT) \
			-- $(PY) -m google_workspace_mcp); \
		echo "  • Google Workspace MCP started (pid $$pid)"; \
	fi

stop:  ## Stop all background services
	@for svc in workspace gateway mock-mcp dashboard identity github-mcp slack-mcp linear-mcp notion-mcp google-workspace-mcp; do \
		if [ -f $(PID_DIR)/$$svc.pid ]; then \
			pid=$$(cat $(PID_DIR)/$$svc.pid); \
			if kill -0 $$pid 2>/dev/null; then \
				kill $$pid && echo "  • Stopped $$svc (pid $$pid)" || true; \
			fi; \
			rm -f $(PID_DIR)/$$svc.pid; \
		fi; \
	done
	@echo "✔ All services stopped"

healthcheck:  ## Curl health endpoints of all services
	@bash scripts/healthcheck.sh

# ───────── demo ─────────

demo:  ## Run the headline token-comparison demo (example 01)
	@bash scripts/demo.sh

demo-handoff:  ## Run the multi-agent handoff demo (example 02)
	@if [ -d ./examples/02-multi-agent-handoff ]; then \
		$(PY) examples/02-multi-agent-handoff/orchestrate.py --topic "renewable energy" --mode simulation; \
	else \
		echo "  (example 02 not built yet)"; \
	fi

demo-resume:  ## Run the resumable-workflow demo (example 03)
	@if [ -d ./examples/03-resumable-workflow ]; then \
		$(PY) examples/03-resumable-workflow/crash_resume.py --topic "renewable energy"; \
	else \
		echo "  (example 03 not built yet)"; \
	fi

demo-triage:  ## Run the GitHub issue-triage demo (example 04)
	@if [ -d ./examples/04-github-issue-triage ]; then \
		$(PY) examples/04-github-issue-triage/triage_agent.py --repo demo/repo --limit 10 --mode simulation; \
	else \
		echo "  (example 04 not built yet)"; \
	fi

# ───────── benchmarks ─────────

install-bench: $(VENV)/bin/activate  ## Install the plinth-bench harness
	@echo "→ Installing bench tooling"
	@$(PIP) install --ignore-requires-python -e "./benchmarks[dev]" >/dev/null

test-bench: install-bench  ## Run bench harness unit tests
	@echo "→ Bench tests"
	@cd benchmarks && $(abspath $(VENV_BIN))/pytest -q

bench: install-bench  ## Run the standard benchmark suite (~3 min/workload, ~15 min total)
	@echo "→ Running standard benchmark suite"
	@$(VENV_BIN)/plinth-bench all --output-dir benchmarks/results

bench-quick: install-bench  ## Quick benchmark sanity (target_rps=100, hold=10s)
	@echo "→ Running quick benchmark suite (target_rps=100, hold=10s)"
	@$(VENV_BIN)/plinth-bench all --target-rps 100 --hold-seconds 10 --ramp-seconds 5 --cooldown-seconds 2 --output-dir benchmarks/results

bench-compare: install-bench  ## Compare two run JSONs: BASELINE=path/to/A.json LATEST=path/to/B.json
	@if [ -z "$(BASELINE)" ] || [ -z "$(LATEST)" ]; then \
		echo "Usage: make bench-compare BASELINE=results/A.json LATEST=results/B.json"; \
		exit 2; \
	fi
	@$(VENV_BIN)/plinth-bench compare $(BASELINE) $(LATEST)

# ───────── housekeeping ─────────

clean:  ## Remove venv and build artifacts
	@rm -rf $(VENV) **/dist **/build **/*.egg-info
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name .coverage -delete 2>/dev/null || true
	@echo "✔ Cleaned"

clean-data: stop  ## Stop services and wipe data dir
	@rm -rf $(PLINTH_DATA_DIR) $(LOG_DIR) $(PID_DIR)
	@echo "✔ Data wiped at $(PLINTH_DATA_DIR)"

ci: install lint test  ## What CI runs

tree:  ## Print a quick repo tree (top 2 levels)
	@find . -maxdepth 2 -not -path '*/\.*' -not -path '*/node_modules*' -not -path '*/__pycache__*' -not -path '*/.venv*' | sort
