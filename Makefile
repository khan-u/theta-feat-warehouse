PY ?= python3
CONFIG ?= config/pipeline.yml
export PYTHONPATH := src

.PHONY: help install clean

help:
	@echo "make install  - install dependencies"
	@echo "make clean    - remove the warehouse and generated data"

install:
	$(PY) -m pip install -r requirements.txt

clean:
	rm -rf warehouse data/cycle_features data/trial_metadata
