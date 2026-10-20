// Read-only authenticated capacity probe. Use synthetic accounts in staging.
// BASE_URL=https://staging.example k6 run tests/load/k6_smoke.js
// Provide TOKEN via the local environment; it is never logged or saved.
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  vus: Number(__ENV.VUS || 1),
  duration: __ENV.DURATION || "30s",
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<1000"],
    checks: ["rate>0.99"],
  },
};

export function setup() {
  if (!__ENV.BASE_URL || !__ENV.TOKEN) {
    throw new Error("Set BASE_URL and a short-lived synthetic account TOKEN");
  }
}

export default function () {
  const base = __ENV.BASE_URL.replace(/\/$/, "");
  const params = { headers: { Authorization: `Bearer ${__ENV.TOKEN}` }, timeout: "10s" };
  for (const path of ["/api/v1/users/me", "/api/v1/jobs/applications?limit=20"]) {
    const response = http.get(`${base}${path}`, params);
    check(response, { "authenticated read succeeds": (r) => r.status === 200 });
  }
  sleep(2 + Math.random() * 2);
}
