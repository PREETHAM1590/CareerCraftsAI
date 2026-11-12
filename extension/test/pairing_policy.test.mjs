import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";
import test from "node:test";
import { validBridgePair, pairingNeedsConfirmation, isKnownJobHost, hostApproved, approveHost } from "../src/common.js";
import { buildExtension } from "../scripts/build.mjs";

const TOKEN = "ccx_" + "a".repeat(43);

test("bridge pair messages must carry a well-formed token for the page's own origin", () => {
  const origin = "https://app.careercraftsai.me";
  assert.equal(validBridgePair({ token: TOKEN, appOrigin: origin }, origin), true);
  for (const bad of [null, "x", {}, { token: TOKEN }, { token: "abc", appOrigin: origin }, { token: TOKEN, appOrigin: "https://evil.example" }, { token: { toString: () => TOKEN }, appOrigin: origin }, { token: TOKEN + " ", appOrigin: origin }]) {
    assert.equal(validBridgePair(bad, origin), false, JSON.stringify(bad));
  }
});

test("a different account or missing identity needs confirmation; first pairing and the same account do not", () => {
  const current = { appOrigin: "https://app.careercraftsai.me", token: "ccx_old" };
  assert.equal(pairingNeedsConfirmation(null, null, "a@x.com"), false);
  assert.equal(pairingNeedsConfirmation(current, "a@x.com", "A@x.com"), false);
  assert.equal(pairingNeedsConfirmation(current, "a@x.com", "b@x.com"), true);
  assert.equal(pairingNeedsConfirmation(current, "a@x.com", null), true);
  assert.equal(pairingNeedsConfirmation(current, null, "a@x.com"), true);
});

test("only job and ATS hosts are known; the app and localhost never are", () => {
  for (const h of ["boards.greenhouse.io", "jobs.lever.co", "acme.wd5.myworkdayjobs.com", "www.foundit.in", "x.darwinbox.in"]) assert.equal(isKnownJobHost(h), true, h);
  for (const h of ["mail.google.com", "bank.example.com", "evil-greenhouse.io", "greenhouse.io.evil.com", "localhost", "app.careercraftsai.me", "", undefined]) assert.equal(isKnownJobHost(h), false, String(h));
});

test("an unknown host is usable only after the member approves it", async () => {
  const store = {};
  globalThis.chrome = {
    storage: {
      local: {
        get: async (key) => ({ [key]: store[key] }),
        set: async (values) => Object.assign(store, values),
      },
    },
  };
  assert.equal(await hostApproved("https://boards.greenhouse.io/acme/jobs/1"), true);
  assert.equal(await hostApproved("https://careers.acme.in/apply"), false);
  await approveHost("careers.acme.in");
  assert.equal(await hostApproved("https://careers.acme.in/apply"), true);
  assert.equal(await hostApproved("https://other.acme.in/apply"), false);
  await approveHost("localhost");
  assert.equal(await hostApproved("https://localhost/"), false);
  assert.equal(await hostApproved("not a url"), false);
  delete globalThis.chrome;
});

test("the production manifest has no localhost access; the dev build adds it", () => {
  const manifest = JSON.parse(fs.readFileSync(new URL("../manifest.json", import.meta.url), "utf8"));
  const hosts = (m) => [...m.host_permissions, ...m.content_scripts.flatMap((c) => c.matches), ...(m.optional_host_permissions || [])];
  assert.equal(hosts(manifest).some((h) => /localhost|127\.0\.0\.1/.test(h)), false);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cc-build-"));
  try {
    const prod = JSON.parse(fs.readFileSync(path.join(buildExtension({ out: path.join(tmp, "prod") }), "manifest.json"), "utf8"));
    const dev = JSON.parse(fs.readFileSync(path.join(buildExtension({ out: path.join(tmp, "dev"), dev: true }), "manifest.json"), "utf8"));
    assert.equal(hosts(prod).some((h) => /localhost|127\.0\.0\.1/.test(h)), false);
    assert.equal(dev.host_permissions.includes("http://127.0.0.1/*"), true);
    assert.equal(dev.content_scripts[0].matches.includes("http://localhost/*"), true);
    assert.equal(fs.existsSync(path.join(tmp, "prod", "test")), false);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

function loadBridge(origin) {
  const listeners = [];
  const sent = [];
  const posted = [];
  const window = {
    postMessage: (data) => posted.push(data),
    addEventListener: (type, fn) => listeners.push(fn),
  };
  const context = {
    window,
    location: { origin },
    chrome: { runtime: { getManifest: () => ({ version: "0" }), sendMessage: (msg) => sent.push(msg) } },
  };
  vm.runInNewContext(fs.readFileSync(new URL("../src/content/bridge.js", import.meta.url), "utf8"), context);
  const deliver = (data, over = {}) => listeners.forEach((fn) => fn({ source: window, origin, data, ...over }));
  return { sent, deliver, window };
}

test("the page bridge forwards only well-formed pair requests from its own window and origin", () => {
  const origin = "https://app.careercraftsai.me";
  const { sent, deliver, window } = loadBridge(origin);
  const base = { source: "careercraft-app", type: "CAREERCRAFT_PAIR", token: TOKEN, appOrigin: origin };
  deliver({ ...base, token: "nope" });
  deliver({ ...base, appOrigin: "https://evil.example" });
  deliver({ ...base, token: 42 });
  deliver(base, { origin: "https://evil.example" });
  deliver(base, { source: {} });
  deliver({ ...base, source: "other" });
  assert.equal(sent.length, 0);
  deliver({ ...base, extra: "ignored" });
  assert.equal(JSON.stringify(sent), JSON.stringify([{ type: "CC_PAIR_FROM_BRIDGE", token: TOKEN, appOrigin: origin }]));
  assert.ok(window);
});
