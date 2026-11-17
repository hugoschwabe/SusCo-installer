SHELL := /bin/bash
PYTHON ?= .venv/bin/python
CONFIG ?= configs/experiment.yaml
HOST_CONFIG ?= configs/host.local.yaml
RESULTS ?= results/latest

.PHONY: help install verify test pilot run analyze unit data preflight restore-cpu clean

help:
	@printf '%s\n' \
	  'SusCo targets:' \
	  '  make install     Provision the Linux measurement host (sudo)' \
	  '  make verify      Independently verify the installation' \
	  '  make test        Five-question end-to-end smoke run' \
	  '  make pilot       100-question pilot, one repetition' \
	  '  make run         Full GSM8K run, three repetitions' \
	  '  make analyze     Analyze results/latest and write plots' \
	  '  make unit        Run pytest' \
	  '  make data        Download and prepare GSM8K' \
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
	./scripts/run_experiment.sh --profile full --config "$(CONFIG)" --host-config "$(HOST_CONFIG)"

analyze:
	$(PYTHON) -m susco.analysis.report --results "$(RESULTS)"

unit:
	$(PYTHON) -m pytest

data:
	$(PYTHON) -m susco.data prepare --output data/processed/gsm8k_test.jsonl

preflight:
	$(PYTHON) -m susco.preflight --config "$(CONFIG)" --host-config "$(HOST_CONFIG)"

restore-cpu:
	sudo ./scripts/setup_linux.sh --restore-cpu

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
