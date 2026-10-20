#!/usr/bin/env bash
# Run the capacity test and stream its metrics into Grafana ("k6 Prometheus"
# dashboard, filter by testid). Needs the k6 ingest password, which lives on
# the APP VM in /opt/careercraft-secrets/monitoring.env (k6_INGEST_PASSWORD):
#
#   K6_INGEST_PASSWORD=... tests/load/run_k6.sh api        # or web / auth
#   K6_INGEST_PASSWORD=... TOKEN=<jwt> tests/load/run_k6.sh auth
#
# Extra k6 flags pass through, e.g. -e MAX_RPS=200.
set -euo pipefail
scenario=${1:-api}
shift || true
here=$(cd "$(dirname "$0")" && pwd)
out="$here/results"
mkdir -p "$out"
stamp=$(date +%Y%m%d-%H%M)

export K6_PROMETHEUS_RW_SERVER_URL=https://grafana.careercraftsai.me/ingest/prometheus
export K6_PROMETHEUS_RW_USERNAME=k6
export K6_PROMETHEUS_RW_PASSWORD=${K6_INGEST_PASSWORD:?set K6_INGEST_PASSWORD}
export K6_PROMETHEUS_RW_TREND_STATS="p(50),p(90),p(95),p(99),min,max,avg"

k6 run -o experimental-prometheus-rw \
  -e SCENARIO="$scenario" ${TOKEN:+-e TOKEN="$TOKEN"} \
  --summary-export "$out/$scenario-$stamp.json" \
  "$@" "$here/k6_capacity.js"
