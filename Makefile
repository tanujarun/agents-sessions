.PHONY: test test-tui lint format typecheck coverage dev check need-venv need-tools

VENV := .venv
BIN := $(VENV)/bin
SOURCES := src tests bin/agent-sessions

# The engine suite: bare system Python, no venv (the TUI app tests skip
# themselves when Textual is absent).
test:
	python3 -m unittest discover -s tests -v

test-tui: need-venv
	$(BIN)/python -m unittest discover -s tests -p 'test_tui*.py' -v

lint: need-tools
	$(BIN)/ruff check $(SOURCES)
	$(BIN)/ruff format --check $(SOURCES)
	python3 -m compileall -q src tests

format: need-tools
	$(BIN)/ruff format $(SOURCES)

typecheck: need-tools
	$(BIN)/mypy

# Every suite in one run under the venv's Python (it has Textual), then the
# per-module report.
coverage: need-tools
	$(BIN)/coverage erase
	$(BIN)/coverage run -m unittest discover -s tests
	$(BIN)/coverage report

# .venv with Textual and the development tools, the project installed editable.
dev:
	test -x $(BIN)/python || python3 -m venv $(VENV)
	$(BIN)/python -m pip install --disable-pip-version-check -e '.[tui,dev]'

check: lint typecheck test test-tui

need-venv:
	@test -x $(BIN)/python || { echo "no $(VENV) here: run 'make dev' first" >&2; exit 1; }

need-tools: need-venv
	@test -x $(BIN)/ruff -a -x $(BIN)/mypy -a -x $(BIN)/coverage || \
	  { echo "$(VENV) has no development tools: run 'make dev' first" >&2; exit 1; }
