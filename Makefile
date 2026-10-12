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

# Engine suite and TUI suite, measured apart and combined, then the per-module
# report. Two runs because test_smoke checks that the engine leaves Textual
# unimported, which cannot hold in a process that also loads the TUI app tests.
ENGINE_TESTS := $(filter-out test_tui_app,$(basename $(notdir $(wildcard tests/test_*.py))))

coverage: need-tools
	$(BIN)/coverage erase
	PYTHONPATH=tests $(BIN)/coverage run -p -m unittest $(ENGINE_TESTS)
	$(BIN)/coverage run -p -m unittest discover -s tests -p 'test_tui*.py'
	$(BIN)/coverage combine
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
