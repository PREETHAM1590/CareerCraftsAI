# Scaling CareerCraft AI

Measured 2026-10-20. Everything labelled "estimate" is not measured. Everything else comes from the k6 runs and in-process benchmarks described below.

---

## 1. TL;DR

- The API (`GET /health`, the cheapest route) was clean up to 150 rps (p95 19 ms) and degraded at 200 rps (p95 471 ms). At a 300 rps target it collapsed to 93 rps achieved.
- The bottleneck was not the VM. Host CPU was 33-42% while the backend container sat at 98% of its `cpus: 0.5` cap, with a single uvicorn worker.
- Each request cost about 2.4 ms of CPU in production. 0.5 core / 2.4 ms is about 210 rps, which matches the knee. About 1.15 ms of the per-request cost was three `BaseHTTPMiddleware` wrappers (bare FastAPI route 0.26 ms, same route through our middleware stack 1.41 ms).
- Fixed on branch `perf/api-throughput`: one pure-ASGI middleware (about 5x less overhead on a local benchmark), a verified-token cache, a Redis-backed rate limiter, `--workers 2`, backend `cpus: 1.5`, Postgres `cpus: 1.0`. Not yet re-measured on the VM.
- The website test did not find the limit. It stopped at the test PC's download speed (about 62 Mbps), not the server.
- What the traffic numbers mean in rps:

| Target | Average rps | Notes |
|---|---|---|
| 10M requests/day | about 116 | Peak is 3-5x average (estimate), so about 350-580 rps |
| 10M requests/hour | about 2,778 | Needs several cores of work; not one 2-vCPU VM for real routes |
| 10M requests/second | 10,000,000 | Impossible on one box. Not a realistic target for this product |

- "2 CPUs should handle 10 million requests": yes for 10M per day, and 2 CPUs only handled 300 rps because the container was capped at half a core running one Python process. Not for 10M per hour on authenticated, DB-backed routes.
- Do not rewrite in Go or Fastify now (section 4).

---

## 2. What we measured

### Infrastructure under test

| Item | Value |
|---|---|
| APP VM | Oracle ARM, 2 vCPU, 11 GB RAM, Docker 29 |
| APP VM runs | backend (FastAPI/uvicorn), frontend (Next.js), temporal-worker, notification-worker, Postgres 16 + pgvector, Redis, gateway nginx, sandbox-server, Temporal server |
| INFRA VM | 2 vCPU, 11 GB RAM; Nango + Ollama |
| TLS | Host nginx + certbot on APP |
| Monitoring | https://grafana.careercraftsai.me: "CareerCraft · APP server", "CareerCraft · INFRA server", "k6 Prometheus" |

Limits before tuning (`deploy/oracle-vm/compose.yml`):

| Service | `cpus` |
|---|---|
| backend | 0.5, single uvicorn worker |
| frontend | 0.5 |
| postgres | 0.5 |
| temporal-worker | 0.75 |
| host nginx | `worker_connections 768` |

### Test 1: API, `GET /health`

Tool: `tests/load/k6_capacity.js`, step ramp, run from a home PC. Auto-aborts at more than 5% errors or p95 above 3 s. `/health` is the cheapest route (deep check cached 5 s), so this is a ceiling for the API, not a typical route.

| Target rps | Achieved rps | p50 | p95 | p99 | Success | Host CPU |
|---|---|---|---|---|---|---|
| 10-100 | on target | 18 ms | 19-20 ms | 26-61 ms | ~100% | - |
| 150 | 150 | - | 19 ms | 80 ms | 100% | 33% |
| 200 | 196 | - | 471 ms | 730 ms | - | 42% |
| 300 | 93 (collapse) | - | 3.0 s | 4.6 s (max 11.3 s) | 95.5%, 182 failures (resets/EOF) | - |

Supporting observations:

- Backend container used 0.49 cores = 98% of its 0.5 cap while the host was at 42%.
- Host nginx logged `768 worker_connections are not enough`.
- Server-side nginx request time stayed at 2-3 ms up to 150 rps. The latency was queueing in the capped backend, not slow handling elsewhere.

### Test 2: Website, `GET https://careercraftsai.me/`

Next.js page, about 90 KB.

| Target rps | Achieved | Client p50 | Client p95 | Notes |
|---|---|---|---|---|
| 10-50 | on target | 49 ms | 53-56 ms | clean |
| 75 | 66 | - | 2.4 s | server-side nginx p95 was 12 ms |

Server egress peaked at 7.8 MB/s (about 62 Mbps), which is the test PC's download limit. The true website limit was not found; it is above 75 rps.

### Per-request CPU

Benchmarks run in-process inside the production backend container (ARM):

| Case | CPU per request |
|---|---|
| Bare FastAPI route | 0.26 ms |
| Same route through the app's middleware stack | 1.41 ms |
| Difference (three `@app.middleware("http")` wrappers: JWT, request-id, security headers, plus CORS) | about 1.15 ms |
| Production at 150 rps (0.36 cores / 150 rps), includes uvicorn HTTP parsing | about 2.4 ms |

Theoretical max at the 0.5-core cap: 0.5 / 2.4 ms is about 210 rps. Measured knee: between 150 and 200 rps.

### Effect of the fix (local x86 benchmark, not the prod VM)

| Path | Before | After |
|---|---|---|
| Public route, CPU/request | 0.691 ms | 0.312 ms |
| Rejected-auth path, CPU/request | 0.461 ms | 0.164 ms |
| Overhead above bare FastAPI | 0.49 ms | 0.10 ms (about 5x less) |

---

## 3. Bottlenecks, in order

| # | Bottleneck | Evidence | Status |
|---|---|---|---|
| 1 | Backend CPU cap (0.5) and a single uvicorn worker | Container at 98% of cap while host at 42%; 0.5 core / 2.4 ms matches the knee | Fixed on branch: `--workers 2`, `cpus: 1.5` |
| 2 | `BaseHTTPMiddleware` overhead | 1.41 ms vs 0.26 ms for the same route; about 1.15 ms of the 2.4 ms was middleware | Fixed on branch: single pure-ASGI `ApiRequestMiddleware` in `backend/app/core/request_middleware.py` |
| 3 | Host nginx `worker_connections 768` | Log line "768 worker_connections are not enough" during the 300 rps step; resets/EOF failures | Not fixed. Raise to 4096 (Stage 1) |
| 4 | Per-request auth DB work | Every authenticated request does JWT verify, `SELECT users` by clerk id (`get_or_provision_user`), pool pre-ping, commit | Token cache done; user-row cache deliberately not done (see section 5) |
| 5 | Postgres at 0.5 core for real routes | Not measured. `/health` barely touches the DB, so it cannot show this | Raised to `cpus: 1.0` on branch. Needs an authenticated k6 run to find the actual limit |
| 6 | Client bandwidth in the web test | 62 Mbps egress equals the test PC's download limit; server p95 was 12 ms | Test artefact, not a server limit. Run from a cloud VM |

Notes on each:

- **1 and 2** explain the whole 300 rps result. Adding `--workers 2` only helps if the CPU ceiling is raised too, which is why both changed together. Caches are per-process with two workers; rate limits must therefore live in Redis (done, and it fails open if Redis is down).
- **3** is cheap to fix and will otherwise become the next wall once the backend stops being the limit.
- **4** matters more than the JWT crypto. RS256 verification itself is only 0.031 ms; the token cache's main saving is skipping a thread hop per authenticated request. Clerk tokens rotate about every 60 s, and the frontend polls every 5-30 s, so most authenticated requests should hit the cache. The cache is a bounded LRU keyed by `sha256(token)`, never held past `exp`, successes only (`backend/app/core/clerk_auth.py`).
- **5** is the honest unknown. DB pools are async 10+5 and sync 5+10 per process (`backend/app/core/database.py`); with two workers that doubles. Postgres `max_connections` is 100 and 24 are in use at idle, so keep an eye on headroom before adding workers (this is the case for PgBouncer in Stage 2).

---

## 4. Go or Fastify?

**Recommendation: no, not now.**

Reasons:

- **The framework was not the problem.** A bare FastAPI route costs 0.26 ms. The extra 1.15 ms was our middleware design, and it is fixed (overhead above bare FastAPI is now about 0.10 ms locally). Rewriting to get what a middleware change already delivered is not a good trade.
- **Real requests are Postgres-bound.** A typical authenticated route is dominated by queries, not by HTTP parsing. A faster HTTP layer does not make Postgres faster.
- **Agent work is LLM-bound.** Agent runs spend seconds per LLM call. The entire agent stack is Python: LangGraph, LangChain, Temporal Python workers, Playwright/browser-use, RAG on pgvector. A rewrite would not change LLM or DB latency.
- **Cost is months, not days.** It means re-implementing the API and either re-implementing or bridging the Python agent stack, and re-earning confidence on the 1,450 backend tests.
- **Fastify buys little.** For a DB-bound app, tuned FastAPI with 2+ workers and Fastify are in the same range. You would pay the rewrite cost for a rounding error and lose the shared Python code with the workers.

When it would make sense: a **separate, high-QPS edge service**, for example a public job-search or catalog read API, once there is a measured sustained need above roughly 1-2k rps that caching and horizontal scaling cannot cover. Build that slice in Go only. Leave the app, agents and workers in Python.

---

## 5. Caching: what helps and what does not

### Does not help much

- **Short-TTL response caching of per-user endpoints** such as `/agents/runs` (polled every 5 s), awaiting-approval runs (10 s) and `/notifications` (30 s). Each client asks once per interval, so a TTL shorter than the interval never gets a hit, and a longer TTL shows stale data. The saving is close to zero.

### Helps

| Cache / change | What it removes | Status |
|---|---|---|
| Verified-token cache (in-process LRU) | Thread hop per authenticated request | Done on branch |
| Auth user-row cache | The `SELECT users` per authenticated request (well under 1 ms on loopback) | **Not doing**: that row drives the consent (`policy_accepted_at`) and erasure (`deletion_scheduled_for`) gates, and a per-worker cache could wrongly 403 a user who just accepted the terms, or admit one whose erasure is due |
| CDN / edge cache (Cloudflare or similar) for public marketing pages and static Next assets (`/_next/static`) | Most website traffic never reaches the VM; removes the egress limit too | Not done |
| HTTP caching headers on static assets | Repeat downloads by returning visitors | Not done |
| Shared job-catalog/search result cache in Redis, keyed by normalised query | Repeated identical public-data queries across users | Not done |
| Move `/agents/runs` and `/notifications` from polling to the existing Redis SSE bus | The largest steady-state request source | In progress on branch `perf/frontend-polling` |

On the polling item: the frontend currently polls `/agents/runs` every 5 s, awaiting-approval every 10 s, `/notifications` every 30 s on every page, the companion every 3 s while active, and `sse.ts` reconciles every 3 s. Replacing polls with pushed events, and keeping a slow reconcile as a safety net, cuts more requests than any response cache can. This is the highest-value change in this section.

Caches are per-process with `--workers 2`. That is fine for the token cache (a miss just costs one verification), but anything that must be consistent across workers belongs in Redis.

---

## 6. Scaling roadmap

### Stage 1: now, same VM

- Merge `perf/api-throughput`: pure-ASGI middleware, token cache, Redis rate-limit storage, `--workers 2`, backend `cpus: 1.5`, Postgres `cpus: 1.0`.
- Raise host nginx `worker_connections` to 4096.
- Land `perf/frontend-polling` (SSE-driven refresh instead of fixed polling).
- Re-run k6 for `api`, `auth` and `web` (section 7).
- Expected `/health` capacity: about 500-1,000 rps (estimate, not measured). Authenticated routes will be lower; the authenticated run decides by how much.
- Note that `cpus` values are ceilings, not reservations. The 2-vCPU host is shared with Postgres, Redis, Temporal, the frontend and the workers, so these ceilings can sum to more than 2 cores. Under combined load they will contend.

### Stage 2: headroom on a single VM

- PgBouncer in front of Postgres, so more uvicorn workers do not exhaust `max_connections` (100). Tune pool sizes after.
- Move Temporal server (and Ollama, if it ends up on APP) off the APP VM if CPU contention shows in Grafana.
- Move to a larger VM. Oracle A1 can go up to 4 OCPU / 24 GB on the free tier.
- Move `/agents/runs` and `/notifications` to SSE (section 5).

### Stage 3: horizontal

- Blocker: uploaded documents live on the local Docker volume `docstore`, shared by backend and temporal-worker. Move them to object storage (OCI Object Storage or any S3-compatible store) before running the backend on a second VM.
- Then 2+ app VMs behind a load balancer (for example OCI LB). These already work across machines: JWT auth (stateless, verified locally via JWKS), the Redis SSE bus, Redis rate limits, and the Temporal task queue.
- Run `temporal-worker` on separate hosts for agent throughput. Agent concurrency is set by `TEMPORAL_WORKER_CONCURRENCY` per replica; max 2 concurrent runs per user is DB-enforced and does not change.
- Postgres: managed Postgres, or a primary plus read replica. Redis may also need to move off the app VMs at this point.

### Stage 4: edge

- CDN in front of the frontend: caches static assets and marketing pages, absorbs most website traffic and bandwidth.
- Only now consider a separate Go service for a public read-heavy API, if measurements justify it (section 4).

---

## 7. How to re-test

Scripts live under `tests/load/` (`k6_capacity.js`, `run_k6.sh`).

```bash
tests/load/run_k6.sh api    # GET /health step ramp
tests/load/run_k6.sh web    # GET https://careercraftsai.me/
tests/load/run_k6.sh auth   # authenticated routes (needs a token)
```

Rules for a valid run:

- **Authenticated scenario:** create a Clerk JWT template named `loadtest` and generate a token from it. This is the run that finds the real (Postgres-bound) limit; the `/health` ramp cannot.
- **Run the web test from a cloud VM**, not a home PC. The last web run measured the home connection (62 Mbps), not the server.
- **Watch Grafana during every run:** "CareerCraft · APP server" (host CPU, per-container CPU against its cap), "CareerCraft · INFRA server", and "k6 Prometheus" (client-side rps, latency, failures).
- **Check both sides.** Compare client-side p95 with server-side nginx request time. If the client is slow and nginx is fast, the problem is the network or the test machine, as in the web test.
- **Check these specific signals:** backend container CPU vs its cap, host nginx error log for `worker_connections`, Postgres connection count vs 100, Redis latency.
- The script auto-aborts at more than 5% errors or p95 above 3 s. Do not raise those thresholds on the production VM.
- Record the numbers and date in this file after each run so the next comparison is against measured data.
