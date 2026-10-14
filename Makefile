.PHONY: test test-tui lint format typecheck coverage dev check dist dist-check need-venv need-tools

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
# per-module report. Fails when the total is under the floor
# (`fail_under` in pyproject.toml).
coverage: need-tools
	$(BIN)/coverage erase
	$(BIN)/coverage run -m unittest discover -s tests
	$(BIN)/coverage report

# .venv with Textual and the development tools, the project installed editable.
dev:
	test -x $(BIN)/python || python3 -m venv $(VENV)
	$(BIN)/python -m pip install --disable-pip-version-check -e '.[tui,dev]'

check: lint typecheck test test-tui

# An sdist, and a wheel built from that sdist, in dist/ (what was there of an
# earlier build goes first). What the sdist carries: MANIFEST.in.
dist: need-venv
	@$(BIN)/python -c 'import build' 2>/dev/null || \
	  { echo "$(VENV) has no 'build': run 'make dev' again" >&2; exit 1; }
	rm -f dist/*.whl dist/*.tar.gz
	$(BIN)/python -m build --outdir dist

# What `make dist` built, tried: each file installed into a venv of its own
# (dist/check-whl, dist/check-gz) and the installed command run from outside
# the checkout; the sdist carries no tests, and both carry the stylesheet.
dist-check:
	@test -f dist/*.whl -a -f dist/*.tar.gz || { echo "nothing built here: run 'make dist' first" >&2; exit 1; }
	@for built in dist/*.whl dist/*.tar.gz; do \
	  venv="dist/check-$${built##*.}"; \
	  python3 -m venv --clear "$$venv" && \
	  "$$venv/bin/python" -m pip install --quiet --disable-pip-version-check "$$built" && \
	  echo "$$built: $$(cd / && "$(CURDIR)/$$venv/bin/agent-sessions" --version)" || exit 1; \
	done
	@if tar -tzf dist/*.tar.gz | grep -E '^[^/]+/tests(/|$$)'; then \
	  echo "the sdist carries tests (MANIFEST.in prunes them)" >&2; exit 1; fi
	@tar -tzf dist/*.tar.gz | grep -q '/src/agent_sessions/tui/app.tcss$$' || \
	  { echo "the sdist has no tui/app.tcss" >&2; exit 1; }
	@python3 -m zipfile -l dist/*.whl | grep -q 'agent_sessions/tui/app.tcss' || \
	  { echo "the wheel has no tui/app.tcss" >&2; exit 1; }
	@echo "dist: the wheel and the sdist install and run; no tests in the sdist"

need-venv:
	@test -x $(BIN)/python || { echo "no $(VENV) here: run 'make dev' first" >&2; exit 1; }

need-tools: need-venv
	@test -x $(BIN)/ruff -a -x $(BIN)/mypy -a -x $(BIN)/coverage || \
	  { echo "$(VENV) has no development tools: run 'make dev' first" >&2; exit 1; }
