// CareerCraft AI — "My browser" agent mode (docs/design/browser-mode.md).
// The backend queues start/stop/action commands for this device; this module
// long-polls them, runs each one in ONE dedicated agent tab, and posts the
// result back. Every check that guards a page write lives here; the page-side
// function below only does what it is told on refs from the latest snapshot.
import { getPairing, jobUrl, SESSION_KEYS } from "./common.js";

const AGENT_KEY = "agentTab";
const AGENT_PERMISSION_KEY = "agentPermissionNeeded";
const CLOSED = "The agent tab was closed; resume to reopen it.";
const STALE = "Browser state changed; take a new snapshot and review again";
const UNSUPPORTED = "Not available in your browser mode: watch and use the agent tab directly.";
const WRITES = new Set(["navigate", "click", "type", "scroll", "upload"]);
const ON_REF = new Set(["click", "type", "upload"]);
const SUPPORTED = new Set([...WRITES, "read", "snapshot", "human/navigate"]);
const CARECRAFT_HOSTS = ["careercraftsai.me", "www.careercraftsai.me", "app.careercraftsai.me"];
const NAVIGATION_TIMEOUT_MS = 40000; // under the backend's 60 s result wait

let deps = {
  apiFetch: null,
  onUnpaired: async () => {},
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
};
export function configureAgent(overrides) {
  deps = { ...deps, ...overrides };
}

const ok = (result) => ({ ok: true, result });
const fail = (status, error, extra) => ({ ok: false, status, error, ...extra });

export async function getAgentState() {
  const { [AGENT_KEY]: state } = await chrome.storage.session.get(AGENT_KEY);
  return state || null;
}
async function setAgentState(state) {
  if (state) await chrome.storage.session.set({ [AGENT_KEY]: state });
  else await chrome.storage.session.remove(AGENT_KEY);
}

// ── Command loop ────────────────────────────────────────────────────────

let loop = null;

// One loop per worker. The 30 s alarm calls this again after the worker was
// suspended; while a loop is running the call is a no-op.
export function startCommandLoop() {
  if (!loop) loop = runLoop().finally(() => { loop = null; });
  return loop;
}

function validCommand(cmd) {
  return !!cmd && typeof cmd.id === "string" && /^[\w-]{1,64}$/.test(cmd.id) && typeof cmd.kind === "string";
}

async function runLoop() {
  let backoff = 0;
  for (;;) {
    const pairing = await getPairing();
    if (!pairing) return;
    const res = await deps.apiFetch(pairing, "/extension/device/commands/next?wait=25");
    if (res.status === 401) {
      await deps.onUnpaired();
      return;
    }
    if (res.status === 204) {
      backoff = 0;
      await deps.sleep(500); // a backend that answers 204 at once is never hammered
      continue;
    }
    if (!res.ok || !validCommand(res.data)) {
      backoff = Math.min(backoff ? backoff * 2 : 2000, 60000);
      await deps.sleep(backoff);
      continue;
    }
    backoff = 0;
    const cmd = res.data;
    if (typeof cmd.expires === "number" && cmd.expires * 1000 < Date.now()) continue; // nobody waits for it
    const result = await executeCommand(cmd);
    await postResult(pairing, cmd.id, result);
  }
}

// The action may already have happened: a lost result would look like a
// timeout. Retry network and server errors; any other answer (stored, or 404
// once the backend stopped waiting) is final, so a repeat is harmless.
async function postResult(pairing, id, result) {
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt) await deps.sleep(1000 * 2 ** (attempt - 1));
    const res = await deps.apiFetch(pairing, `/extension/device/commands/${id}/result`, { method: "POST", json: result });
    if (res.status !== 0 && res.status < 500) return;
  }
}

export async function executeCommand(cmd) {
  try {
    if (cmd.kind === "start") return await startAgentTab();
    if (cmd.kind === "stop") return await stopAgentTab();
    if (cmd.kind === "action") return await runAction(cmd.body || {});
    return fail(422, "Unknown command");
  } catch (e) {
    console.warn("CareerCraft: agent command failed", e && e.message);
    // Stop or a closed tab mid-action: the contract's 409, not a generic 502.
    const state = await getAgentState();
    try {
      if (!state) throw new Error("stopped");
      await chrome.tabs.get(state.tabId);
    } catch (gone) {
      return fail(409, CLOSED);
    }
    return fail(502, "The browser could not complete this action.");
  }
}

// ── Agent tab ───────────────────────────────────────────────────────────

async function startAgentTab() {
  const state = await getAgentState();
  if (state) {
    try {
      const tab = await chrome.tabs.get(state.tabId);
      await chrome.tabs.update(tab.id, { active: true });
      try {
        await chrome.windows.update(tab.windowId, { focused: true });
      } catch (e) {
        /* focusing the window is best effort */
      }
      return ok({ running: true, enabled: true });
    } catch (e) {
      /* the tab is gone: open a new one with a new run */
    }
  }
  const tab = await chrome.tabs.create({ url: "about:blank", active: true });
  try {
    // Shown by Chrome itself, so a page can't hide which tab the agent drives.
    const group = await chrome.tabs.group({ tabIds: tab.id });
    await chrome.tabGroups.update(group, { title: "CareerCraft agent", color: "blue" });
  } catch (e) {
    /* tab groups unavailable: the in-page banner remains */
  }
  await setAgentState({ tabId: tab.id, run: crypto.randomUUID(), snapshotId: 0, snapshotValid: false });
  return ok({ running: true, enabled: true });
}

export async function stopAgentTab() {
  const state = await getAgentState();
  await setAgentState(null);
  if (state) {
    try {
      await chrome.tabs.remove(state.tabId);
    } catch (e) {
      /* already closed */
    }
  }
  return ok({ running: false, enabled: false });
}

export async function onAgentTabRemoved(tabId) {
  const state = await getAgentState();
  if (state && state.tabId === tabId) await setAgentState(null);
}

// A URL change (the user, a redirect, an SPA route) makes the reviewed refs
// meaningless; each load gets the banner back.
export async function onAgentTabUpdated(tabId, changeInfo, tab) {
  const state = await getAgentState();
  if (!state || state.tabId !== tabId) return;
  // Bumping the id (not just the flag) also voids a snapshot still in flight.
  // SPAs (Greenhouse) replaceState the same URL after load; Chrome reports
  // that as a URL change too, so only a different URL counts.
  if (changeInfo.url && changeInfo.url !== state.snapshotUrl) {
    await setAgentState({ ...state, snapshotId: state.snapshotId + 1, snapshotValid: false });
  }
  if (changeInfo.status === "complete" && pageAllowed(tab?.url) && (await hasPermission(tab.url))) {
    try {
      await chrome.scripting.executeScript({ target: { tabId }, func: agentPage, args: ["banner", {}] });
    } catch (e) {
      /* chrome:// or an error page */
    }
  }
}

// The banner's Stop button. Only the agent tab's top frame may stop it.
export async function stopFromBanner(sender) {
  const state = await getAgentState();
  if (!state || sender.tab?.id !== state.tabId || sender.frameId !== 0) return { error: "Not the agent tab." };
  await stopAgentTab();
  return { ok: true };
}

export async function onPermissionAdded() {
  const { [AGENT_PERMISSION_KEY]: waiting } = await chrome.storage.session.get(AGENT_PERMISSION_KEY);
  if (!waiting) return;
  await chrome.storage.session.remove([AGENT_PERMISSION_KEY, SESSION_KEYS.HOST_PERMISSION_NEEDED]);
  try {
    await chrome.action.setBadgeText({ text: "" });
  } catch (e) {
    /* action API unavailable */
  }
}

// ── Actions ─────────────────────────────────────────────────────────────

// Public HTTPS only, and never CareerCraft itself: an agent tab on the app
// could approve its own steps, and localhost is a static host permission.
function pageAllowed(url, appOrigin) {
  try {
    const u = jobUrl(url);
    if (CARECRAFT_HOSTS.includes(u.hostname)) return false;
    return !appOrigin || u.origin !== new URL(appOrigin).origin;
  } catch (e) {
    return false;
  }
}

async function hasPermission(url) {
  try {
    return await chrome.permissions.contains({ origins: [new URL(url).origin + "/*"] });
  } catch (e) {
    return false;
  }
}

// Same prompt flow as an application that reaches a new site: the popup
// shows "Allow the extension on <host>", the badge says "!".
async function permissionRequired(url) {
  const u = new URL(url);
  await chrome.storage.session.set({ [SESSION_KEYS.HOST_PERMISSION_NEEDED]: u.hostname, [AGENT_PERMISSION_KEY]: true });
  try {
    await chrome.action.setBadgeText({ text: "!" });
    await chrome.action.setBadgeBackgroundColor({ color: "#d97706" });
    await chrome.action.openPopup();
  } catch (e) {
    /* the badge is the fallback */
  }
  return fail(403, "permission_required", { origin: u.origin });
}

function waitForComplete(tabId) {
  return new Promise((resolve, reject) => {
    const done = (fn, value) => {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(updated);
      chrome.tabs.onRemoved.removeListener(removed);
      fn(value);
    };
    const updated = (id, info) => { if (id === tabId && info.status === "complete") done(resolve); };
    const removed = (id) => { if (id === tabId) done(reject, new Error("closed")); };
    // A slow page is read as far as it got.
    const timer = setTimeout(() => done(resolve), NAVIGATION_TIMEOUT_MS);
    chrome.tabs.onUpdated.addListener(updated);
    chrome.tabs.onRemoved.addListener(removed);
  });
}

async function inPage(tabId, op, params) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["src/content/dom.js"] });
  const [frame] = await chrome.scripting.executeScript({ target: { tabId }, func: agentPage, args: [op, params] });
  return frame?.result || fail(502, "The page did not answer.");
}

async function runAction(body) {
  const op = body.operation;
  const params = body.parameters || {};
  if (!SUPPORTED.has(op)) return fail(409, UNSUPPORTED);
  const agent = body.actor !== "human";
  const state = await getAgentState();
  if (!state) return fail(409, CLOSED);
  let tab;
  try {
    tab = await chrome.tabs.get(state.tabId);
  } catch (e) {
    await setAgentState(null);
    return fail(409, CLOSED);
  }
  if (agent && WRITES.has(op) && (body.computer_run !== state.run || !state.snapshotValid || body.approved_snapshot !== state.snapshotId)) {
    return fail(409, STALE);
  }
  if (ON_REF.has(op) && (!state.snapshotValid || params.snapshotId !== state.snapshotId)) return fail(409, STALE);

  const appOrigin = (await getPairing())?.appOrigin;
  if (op === "navigate" || op === "human/navigate") {
    if (!pageAllowed(params.url, appOrigin)) return fail(422, "The agent can only open public HTTPS pages outside CareerCraft.");
    if (!(await hasPermission(params.url))) return permissionRequired(params.url);
    // The checks above awaited; the user may have moved the tab meanwhile.
    if (agent && !(await unchanged(state))) return fail(409, STALE);
    await setAgentState({ ...state, snapshotValid: false });
    const loaded = waitForComplete(tab.id);
    await chrome.tabs.update(tab.id, { url: params.url });
    try {
      await loaded;
    } catch (e) {
      return fail(409, CLOSED);
    }
    tab = await chrome.tabs.get(tab.id);
    if (!pageAllowed(tab.url, appOrigin)) return fail(422, "The page redirected outside public HTTPS pages; navigate somewhere else.");
    if (!(await hasPermission(tab.url))) return permissionRequired(tab.url);
    return inPage(tab.id, "read", {});
  }

  if (!pageAllowed(tab.url, appOrigin)) {
    // A fresh agent tab (about:blank) must still give the agent a snapshot to
    // approve its first navigate against; nothing on such a page is touched.
    const blank = { url: tab.url || "", title: tab.title || "" };
    if (op === "read") return ok({ ...blank, text: "" });
    if (op === "snapshot") {
      const snapshotId = state.snapshotId + 1;
      await setAgentState({ ...state, snapshotId, snapshotValid: true });
      return ok({ snapshotId, computer_run: state.run, ...blank, elements: [] });
    }
    return fail(422, "Navigate the agent tab to a public HTTPS page first.");
  }
  if (!(await hasPermission(tab.url))) return permissionRequired(tab.url);

  if (op === "snapshot") {
    const snapshotId = state.snapshotId + 1;
    // Claim the id before the page answers so a concurrent URL change wins.
    await setAgentState({ ...state, snapshotId, snapshotValid: false, snapshotUrl: tab.url });
    const out = await inPage(tab.id, "snapshot", {});
    if (!out.ok) return out;
    const now = await getAgentState();
    if (!now || now.run !== state.run || now.snapshotId !== snapshotId) return fail(409, STALE);
    await setAgentState({ ...now, snapshotValid: true });
    return ok({ snapshotId, computer_run: state.run, ...out.result });
  }
  if (op === "read") return inPage(tab.id, "read", {});

  // click / type / scroll / upload: one approval, one write. A clean refusal
  // (password field, stale ref) wrote nothing, so the snapshot stays usable.
  if (agent && !(await unchanged(state))) return fail(409, STALE);
  // The page re-checks its URL in the same task as the write (an SPA route
  // change can keep a reviewed element connected on a different page).
  const out = await inPage(tab.id, op, agent ? { ...params, expectUrl: state.snapshotUrl } : params);
  if (out.ok || ![409, 422].includes(out.status)) {
    const now = await getAgentState();
    if (now) await setAgentState({ ...now, snapshotValid: false });
  }
  return out;
}

// Nothing voided the reviewed snapshot while this action awaited, and the tab
// still shows the page it was taken on.
async function unchanged(state) {
  const now = await getAgentState();
  if (!now || now.run !== state.run || now.snapshotId !== state.snapshotId || !now.snapshotValid) return false;
  if (!state.snapshotUrl) return true; // the blank first tab: only navigate gets here
  try {
    return (await chrome.tabs.get(state.tabId)).url === state.snapshotUrl;
  } catch (e) {
    return false;
  }
}

// ── Page side ───────────────────────────────────────────────────────────
// Injected with chrome.scripting.executeScript({func}) into the agent tab's
// isolated world, after dom.js. Must stay self-contained: no module scope.
export function agentPage(op, p) {
  const STALE_TEXT = "Browser state changed; take a new snapshot and review again";
  const fail = (status, error) => ({ ok: false, status, error });

  try {
    // Tracked in the isolated world, not by a page-visible id the page could
    // pre-create; put back if the page removes it. The tab group set by the
    // extension stays the indicator a page cannot touch.
    if (!window.__ccAgentBanner) {
      const host = document.createElement("div");
      host.style.cssText = "all:initial!important;display:block!important;position:fixed!important;top:8px!important;left:50%!important;transform:translateX(-50%)!important;z-index:2147483647!important;visibility:visible!important;opacity:1!important;";
      const shadow = host.attachShadow({ mode: "closed" });
      shadow.innerHTML = `<style>
        .b{display:flex;gap:12px;align-items:center;padding:8px 10px 8px 14px;border-radius:999px;
          font:600 13px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
          background:#2563eb;color:#fff;box-shadow:0 8px 24px rgba(0,0,0,.25)}
        button{font:inherit;padding:4px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.6);
          background:#fff;color:#1d4ed8;cursor:pointer}
        button:hover{background:#dbeafe}
      </style><div class="b" role="status"><span>CareerCraft agent is controlling this tab</span><button type="button">Stop</button></div>`;
      shadow.querySelector("button").addEventListener("click", () => {
        chrome.runtime.sendMessage({ type: "CC_AGENT_STOP" });
      });
      window.__ccAgentBanner = host;
    }
    const host = window.__ccAgentBanner;
    if (!host.isConnected) {
      // In <body>, not <html>: an extra child of <html> fails React hydration
      // on Remix sites (Greenhouse job boards), which then rebuild the whole
      // document and every reviewed ref goes stale.
      (document.body || document.documentElement).appendChild(host);
    }
  } catch (e) {
    /* the banner never blocks an action */
  }
  if (op === "banner") return { ok: true };

  const D = window.CareerCraftDOM;
  const page = () => ({ url: location.href, title: document.title });
  if (op === "read") return { ok: true, result: { ...page(), text: D.visibleText(document, 4000) } };

  if (op === "snapshot") {
    const SELECTOR = [
      "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
      "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]", "[role=tab]", "[role=menuitem]",
      "[role=option]", "[role=combobox]", "[role=switch]", "[role=textbox]", "[contenteditable=true]",
    ].join(",");
    for (const el of D.deepQueryAll(document, "[data-careercraft-ref]")) el.removeAttribute("data-careercraft-ref");
    // Refs resolve through this isolated-world map, not the attribute, so a
    // page script moving the attribute cannot redirect a reviewed click.
    const refs = new Map();
    window.__ccAgentRefs = refs;
    const roleOf = (el) => {
      const explicit = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
      if (explicit) return explicit;
      const tag = el.tagName.toLowerCase();
      if (tag === "a") return "link";
      if (tag === "button" || tag === "summary") return "button";
      if (tag === "select") return el.multiple ? "listbox" : "combobox";
      if (tag === "textarea") return "textbox";
      if (tag !== "input") return "textbox"; // contenteditable
      const type = (el.type || "text").toLowerCase();
      if (["checkbox", "radio", "file"].includes(type)) return type;
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (type === "range") return "slider";
      if (type === "search") return "searchbox";
      return "textbox";
    };
    // Accessible name from labels, aria, text or placeholder; never a typed value.
    const nameOf = (el) => {
      const tag = el.tagName.toLowerCase();
      const type = (el.type || "").toLowerCase();
      if (tag === "input" && ["submit", "button", "reset"].includes(type)) return el.getAttribute("aria-label") || el.value || "";
      if (["input", "select", "textarea"].includes(tag)) return D.getLabel(el);
      return (
        (el.getAttribute("aria-label") || "").trim() ||
        D.textOf(el) ||
        (el.getAttribute("title") || "").trim() ||
        (el.querySelector && el.querySelector("img[alt]")?.getAttribute("alt")) ||
        ""
      );
    };
    const elements = [];
    for (const el of D.deepQueryAll(document, SELECTOR)) {
      if (elements.length >= 400) break; // keeps the result far under 1 MB
      if (el.disabled || !D.isVisible(el)) continue;
      const ref = "e" + (elements.length + 1);
      el.setAttribute("data-careercraft-ref", ref);
      refs.set(ref, el);
      elements.push({ ref, role: roleOf(el), name: String(nameOf(el)).replace(/\s+/g, " ").trim().slice(0, 200) });
    }
    return { ok: true, result: { ...page(), elements } };
  }

  if (p.expectUrl && location.href !== p.expectUrl) return fail(409, STALE_TEXT);
  if (op === "scroll") {
    window.scrollBy(0, Number(p.deltaY) || 0);
    return { ok: true, result: { ok: true, url: location.href } };
  }

  const el = window.__ccAgentRefs && window.__ccAgentRefs.get(p.ref);
  if (!el || !el.isConnected) return fail(409, STALE_TEXT);
  const done = () => ({ ok: true, result: { ok: true, url: location.href } });
  const tag = el.tagName.toLowerCase();
  const type = (el.type || "").toLowerCase();

  if (op === "click") {
    el.scrollIntoView({ block: "center" });
    D.clickLike(el);
    return done();
  }
  if (op === "type") {
    if ((tag === "input" && type === "password") || /password/i.test(el.getAttribute("autocomplete") || "")) {
      return fail(422, "Typing into password fields is not allowed.");
    }
    if (tag === "select") {
      return Promise.resolve(D.fillSelect(el, p.text)).then((matched) => (matched ? done() : fail(422, "No option matches that text.")));
    }
    if (tag === "textarea" || (tag === "input" && !["checkbox", "radio", "file", "submit", "button", "reset", "image", "range", "color"].includes(type))) {
      return Promise.resolve(D.fillText(el, p.text)).then(done);
    }
    if (el.isContentEditable) {
      el.focus();
      el.textContent = p.text;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return done();
    }
    return fail(422, "Choose a text field.");
  }
  if (op === "upload") {
    const types = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain"];
    if (tag !== "input" || type !== "file") return fail(422, "Choose a file input");
    if (typeof p.base64 !== "string" || !types.includes(p.mimeType) || typeof p.filename !== "string" || /[\\/]/.test(p.filename)) {
      return fail(422, "Invalid resume upload");
    }
    return Promise.resolve(D.attachFile(el, p.base64, p.filename, p.mimeType)).then((attached) =>
      attached ? done() : fail(502, "Resume upload failed; inspect the page before retrying"));
  }
  return fail(409, "Not available in your browser mode: watch and use the agent tab directly.");
}
