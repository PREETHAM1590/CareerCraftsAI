// Real-user load test: concurrent people browsing with think time, not one URL
// at a fixed rate. Steps the number of simultaneous users up until the site breaks.
//
//   k6 run tests/load/k6_users.js                         # visitors only
//   k6 run -e TOKENS_FILE=tokens.txt tests/load/k6_users.js  # + signed-in members
//
// visitors  anonymous marketing journey: home (+ its JS/CSS/fonts on a first
//           visit), pricing, docs or login, with 3-10 s reading time between pages.
// members   signed-in dashboard journey (read-only, no agent runs): the dashboard's
//           own API calls in parallel, the approvals poll, then the jobs page.
//           Needs one synthetic-account token per line in TOKENS_FILE; each VU
//           uses its own account so per-user rate limits behave as in production.
// Optional: -e USERS=250,500,1000,1500,2000,3000 -e STEP_SECONDS=120
//           -e MEMBER_SHARE=0.3 (fraction of users signed in, when tokens exist)
// Each step aborts the run when errors > 5% or page p95 > 3 s.

import http from "k6/http";
import { check, sleep } from "k6";
import exec from "k6/execution";
import { SharedArray } from "k6/data";

const SITE = __ENV.SITE || "https://careercraftsai.me";
const API = __ENV.API || "https://api.careercraftsai.me/api/v1";
const USERS = (__ENV.USERS || "250,500,1000,1500,2000,3000").split(",").map(Number);
const STEP_SECONDS = Number(__ENV.STEP_SECONDS || 120);
const RAMP_SECONDS = 30;
const TOKENS = new SharedArray("tokens", () =>
  __ENV.TOKENS_FILE ? open(__ENV.TOKENS_FILE).split(/\r?\n/).filter(Boolean) : [],
);
const MEMBER_SHARE = TOKENS.length ? Number(__ENV.MEMBER_SHARE || 0.3) : 0;

function stages(share) {
  const out = [];
  for (const n of USERS) {
    const users = Math.round(n * share);
    out.push({ target: users, duration: `${RAMP_SECONDS}s` });
    out.push({ target: users, duration: `${STEP_SECONDS - RAMP_SECONDS}s` });
  }
  out.push({ target: 0, duration: "20s" });
  return out;
}

function currentStep() {
  const t = exec.instance.currentTestRunDuration / 1000;
  const i = Math.floor(t / STEP_SECONDS);
  if (i >= USERS.length) return "rampdown";
  return t - i * STEP_SECONDS < RAMP_SECONDS ? "ramp" : String(USERS[i]);
}

function thresholds() {
  const out = { http_req_failed: [{ threshold: "rate<0.05", abortOnFail: true, delayAbortEval: "60s" }] };
  for (const n of USERS) {
    out[`http_req_failed{step:${n}}`] = [{ threshold: "rate<0.05", abortOnFail: true }];
    out[`http_req_duration{step:${n},kind:page}`] = [{ threshold: "p(95)<3000", abortOnFail: true }];
    out[`http_req_duration{step:${n},kind:api}`] = ["p(95)<3000"];
  }
  return out;
}

const scenarios = {
  visitors: {
    executor: "ramping-vus",
    exec: "visitor",
    startVUs: 0,
    stages: stages(1 - MEMBER_SHARE),
    gracefulRampDown: "10s",
  },
};
if (MEMBER_SHARE > 0) {
  scenarios.members = {
    executor: "ramping-vus",
    exec: "member",
    startVUs: 0,
    stages: stages(MEMBER_SHARE),
    gracefulRampDown: "10s",
  };
}

export const options = {
  scenarios,
  thresholds: thresholds(),
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
  summaryTimeUnit: "ms",
  discardResponseBodies: true,
  userAgent: "careercraft-k6-users/1.0",
  tags: { testid: `users-${new Date().toISOString().slice(0, 16)}` },
};

// Static assets each page references, collected once (a first-time visitor
// downloads them; a returning visitor has them cached).
export function setup() {
  const assets = {};
  for (const path of ["/", "/pricing", "/docs", "/login"]) {
    const res = http.get(`${SITE}${path}`, { responseType: "text" });
    const found = new Set();
    for (const m of (res.body || "").matchAll(/(?:src|href)="(\/_next\/[^"]+)"/g)) found.add(m[1]);
    assets[path] = [...found];
  }
  return { assets };
}

const think = (lo, hi) => sleep(lo + Math.random() * (hi - lo));

function page(path, data, firstVisit, step) {
  const res = http.get(`${SITE}${path}`, {
    headers: { "Accept-Encoding": "gzip, br" },
    tags: { name: `page ${path}`, kind: "page", step },
  });
  check(res, { "page 200": (r) => r.status === 200 });
  if (firstVisit && data.assets[path].length) {
    http.batch(
      data.assets[path].map((a) => ({
        method: "GET",
        url: `${SITE}${a}`,
        params: { headers: { "Accept-Encoding": "gzip, br" }, tags: { name: "asset", kind: "asset", step } },
      })),
    );
  }
}

export function visitor(data) {
  const step = currentStep();
  const firstVisit = Math.random() < 0.6;
  page("/", data, firstVisit, step);
  think(3, 10);
  page("/pricing", data, firstVisit, currentStep());
  think(3, 10);
  page(Math.random() < 0.5 ? "/docs" : "/login", data, firstVisit, currentStep());
  think(5, 15);
}

const DASHBOARD = [
  "/users/me",
  "/users/me/stats",
  "/notifications",
  "/jobs/applications?status=saved&limit=3",
  "/agents/runs?status=awaiting_approval&limit=100",
  "/metrics/agent",
  "/rag/documents?doc_type=resume",
  "/integrations",
];

function api(paths, token, step) {
  const responses = http.batch(
    paths.map((p) => ({
      method: "GET",
      url: `${API}${p}`,
      params: {
        headers: { Authorization: `Bearer ${token}` },
        tags: { name: `api ${p.split("?")[0]}`, kind: "api", step },
        timeout: "15s",
      },
    })),
  );
  for (const r of responses) check(r, { "api 2xx": (x) => x.status >= 200 && x.status < 300 });
}

export function member() {
  const token = TOKENS[(exec.vu.idInTest - 1) % TOKENS.length];
  api(DASHBOARD, token, currentStep());
  // The dashboard re-polls approvals while open; a member reads it for ~1 minute.
  for (let i = 0; i < 4; i++) {
    think(10, 20);
    api(["/agents/runs?status=awaiting_approval&limit=100"], token, currentStep());
  }
  api(["/jobs/applications"], token, currentStep());
  think(10, 30);
  api(["/users/me", "/notifications"], token, currentStep());
  think(10, 30);
}
