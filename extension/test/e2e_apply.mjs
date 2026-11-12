// End-to-end check of the extension in a real Chromium, from "Apply" in the
// web app to one approved submit:
//   wake from the app page → claim → open the job tab → reach the form →
//   fill → in-page review → approval in the real toolbar popup → one
//   submit → confirmation reported.
//
// Everything is local. A fake CareerCraft API (same status rules as
// backend/app/api/v1/extension.py) runs on 127.0.0.1, and recorded job pages
// are served over HTTPS under their real hostnames through Chromium's
// host-resolver rules, so no request leaves this machine and no real
// application is sent.
//
//   node extension/test/e2e_apply.mjs            (ONLY=Workday runs one scenario)
//
// Needs the `playwright` package (a global install is fine), a Chromium it
// can launch (CHROMIUM_PATH overrides it) and openssl.
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildExtension } from "../scripts/build.mjs";

const require = createRequire(import.meta.url);
function loadPlaywright() {
  try {
    return require("playwright");
  } catch {
    const root = execFileSync("npm", ["root", "-g"]).toString().trim();
    return require(path.join(root, "playwright"));
  }
}
const { chromium } = loadPlaywright();

const here = path.dirname(fileURLToPath(import.meta.url));
// The harness pairs with an app on 127.0.0.1, which only the dev build allows.
const extensionDir = buildExtension({ out: path.join(os.tmpdir(), `cc-e2e-ext-${process.pid}`), dev: true });
process.on("exit", () => fs.rmSync(extensionDir, { recursive: true, force: true }));
const fixture = (name) => fs.readFileSync(path.join(here, "fixtures", name), "utf8");
const HOSTS = ["boards.greenhouse.io", "jobs.lever.co", "careers.indeed.com", "acme.wd5.myworkdayjobs.com", "jobs.smartrecruiters.com", "apply.workable.com", "acme.wd3.myworkdaysite.com"];

// ── Fake job sites (HTTPS, self-signed) ─────────────────────────────────

const submissions = [];
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cc-e2e-"));
execFileSync("openssl", [
  "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
  "-keyout", path.join(tmp, "key.pem"), "-out", path.join(tmp, "cert.pem"),
  "-subj", `/CN=${HOSTS[0]}`, "-addext", `subjectAltName=${HOSTS.map((h) => `DNS:${h}`).join(",")}`,
], { stdio: "ignore" });

function collect(req, encoding) {
  return new Promise((resolve) => {
    let raw = "";
    req.setEncoding(encoding);
    req.on("data", (c) => (raw += c));
    req.on("end", () => resolve(raw));
  });
}

const site = https.createServer(
  { key: fs.readFileSync(path.join(tmp, "key.pem")), cert: fs.readFileSync(path.join(tmp, "cert.pem")) },
  async (req, res) => {
    const host = (req.headers.host || "").split(":")[0];
    const url = new URL(req.url, `https://${host}`);
    const html = (body) => {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(body);
    };
    if (req.method === "POST" && url.pathname === "/__submitted") {
      submissions.push({ site: host, ...JSON.parse(await collect(req, "utf8")) });
      res.writeHead(204);
      return res.end();
    }
    if (host === "jobs.lever.co" && req.method === "POST" && url.pathname === "/acme/1/apply") {
      const raw = await collect(req, "latin1");
      const part = (name) => new RegExp(`name="${name}"(?:; filename="([^"]*)")?\\r\\n(?:Content-Type: [^\\r]*\\r\\n)?\\r\\n([^\\r]*)`).exec(raw) || [];
      submissions.push({ site: "lever", name: part("name")[2], email: part("email")[2], resume: part("resume")[1] });
      return html("<!doctype html><title>Thanks</title><h1>Application submitted!</h1><p>Thank you for applying to Acme.</p>");
    }
    if (host === "jobs.lever.co" && url.pathname === "/acme/1") return html(fixture("lever/posting.html"));
    if (host === "jobs.lever.co" && url.pathname === "/acme/1/apply") return html(fixture("lever/apply.html"));
    if (host === "careers.indeed.com") {
      return html(fixture("company/careers.html").replace("https://boards.greenhouse.io/", `https://boards.greenhouse.io:${sitePort}/`));
    }
    if (host === "boards.greenhouse.io") {
      // Report in-page submits of the recorded form back to this process.
      let page = fixture("greenhouse.html").replace(
        "window.submittedData = data;",
        "window.submittedData = data; fetch('/__submitted', { method: 'POST', body: JSON.stringify(data) });"
      );
      if (url.pathname === "/acme/jobs/2") {
        // Newer boards: the form stays hidden until "Apply" is pressed.
        page = page
          .replace('<form id="application"', '<button type="button" id="reveal" onclick="document.getElementById(\'application\').hidden = false; this.remove()">Apply</button>\n    <form id="application" hidden')
      }
      return html(page);
    }
    if (host === "acme.wd5.myworkdayjobs.com") return html(fixture("workday/index.html"));
    if (host === "acme.wd3.myworkdaysite.com") return html(fixture("workday/full.html"));
    if (host === "jobs.smartrecruiters.com") return html(fixture("smartrecruiters/index.html"));
    if (host === "apply.workable.com") return html(fixture("workable/index.html"));
    res.writeHead(404);
    res.end();
  }
);
await new Promise((resolve) => site.listen(0, "127.0.0.1", resolve));
const sitePort = site.address().port;

// ── Fake CareerCraft API ────────────────────────────────────────────────

const TOKEN = "ccx_e2e";
const OPEN = new Set(["pending", "claimed", "filling", "needs_input", "review", "login_required", "submitting"]);
let task = null;
let log = [];
let reviewHash = null;
let permit = null;
const approvals = []; // auto_approved flag of each approve-submit call
const evidence = []; // sizes of review screenshots received

function planFor(fields) {
  const byLabel = [
    // Blocks the driver adds with "Add", filled from the member's resume facts.
    [/^work experience 2: job title/i, "Analyst"],
    [/^work experience 2: company/i, "Babbage & Co"],
    [/^work experience 2: from/i, "03/2018"],
    [/^work experience 2: to/i, "06/2020"],
    [/^education 1: school/i, "University of London"],
    [/^education 1: degree/i, "Bachelor's Degree"],
    [/^education 1: from/i, "2012"],
    [/^education 1: to/i, "2016"],
    [/first name/i, "Ada"],
    [/last name/i, "Lovelace"],
    [/full name/i, "Ada Lovelace"],
    [/email/i, "ada@example.com"],
    [/phone/i, "+91 90000 00000"],
    [/linkedin/i, "https://www.linkedin.com/in/ada"],
    [/hear about/i, "LinkedIn"],
    // Workday multi-step fixture. The saved veteran answer is the member's
    // own; the gender value from the profile must never be used.
    [/address line 1/i, "1 Main St"],
    [/^city/i, "Pune"],
    [/postal/i, "411001"],
    [/legally authorized/i, "Yes"],
    [/when can you start/i, "2026-12-01"],
    [/job title/i, "Should not overwrite"],
    [/gender/i, "Female"],
    [/veteran/i, "I am not a protected veteran", "user"],
  ];
  const groups = new Map();
  for (const f of fields) {
    if ((f.type === "radio" || f.type === "checkbox") && f.name) {
      if (!groups.has(f.name)) groups.set(f.name, []);
      groups.get(f.name).push(f);
    }
  }
  const plan = [];
  for (const [name, group] of groups) {
    const radio = group[0].type === "radio";
    plan.push({ field_id: name, label: radio ? group[0].group_label || name : group[0].label, input_type: radio ? "radio" : "checkbox", required: group.some((g) => g.required), value: null, source: "unresolved", confidence: 0, requires_review: true, missing_reason: radio ? "Sponsorship is never guessed." : "Tick this yourself if it applies." });
  }
  for (const f of fields) {
    if (groups.has(f.name) || !f.visible) continue;
    const hit = byLabel.find(([re]) => re.test(f.label));
    let entry = { field_id: f.id, label: f.label, input_type: ["email", "tel", "url"].includes(f.type) ? "text" : f.type, required: f.required, value: hit ? hit[1] : null, source: hit ? hit[2] || "profile" : "unresolved", confidence: hit ? 0.95 : 0, requires_review: !hit, missing_reason: null };
    if (f.type === "file") entry = { ...entry, value: "__resume__", source: "profile", confidence: 1, requires_review: false };
    if (f.type === "textarea") entry = { ...entry, value: "I build reliable backends.", source: "generated", confidence: 0.6, requires_review: true };
    plan.push(entry);
  }
  // Like the server: the facts hold two jobs and one school.
  const blocks = (re) => new Set(fields.map((f) => re.exec(f.label || "")?.[1]).filter(Boolean)).size;
  const add_blocks = { work_experience: Math.max(0, 2 - blocks(/^Work Experience (\d+):/)), education: Math.max(0, 1 - blocks(/^Education (\d+):/)) };
  return { fields: plan, unresolved_required: plan.filter((p) => p.required && (p.value == null || p.value === "")).map((p) => p.field_id), add_blocks };
}

const api = http.createServer(async (req, res) => {
  const send = (status, json) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(json === undefined ? "" : JSON.stringify(json));
  };
  if (req.url === "/") {
    // Stands in for the CareerCraft web app page that wakes the extension.
    res.writeHead(200, { "Content-Type": "text/html" });
    return res.end("<!doctype html><title>CareerCraft</title>");
  }
  if (req.headers.authorization !== `Bearer ${TOKEN}`) return send(401, { detail: "Extension is not connected" });
  const p = new URL(req.url, "http://x").pathname.replace(/^\/api\/v1\/extension\/device/, "");
  const raw = req.method === "POST" ? await collect(req, "utf8") : "";
  const json = raw ? JSON.parse(raw) : null;
  log.push([req.method, p.replace(task?.id || "-", ":id"), task?.status, json?.stage]);

  if (p === "/me") return send(200, { device_id: "d1", device_name: "e2e", user: { email: "ada@example.com" } });
  if (p === "/tasks/claim") {
    if (!task || task.status !== "pending") return send(204);
    task.status = "claimed";
    return send(200, task);
  }
  if (p === "/answers") return send(200, { question_key: "custom.x" });
  if (p === "/decide") return send(200, { provider: "heuristic", answers: {} });
  if (!task || !p.startsWith(`/tasks/${task.id}/`)) return send(404, { detail: "Task not found" });
  const action = p.slice(`/tasks/${task.id}/`.length);
  if (action === "plan") {
    if (!OPEN.has(task.status)) return send(409, { detail: "This application is no longer active" });
    return send(200, planFor(json.fields));
  }
  if (action === "resume") {
    res.writeHead(200, { "Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="ada-resume.pdf"' });
    return res.end(Buffer.from("%PDF-1.4\n%e2e\n"));
  }
  if (action === "events") {
    if (!OPEN.has(task.status)) return send(200, { status: task.status, active: false });
    if (task.status === "submitting" && !["submitted", "failed", "cancelled"].includes(json.stage)) return send(409, { detail: "Submission is in progress; do not retry" });
    if (json.stage === "submitted" && (task.status !== "submitting" || json.submission_token !== permit || !json.confirmation_text)) return send(409, { detail: "Submission requires an approved review and confirmation" });
    task.status = json.stage;
    task.result = json;
    return send(200, { status: json.stage, active: !["submitted", "failed", "cancelled"].includes(json.stage) });
  }
  if (action === "review") {
    if (task.status !== "review") return send(409, { detail: "Request a fresh form review before submitting" });
    reviewHash = (Date.now().toString(16) + "a".repeat(64)).slice(0, 64);
    return send(200, { review_hash: reviewHash, expires_at: new Date(Date.now() + 5 * 60000).toISOString() });
  }
  if (action === "evidence") {
    if (json.review_hash !== reviewHash || !/^data:image\/jpeg;base64,/.test(json.screenshot || "")) return send(422, { detail: "bad evidence" });
    evidence.push(json.screenshot.length);
    return send(200, { status: "stored" });
  }
  if (action === "approve-submit") {
    if (typeof json.auto_approved !== "boolean") return send(426, { detail: "Update the CareerCraft extension" });
    if (task.status !== "review" || json.review_hash !== reviewHash || !json.user_confirmed) return send(409, { detail: "Review expired, changed or already approved" });
    approvals.push(json.auto_approved);
    // Like the server: auto-approval only when this member's setting allows it.
    if (json.auto_approved && !task.server_allows_auto) return send(403, { detail: "Auto-submit is off in your CareerCraft settings." });
    task.status = "submitting";
    permit = `permit-${task.id}`;
    return send(200, { submission_token: permit, expires_at: new Date(Date.now() + 2 * 60000).toISOString() });
  }
  return send(404, { detail: "not found" });
});
await new Promise((r) => api.listen(0, "127.0.0.1", r));
const appOrigin = `http://127.0.0.1:${api.address().port}`;

// ── Browser ─────────────────────────────────────────────────────────────

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-e2e-profile-"));
const context = await chromium.launchPersistentContext(userDataDir, {
  headless: false,
  executablePath: process.env.CHROMIUM_PATH || undefined,
  ignoreHTTPSErrors: true,
  args: [
    "--headless=new",
    `--disable-extensions-except=${extensionDir}`,
    `--load-extension=${extensionDir}`,
    `--host-resolver-rules=${HOSTS.map((h) => `MAP ${h} 127.0.0.1`).join(", ")}`,
    "--ignore-certificate-errors",
    "--no-proxy-server",
  ],
});

let failed = false;
function check(ok, message) {
  console.log(`${ok ? "ok  " : "FAIL"} ${message}`);
  if (!ok) failed = true;
}

async function waitUntil(fn, timeoutMs, label) {
  const start = Date.now();
  for (;;) {
    const value = await Promise.resolve().then(fn).catch(() => null);
    if (value) return value;
    if (Date.now() - start > timeoutMs) throw new Error(`Timed out waiting for ${label}`);
    await new Promise((r) => setTimeout(r, 250));
  }
}

// The review panel lives in a closed shadow root. CDP can still see it, so
// press its controls the way a person would: a real mouse click at the
// element's position.
async function panelClick(page, selector) {
  const cdp = await page.context().newCDPSession(page);
  try {
    const box = await waitUntil(async () => {
      const { root } = await cdp.send("DOM.getDocument", { depth: -1, pierce: true });
      const host = (function find(node) {
        if (node.attributes && node.attributes.includes("careercraft-panel-host")) return node;
        for (const child of [...(node.children || []), ...(node.shadowRoots || [])]) {
          const hit = find(child);
          if (hit) return hit;
        }
        return null;
      })(root);
      const shadow = host?.shadowRoots?.[0];
      if (!shadow) return null;
      const { nodeId } = await cdp.send("DOM.querySelector", { nodeId: shadow.nodeId, selector });
      if (!nodeId) return null;
      await cdp.send("DOM.scrollIntoViewIfNeeded", { nodeId });
      const { model } = await cdp.send("DOM.getBoxModel", { nodeId });
      return model.border;
    }, 20000, `panel control ${selector}`);
    await page.mouse.click((box[0] + box[2]) / 2, (box[1] + box[5]) / 2);
  } finally {
    await cdp.detach();
  }
}

// For failure output: what the review panel (closed shadow root) shows.
async function panelHtml(page, limit = 1500) {
  const cdp = await page.context().newCDPSession(page);
  try {
    const { root } = await cdp.send("DOM.getDocument", { depth: -1, pierce: true });
    const find = (node) => {
      if (node.attributes && node.attributes.includes("careercraft-panel-host")) return node;
      for (const child of [...(node.children || []), ...(node.shadowRoots || [])]) {
        const hit = find(child);
        if (hit) return hit;
      }
      return null;
    };
    const shadow = find(root)?.shadowRoots?.[0];
    if (!shadow) return "(no panel)";
    const { outerHTML } = await cdp.send("DOM.getOuterHTML", { backendNodeId: shadow.backendNodeId });
    return outerHTML.replace(/<style>[\s\S]*?<\/style>/, "").replace(/\s+/g, " ").slice(0, limit);
  } finally {
    await cdp.detach();
  }
}

// Opens the real toolbar popup (chrome.action.openPopup) and presses its
// Submit button over CDP, since Playwright does not expose the popup as a page.
async function popupApprove(worker, extensionId) {
  const cdp = await context.newCDPSession(context.pages()[0]);
  await worker.evaluate(() => chrome.action.openPopup()).catch(() => {});
  const popupUrl = `chrome-extension://${extensionId}/popup.html`;
  const target = await waitUntil(async () => {
    const { targetInfos } = await cdp.send("Target.getTargets");
    return targetInfos.find((t) => t.url === popupUrl);
  }, 10000, "toolbar popup");
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId: target.targetId, flatten: false });
  let id = 0;
  const call = (method, params) =>
    new Promise((resolve, reject) => {
      const msgId = ++id;
      const onMessage = (event) => {
        if (event.sessionId !== sessionId) return;
        const data = JSON.parse(event.message);
        if (data.id !== msgId) return;
        cdp.off("Target.receivedMessageFromTarget", onMessage);
        resolve(data.result);
      };
      cdp.on("Target.receivedMessageFromTarget", onMessage);
      cdp.send("Target.sendMessageToTarget", { sessionId, message: JSON.stringify({ id: msgId, method, params }) }).catch(reject);
    });
  const rect = await waitUntil(async () => {
    const result = await call("Runtime.evaluate", {
      expression: "(() => { const r = document.getElementById('review'); const b = document.getElementById('approve-submit'); if (!r || r.hidden || b.disabled) return null; b.scrollIntoView({ block: 'center' }); return JSON.stringify(b.getBoundingClientRect()); })()",
      returnByValue: true,
    });
    return result?.result?.value ? JSON.parse(result.result.value) : null;
  }, 10000, "review card in popup");
  const point = { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, button: "left", clickCount: 1 };
  await call("Input.dispatchMouseEvent", { type: "mousePressed", ...point });
  await call("Input.dispatchMouseEvent", { type: "mouseReleased", ...point });
  await cdp.detach();
}

async function answerGreenhouse(page) {
  await page.waitForFunction(() => document.getElementById("first_name").value === "Ada", null, { timeout: 15000 });
  await page.waitForFunction(() => document.getElementById("resume").files.length === 1, null, { timeout: 15000 });
  check(await page.evaluate(() => document.getElementById("email").value === "ada@example.com" && document.getElementById("source").selectedOptions[0].text === "LinkedIn"), "profile fields, dropdown and resume filled on the page");
  // What CareerCraft never guesses stays for the user: sponsorship and consent.
  await panelClick(page, `[data-field-id="sponsorship"] select`);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await panelClick(page, `[data-field-id="consent"] input[type=checkbox]`);
}

// "No" in the panel's dropdown for a radio group CareerCraft never guesses.
async function pickNo(page, fieldId) {
  await panelClick(page, `[data-field-id="${fieldId}"] select`);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
}

const profileSubmitted = (s) =>
  s.first_name === "Ada" && s.last_name === "Lovelace" && s.email === "ada@example.com" && s.resume === "ada-resume.pdf";

const greenhouseSubmitted = (s) => s.first_name === "Ada" && s.sponsorship === "no" && s.consent === "on" && s.resume === "ada-resume.pdf";

const SCENARIOS = [
  {
    name: "Greenhouse form on the job page",
    url: `https://boards.greenhouse.io:${sitePort}/acme/jobs/1`,
    platform: "greenhouse",
    formHost: "boards.greenhouse.io",
    answer: answerGreenhouse,
    verify: greenhouseSubmitted,
  },
  {
    // An unknown in-page Apply button may submit on its own, so CareerCraft
    // never presses it: the task fails and the person opens the form.
    name: "In-page Apply button is left for the person",
    url: `https://boards.greenhouse.io:${sitePort}/acme/jobs/2`,
    platform: "greenhouse",
    formHost: "boards.greenhouse.io",
    refuses: true,
    async answer(page) {
      await waitUntil(async () => task.status === "failed", 20000, "failed outcome");
      check(await page.evaluate(() => document.getElementById("application").hidden), "did not press the in-page Apply button");
      check(submissions.length === 0, "nothing submitted");
    },
  },
  {
    name: "Workday: Apply → Apply Manually → sign-in → steps → Review → Submit",
    url: `https://acme.wd5.myworkdayjobs.com:${sitePort}/acme/job/123`,
    platform: "workday",
    formHost: "acme.wd5.myworkdayjobs.com",
    async answer(page) {
      await page.waitForURL(/\/apply$/, { timeout: 25000 });
      // Signed out: CareerCraft stops and waits for the person, and never
      // touches the sign-in form.
      await waitUntil(() => log.some((e) => e[3] === "login_required"), 20000, "login_required event");
      check(await page.evaluate(() => document.querySelector("input[type=password]").value === ""), "stopped at the Workday sign-in without touching it");
      await page.evaluate(() => window.__signIn());
      await panelClick(page, `[data-role="primary"]`);
      await page.waitForFunction(() => document.getElementById("firstName")?.value === "Ada", null, { timeout: 25000 });
      await page.waitForFunction(() => document.getElementById("cv").files.length === 1, null, { timeout: 15000 });
      check(true, "filled step 1 (My Information) and attached the resume");
      await waitUntil(() => log.some((e) => e[3] === "needs_input"), 15000, "needs_input event");
      await pickNo(page, "sponsor");
      await panelClick(page, `[data-role="primary"]`);
      await page.waitForFunction(() => document.querySelector("h2")?.textContent === "Review", null, { timeout: 25000 });
      check(submissions.length === 0, "advanced Save and Continue to Review without submitting");
      await waitUntil(() => log.some((e) => e[3] === "review"), 15000, "review event");
      await new Promise((r) => setTimeout(r, 800));
    },
    verify: (s) => profileSubmitted(s) && s.sponsorship === "no",
  },
  {
    // platform "generic": the driver must recognise Workday from the host.
    name: "Workday multi-step: account → listboxes, prompt, dates, resume parse, validation, disclosures → Submit",
    url: `https://acme.wd3.myworkdaysite.com:${sitePort}/acme/job/456`,
    platform: "generic",
    formHost: "acme.wd3.myworkdaysite.com",
    async answer(page) {
      const panelHas = async (text) => (await panelHtml(page, 20000)).includes(text);
      await page.waitForURL(/\/apply$/, { timeout: 25000 });
      await waitUntil(() => log.some((e) => e[3] === "login_required"), 20000, "login_required at account creation");
      check(
        await page.evaluate(() => Array.from(document.querySelectorAll("input[type=password]")).every((i) => i.value === "") && !document.querySelector("[data-automation-id=createAccountCheckbox]").checked),
        "stopped at Create Account without touching it"
      );
      await page.evaluate(() => window.__signIn());
      await panelClick(page, `[data-role="primary"]`);
      // My Information: Workday rejects "+91 …"; the error is handed back.
      await waitUntil(() => panelHas("without the country code"), 45000, "phone validation error handed back");
      check(
        await page.evaluate(
          () =>
            document.getElementById("legalNameSection_firstName").value === "Ada" &&
            document.getElementById("addressSection_city").value === "Pune" &&
            document.getElementById("phone-device-type").textContent.trim() === "Mobile" &&
            document.querySelector("[data-automation-id=selectedItem]")?.textContent === "LinkedIn" &&
            document.getElementById("addressSection_countryRegion").textContent.trim() === "India"
        ),
        "filled text fields, the Phone Device Type listbox and the source prompt; kept the pre-filled country"
      );
      await page.fill("#phone-number", "9000000000");
      await panelClick(page, `[data-role="primary"]`);
      // Application Questions: sponsorship is never guessed.
      await waitUntil(() => panelHas('data-field-id="sponsorship"'), 60000, "sponsorship question handed back");
      check(await page.evaluate(() => window.__wdValues.resume === "ada-resume.pdf" && window.__wdValues.job_title === "Engineer"), "uploaded the resume through the hidden input and kept the parsed work history");
      check(
        await page.evaluate(
          () =>
            document.getElementById("authorized").textContent.trim() === "Yes" &&
            Array.from(document.querySelectorAll("[data-automation-id=formField-startDate] input")).map((i) => i.value).join("/") === "12/01/2026"
        ),
        "answered a listbox question and typed a split month/day/year date"
      );
      await pickNo(page, "sponsorship");
      await panelClick(page, `[data-role="primary"]`);
      // Voluntary Disclosures: consent is the member's to give.
      await waitUntil(() => panelHas('data-field-id="terms"'), 45000, "consent checkbox handed back");
      check(
        await page.evaluate(() => document.getElementById("gender").textContent.trim() === "Select One" && document.getElementById("veteranStatus").textContent.trim() === "I am not a protected veteran"),
        "left gender blank (a profile value is not a saved disclosure) and used the saved veteran answer"
      );
      await panelClick(page, `[data-field-id="terms"] input[type=checkbox]`);
      await panelClick(page, `[data-role="primary"]`);
      await page.waitForFunction(() => document.querySelector("h2")?.textContent === "Review", null, { timeout: 30000 });
      check(submissions.length === 0, "reached Review without submitting");
      await waitUntil(() => log.some((e) => e[3] === "review"), 15000, "review event");
      await new Promise((r) => setTimeout(r, 800));
    },
    verify: (s) =>
      s.first_name === "Ada" && s.last_name === "Lovelace" && s.phone === "9000000000" && s.device === "Mobile" && s.source === "LinkedIn" &&
      s.country === "India" && s.city === "Pune" && s.job_title === "Engineer" && s.resume === "ada-resume.pdf" &&
      s.linkedin === "https://www.linkedin.com/in/ada" && s.authorized === "Yes" && s.sponsorship === "No" && s.start === "12/01/2026" &&
      s.gender === "" && s.veteran === "I am not a protected veteran" && s.terms === true &&
      // Added with "Add" and filled, dates through the calendar popups.
      s.job2_title === "Analyst" && s.job2_company === "Babbage & Co" && s.job2_from === "03/2018" && s.job2_to === "06/2020" &&
      s.school === "University of London" && s.degree === "Bachelor's Degree" && s.edu_from === "2012" && s.edu_to === "2016",
  },
  {
    name: "Auto-apply on: a form with open questions still stops for the person",
    url: `https://boards.greenhouse.io:${sitePort}/acme/jobs/1`,
    platform: "greenhouse",
    formHost: "boards.greenhouse.io",
    autoApply: true,
    refuses: true,
    async answer(page) {
      await waitUntil(async () => (await panelHtml(page, 20000)).includes("Auto-apply paused"), 30000, "auto-apply pause");
      await new Promise((r) => setTimeout(r, 1000));
      check(submissions.length === 0 && !log.some((e) => e[1] === "/tasks/:id/approve-submit"), "nothing approved or submitted while sponsorship, consent and a draft need review");
      await panelClick(page, `[data-role="cancel"]`);
      await waitUntil(async () => task.status === "cancelled", 15000, "cancelled");
      check(true, "cancel in the panel ends the application");
    },
  },
  {
    name: "Auto-apply on: a fully answered Lever form is submitted without the approval click",
    url: `https://jobs.lever.co:${sitePort}/acme/1`,
    platform: "lever",
    formHost: "jobs.lever.co",
    autoApply: true,
    auto: true,
    async answer() {
      await waitUntil(async () => ["submitted", "failed"].includes(task.status), 45000, "auto-applied outcome");
    },
    verify: (s) => s.name === "Ada Lovelace" && s.email === "ada@example.com" && s.resume === "ada-resume.pdf",
  },
  {
    // The extension cannot auto-submit when the server says no (setting
    // turned off since the claim, cap reached, a drafted answer…).
    name: "Auto-apply refused by the server: falls back to the popup approval",
    url: `https://jobs.lever.co:${sitePort}/acme/1`,
    platform: "lever",
    formHost: "jobs.lever.co",
    autoApply: true,
    serverRefuses: true,
    skipPanel: true,
    async answer(page) {
      await waitUntil(async () => approvals.length === 1, 45000, "refused auto-approval");
      await waitUntil(async () => (await panelHtml(page, 4000)).includes("Auto-submit paused"), 10000, "refusal shown in the page");
      check(task.status === "review" && submissions.length === 0, "nothing submitted after the server refused auto-submit");
    },
    verify: (s) => s.name === "Ada Lovelace" && s.email === "ada@example.com" && s.resume === "ada-resume.pdf",
  },
  {
    name: "SmartRecruiters: I'm interested → form inside a shadow root",
    url: `https://jobs.smartrecruiters.com:${sitePort}/Acme/123-backend-engineer`,
    platform: "smartrecruiters",
    formHost: "jobs.smartrecruiters.com",
    async answer(page) {
      await page.waitForURL(/\/oneclick-ui\//, { timeout: 25000 });
      const filled = () => page.evaluate(() => {
        const sr = document.querySelector("sr-application").shadowRoot;
        return sr.getElementById("em").value === "ada@example.com" && sr.getElementById("cv").files.length === 1;
      });
      await waitUntil(filled, 25000, "shadow-DOM form filled");
      check(true, "filled a form that only exists inside a shadow root");
    },
    verify: profileSubmitted,
  },
  {
    name: "Workable: Apply for this job → application form",
    url: `https://apply.workable.com:${sitePort}/acme/j/ABC123/`,
    platform: "workable",
    formHost: "apply.workable.com",
    async answer(page) {
      await page.waitForURL(/\/apply\/$/, { timeout: 25000 });
      await page.waitForFunction(() => document.getElementById("email")?.value === "ada@example.com" && document.getElementById("resume").files.length === 1, null, { timeout: 25000 });
      check(true, "filled the Workable form and attached the resume");
    },
    verify: profileSubmitted,
  },
  {
    name: "Lever description page → Apply link → form (submit navigates)",
    url: `https://jobs.lever.co:${sitePort}/acme/1`,
    platform: "lever",
    formHost: "jobs.lever.co",
    async answer(page) {
      await page.waitForURL(/\/apply$/, { timeout: 20000 });
      await page.waitForFunction(() => document.querySelector("input[name=email]").value === "ada@example.com", null, { timeout: 20000 });
      await page.waitForFunction(() => document.getElementById("resume-upload-input").files.length === 1, null, { timeout: 15000 });
      check(await page.evaluate(() => document.querySelector("input[name=name]").value === "Ada Lovelace"), "followed Apply in the same tab (not the search form) and filled the form");
    },
    verify: (s) => s.name === "Ada Lovelace" && s.email === "ada@example.com" && s.resume === "ada-resume.pdf",
  },
  {
    name: "Company careers page with an embedded Greenhouse form",
    url: `https://careers.indeed.com:${sitePort}/acme/backend-engineer`,
    platform: "generic",
    formHost: "boards.greenhouse.io",
    async answer(page) {
      await page.waitForURL(/boards\.greenhouse\.io(:\d+)?\/embed\/job_app/, { timeout: 20000 });
      check(true, "opened the embedded form in the same tab (not the job-alert form)");
      await answerGreenhouse(page);
    },
    verify: greenhouseSubmitted,
  },
];

try {
  let [worker] = context.serviceWorkers();
  if (!worker) worker = await context.waitForEvent("serviceworker");
  const extensionId = new URL(worker.url()).host;
  // The extension APIs attach to the worker a moment after it starts.
  await waitUntil(() => worker.evaluate(() => !!(globalThis.chrome && chrome.storage)), 10000, "extension APIs");
  // Pair as the popup does after validating a connection code.
  await worker.evaluate(async (pairing) => chrome.storage.local.set({ pairing }), { appOrigin, token: TOKEN });

  // The web app page; the bridge content script runs here and relays WAKE.
  const app = await context.newPage();
  await app.goto(appOrigin + "/");

  // Review screenshots: without <all_urls> Chrome needs activeTab, which only
  // a real click on the toolbar icon grants (not possible headless). Prove
  // that limit, then stand in for the capture so the upload path is tested.
  const capture = await worker.evaluate(async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    try {
      await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg" });
      return "captured";
    } catch (e) {
      return e.message;
    }
  });
  check(/activeTab/.test(capture), `no screenshot without the member's toolbar click (${capture})`);
  await worker.evaluate(() => {
    chrome.tabs.captureVisibleTab = async () => "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ==";
  });

  for (const [index, scenario] of SCENARIOS.entries()) {
    if (process.env.ONLY && !scenario.name.includes(process.env.ONLY)) continue;
    console.log(`\n# ${scenario.name}`);
    log = [];
    submissions.length = 0;
    approvals.length = 0;
    evidence.length = 0;
    // auto_submit is what the server tells the extension at claim time;
    // server_allows_auto is what it decides at approve-submit.
    task = {
      id: `11111111-1111-4111-8111-1111111111${String(index).padStart(2, "0")}`,
      status: "pending",
      job_url: scenario.url,
      company: "Acme",
      role: "Engineer",
      platform: scenario.platform,
      has_resume: true,
      auto_submit: !!scenario.autoApply,
      server_allows_auto: !!scenario.autoApply && !scenario.serverRefuses,
    };
    const jobHost = new URL(scenario.url).host;
    try {
      await app.bringToFront();
      await app.evaluate(() => window.postMessage({ source: "careercraft-app", type: "CAREERCRAFT_WAKE" }, location.origin));
      const page = await waitUntil(async () => context.pages().find((pg) => pg !== app && !pg.isClosed() && pg.url().startsWith(`https://${jobHost}/`)), 20000, "job tab");
      check(true, "app wake → task claimed → job page opened in a new tab");

      await scenario.answer(page);
      if (scenario.refuses) {
        await page.close();
        await worker.evaluate(() => chrome.storage.session.clear()).catch(() => {});
        continue;
      }
      if (scenario.auto) {
        check(task.status === "submitted", `outcome reported: ${task.status}${task.result?.error ? ` (${task.result.error})` : ""}`);
        check(JSON.stringify(approvals) === "[true]", `approved as auto_approved by the server-side setting, without the popup ${JSON.stringify(approvals)}`);
        check(submissions.length === 1 && scenario.verify(submissions[0]), `exactly one submit ${JSON.stringify(submissions)}`);
        check((await worker.evaluate(() => chrome.action.getBadgeText({}))) === "", "no approval badge");
        check(evidence.length === 1, `a screenshot of the reviewed form was stored (${evidence.length})`);
        await page.close();
        continue;
      }
      check(submissions.length === 0, "nothing submitted before review");
      if (!scenario.skipPanel) await panelClick(page, `[data-role="primary"]`);

      await waitUntil(async () => log.some((e) => e[1] === "/tasks/:id/review"), 20000, "final review request");
      await new Promise((r) => setTimeout(r, 1500));
      check(submissions.length === 0, "in-page Submit alone sends nothing; approval waits for the popup");
      check((await worker.evaluate(() => chrome.action.getBadgeText({}))) === "1", "toolbar badge asks for the approval");

      await popupApprove(worker, extensionId);
      await waitUntil(async () => ["submitted", "failed"].includes(task.status), 25000, "reported outcome");
      check(task.status === "submitted", `outcome reported: ${task.status}${task.result?.error ? ` (${task.result.error})` : ""}`);
      check(submissions.length === 1 && scenario.verify(submissions[0]), `exactly one submit with the reviewed values ${JSON.stringify(submissions)}`);
      check(new URL(task.result.confirmation_url).hostname === scenario.formHost, "confirmation read from the form's page");
      check((await worker.evaluate(() => chrome.action.getBadgeText({}))) === "", "badge cleared");
      const expected = scenario.serverRefuses ? "[true,false]" : "[false]";
      check(JSON.stringify(approvals) === expected, `approvals recorded as ${expected} (auto_approved flags): ${JSON.stringify(approvals)}`);
      check(evidence.length === 1, `a screenshot of the reviewed form was stored (${evidence.length})`);
      await page.close();
    } catch (e) {
      check(false, e.message);
      console.log("API calls:", JSON.stringify(log));
      for (const pg of context.pages()) {
        if (pg !== app) console.log("panel on", pg.url(), "=>", await panelHtml(pg).catch((err) => err.message));
      }
      console.log("session:", JSON.stringify(await worker.evaluate(() => chrome.storage.session.get(null)).catch(() => null)));
      for (const pg of context.pages()) if (pg !== app) await pg.close();
      await worker.evaluate(() => chrome.storage.session.clear()).catch(() => {});
    }
  }
  // "Autofill this page" without any CareerCraft task: what the popup button
  // runs (the same content scripts and call), from the worker.
  if (!process.env.ONLY || "Autofill".includes(process.env.ONLY)) {
    console.log("\n# Autofill this page (no task): Workday My Information and a Greenhouse form");
    submissions.length = 0;
    const profile = { firstName: "Ada", lastName: "Lovelace", email: "ada@example.com", phone: "9000000000", city: "Pune", howDidYouHear: "LinkedIn", gender: "" };
    const resume = { filename: "local-cv.pdf", mime: "application/pdf", base64: Buffer.from("%PDF-1.4\n%local\n").toString("base64") };
    const autofill = async (page, marker) =>
      worker.evaluate(
        async ({ marker, profile, resume }) => {
          const tab = (await chrome.tabs.query({})).find((t) => (t.url || "").includes(marker));
          const files = ["src/content/dom.js", "src/content/panel.js", "src/content/drivers.js", "src/content/workday.js", "src/content/autofill.js", "src/content/runner.js"];
          await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files });
          const results = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: (p) => window.CareerCraftAutofill.run(p), args: [{ profile, resume }] });
          return results.map((r) => r.result);
        },
        { marker, profile, resume }
      );
    try {
      const wd = await context.newPage();
      await wd.goto(`https://acme.wd3.myworkdaysite.com:${sitePort}/acme/job/789/apply`);
      await wd.evaluate(() => window.__signIn());
      const [wdResult] = await autofill(wd, "/job/789/apply");
      check(
        wdResult?.ok &&
          (await wd.evaluate(
            () =>
              document.getElementById("legalNameSection_firstName").value === "Ada" &&
              document.getElementById("phone-number").value === "9000000000" &&
              document.getElementById("phone-device-type").textContent.trim() === "Mobile" &&
              document.querySelector("[data-automation-id=selectedItem]")?.textContent === "LinkedIn" &&
              document.querySelector("h2").textContent === "My Information"
          )),
        `Workday step filled from the local profile and nothing advanced ${JSON.stringify(wdResult)}`
      );
      await wd.close();

      const gh = await context.newPage();
      await gh.goto(`https://boards.greenhouse.io:${sitePort}/acme/jobs/1`);
      const ghResults = await autofill(gh, "/acme/jobs/1");
      check(
        ghResults.some((r) => r?.ok && r.attached) &&
          (await gh.evaluate(
            () =>
              document.getElementById("first_name").value === "Ada" &&
              document.getElementById("email").value === "ada@example.com" &&
              document.getElementById("resume").files[0]?.name === "local-cv.pdf" &&
              !document.querySelector("input[name=sponsorship]:checked") &&
              !document.getElementById("consent").checked
          )),
        `Greenhouse form filled with the local resume; sponsorship and consent left alone ${JSON.stringify(ghResults)}`
      );
      await new Promise((r) => setTimeout(r, 500));
      check(submissions.length === 0, "autofill never submits");
      await gh.close();
    } catch (e) {
      check(false, `autofill: ${e.message}`);
    }
  }
} catch (e) {
  check(false, e.message);
} finally {
  await context.close();
  api.close();
  site.close();
}
process.exit(failed ? 1 : 0);
