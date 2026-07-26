#!/usr/bin/env bash
# Runs the pipeline once per subject arrival.
#
# Ingests the subjects already present under data/cycle_features one at a time
# across successive pipeline runs: run-01 sees one subject, run-02 two, and so on
# until the final run sees them all. Because each run's cycle features and trial
# metadata are staged together, every accrual run is internally consistent and
# passes the data-quality gate, so Run Health shows data accruing over time
# instead of a single point.
#
# It runs on whatever populated data/ beforehand, so it is source-agnostic:
#   make accrual-demo  -> synthetic fixtures
#   make accrual-real  -> real SBCAT NWB LFP
#
# Variables (override on the make command line, e.g. `make accrual-real GAP=90`):
#   PY         Python interpreter             (default: python3)
#   CONFIG     pipeline config file           (default: config/pipeline.yml)
#   GAP        seconds to wait between runs   (default: 0; set >0 for
#              wall-clock-spaced started_at timestamps)
#   FAIL_DEMO  when set (e.g. FAIL_DEMO=1), prepend one run that the DQ gate
#              blocks: it fires while every subject's trial log is registered but
#              only the first subject's cycle features have landed, so
#              trial_coverage_gaps fails and a real failed run-00 is recorded.
#              Demonstrates the gate rejecting incomplete data.
set -euo pipefail

PY="${PY:-python3}"
CONFIG="${CONFIG:-config/pipeline.yml}"
GAP="${GAP:-0}"
FAIL_DEMO="${FAIL_DEMO:-}"

cli() { PYTHONPATH=src "$PY" -m theta_warehouse.cli --config "$CONFIG" "$@"; }

# Start run history clean; leave the already-extracted data in place.
rm -rf warehouse

# Discover the subjects present, in stable sorted order.
subjects=()
for dir in data/cycle_features/*/; do
  subjects+=("$(basename "$dir")")
done
IFS=$'\n' subjects=($(sort <<<"${subjects[*]}")); unset IFS
total=${#subjects[@]}
if [ "$total" -eq 0 ]; then
  echo "no subjects found under data/cycle_features" >&2
  exit 1
fi

# Restore anything held on exit, even if a run fails.
hold="$(mktemp -d)"
mkdir -p "$hold/cycle_features" "$hold/trial_metadata"
restore() {
  mv "$hold"/cycle_features/* data/cycle_features/ 2>/dev/null || true
  mv "$hold"/trial_metadata/* data/trial_metadata/ 2>/dev/null || true
  rm -rf "$hold"
}
trap restore EXIT

# Hold every subject's cycle features except the first, so they arrive one per run.
for subject in "${subjects[@]:1}"; do
  mv "data/cycle_features/$subject" "$hold/cycle_features/"
done

# Optional gate demonstration: run once while all trial logs are registered but
# only the first subject's cycles are present. trial_coverage_gaps blocks it and
# records a failed run-00 before any incomplete result reaches the marts.
if [ -n "$FAIL_DEMO" ] && [ "$FAIL_DEMO" != "0" ]; then
  printf '=== run-00: trial logs present, cycle features incomplete -> DQ gate blocks ===\n'
  cli run-all --run-id run-00 || true
  if [ "$GAP" -gt 0 ]; then sleep "$GAP"; fi
fi

# Hold the held subjects' trial metadata too, so each accrual run is consistent.
for subject in "${subjects[@]:1}"; do
  mv "data/trial_metadata/${subject}_trial_metadata.csv" "$hold/trial_metadata/"
done

# One pipeline run per subject arrival.
for ((i = 0; i < total; i++)); do
  if [ "$i" -gt 0 ]; then
    subject="${subjects[$i]}"
    mv "$hold/cycle_features/$subject" data/cycle_features/
    mv "$hold/trial_metadata/${subject}_trial_metadata.csv" data/trial_metadata/
    if [ "$GAP" -gt 0 ]; then sleep "$GAP"; fi
  fi
  run_id="$(printf 'run-%02d' "$((i + 1))")"
  printf '=== %s: %d of %d subject(s) present ===\n' "$run_id" "$((i + 1))" "$total"
  cli run-all --run-id "$run_id"
done

# Build the offline dashboard from the accrued extracts.
"$PY" dashboard/build_dashboard.py
