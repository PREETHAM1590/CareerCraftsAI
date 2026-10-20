# Measure capacity before increasing concurrency

Use `k6_smoke.js` with synthetic accounts on staging. It performs only profile
and application-list reads. Supply BASE_URL and a short-lived TOKEN in the local
environment; start with VUS=1, then 5 and 10. Do not bypass per-user rate limits:
use multiple synthetic identities when measuring aggregate capacity.

Record p50/p95 latency, request errors, rate-limit responses, CPU/RAM, PostgreSQL
connections/wait time, Redis memory, Temporal queue age and provider timeouts.
The thresholds in the script are initial acceptance targets, not measured results.
This workload does not measure LLM, search, browser or submission capacity.

The API pool ceiling per process is DB_POOL_SIZE + DB_MAX_OVERFLOW. The synchronous
agent ceiling is SYNC_DB_POOL_SIZE + SYNC_DB_MAX_OVERFLOW. Multiply each by the
process count and reserve connections for Temporal, migrations and administration.
Notification and email activity concurrency have separate controls. Increase one
control at a time only when CPU, memory, DB and provider budgets permit it.

Search adapters share four threads per worker process. Up to five roles and five
cities are accepted; the overall search budget returns partial results with a
warning. Do not increase adapter concurrency to hide slow external providers.

The existing `k6_capacity.js` and `run_k6.sh` provide the step-ramp/Grafana
workflow. Keep the small authenticated smoke probe separate from that capacity
workload; neither provides a capacity claim until run against a suitable target.
