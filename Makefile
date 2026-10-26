.PHONY: test lint fmt check

test:
	bash tests/run.sh

lint:
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check .; \
	else \
		python3 -m compileall -q engine actions tests ci/scripts && \
		echo "ruff not installed in PATH; verified python bytecode compilation."; \
	fi

fmt:
	@if command -v ruff >/dev/null 2>&1; then \
		ruff format .; \
	else \
		echo "ruff not installed in PATH; skipping fmt."; \
	fi

check: lint test
