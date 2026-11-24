// Fails when production dependencies carry a high or critical advisory that
// is not listed below. Used by CI; run it from frontend/ after `npm ci`.
import { spawnSync } from "node:child_process";

// Advisories with no patched release in the version line a dependency pins.
// Each entry needs a reason; remove it once the upstream package updates.
const ACCEPTED = {
  "GHSA-mrhx-c7xc-m3w5":
    "zod 3.x (nested under @copilotkit/* and @ag-ui/*) has no patched 3.x release; the fix is 4.6.6+ and CopilotKit pins ^3.",
};

const result = spawnSync("npm", ["audit", "--omit=dev", "--json"], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
let report;
try {
  report = JSON.parse(result.stdout);
} catch {
  console.error("npm audit did not return JSON:\n" + (result.stderr || result.stdout));
  process.exit(2);
}
if (report.error) {
  console.error("npm audit failed: " + (report.error.summary || JSON.stringify(report.error)));
  process.exit(2);
}

const blocking = new Map();
const accepted = new Set();
for (const [name, vuln] of Object.entries(report.vulnerabilities || {})) {
  for (const via of vuln.via || []) {
    if (typeof via !== "object" || !["high", "critical"].includes(via.severity)) continue;
    const id = String(via.url || "").split("/").pop();
    if (ACCEPTED[id]) accepted.add(id);
    else blocking.set(id, `${via.severity}: ${name} — ${via.title} (${via.url})`);
  }
}

for (const id of accepted) console.log(`accepted ${id}: ${ACCEPTED[id]}`);
if (blocking.size) {
  console.error("Unaccepted high/critical advisories in production dependencies:");
  for (const line of blocking.values()) console.error("  " + line);
  process.exit(1);
}
console.log("No unaccepted high/critical production advisories.");
