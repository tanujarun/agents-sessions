.PHONY: test lint

test:
	python3 -m unittest discover -s tests -v

lint:
	python3 -m compileall -q src tests
