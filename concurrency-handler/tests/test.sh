#!/bin/bash
set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="/tmp/verifier_results"
mkdir -p "$RESULTS_DIR" /logs/verifier

python3 -m pytest -p no:cacheprovider "$TEST_DIR/fixtures/test_inventory_regresssion.py" -q --tb=short
REGRESSION_PASS=$?
if [ "$REGRESSION_PASS" -eq 0 ]; then REGRESSION_PASS=1; else REGRESSION_PASS=0; fi

python3 "$TEST_DIR/duplicate_race.py" --app /app --out "$RESULTS_DIR/duplicate_race.json"

python3 "$TEST_DIR/stress.py" \
  --app /app \
  --jobs "$TEST_DIR/fixtures/jobs_stress.jsonl" \
  --inventory "$TEST_DIR/fixtures/initial_inventory_stress.json" \
  --out "$RESULTS_DIR/stress.json"

python3 "$TEST_DIR/grade_report.py" --app /app --out "$RESULTS_DIR/artifact.json"

python3 "$TEST_DIR/aggregate_reward.py" \
  --regression-pass "$REGRESSION_PASS" \
  --duplicate-race "$RESULTS_DIR/duplicate_race.json" \
  --stress "$RESULTS_DIR/stress.json" \
  --artifact "$RESULTS_DIR/artifact.json" \
  --out /logs/verifier/reward.json

exit 0
