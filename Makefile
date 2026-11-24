.PHONY: test coverage

test:
	python -m unittest discover -s tests

coverage:
	uv run --with coverage coverage run --source=src/elisa -m unittest discover -s tests
	uv run --with coverage coverage report -m --fail-under=90