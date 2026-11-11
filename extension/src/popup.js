// CareerCraft AI — toolbar popup: pairing, status and permissions.
import { isStaticOrigin, getSettings, setSettings, getActiveTask, CONTENT_SCRIPT_FILES, LOCAL_KEYS } from "./common.js";

const $ = (id) => document.getElementById(id);

const STAGE_LABELS = {
  claimed: "Opening the job page",
  filling: "Filling the application",
  needs_input: "Waiting for your answers in the page",
  review: "Waiting for your review in the page",
  login_required: "Sign in to the job site in that tab",
  permission_required: "Waiting for site access",
  submitting: "Submission approved; verifying the outcome",
};

function send(message) {
  return new Promise((resolve) => chrome.runtime.sendMessage(message, (response) => {
    const error = chrome.runtime.lastError;
    resolve(response || { error: error?.message || "The extension did not respond. Reload it and reconnect." });
  }));
}

function showError(text) {
  $("error").textContent = text || "";
  $("error").hidden = !text;
}

async function ensureOriginPermission(origin) {
  if (isStaticOrigin(origin)) return true;
  // Must run inside the click handler: Chrome only prompts on a user gesture.
  return chrome.permissions.request({ origins: [origin.replace(/\/+$/, "") + "/*"] });
}

async function render() {
  const status = await send({ type: "CC_GET_STATUS" });
  if (status.error) showError(status.error);
  $("pair").hidden = !!status.paired;
  $("status").hidden = !status.paired;
  if (!status.paired) return;

  const user = status.device && status.device.user;
  const who = user ? user.email || user.name : "your account";
  const device = status.device ? ` (${status.device.device_name})` : "";
  $("who").textContent = `Connected as ${who}${device}`;

  const active = status.activeTask;
  if (active && active.task) {
    const what = [active.task.company, active.task.role].filter(Boolean).join(" · ") || "Job application";
    const stage = active.review && !active.submitting ? "Ready for your final approval below" : STAGE_LABELS[active.stage] || active.stage;
    $("task").textContent = `${what} — ${stage}`;
  } else {
    $("task").textContent = "No applications waiting.";
  }

  const host = status.hostPermissionNeeded;
  $("permission").hidden = !host;
  if (host) $("permission-text").textContent = `Allow the extension on ${host} to continue this application.`;
  const review = active?.review;
  $("review").hidden = !review || active.submitting;
  if (review) {
    $("review-url").textContent = review.snapshot.url;
    $("review-fields").replaceChildren();
    for (const field of review.snapshot.fields) {
      const label = document.createElement("dt");
      const value = document.createElement("dd");
      label.textContent = field.label || field.id;
      value.textContent = Array.isArray(field.value) ? field.value.join(", ") : String(field.value ?? "");
      $("review-fields").append(label, value);
    }
    $("approve-submit").disabled = !!active.submitPermit || Date.parse(review.expires_at) <= Date.now();
    $("approve-submit").onclick = async (event) => {
      if (!event.isTrusted) return;
      $("approve-submit").disabled = true;
      const result = await send({ type: "CC_APPROVE_SUBMIT", taskId: active.taskId, reviewHash: review.review_hash });
      if (!result.ok) showError(result.error || "Approval failed. No submit was authorized.");
      else $("approve-submit").textContent = "Approved — checking the outcome";
    };
  }
}

$("connect").addEventListener("click", async () => {
  showError("");
  const appOrigin = $("app-origin").value.trim().replace(/\/+$/, "");
  const token = $("token").value.trim();
  if (!/^https?:\/\//.test(appOrigin)) return showError("Enter the full CareerCraft URL, e.g. https://app.example.com");
  if (!token.startsWith("ccx_")) return showError("Paste the connection code from CareerCraft (it starts with ccx_).");
  if (!(await ensureOriginPermission(appOrigin))) return showError("The extension needs access to your CareerCraft site to connect.");
  $("connect").disabled = true;
  const result = await send({ type: "CC_VALIDATE_AND_PAIR", appOrigin, token });
  $("connect").disabled = false;
  if (!result.ok) return showError(result.error || "Could not connect.");
  $("token").value = "";
  render();
});

$("check").addEventListener("click", async () => {
  await send({ type: "CC_CHECK_NOW" });
  setTimeout(render, 1500);
});

$("disconnect").addEventListener("click", async () => {
  await send({ type: "CC_DISCONNECT" });
  render();
});

$("grant").addEventListener("click", async () => {
  const status = await send({ type: "CC_GET_STATUS" });
  const host = status.hostPermissionNeeded;
  if (!host) return;
  const granted = await chrome.permissions.request({ origins: [`https://${host}/*`] });
  if (granted) {
    await chrome.storage.session.remove("hostPermissionNeeded");
    await send({ type: "CC_CHECK_NOW" });
    render();
  }
});

// ── Auto-apply and one-click autofill ─────────────────────────────────────

const AUTO_HINTS = {
  false: "Off: you approve every application here before it is submitted.",
  true: "On: applications that need nothing from you are submitted without the approval click. It still stops for sign-in, CAPTCHA, unanswered required questions, AI-drafted answers, validation errors and consent boxes.",
};

async function renderTools() {
  const { autoApply } = await getSettings();
  $("auto-apply").checked = autoApply;
  $("auto-apply-hint").textContent = AUTO_HINTS[autoApply];
}

$("auto-apply").addEventListener("change", async (event) => {
  if (!event.isTrusted) return;
  const next = await setSettings({ autoApply: $("auto-apply").checked });
  $("auto-apply-hint").textContent = AUTO_HINTS[next.autoApply];
});

$("open-options").addEventListener("click", () => chrome.runtime.openOptionsPage());

async function injectAutofill(tabId, payload) {
  const run = (target) =>
    chrome.scripting
      .executeScript({ target, files: CONTENT_SCRIPT_FILES })
      .then(() => chrome.scripting.executeScript({ target, func: (p) => window.CareerCraftAutofill.run(p), args: [payload] }));
  try {
    // Every frame: Greenhouse and others are often embedded in an iframe.
    return await run({ tabId, allFrames: true });
  } catch (e) {
    return run({ tabId });
  }
}

$("autofill").addEventListener("click", async () => {
  showError("");
  const out = $("autofill-result");
  out.textContent = "";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) return showError("Open a job application page first.");
  const active = await getActiveTask();
  if (active && active.tabId === tab.id) return showError("CareerCraft is already applying in this tab.");
  const stored = await chrome.storage.local.get([LOCAL_KEYS.AUTOFILL_PROFILE, LOCAL_KEYS.AUTOFILL_RESUME]);
  const profile = stored[LOCAL_KEYS.AUTOFILL_PROFILE];
  if (!profile || !profile.email) {
    showError("Add your details in Autofill profile & settings first.");
    return chrome.runtime.openOptionsPage();
  }
  $("autofill").disabled = true;
  out.textContent = "Filling…";
  try {
    const results = (await injectAutofill(tab.id, { profile, resume: stored[LOCAL_KEYS.AUTOFILL_RESUME] || null }))
      .map((r) => r && r.result)
      .filter(Boolean);
    const best = results.filter((r) => r.ok).sort((a, b) => (b.filled || 0) - (a.filled || 0))[0];
    const failure = results.find((r) => !r.ok);
    if (best && (best.filled || best.attached)) out.textContent = best.message;
    else if (failure) showError(failure.error);
    else out.textContent = (best && best.message) || "No application form found on this page.";
  } catch (e) {
    showError(`Could not fill this page: ${e.message}. Use "Allow on this site" from a CareerCraft task or reload the page.`);
    out.textContent = "";
  } finally {
    $("autofill").disabled = false;
  }
});

render();
renderTools();
