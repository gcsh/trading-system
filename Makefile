# Test runner — Enterprise QA framework (12-layer).
# Each target maps to a category in the Trading Bot QA Strategy doc.
.PHONY: help smoke unit integration system invariant security all coverage clean

VENV ?= .venv
PY ?= $(VENV)/bin/python
PYTEST ?= $(VENV)/bin/pytest

help:
	@echo "Test categories:"
	@echo "  make smoke      — Post-deploy <60s pack (always run)"
	@echo "  make invariant  — Business invariants + safety guards"
	@echo "  make unit       — Pure-function unit tests"
	@echo "  make integration— Cross-module integration"
	@echo "  make system     — Full-platform under load"
	@echo "  make security   — Secrets, auth, deps"
	@echo "  make all        — Everything except slow/network"
	@echo "  make coverage   — Coverage report (HTML in coverage_html/)"

smoke:
	$(PYTEST) -m smoke -v --tb=short

invariant:
	$(PYTEST) -m invariant -v --tb=line

unit:
	$(PYTEST) -m "unit and not slow" tests/unit -q

integration:
	$(PYTEST) -m "integration and not requires_network" tests/integration -q

system:
	$(PYTEST) -m "system and not slow" tests/system -q

security:
	$(PYTEST) -m "security and not slow" -q

risk:
	$(PYTEST) -m risk -v

learning_safety:
	$(PYTEST) -m learning_safety -v

ai_safety:
	$(PYTEST) -m ai_safety -v

data_integrity:
	$(PYTEST) -m data_integrity -v

all:
	$(PYTEST) -m "not slow and not requires_network and not requires_thetadata and not requires_anthropic" -q

coverage:
	$(PYTEST) -m "not slow and not requires_network" \
	    --cov=backend --cov-report=term-missing --cov-report=html

clean:
	rm -rf coverage_html .coverage .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
