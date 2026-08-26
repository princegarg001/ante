.PHONY: help install test verify mutants world demo replay check clean

PY ?= python

help:
	@echo "install   install the package and dev dependencies"
	@echo "test      fast test suite (excludes slow model-check runs)"
	@echo "verify    exhaustive constraint verification, prints the headline figure"
	@echo "mutants   mutate the regulation and confirm the suite catches it"
	@echo "world     generate a mandate book and check it against market base rates"
	@echo "demo      kill -9 a live batch mid-flight and prove zero double-debits"
	@echo "replay    reconstruct the last demo run from the journal"
	@echo "check     test + verify + mutants — the full compliance gate"

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q -m "not slow"

verify:
	$(PY) -m mandate_recovery.constraints.modelcheck --days 3 --reach-days 6

mutants:
	$(PY) -m tests.mutation

world:
	$(PY) -m mandate_recovery.sim.generate --seed 42
	$(PY) -m mandate_recovery.sim.calibrate --seed 42

demo:
	$(PY) -m demo.crash_demo

replay:
	$(PY) -m mandate_recovery.act.replay runs/crash-demo/journal.jsonl -v

check: test verify world demo mutants

clean:
	rm -rf .pytest_cache .hypothesis **/__pycache__ *.egg-info
