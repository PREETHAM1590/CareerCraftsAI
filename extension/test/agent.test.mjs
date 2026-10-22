// "My browser" agent mode: command loop, agent tab, snapshot refs and the
// write guards from docs/design/browser-mode.md, against a fake chrome API.
//   node --test "extension/test/*.test.mjs"
import test from "node:test";
import assert from "node:assert/strict";

const STALE = "Browser state changed; take a new snapshot and review again";
const CLOSED = "The agent tab was closed; resume to reopen it.";

// ── Fake chrome ─────────────────────────────────────────────────────────
const session = {};
const local = { pairing: { appOrigin: "https://app.careercraftsai.me", token: "ccx_test" } };
const tabs = new Map();
const granted = new Set(["https://jobs.lever.co"]);
let nextTabId = 100;
const listeners = { updated: new Set(), removed: new Set() };
const store = (data) => ({
  get: async (key) => (key in data ? { [key]: data[key] } : {}),
  set: async (obj) => Object.assign(data, obj),
  remove: async (keys) => [].concat(keys).forEach((k) => delete data[k]),
});
const event = (set) => ({ addListener: (f) => set.add(f), removeListener: (f) => set.delete(f) });
const badge = [];

globalThis.chrome = {
  storage: { session: store(session), local: store(local) },
  tabs: {
    get: async (id) => {
      if (!tabs.has(id)) throw new Error("No tab with id");
      return tabs.get(id);
    },
    create: async ({ url }) => {
      const tab = { id: nextTabId++, url, windowId: 1, status: "complete" };
      tabs.set(tab.id, tab);
      return tab;
    },
    update: async (id, props) => {
      const tab = tabs.get(id);
      if (props.url) {
        tab.url = props.url;
        setTimeout(() => listeners.updated.forEach((f) => f(id, { status: "complete" }, tab)), 0);
      }
      return tab;
    },
    remove: async (id) => {
      tabs.delete(id);
      listeners.removed.forEach((f) => f(id));
    },
    onUpdated: event(listeners.updated),
    onRemoved: event(listeners.removed),
  },
  windows: { update: async () => {} },
  permissions: { contains: async ({ origins }) => granted.has(origins[0].replace(/\/\*$/, "")) },
  action: {
    setBadgeText: async ({ text }) => badge.push(text),
    setBadgeBackgroundColor: async () => {},
    openPopup: async () => {},
  },
  scripting: {
    // Runs the injected page function directly against the fake page below.
    executeScript: async ({ func, args }) => (func ? [{ result: await func(...args) }] : []),
  },
  runtime: { sendMessage: () => {} },
};

// ── Fake page ───────────────────────────────────────────────────────────
function element(tagName, attrs = {}, extra = {}) {
  const a = { ...attrs };
  return {
    tagName: tagName.toUpperCase(),
    type: a.type,
    isConnected: true,
    getAttribute: (k) => (k in a ? a[k] : null),
    setAttribute: (k, v) => { a[k] = v; },
    removeAttribute: (k) => { delete a[k]; },
    scrollIntoView: () => {},
    attrs: a,
    ...extra,
  };
}
const page = {
  elements: [
    element("button", {}, { text: "Submit application" }),
    element("input", { type: "text" }, { label: "Full name" }),
    element("input", { type: "password" }, { label: "Password", value: "hunter2" }),
    element("a", { href: "/x" }, { text: "Hidden", hidden: true }),
    element("input", { type: "file" }, { label: "Resume" }),
  ],
};
globalThis.window = {
  CareerCraftDOM: {
    deepQueryAll: (_root, selector) =>
      selector === "[data-careercraft-ref]" ? page.elements.filter((e) => e.attrs["data-careercraft-ref"]) : page.elements,
    isVisible: (el) => !el.hidden,
    getLabel: (el) => el.label || "",
    textOf: (el) => el.text || "",
    visibleText: (_doc, limit) => "x".repeat(10000).slice(0, limit),
    clickLike: (el) => { el.clicked = true; },
    fillText: async (el, v) => { el.value = v; },
    fillSelect: async () => true,
    attachFile: async (el, b64, name) => { el.files = [name]; return true; },
  },
  scrollBy: () => {},
};
globalThis.document = { title: "Job", getElementById: () => ({}) }; // banner already mounted
globalThis.location = { href: "https://jobs.lever.co/acme/1" };

const agent = await import("../src/agent.js");
const run = (body) => agent.executeCommand({ id: "c1", kind: "action", body });

async function freshStart() {
  await agent.stopAgentTab();
  const out = await agent.executeCommand({ id: "s", kind: "start" });
  assert.deepEqual(out, { ok: true, result: { running: true, enabled: true } });
  const state = await agent.getAgentState();
  tabs.get(state.tabId).url = "https://jobs.lever.co/acme/1";
  return state;
}

test("command loop executes commands, posts results and stops on 401", async () => {
  const calls = [];
  const replies = [
    { ok: true, status: 200, data: { id: "cmd-1", kind: "start", body: {}, expires: Date.now() / 1000 + 60 } },
    { ok: true, status: 204, data: null },
    { ok: true, status: 200, data: { device_name: "not a command" } }, // malformed: backoff, no post
    { ok: true, status: 200, data: { id: "cmd-old", kind: "stop", expires: Date.now() / 1000 - 1 } }, // expired: dropped
    { ok: true, status: 200, data: { id: "cmd-2", kind: "action", body: { operation: "screenshot", actor: "agent" } } },
    { ok: false, status: 401, data: null },
  ];
  let unpaired = 0;
  const sleeps = [];
  agent.configureAgent({
    apiFetch: async (_pairing, path, init = {}) => {
      calls.push({ path, ...init });
      return path.includes("/result") ? { ok: true, status: 204 } : replies.shift();
    },
    onUnpaired: async () => { unpaired++; },
    sleep: async (ms) => { sleeps.push(ms); },
  });
  const first = agent.startCommandLoop();
  assert.equal(agent.startCommandLoop(), first, "one loop at a time");
  await first;
  const polls = calls.filter((c) => c.path === "/extension/device/commands/next?wait=25");
  const posts = calls.filter((c) => c.path.endsWith("/result"));
  assert.equal(polls.length, 6);
  assert.deepEqual(posts.map((p) => p.path), ["/extension/device/commands/cmd-1/result", "/extension/device/commands/cmd-2/result"]);
  assert.equal(posts[0].method, "POST");
  assert.deepEqual(posts[0].json, { ok: true, result: { running: true, enabled: true } });
  assert.deepEqual(posts[1].json, { ok: false, status: 409, error: "Not available in your browser mode: watch and use the agent tab directly." });
  assert.ok(sleeps.includes(2000), "malformed reply backs off");
  assert.equal(unpaired, 1);
});

test("snapshot assigns refs to visible elements and ids increase per run", async () => {
  const state = await freshStart();
  const a = await run({ operation: "snapshot", actor: "agent" });
  assert.equal(a.ok, true);
  assert.equal(a.result.snapshotId, 1);
  assert.equal(a.result.computer_run, state.run);
  assert.equal(a.result.url, "https://jobs.lever.co/acme/1");
  assert.deepEqual(a.result.elements, [
    { ref: "e1", role: "button", name: "Submit application" },
    { ref: "e2", role: "textbox", name: "Full name" },
    { ref: "e3", role: "textbox", name: "Password" },
    { ref: "e4", role: "file", name: "Resume" },
  ]);
  assert.equal(page.elements[0].attrs["data-careercraft-ref"], "e1");
  assert.equal(page.elements[3].attrs["data-careercraft-ref"], undefined);
  assert.ok(!JSON.stringify(a).includes("hunter2"), "password values never leave the page");
  const b = await run({ operation: "snapshot", actor: "agent" });
  assert.equal(b.result.snapshotId, 2);
});

test("agent writes need the current run and latest snapshot; one approval, one write", async () => {
  const state = await freshStart();
  const snap = (await run({ operation: "snapshot", actor: "agent" })).result;
  const click = (over = {}) =>
    run({ operation: "click", actor: "agent", computer_run: state.run, approved_snapshot: snap.snapshotId,
      parameters: { ref: "e1", snapshotId: snap.snapshotId }, ...over });
  assert.deepEqual(await click({ computer_run: "other-run" }), { ok: false, status: 409, error: STALE });
  assert.deepEqual(await click({ approved_snapshot: snap.snapshotId - 1 }), { ok: false, status: 409, error: STALE });
  assert.deepEqual(await click({ parameters: { ref: "e1", snapshotId: snap.snapshotId - 1 } }), { ok: false, status: 409, error: STALE });
  assert.equal(page.elements[0].clicked, undefined);
  assert.deepEqual(await click(), { ok: true, result: { ok: true, url: "https://jobs.lever.co/acme/1" } });
  assert.equal(page.elements[0].clicked, true);
  assert.deepEqual(await click(), { ok: false, status: 409, error: STALE }, "the snapshot is spent after a write");

  // A URL change in the agent tab voids the reviewed snapshot.
  const again = (await run({ operation: "snapshot", actor: "agent" })).result;
  // ...but an SPA's replaceState of the same URL does not.
  await agent.onAgentTabUpdated(state.tabId, { url: "https://jobs.lever.co/acme/1" }, tabs.get(state.tabId));
  assert.equal((await agent.getAgentState()).snapshotValid, true);
  await agent.onAgentTabUpdated(state.tabId, { url: "https://jobs.lever.co/acme/2" }, tabs.get(state.tabId));
  const out = await run({ operation: "scroll", actor: "agent", computer_run: state.run, approved_snapshot: again.snapshotId, parameters: { deltaY: 300 } });
  assert.deepEqual(out, { ok: false, status: 409, error: STALE });
});

test("typing into a password field is refused with 422", async () => {
  const state = await freshStart();
  const snap = (await run({ operation: "snapshot", actor: "agent" })).result;
  const out = await run({ operation: "type", actor: "agent", computer_run: state.run, approved_snapshot: snap.snapshotId,
    parameters: { ref: "e3", snapshotId: snap.snapshotId, text: "guess" } });
  assert.deepEqual(out, { ok: false, status: 422, error: "Typing into password fields is not allowed." });
  assert.equal(page.elements[2].value, "hunter2");
  // The refusal wrote nothing, so the reviewed snapshot is still usable.
  const typed = await run({ operation: "type", actor: "agent", computer_run: state.run, approved_snapshot: snap.snapshotId,
    parameters: { ref: "e2", snapshotId: snap.snapshotId, text: "Ada" } });
  assert.equal(typed.ok, true);
});

test("type and upload write through the reviewed refs", async () => {
  let state = await freshStart();
  let snap = (await run({ operation: "snapshot", actor: "agent" })).result;
  const typed = await run({ operation: "type", actor: "agent", computer_run: state.run, approved_snapshot: snap.snapshotId,
    parameters: { ref: "e2", snapshotId: snap.snapshotId, text: "Ada Lovelace" } });
  assert.equal(typed.ok, true);
  assert.equal(page.elements[1].value, "Ada Lovelace");
  snap = (await run({ operation: "snapshot", actor: "agent" })).result;
  const uploaded = await run({ operation: "upload", actor: "agent", computer_run: state.run, approved_snapshot: snap.snapshotId,
    parameters: { ref: "e4", snapshotId: snap.snapshotId, filename: "cv.pdf", mimeType: "application/pdf", base64: "JVBERg==" } });
  assert.equal(uploaded.ok, true);
  assert.deepEqual(page.elements[4].files, ["cv.pdf"]);
});

test("a closed agent tab or a stopped session answers 409", async () => {
  const state = await freshStart();
  await chrome.tabs.remove(state.tabId); // fires onRemoved like Chrome would
  await agent.onAgentTabRemoved(state.tabId);
  assert.deepEqual(await run({ operation: "read", actor: "agent" }), { ok: false, status: 409, error: CLOSED });

  const next = await freshStart();
  assert.notEqual(next.run, state.run, "a reopened tab gets a new computer_run");
  // Banner Stop from another tab is ignored; from the agent tab it ends the session.
  assert.ok((await agent.stopFromBanner({ tab: { id: 999 }, frameId: 0 })).error);
  assert.deepEqual(await agent.stopFromBanner({ tab: { id: next.tabId }, frameId: 0 }), { ok: true });
  assert.equal(tabs.has(next.tabId), false);
  assert.deepEqual(await run({ operation: "snapshot", actor: "agent" }), { ok: false, status: 409, error: CLOSED });
  assert.deepEqual(await agent.executeCommand({ id: "x", kind: "stop" }), { ok: true, result: { running: false, enabled: false } });
});

test("an ungranted site answers 403 permission_required and prompts", async () => {
  const state = await freshStart();
  const snap = (await run({ operation: "snapshot", actor: "agent" })).result;
  badge.length = 0;
  const out = await run({ operation: "navigate", actor: "agent", computer_run: state.run, approved_snapshot: snap.snapshotId,
    parameters: { url: "https://careers.acme.example/jobs/1" } });
  assert.deepEqual(out, { ok: false, status: 403, error: "permission_required", origin: "https://careers.acme.example" });
  assert.equal(session.hostPermissionNeeded, "careers.acme.example");
  assert.deepEqual(badge, ["!"]);
  assert.equal(tabs.get(state.tabId).url, "https://jobs.lever.co/acme/1", "nothing navigated");

  await agent.onPermissionAdded();
  assert.equal(session.hostPermissionNeeded, undefined);
  assert.deepEqual(badge, ["!", ""]);
});

test("a fresh about:blank agent tab can be snapshotted and navigated", async () => {
  await agent.stopAgentTab();
  await agent.executeCommand({ id: "s", kind: "start" });
  const state = await agent.getAgentState();
  assert.equal(tabs.get(state.tabId).url, "about:blank");
  const snap = await run({ operation: "snapshot", actor: "agent" });
  assert.deepEqual(snap, { ok: true, result: { snapshotId: 1, computer_run: state.run, url: "about:blank", title: "", elements: [] } });
  const click = await run({ operation: "click", actor: "agent", computer_run: state.run, approved_snapshot: 1, parameters: { ref: "e1", snapshotId: 1 } });
  assert.equal(click.status, 422);
  const nav = await run({ operation: "navigate", actor: "agent", computer_run: state.run, approved_snapshot: 1, parameters: { url: "https://jobs.lever.co/acme/1" } });
  assert.equal(nav.ok, true);
});

test("Stop during an action reports the closed-tab 409", async () => {
  const state = await freshStart();
  const original = chrome.scripting.executeScript;
  chrome.scripting.executeScript = async () => {
    await agent.stopFromBanner({ tab: { id: state.tabId }, frameId: 0 });
    throw new Error("Frame with ID 0 was removed.");
  };
  try {
    assert.deepEqual(await run({ operation: "read", actor: "agent" }), { ok: false, status: 409, error: CLOSED });
  } finally {
    chrome.scripting.executeScript = original;
  }
});

test("navigate loads the page and returns url, title and capped text", async () => {
  const state = await freshStart();
  const snap = (await run({ operation: "snapshot", actor: "agent" })).result;
  const out = await run({ operation: "navigate", actor: "agent", computer_run: state.run, approved_snapshot: snap.snapshotId,
    parameters: { url: "https://jobs.lever.co/acme/2" } });
  assert.equal(out.ok, true);
  assert.equal(out.result.title, "Job");
  assert.equal(out.result.text.length, 4000);
  assert.equal(tabs.get(state.tabId).url, "https://jobs.lever.co/acme/2");
  // Never CareerCraft itself, localhost or plain http.
  for (const url of ["https://app.careercraftsai.me/approvals", "http://localhost:3000/", "http://jobs.lever.co/x"]) {
    const refused = await run({ operation: "human/navigate", actor: "human", parameters: { url } });
    assert.equal(refused.status, 422, url);
  }
});
