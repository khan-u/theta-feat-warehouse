PY ?= python3
CONFIG ?= config/pipeline.yml
NWB ?= ..
export PYTHONPATH := src

.PHONY: help install synth nwb test clean

help:
	@echo "make install  - install dependencies"
	@echo "make synth    - generate synthetic cycle-feature data"
	@echo "make nwb      - extract cycle features from NWB files (NWB=<dir|file>)"
	@echo "make test     - run unit tests"
	@echo "make clean    - remove the warehouse and generated data"

install:
	$(PY) -m pip install -r requirements.txt

synth:
	$(PY) -m theta_warehouse.cli --config $(CONFIG) synth --profile demo

nwb:
	$(PY) -m theta_warehouse.cli --config $(CONFIG) nwb $(NWB)

test:
	$(PY) -m pytest tests -q

clean:
	rm -rf warehouse data/cycle_features data/trial_metadata
