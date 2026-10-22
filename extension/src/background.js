// CareerCraft AI — background service worker (MV3 module).
// Owns every network call (host permissions here bypass CORS) and the
// one-task-at-a-time lifecycle: poll → claim → open a tab → inject the
// content scripts → relay progress back to the API.
import {
  apiBase,
  isStaticOrigin,
  CONTENT_SCRIPT_FILES,
  BRIDGE_SCRIPT_ID,
  BRIDGE_SCRIPT_FILE,
  TERMINAL_STAGES,
  SESSION_KEYS,
  getPairing,
  setPairing,
  clearPairing,
  getActiveTask,
  setActiveTask,
  clearActiveTask,
  appOrigin,
  jobUrl,
  taskSenderAllowed,
  snapshotKey,
} from "./common.js";
import {
  configureAgent,
  startCommandLoop,
  onAgentTabRemoved,
  onAgentTabUpdated,
  onPermissionAdded,
  stopFromBanner,
  stopAgentTab,
} from "./agent.js";

const POLL_ALARM = "poll";
// A job page often links to the real application form (Lever's /apply, an
// "Apply" button, a Greenhouse iframe on a company site). The runner may
// follow at most this many such hops per application, so a page that keeps
// linking somewhere else can never loop the tab.
const MAX_NAVIGATIONS = 4;

// ── Toolbar badge ───────────────────────────────────────────────────────
// The final approval happens in the popup, so the icon has to say when it is
// waiting for the user; otherwise the page panel looks stuck.

async function setBadge(text, color) {
  try {
    await chrome.action.setBadgeText({ text: text || "" });
    if (color) await chrome.action.setBadgeBackgroundColor({ color });
  } catch (e) {
    /* action API unavailable in some test harnesses */
  }
}

async function resetActiveTask() {
  await clearActiveTask();
  await setBadge("");
}

async function openPopupIfPossible() {
  try {
    // Chrome 127+: opens the popup on the focused window without a click.
    // Older versions or an unfocused window reject; the badge still shows.
    await chrome.action.openPopup();
  } catch (e) {
    /* the badge is the fallback */
  }
}

// ── HTTP ────────────────────────────────────────────────────────────────

// The deadline outlasts the 25 s command long-poll; a request a network or
// proxy leaves hanging resolves as status 0 and the caller backs off.
async function apiFetch(pairing, path, { method = "GET", json, timeoutMs = 40000 } = {}) {
  const url = apiBase(pairing.appOrigin) + path;
  const headers = { Authorization: `Bearer ${pairing.token}` };
  const init = { method, headers, signal: AbortSignal.timeout(timeoutMs) };
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  }
  try {
    const res = await fetch(url, init);
    let data = null;
    if (res.status !== 204) {
      const text = await res.text();
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (e) {
          data = null;
        }
      }
    }
    const error = res.ok ? undefined : (typeof data?.detail === "string" ? data.detail : `CareerCraft returned HTTP ${res.status}. Check the app URL and connection.`);
    return { ok: res.ok, status: res.status, data, error };
  } catch (e) {
    return { ok: false, status: 0, error: "Could not reach CareerCraft. Check your network and app URL.", data: null };
  }
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function fetchResume(pairing, taskId) {
  const url = apiBase(pairing.appOrigin) + `/extension/device/tasks/${taskId}/resume`;
  try {
    const res = await fetch(url, { headers: { Authorization: `Bearer ${pairing.token}` } });
    if (!res.ok) return { ok: false, status: res.status };
    const disposition = res.headers.get("Content-Disposition") || "";
    const match = /filename="?([^";]+)"?/i.exec(disposition);
    const filename = match ? match[1].trim() : "resume.pdf";
    const buf = new Uint8Array(await res.arrayBuffer());
    return { ok: true, base64: bytesToBase64(buf), filename, mime: res.headers.get("Content-Type") || "application/pdf" };
  } catch (e) {
    return { ok: false, status: 0, error: "network" };
  }
}

async function hasHostPermission(urlString) {
  try {
    const u = new URL(urlString);
    return await chrome.permissions.contains({ origins: [u.origin + "/*"] });
  } catch (e) {
    return false;
  }
}

// Retire this browser's token on the server before forgetting it locally,
// so disconnected or re-paired browsers never pile up as active devices.
async function revokeCurrentPairing() {
  const current = await getPairing();
  if (current) await apiFetch(current, "/extension/device/me", { method: "DELETE" });
}

// ── Bridge registration (content script on the web-app origin) ─────────

async function syncBridgeRegistration(appOrigin) {
  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({ ids: [BRIDGE_SCRIPT_ID] });
    if (existing.length) await chrome.scripting.unregisterContentScripts({ ids: [BRIDGE_SCRIPT_ID] });
  } catch (e) {
    /* nothing registered yet */
  }
  if (!appOrigin) return;
  try {
    await chrome.scripting.registerContentScripts([
      {
        id: BRIDGE_SCRIPT_ID,
        matches: [appOrigin.replace(/\/+$/, "") + "/*"],
        js: [BRIDGE_SCRIPT_FILE],
        runAt: "document_idle",
        persistAcrossSessions: true,
      },
    ]);
  } catch (e) {
    console.warn("CareerCraft: could not register app bridge", e && e.message);
  }
}

// ── Poll / claim / open ─────────────────────────────────────────────────

let polling = false;

async function unpair() {
  await clearPairing();
  await syncBridgeRegistration(null);
  await resetActiveTask();
  await stopAgentTab();
}

// "My browser" agent mode: the command long-poll lives in agent.js.
configureAgent({ apiFetch, onUnpaired: unpair });

const submitClaims = new Set();

const PERMISSION_WAIT_MS = 10 * 60 * 1000;

// Tell the server this application cannot proceed, so it is not left
// "claimed" (which blocks queuing it again) until it expires.
async function failTask(pairing, taskId, message) {
  try {
    await apiFetch(pairing, `/extension/device/tasks/${taskId}/events`, {
      method: "POST",
      json: { stage: "failed", message },
    });
  } catch (e) {
    console.warn("CareerCraft: could not report failure", e && e.message);
  }
}

async function pollOnce() {
  if (polling) return;
  polling = true;
  try {
    const pairing = await getPairing();
    if (!pairing) return;

    const active = await getActiveTask();
    if (active && !TERMINAL_STAGES.has(active.stage)) {
      if (active.stage === "permission_required") {
        if (!(await hasHostPermission(active.task.job_url))) {
          if (active.since && Date.now() - active.since > PERMISSION_WAIT_MS) {
            await failTask(pairing, active.taskId, "Site access was not granted, so this application was skipped.");
            await chrome.storage.session.remove(SESSION_KEYS.HOST_PERMISSION_NEEDED);
            await setBadge("");
            await resetActiveTask();
          }
          return;
        }
        await chrome.storage.session.remove(SESSION_KEYS.HOST_PERMISSION_NEEDED);
        await setBadge("");
        const tab = await chrome.tabs.create({ url: active.task.job_url, active: true });
        await setActiveTask({ ...active, tabId: tab.id, stage: "claimed" });
        return;
      }
      if (active.waitingForHost) {
        if (!(await hasHostPermission(active.waitingForHost))) return;
        await chrome.storage.session.remove(SESSION_KEYS.HOST_PERMISSION_NEEDED);
        const { waitingForHost, ...rest } = active;
        await setActiveTask(rest);
        await setBadge("");
        await injectAndRun(active.tabId, active.task, active.submitting);
        return;
      }
      try {
        await chrome.tabs.get(active.tabId);
        return; // one task at a time — still working this one.
      } catch (e) {
        await resetActiveTask(); // its tab is gone; onRemoved should have caught this already.
      }
    }

    const res = await apiFetch(pairing, "/extension/device/tasks/claim", { method: "POST" });
    if (res.status === 401) {
      await unpair();
      return;
    }
    if (!res.ok && res.status !== 204) {
      await chrome.storage.session.set({ lastError: res.error });
      return;
    }
    if (res.status === 204 || !res.data || !res.data.job_url) return;

    const task = res.data;
    try {
      jobUrl(task.job_url);
    } catch (e) {
      await failTask(pairing, task.id, "This application's address is not a public https page.");
      await chrome.storage.session.set({ lastError: e.message || "Could not open this application." });
      return;
    }
    const permitted = await hasHostPermission(task.job_url);
    if (!permitted) {
      let host = task.job_url;
      try {
        host = new URL(task.job_url).hostname;
      } catch (e) {
        /* keep raw url */
      }
      await chrome.storage.session.set({ [SESSION_KEYS.HOST_PERMISSION_NEEDED]: host });
      await setActiveTask({ taskId: task.id, tabId: null, stage: "permission_required", task, since: Date.now() });
      await setBadge("!", "#d97706");
      await openPopupIfPossible();
      return;
    }

    let tab;
    try {
      tab = await chrome.tabs.create({ url: task.job_url, active: true });
    } catch (e) {
      await failTask(pairing, task.id, "The browser could not open this application.");
      throw e;
    }
    await setActiveTask({ taskId: task.id, tabId: tab.id, stage: task.status || "claimed", task });
  } catch (e) {
    await chrome.storage.session.set({ lastError: e.message || "Could not open this application." });
    console.warn("CareerCraft: poll failed", e && e.message);
  } finally {
    polling = false;
  }
}

// A wake-up usually arrives right as the web app starts an application;
// its task appears a moment later, once the workflow has reserved it.
function pollBurst() {
  for (const ms of [0, 1500, 4000, 8000, 15000]) setTimeout(pollOnce, ms);
}

// The job tab moved to a site the extension may not touch yet (a company
// career site, an ATS that is not in the manifest). Ask in the popup, then
// continue in the same tab once the user allows it.
async function requestTabPermission(active, url) {
  let host = url;
  try {
    host = new URL(url).hostname;
  } catch (e) {
    /* keep raw url */
  }
  await chrome.storage.session.set({ [SESSION_KEYS.HOST_PERMISSION_NEEDED]: host });
  await setActiveTask({ ...active, waitingForHost: url });
  await setBadge("!", "#d97706");
  await openPopupIfPossible();
}

async function injectAndRun(tabId, task, submitting) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: CONTENT_SCRIPT_FILES });
    await chrome.tabs.sendMessage(tabId, { type: "RUN_TASK", task, submitting: !!submitting });
  } catch (e) {
    // Common and harmless: chrome:// pages, PDF viewer tabs, or a tab that
    // navigated away again before the script could run.
    console.warn("CareerCraft: injection skipped for tab", tabId, e && e.message);
    await chrome.storage.session.set({ lastError: `Could not run the application assistant: ${e.message}. Grant site access from the popup.` });
  }
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  onAgentTabUpdated(tabId, changeInfo, tab);
  if (changeInfo.status !== "complete") return;
  (async () => {
    const active = await getActiveTask();
    if (!active || active.tabId !== tabId) return;
    if (TERMINAL_STAGES.has(active.stage)) return;
    const url = tab?.url || "";
    if (/^https:/.test(url) && !(await hasHostPermission(url))) {
      await requestTabPermission(active, url);
      return;
    }
    if (active.waitingForHost) {
      // The tab moved on to a page the extension can already run on.
      const { waitingForHost, ...rest } = active;
      await setActiveTask(rest);
      await chrome.storage.session.remove(SESSION_KEYS.HOST_PERMISSION_NEEDED);
      await setBadge("");
    }
    await injectAndRun(tabId, active.task, active.submitting);
  })();
});

// The popup's permission prompt usually closes the popup before its own
// promise settles, so continue from here as soon as Chrome records the grant.
chrome.permissions.onAdded.addListener(() => {
  onPermissionAdded();
  pollOnce();
});

chrome.tabs.onRemoved.addListener((tabId) => {
  onAgentTabRemoved(tabId);
  (async () => {
    const active = await getActiveTask();
    if (!active || active.tabId !== tabId) return;
    if (!TERMINAL_STAGES.has(active.stage)) {
      const pairing = await getPairing();
      if (pairing) {
        await apiFetch(pairing, `/extension/device/tasks/${active.taskId}/events`, {
          method: "POST",
          json: { stage: "cancelled", message: "Tab closed", submit_attempted: !!active.submitting },
        });
      }
    }
    await resetActiveTask();
  })();
});

// ── Messages ─────────────────────────────────────────────────────────────

async function handleMessage(msg, sender) {
  const popup = sender.id === chrome.runtime.id && !sender.tab && sender.url === chrome.runtime.getURL("popup.html");
  const contentMessages = new Set(["CC_PLAN", "CC_EVENT", "CC_REVIEW", "CC_SUBMIT_STATUS", "CC_ANSWER", "CC_DECIDE", "CC_RESUME", "CC_NAVIGATE"]);
  if (contentMessages.has(msg?.type) && !taskSenderAllowed(sender, await getActiveTask(), msg, chrome.runtime.id)) {
    return { error: "This page does not own the active application." };
  }
  if (["CC_GET_STATUS", "CC_VALIDATE_AND_PAIR", "CC_DISCONNECT", "CC_CHECK_NOW", "CC_APPROVE_SUBMIT"].includes(msg?.type) && !popup) return { error: "Open the CareerCraft extension popup to perform this action." };
  switch (msg && msg.type) {
    case "WAKE":
    case "CC_CHECK_NOW": {
      if (!popup) {
        const pairing = await getPairing();
        if (!pairing || new URL(sender.url || "https://invalid.invalid").origin !== pairing.appOrigin) return { error: "Invalid app origin" };
      }
      pollBurst();
      return { ok: true };
    }

    case "AGENT_WAKE": {
      const pairing = await getPairing();
      if (!pairing || new URL(sender.url || "https://invalid.invalid").origin !== pairing.appOrigin) return { error: "Invalid app origin" };
      startCommandLoop();
      return { ok: true };
    }

    case "CC_AGENT_STOP":
      return stopFromBanner(sender);

    case "CC_PLAN": {
      const pairing = await getPairing();
      if (!pairing) return { error: "not_paired" };
      const res = await apiFetch(pairing, `/extension/device/tasks/${msg.taskId}/plan`, {
        method: "POST",
        json: { url: msg.url, fields: msg.fields },
      });
      if (!res.ok) return { error: res.error || `http_${res.status}`, status: res.status };
      return { data: res.data };
    }

    case "CC_EVENT": {
      const pairing = await getPairing();
      if (!pairing) return { error: "not_paired" };
      const current = await getActiveTask();
      const res = await apiFetch(pairing, `/extension/device/tasks/${msg.taskId}/events`, {
        method: "POST",
        json: {
          stage: msg.stage,
          message: msg.message,
          confirmation_text: msg.confirmation_text,
          confirmation_url: msg.confirmation_url,
          error: msg.error,
          submission_token: current?.submitPermit?.token,
          submit_attempted: current?.taskId !== msg.taskId || !!current?.submitting,
        },
      });
      if (!res.ok) return { error: res.error || `http_${res.status}`, status: res.status, active: false };
      const active = await getActiveTask();
      if (active && active.taskId === msg.taskId) {
        if (TERMINAL_STAGES.has(msg.stage) || res.data.active === false) {
          await resetActiveTask();
        } else {
          await setActiveTask({ ...active, stage: msg.stage });
        }
      }
      return { data: res.data };
    }

    case "CC_NAVIGATE": {
      // The runner found the way to the application form: follow it in the
      // same tab (never a new one), within the hop budget. `to` is null for
      // an in-page Apply button the runner clicks itself; it still counts.
      const active = await getActiveTask();
      if (active.submitting || active.review) return { error: "The application is already under review." };
      const hops = (active.navigations || 0) + 1;
      if (hops > MAX_NAVIGATIONS) return { error: "Could not find the application form after following the Apply links." };
      let target = null;
      if (msg.to) {
        try {
          target = jobUrl(msg.to).href;
        } catch (e) {
          return { error: e.message };
        }
      }
      await setActiveTask({ ...active, navigations: hops });
      if (target) await chrome.tabs.update(active.tabId, { url: target });
      return { ok: true };
    }

    case "CC_REVIEW": {
      const pairing = await getPairing();
      if (!pairing) return { error: "Reconnect the extension." };
      const res = await apiFetch(pairing, `/extension/device/tasks/${msg.taskId}/review`, { method: "POST", json: msg.snapshot });
      if (!res.ok) return { error: res.error };
      const active = await getActiveTask();
      if (!active || active.taskId !== msg.taskId) return { error: "This application is no longer active." };
      await setActiveTask({ ...active, review: { ...res.data, snapshot: msg.snapshot }, submitPermit: null });
      // The user approves the final form in the popup: point them at it.
      await setBadge("1", "#2563eb");
      await openPopupIfPossible();
      return { data: res.data };
    }

    case "CC_APPROVE_SUBMIT": {
      const active = await getActiveTask();
      const pairing = await getPairing();
      if (!active?.review || !pairing || active.taskId !== msg.taskId || active.review.review_hash !== msg.reviewHash) return { error: "Review changed; reopen the popup." };
      if (active.submitPermit || active.submitting) return { error: "Already approved; do not submit again." };
      const res = await apiFetch(pairing, `/extension/device/tasks/${active.taskId}/approve-submit`, { method: "POST", json: { review_hash: active.review.review_hash, user_confirmed: true } });
      if (!res.ok) return { error: res.error };
      await setActiveTask({ ...active, submitPermit: { token: res.data.submission_token, expiresAt: res.data.expires_at } });
      await setBadge("");
      return { ok: true };
    }

    case "CC_SUBMIT_STATUS": {
      const active = await getActiveTask();
      if (!active) return { error: "This application is no longer active." };
      if (active.submitting) return { error: "Submission was already claimed. Verify its outcome on the job site." };
      if (!active.review || Date.parse(active.review.expires_at) <= Date.now()) return { error: "Review expired. Request a fresh review." };
      if (!active.submitPermit) return { waiting: true };
      if (Date.parse(active.submitPermit.expiresAt) <= Date.now()) return { error: "Approval expired. Verify the application before retrying." };
      if (!msg.consume) return { approved: true };
      if (snapshotKey(msg.snapshot) !== snapshotKey(active.review.snapshot)) return { error: "The form changed after review. Nothing was clicked; verify before retrying." };
      if (submitClaims.has(active.taskId)) return { error: "Submission was already claimed." };
      submitClaims.add(active.taskId);
      await setActiveTask({ ...active, submitting: true, stage: "submitting" });
      return { ok: true };
    }

    case "CC_ANSWER": {
      const pairing = await getPairing();
      if (!pairing) return { error: "not_paired" };
      const res = await apiFetch(pairing, "/extension/device/answers", {
        method: "POST",
        json: { label: msg.label, value: msg.value, question_key: msg.question_key },
      });
      if (!res.ok) return { error: res.error || `http_${res.status}` };
      return { data: res.data };
    }

    case "CC_DECIDE": {
      const pairing = await getPairing();
      if (!pairing) return { error: "not_paired" };
      const res = await apiFetch(pairing, "/extension/device/decide", {
        method: "POST",
        json: { state: msg.state, questions: msg.questions },
      });
      if (!res.ok) return { error: res.error || `http_${res.status}` };
      return { data: res.data };
    }

    case "CC_RESUME": {
      const pairing = await getPairing();
      if (!pairing) return { error: "not_paired" };
      const result = await fetchResume(pairing, msg.taskId);
      if (!result.ok) return { error: result.error || `http_${result.status}`, status: result.status };
      return { data: result };
    }

    case "CC_GET_STATUS": {
      const pairing = await getPairing();
      const active = await getActiveTask();
      const { [SESSION_KEYS.HOST_PERMISSION_NEEDED]: hostPermissionNeeded } = await chrome.storage.session.get(
        SESSION_KEYS.HOST_PERMISSION_NEEDED
      );
      if (!pairing) return { paired: false, hostPermissionNeeded: hostPermissionNeeded || null };
      const me = await apiFetch(pairing, "/extension/device/me");
      if (me.status === 401) {
        await clearPairing();
        await syncBridgeRegistration(null);
        return { paired: false, hostPermissionNeeded: hostPermissionNeeded || null };
      }
      return {
        paired: true,
        appOrigin: pairing.appOrigin,
        device: me.ok ? me.data : null,
        activeTask: active,
        hostPermissionNeeded: hostPermissionNeeded || null,
        error: (await chrome.storage.session.get("lastError")).lastError,
      };
    }

    case "CC_VALIDATE_AND_PAIR": {
      const pairing = { appOrigin: appOrigin(msg.appOrigin), token: msg.token };
      const me = await apiFetch(pairing, "/extension/device/me");
      if (!me.ok || !me.data) {
        return { ok: false, error: me.status === 401 ? "Invalid or expired connection code" : "Could not reach CareerCraft AI at that URL" };
      }
      const previous = await getPairing();
      if (previous && previous.token !== pairing.token) await revokeCurrentPairing();
      await setPairing(pairing);
      await syncBridgeRegistration(pairing.appOrigin);
      pollOnce();
      startCommandLoop();
      return { ok: true, device: me.data };
    }

    case "CC_PAIR_FROM_BRIDGE": {
      const origin = appOrigin(msg.appOrigin);
      // A page can only (re)pair the extension to its own origin, and never
      // take over a pairing to a different CareerCraft deployment — that
      // needs the popup.
      const current = await getPairing();
      const appUrl = new URL(origin);
      const knownApp = ["careercraftsai.me", "www.careercraftsai.me", "app.careercraftsai.me"].includes(appUrl.hostname);
      if (sender.id !== chrome.runtime.id || sender.frameId !== 0 || new URL(sender.url).origin !== origin || (!knownApp && current?.appOrigin !== origin)) return { error: "Invalid app origin" };
      if (current && current.appOrigin !== origin) return { ok: false, reason: "paired_elsewhere" };
      if (!isStaticOrigin(origin)) {
        const granted = await chrome.permissions.contains({ origins: [origin + "/*"] });
        if (!granted) return { ok: false, reason: "permission_required" };
      }
      const pairing = { appOrigin: origin, token: msg.token };
      const me = await apiFetch(pairing, "/extension/device/me");
      if (!me.ok || !me.data) return { ok: false, reason: "invalid_token" };
      if (current && current.token !== msg.token) await revokeCurrentPairing();
      await setPairing(pairing);
      await syncBridgeRegistration(origin);
      pollOnce();
      startCommandLoop();
      return { ok: true };
    }

    case "CC_DISCONNECT": {
      await revokeCurrentPairing();
      await clearPairing();
      await resetActiveTask();
      await stopAgentTab();
      await syncBridgeRegistration(null);
      return { ok: true };
    }

    default:
      return { error: "unknown_message" };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg, sender).then(sendResponse).catch((error) => sendResponse({ error: error.message || "Extension operation failed." }));
  return true; // keep the channel open for the async response
});

// ── Startup ───────────────────────────────────────────────────────────────

async function init() {
  await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  await chrome.storage.session.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  try {
    chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
  } catch (e) {
    /* alarms API unavailable in some test harnesses */
  }
  const pairing = await getPairing();
  if (pairing) await syncBridgeRegistration(pairing.appOrigin);
  pollOnce();
  startCommandLoop();
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) {
    pollOnce();
    startCommandLoop();
  }
});
chrome.runtime.onInstalled.addListener(() => {
  init();
});
chrome.runtime.onStartup.addListener(() => {
  init();
});

// A service worker can be evicted and later woken by the alarm/message
// listeners above without onInstalled/onStartup firing again — make sure
// the poll alarm exists whenever the worker (re)loads.
init();
