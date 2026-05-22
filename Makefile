SHELL := /usr/bin/env bash

.PHONY: install
install:
	@uv pip install --system -e .

.PHONY: install-dev
install-dev:
	@uv pip install --system -e ".[dev]"
	@pre-commit install

# ---------------------------------------------------------------------------
# Dev CLI wrappers — all test/lint/check commands delegate to `dev`
# ---------------------------------------------------------------------------

.PHONY: test
test:
	@dev test --lane=unit

.PHONY: test-fast
test-fast:
	@dev test --lane=unit

.PHONY: test-integration
test-integration:
	@dev test --lane=integration

.PHONY: test-all
test-all:
	@dev verify

.PHONY: test-lf
test-lf:
	@dev test --rerun-failed

.PHONY: lint
lint:
	@dev lint

.PHONY: format
format:
	@dev format

.PHONY: typecheck
typecheck:
	@dev typecheck

.PHONY: check
check:
	@dev check

# ---------------------------------------------------------------------------
# Targets that remain Make-native (not test/lint commands)
# ---------------------------------------------------------------------------

.PHONY: docstrings-cover
docstrings-cover:
	@interrogate -v --fail-under 100 ai_pipeline_core

.PHONY: deadcode
deadcode:
	@vulture ai_pipeline_core/ .vulture_whitelist.py --min-confidence 80

.PHONY: semgrep
semgrep:
	@uvx semgrep --config .semgrep/ ai_pipeline_core/ tests/ --error

check-claude-md:
	python scripts/check_claude_md_symbols.py

# File size limits: 500 lines warning, 1000 lines error (excluding blanks and comments)
.PHONY: filesize
filesize:
	@echo "Checking file sizes..."
	@error=0; \
	for f in $$(find ai_pipeline_core -name "*.py" -type f); do \
		lines=$$(grep -cvE '^[[:space:]]*$$|^[[:space:]]*#' "$$f" 2>/dev/null || echo 0); \
		if [ "$$lines" -gt 1000 ]; then \
			echo "ERROR: $$f has $$lines lines (max 1000)"; \
			error=1; \
		elif [ "$$lines" -gt 500 ]; then \
			echo "WARNING: $$f has $$lines lines (soft limit 500)"; \
		fi; \
	done; \
	exit $$error

.PHONY: duplicates
duplicates:
	@echo "Checking for duplicate code..."
	@pylint --disable=all --enable=duplicate-code ai_pipeline_core/ || true

# Export discipline: public modules with public symbols should define __all__
.PHONY: exports
exports:
	@echo "Checking __all__ exports (advisory)..."
	@for f in $$(find ai_pipeline_core -name "*.py" -type f ! -name "_*" ! -name "__init__.py" ! -path "*/__pycache__/*" ! -path "*/_*/*"); do \
		if grep -qE '^(def|class) [^_]' "$$f" 2>/dev/null && ! grep -q '^__all__' "$$f" 2>/dev/null; then \
			echo "Advisory: Missing __all__: $$f"; \
		fi; \
	done

# Run all code hygiene checks
.PHONY: hygiene
hygiene: filesize duplicates exports
	@echo "Code hygiene checks completed"

.PHONY: clean
clean:
	@rm -rf build/ dist/ *.egg-info .pytest_cache/ .ruff_cache/ htmlcov/ .coverage
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete

.PHONY: pre-commit
pre-commit:
	@pre-commit run --all-files

.PHONY: lint-pre-commit-config
lint-pre-commit-config:
	@pre-commit validate-config .pre-commit-config.yaml
