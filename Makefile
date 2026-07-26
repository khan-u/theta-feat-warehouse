PY ?= python3
CONFIG ?= config/pipeline.yml
NWB ?= ..
GAP ?= 0
FAIL_DEMO ?=
export PYTHONPATH := src

.PHONY: help install synth demo accrual-demo accrual-real full real nwb pipeline dashboard test clean airflow

help:
	@echo "make install       - install dependencies"
	@echo "make demo          - generate synthetic data, run the pipeline, build the dashboard"
	@echo "make accrual-demo  - synthetic data, ingested one subject per run"
	@echo "make accrual-real  - real SBCAT NWB LFP (NWB=<dir|file>), ingested one subject per run"
	@echo "make full          - same at reference scale (32 subjects, 586 channels)"
	@echo "make real          - ingest real SBCAT NWB LFP (NWB=<dir|file>), run pipeline, dashboard"
	@echo "make nwb           - extract cycle features from NWB files only (NWB=<dir|file>)"
	@echo "make pipeline      - run the pipeline against existing data"
	@echo "make dashboard     - build the offline HTML dashboard from the CSV extracts"
	@echo "make test          - run unit tests"
	@echo "make airflow       - start a local Airflow with this DAG"
	@echo "make clean         - remove the warehouse and generated data"

install:
	$(PY) -m pip install -r requirements.txt

synth:
	$(PY) -m theta_warehouse.cli --config $(CONFIG) synth --profile demo

demo: clean
	$(PY) -m theta_warehouse.cli --config $(CONFIG) synth --profile demo
	$(PY) -m theta_warehouse.cli --config $(CONFIG) run-all
	$(PY) dashboard/build_dashboard.py

accrual-demo: clean
	$(PY) -m theta_warehouse.cli --config $(CONFIG) synth --profile demo
	GAP="$(GAP)" FAIL_DEMO="$(FAIL_DEMO)" PY="$(PY)" CONFIG="$(CONFIG)" ./scripts/accrual.sh

accrual-real: clean
	$(PY) -m theta_warehouse.cli --config $(CONFIG) nwb $(NWB)
	GAP="$(GAP)" FAIL_DEMO="$(FAIL_DEMO)" PY="$(PY)" CONFIG="$(CONFIG)" ./scripts/accrual.sh

full: clean
	$(PY) -m theta_warehouse.cli --config $(CONFIG) synth --profile full
	$(PY) -m theta_warehouse.cli --config $(CONFIG) run-all
	$(PY) dashboard/build_dashboard.py

real: clean
	$(PY) -m theta_warehouse.cli --config $(CONFIG) nwb $(NWB)
	$(PY) -m theta_warehouse.cli --config $(CONFIG) run-all
	$(PY) dashboard/build_dashboard.py

nwb:
	$(PY) -m theta_warehouse.cli --config $(CONFIG) nwb $(NWB)

pipeline:
	$(PY) -m theta_warehouse.cli --config $(CONFIG) run-all
	$(PY) dashboard/build_dashboard.py

dashboard:
	$(PY) dashboard/build_dashboard.py

test:
	$(PY) -m pytest tests -q

airflow:
	AIRFLOW_HOME=$(PWD)/airflow_home \
	AIRFLOW__CORE__DAGS_FOLDER=$(PWD)/dags \
	AIRFLOW__CORE__LOAD_EXAMPLES=False \
	THETA_WAREHOUSE_CONFIG=$(PWD)/$(CONFIG) \
	PYTHONPATH=$(PWD)/src \
	$(PY) -m airflow standalone

clean:
	rm -rf warehouse data/cycle_features data/trial_metadata
