// Shared constants/helpers for the background service worker and the popup
// (both loaded as ES modules). Content scripts are classic scripts and do
// not import this — they only exchange plain messages with the background.

export const STATIC_HOST_ORIGINS = [
  "http://localhost",
  "http://127.0.0.1",
  // The hosted CareerCraft app: lets the page detect and pair the extension
  // on first install, without a connection code or a permission prompt.
  "https://careercraftsai.me",
  "https://www.careercraftsai.me",
  "https://app.careercraftsai.me",
  "https://www.linkedin.com",
  "https://*.naukri.com",
  "https://www.naukri.com",
  "https://*.greenhouse.io",
  "https://jobs.lever.co",
  "https://jobs.ashbyhq.com",
  "https://*.myworkdayjobs.com",
  "https://jobs.smartrecruiters.com",
  "https://apply.workable.com",
  "https://*.icims.com",
  "https://*.taleo.net",
  "https://*.successfactors.com",
  "https://*.successfactors.eu",
  "https://*.darwinbox.in",
  "https://*.darwinbox.com",
  "https://*.keka.com",
  "https://*.indeed.com",
  "https://www.foundit.in",
  "https://www.instahyre.com",
];

export const CONTENT_SCRIPT_FILES = [
  "src/content/dom.js",
  "src/content/panel.js",
  "src/content/drivers.js",
  "src/content/runner.js",
];

export const BRIDGE_SCRIPT_ID = "careercraft-bridge";
export const BRIDGE_SCRIPT_FILE = "src/content/bridge.js";

export const TERMINAL_STAGES = new Set(["submitted", "failed", "cancelled"]);
export const OPEN_TASK_STATUSES = new Set(["claimed", "filling", "needs_input", "review", "login_required"]);

export const LOCAL_KEYS = { PAIRING: "pairing" };
export const SESSION_KEYS = { ACTIVE_TASK: "activeTask", HOST_PERMISSION_NEEDED: "hostPermissionNeeded" };

export function appOrigin(value) {
  const url = new URL(value);
  const local = ["localhost", "127.0.0.1"].includes(url.hostname);
  if ((url.protocol !== "https:" && !(local && url.protocol === "http:")) || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("Enter a HTTPS CareerCraft origin (HTTP is allowed only locally).");
  }
  return url.origin;
}

// Loopback, private, link-local and CGNAT IPv4 (URL() normalises odd forms
// such as 2130706433 to dotted quads), every IPv6 literal, and local-only
// names. Names that resolve to private addresses can't be checked from an
// extension (no DNS API); the backend's audit trail records every URL.
const PRIVATE_HOST = /^(localhost|0\.|10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|\[)|\.(localhost|local|internal|lan|home\.arpa)$/;

export function jobUrl(value) {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password || !url.hostname.includes(".") || PRIVATE_HOST.test(url.hostname.replace(/\.$/, ""))) {
    throw new Error("Application pages must use a public HTTPS URL.");
  }
  return url;
}

export function taskSenderAllowed(sender, active, message, extensionId) {
  if (sender.id !== extensionId || sender.frameId !== 0 || !active || sender.tab?.id !== active.tabId || message.taskId !== active.taskId) return false;
  try {
    return jobUrl(sender.url).origin === jobUrl(message.url || sender.url).origin;
  } catch {
    return false;
  }
}

// A static host_permissions pattern is normally an exact scheme+host (with
// optional leading "*."); a plain hostname match is enough here — we only
// use this to decide whether an *additional* optional permission request
// is needed, not to enforce Chrome's own permission checks.
function hostMatchesStatic(hostname) {
  return STATIC_HOST_ORIGINS.some((origin) => {
    const bare = origin.replace(/^https?:\/\//, "");
    if (bare.startsWith("*.")) {
      const suffix = bare.slice(1); // ".naukri.com"
      return hostname === bare.slice(2) || hostname.endsWith(suffix);
    }
    return hostname === bare;
  });
}

export function isStaticOrigin(origin) {
  try {
    const u = new URL(origin);
    return hostMatchesStatic(u.hostname);
  } catch (e) {
    return false;
  }
}

export function apiBase(appOrigin) {
  return appOrigin.replace(/\/+$/, "") + "/api/v1";
}

export async function getPairing() {
  const { [LOCAL_KEYS.PAIRING]: pairing } = await chrome.storage.local.get(LOCAL_KEYS.PAIRING);
  return pairing || null;
}

export async function setPairing(pairing) {
  await chrome.storage.local.set({ [LOCAL_KEYS.PAIRING]: pairing });
}

export async function clearPairing() {
  await chrome.storage.local.remove(LOCAL_KEYS.PAIRING);
}

export async function getActiveTask() {
  const { [SESSION_KEYS.ACTIVE_TASK]: active } = await chrome.storage.session.get(SESSION_KEYS.ACTIVE_TASK);
  return active || null;
}

export async function setActiveTask(active) {
  await chrome.storage.session.set({ [SESSION_KEYS.ACTIVE_TASK]: active });
}

export async function clearActiveTask() {
  await chrome.storage.session.remove(SESSION_KEYS.ACTIVE_TASK);
}

// Chrome storage may reorder object keys; field array order remains significant.
export function snapshotKey(value) {
  if (Array.isArray(value)) return `[${value.map(snapshotKey).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${snapshotKey(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
