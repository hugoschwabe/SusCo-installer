SHELL := /bin/bash
PYTHON ?= .venv/bin/python
CONFIG ?= configs/experiment.yaml
HOST_CONFIG ?= configs/host.local.yaml
RESULTS ?= results/latest
QUESTIONS ?=
SEED ?=
RUN_ARGS ?=
RUN_FLAGS := $(if $(QUESTIONS),--limit $(QUESTIONS)) $(if $(SEED),--seed $(SEED)) $(RUN_ARGS)

.PHONY: help install verify test pilot run analyze matrix unit data preflight restore-cpu clean

help:
	@printf '%s\n' \
	  'SusCo targets:' \
	  '  make install     Provision the Linux measurement host (sudo)' \
	  '  make verify      Independently verify the installation' \
	  '  make test        Five-question end-to-end smoke run' \
	  '  make pilot       100-question pilot, one repetition' \
	  '  make run         Full MATH-500 run, three repetitions' \
	  '                   e.g. make run QUESTIONS=100 SEED=7' \
	  '  make analyze     Analyze results/latest and write plots' \
	  '  make matrix      Print the thinking-worth matrix for results/latest' \
	  '  make unit        Run pytest' \
	  '  make data        Download, prepare and summarize MATH-500' \
	  '  make preflight   Check services, model, dataset and Kepler' \
	  '  make restore-cpu Restore the saved CPU governor/turbo state' \
	  '  make clean       Remove Python caches (results are kept)'

install:
	sudo ./scripts/setup_linux.sh --skip-k8s --skip-clue

verify:
	sudo ./scripts/verify_linux.sh --skip-k8s --skip-clue

test:
	./scripts/run_experiment.sh --profile test --config "$(CONFIG)" --host-config "$(HOST_CONFIG)"

pilot:
	./scripts/run_experiment.sh --profile pilot --config "$(CONFIG)" --host-config "$(HOST_CONFIG)"

run:
	./scripts/run_experiment.sh --profile full --config "$(CONFIG)" --host-config "$(HOST_CONFIG)" $(RUN_FLAGS)

analyze:
	$(PYTHON) -m susco.analysis.report --results "$(RESULTS)"

matrix:
	$(PYTHON) -m susco.analysis.report --results "$(RESULTS)"

unit:
	$(PYTHON) -m pytest

data:
	$(PYTHON) -m susco.data prepare --output data/processed/math500_test.jsonl
	$(PYTHON) -m susco.data stats --input data/processed/math500_test.jsonl

preflight:
	$(PYTHON) -m susco.preflight --config "$(CONFIG)" --host-config "$(HOST_CONFIG)"

restore-cpu:
	sudo ./scripts/setup_linux.sh --restore-cpu

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
