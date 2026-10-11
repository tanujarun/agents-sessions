.PHONY: test test-tui lint

test:
	python3 -m unittest discover -s tests -v

test-tui:
	.venv/bin/python -m unittest discover -s tests -p 'test_tui*.py' -v

lint:
	python3 -m compileall -q src tests
