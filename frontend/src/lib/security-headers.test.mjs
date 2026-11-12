import assert from "node:assert/strict";
import test from "node:test";
import nextConfig from "../../next.config.js";

test("interview dictation can request the microphone without enabling unrelated capabilities", async () => {
  const rules = await nextConfig.headers();
  const globalHeaders = rules.find((rule) => rule.source === "/:path*").headers;
  const policy = globalHeaders.find((header) => header.key === "Permissions-Policy").value;
  const directives = new Map(policy.split(",").map((part) => {
    const [name, value] = part.trim().split("=");
    return [name, value];
  }));

  assert.equal(directives.get("microphone"), "(self)");
  for (const capability of ["camera", "geolocation", "payment", "usb"]) {
    assert.equal(directives.get(capability), "()");
  }
});
