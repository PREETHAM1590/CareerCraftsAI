// Capacity test: step the request rate up until the app breaks, then stop.
//
//   k6 run -e SCENARIO=api tests/load/k6_capacity.js
//
// SCENARIO
//   api   GET https://api.careercraftsai.me/health (public; nginx + uvicorn +
//         middleware; the deep check inside is cached 5s)
//   web   GET https://careercraftsai.me/ (Next.js marketing page)
//   auth  signed-in API mix; needs -e TOKEN=<Clerk JWT, long-lived template>
// Optional: -e MAX_RPS=400 -e STEP_SECONDS=60 -e START_RPS=10
//
// The test aborts itself once errors exceed 5% or p95 exceeds 3s for 30s, so
// it finds the breaking point without hammering production past it.
// Results: end-of-test summary (p50/p90/p95/p99) + Grafana "k6 Prometheus"
// when run with the Prometheus remote-write output (see run_k6.sh).

import http from "k6/http";
import { check } from "k6";

const SCENARIO = __ENV.SCENARIO || "api";
const MAX_RPS = Number(__ENV.MAX_RPS || 400);
const START_RPS = Number(__ENV.START_RPS || 10);
const STEP_SECONDS = Number(__ENV.STEP_SECONDS || 60);
const TOKEN = __ENV.TOKEN || "";

if (SCENARIO === "auth" && !TOKEN) {
  throw new Error("SCENARIO=auth needs -e TOKEN=<Clerk JWT>");
}

// 10, 25, 50, 75, 100, 150, 200, 300, 400, ... up to MAX_RPS, each held STEP_SECONDS.
function steps() {
  const ladder = [START_RPS, 25, 50, 75, 100, 150, 200, 300, 400, 500, 650, 800, 1000];
  const out = [];
  for (const rps of ladder.filter((r) => r >= START_RPS && r <= MAX_RPS)) {
    out.push({ target: rps, duration: "10s" }); // ramp to the step
    out.push({ target: rps, duration: `${STEP_SECONDS - 10}s` }); // hold it
  }
  return out;
}

export const options = {
  scenarios: {
    [SCENARIO]: {
      executor: "ramping-arrival-rate",
      startRate: START_RPS,
      timeUnit: "1s",
      preAllocatedVUs: 50,
      maxVUs: 1500,
      stages: steps(),
    },
  },
  thresholds: {
    http_req_failed: [{ threshold: "rate<0.05", abortOnFail: true, delayAbortEval: "30s" }],
    http_req_duration: [{ threshold: "p(95)<3000", abortOnFail: true, delayAbortEval: "30s" }],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
  tags: { testid: `${SCENARIO}-${new Date().toISOString().slice(0, 16)}` },
  userAgent: "careercraft-k6-capacity/1.0",
  discardResponseBodies: true,
};

const AUTH_ROUTES = [
  "/api/v1/users/me",
  "/api/v1/jobs/applications",
  "/api/v1/rag/documents",
];

export default function () {
  let res;
  if (SCENARIO === "api") {
    res = http.get("https://api.careercraftsai.me/health", { tags: { name: "health" } });
  } else if (SCENARIO === "web") {
    res = http.get("https://careercraftsai.me/", { tags: { name: "home" } });
  } else {
    const path = AUTH_ROUTES[Math.floor(Math.random() * AUTH_ROUTES.length)];
    res = http.get(`https://api.careercraftsai.me${path}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      tags: { name: path },
    });
  }
  check(res, { "status 2xx": (r) => r.status >= 200 && r.status < 300 });
}
