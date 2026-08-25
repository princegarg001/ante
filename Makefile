.PHONY: help install test verify mutants check clean

PY ?= python

help:
	@echo "install   install the package and dev dependencies"
	@echo "test      fast test suite (excludes slow model-check runs)"
	@echo "verify    exhaustive constraint verification, prints the headline figure"
	@echo "mutants   mutate the regulation and confirm the suite catches it"
	@echo "check     test + verify + mutants — the full compliance gate"

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q -m "not slow"

verify:
	$(PY) -m mandate_recovery.constraints.modelcheck --days 3 --reach-days 6

mutants:
	$(PY) -m tests.mutation

check: test verify mutants

clean:
	rm -rf .pytest_cache .hypothesis **/__pycache__ *.egg-info
