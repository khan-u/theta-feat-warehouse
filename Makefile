PY ?= python3
CONFIG ?= config/pipeline.yml
export PYTHONPATH := src

.PHONY: help install test clean

help:
	@echo "make install  - install dependencies"
	@echo "make test     - run unit tests"
	@echo "make clean    - remove the warehouse and generated data"

install:
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest tests -q

clean:
	rm -rf warehouse data/cycle_features data/trial_metadata
