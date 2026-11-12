// CareerCraft AI — options page: the local autofill profile and the resume
// used by "Autofill this page".
import { LOCAL_KEYS } from "./common.js";

const $ = (id) => document.getElementById(id);
const form = $("profile");
const MAX_RESUME_BYTES = 5 * 1024 * 1024;

function bytesToBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function showError(text) {
  $("error").textContent = text || "";
  $("error").hidden = !text;
}

async function load() {
  const stored = await chrome.storage.local.get([LOCAL_KEYS.AUTOFILL_PROFILE, LOCAL_KEYS.AUTOFILL_RESUME]);
  const profile = stored[LOCAL_KEYS.AUTOFILL_PROFILE] || {};
  for (const el of form.elements) {
    if (el.name && Object.prototype.hasOwnProperty.call(profile, el.name)) el.value = profile[el.name];
  }
  const resume = stored[LOCAL_KEYS.AUTOFILL_RESUME];
  $("resume-name").textContent = resume ? `Saved: ${resume.filename}` : "No resume saved.";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const profile = {};
  for (const el of form.elements) {
    if (el.name) profile[el.name] = el.value.trim();
  }
  if (!profile.email) return showError("Email is required.");
  const update = { [LOCAL_KEYS.AUTOFILL_PROFILE]: profile };
  const file = $("resume").files[0];
  if (file) {
    if (file.size > MAX_RESUME_BYTES) return showError("The resume must be 5 MB or smaller.");
    update[LOCAL_KEYS.AUTOFILL_RESUME] = {
      filename: file.name,
      mime: file.type || "application/pdf",
      base64: bytesToBase64(new Uint8Array(await file.arrayBuffer())),
    };
  }
  await chrome.storage.local.set(update);
  $("saved").textContent = "Saved.";
  load();
});

load();
