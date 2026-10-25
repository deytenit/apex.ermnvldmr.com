.PHONY: test lint fmt check

test:
	python3 -m unittest discover -s tests

lint:
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check .; \
	else \
		python3 -m py_compile engine/**/*.py actions/**/*.py tests/*.py; \
		echo "ruff not installed in PATH; verified python bytecode compilation."; \
	fi

fmt:
	@if command -v ruff >/dev/null 2>&1; then \
		ruff format .; \
	else \
		echo "ruff not installed in PATH; skipping fmt."; \
	fi

check: lint test
