// CareerCraft AI — send approved LinkedIn outreach from the member's own
// LinkedIn, in their own Chrome (beta, off unless the server enables it).
//
// The backend sends a "linkedin" command per approved draft through the device
// command channel: first {op: "probe"} (read only), then {op: "send"}. Nothing
// here decides *what* to send: the text is the draft the member approved, and
// the server enforces the daily caps. The page functions below report success
// only once LinkedIn shows it (message in the thread, request pending).
//
// LinkedIn's User Agreement prohibits automated tools; the member was told so
// before enabling this.

const PROFILE_RE = /^https:\/\/www\.linkedin\.com\/in\/[A-Za-z0-9\-_%]{3,100}\/$/;
const TAB_KEY = "linkedinTab";
const LOAD_TIMEOUT_MS = 25000;
const NOTE_LIMIT = 300;

const ok = (result) => ({ ok: true, result });
const fail = (status, error) => ({ ok: false, status, error });

export async function linkedinCommand(body) {
  const op = body && body.op;
  if (op !== "probe" && op !== "send") return fail(422, "Unknown LinkedIn operation");
  if (typeof body.profile_url !== "string" || !PROFILE_RE.test(body.profile_url)) {
    return fail(422, "Not a LinkedIn profile link");
  }
  if (op === "send") {
    const text = typeof body.message === "string" ? body.message.trim() : "";
    if (!["invite", "message"].includes(body.as) || !text || text.length > 8000) return fail(422, "Invalid message");
    if (body.as === "invite" && text.length > NOTE_LIMIT) return fail(422, "A connection note is at most 300 characters");
  }
  let tabId;
  try {
    tabId = await openProfile(body.profile_url);
  } catch (e) {
    // Nothing was clicked yet: a plain refusal, never "maybe sent".
    return fail(409, "LinkedIn did not load; nothing was sent.");
  }
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId },
    func: op === "probe" ? probePage : sendOnPage,
    args: op === "probe" ? [] : [body.as, body.message.trim()],
  });
  return ok(injection && injection.result ? injection.result : { error: "No result from the page" });
}

// One background tab, reused, grouped so the member can see what it is.
async function openProfile(url) {
  const { [TAB_KEY]: saved } = await chrome.storage.session.get(TAB_KEY);
  let tab = null;
  if (saved) {
    try {
      tab = await chrome.tabs.get(saved);
    } catch (e) {
      tab = null;
    }
  }
  if (!tab) {
    // A blank tab first, so the profile load below is always watched from its start.
    tab = await chrome.tabs.create({ url: "about:blank", active: false });
    await chrome.storage.session.set({ [TAB_KEY]: tab.id });
    try {
      const group = await chrome.tabs.group({ tabIds: tab.id });
      await chrome.tabGroups.update(group, { title: "CareerCraft LinkedIn", color: "blue" });
    } catch (e) {
      /* tab groups unavailable */
    }
  }
  // Listen before navigating: a fast (cached) load must not complete unseen.
  const loaded = waitForLoad(tab.id);
  await chrome.tabs.update(tab.id, { url });
  await loaded;
  return tab.id;
}

function waitForLoad(tabId) {
  return new Promise((resolve, reject) => {
    let started = false;
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("LinkedIn took too long to load"));
    }, LOAD_TIMEOUT_MS);
    function listener(id, info, tab) {
      if (id !== tabId) return;
      // The new tab's own about:blank load can still be reporting: only a
      // linkedin.com page counts (a login or authwall redirect included).
      // www or a country subdomain (uk.linkedin.com) after a LinkedIn redirect.
      const onLinkedIn = /^https:\/\/(?:www\.|[a-z]{2}\.)?linkedin\.com\//.test((tab && tab.url) || "");
      if (info.status === "loading" && onLinkedIn) started = true;
      // "complete" only counts after this navigation began loading.
      if (started && onLinkedIn && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ── Page functions (run inside linkedin.com; self-contained) ─────────────

// Read only: sign-in state, degree, and what the profile offers.
export async function probePage() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const text = (el) => ((el && (el.innerText || el.textContent)) || "").replace(/\s+/g, " ").trim();
  const label = (el) => `${el.getAttribute("aria-label") || ""} ${text(el)}`.trim();

  if (/^\/(login|authwall|checkpoint|uas\/login|signup)/.test(location.pathname)) return { signed_out: true };
  if (document.querySelector("form.login__form, [data-test-id='sign-in-form'], .join-form")) return { signed_out: true };
  let card = null;
  for (let i = 0; i < 24 && !card; i++) {
    const main = document.querySelector("main");
    card = main && main.querySelector("h1") ? main.querySelector("section") || main : null;
    if (!card) await sleep(500);
  }
  if (!card) return { not_found: true };
  if (/this page (doesn.t|does not) exist|profile (is )?not available|page isn.t available/i.test(text(card))) return { not_found: true };

  const buttons = Array.from(card.querySelectorAll("button, a[role=button], a[href*='/messaging/']")).filter(visible);
  const has = (re) => buttons.some((b) => re.test(label(b)));
  const badge = card.querySelector(".dist-value, [class*='distance-badge']");
  const degree = (text(badge) || text(card)).match(/\b(1st|2nd|3rd)\b/);
  const pending = has(/\bpending\b/i);
  let canConnect = !pending && has(/\b(connect|invite .* to connect)\b/i);
  const firstDegree = !!(degree && degree[1] === "1st");
  const more = buttons.find((b) => /^more( actions)?\b/i.test(label(b)));
  if (!pending && !canConnect && more && !firstDegree) {
    // Connect is often tucked under "More": look, then close the menu again,
    // so a daily invite is only claimed when Connect really is offered.
    more.click();
    let item = null;
    for (let i = 0; i < 12 && !item; i++) {
      await sleep(250);
      item = Array.from(document.querySelectorAll("[role=menuitem], div[role=button], li button, .artdeco-dropdown__item")).find(
        (el) => visible(el) && /\b(invite .* to connect|^connect)\b/i.test(label(el))
      );
    }
    canConnect = !!item;
    more.click();
  }
  return {
    degree: degree ? degree[1] : null,
    can_message: has(/\bmessage\b/i) && !has(/^inmail\b/i),
    can_connect: canConnect,
    pending,
  };
}

// Send: "message" to a first-degree connection, or "invite" with a note.
// Returns {delivery} once LinkedIn shows it, {reason: "limit"} when LinkedIn
// refuses for its own limits (nothing sent), {reason: "unconfirmed"} if Send
// was pressed but LinkedIn never showed the result, or {error} when nothing
// was sent.
export async function sendOnPage(kind, message) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const text = (el) => ((el && (el.innerText || el.textContent)) || "").replace(/\s+/g, " ").trim();
  const label = (el) => `${el.getAttribute("aria-label") || ""} ${text(el)}`.trim();
  const waitFor = async (fn, ms) => {
    for (let t = 0; t < ms; t += 250) {
      const v = fn();
      if (v) return v;
      await sleep(250);
    }
    return null;
  };
  const press = (el) => {
    el.scrollIntoView({ block: "center" });
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
    }
    el.click();
  };
  const card = () => {
    const main = document.querySelector("main");
    return main && main.querySelector("h1") ? main.querySelector("section") || main : null;
  };
  const find = (root, re, selector = "button, a[role=button], [role=menuitem], div[role=button], a[href*='/messaging/']") =>
    root ? Array.from(root.querySelectorAll(selector)).find((b) => visible(b) && re.test(label(b))) : null;
  const LIMIT_RE = /(weekly|monthly) (invitation )?limit|used all (of )?your (free )?personali[sz]ed invitations|out of (free )?personali[sz]ed|reached the (weekly )?limit/i;
  const dialog = () => Array.from(document.querySelectorAll("[role=dialog], [role=alertdialog]")).find(visible) || null;
  const dismiss = () => {
    const d = dialog();
    const close = d && find(d, /^(dismiss|cancel|close|got it)\b/i);
    if (close) press(close);
  };

  const top = await waitFor(card, 12000);
  if (!top) return { error: "LinkedIn profile did not load; nothing was sent." };

  if (kind === "message") {
    const open = find(top, /\bmessage\b/i);
    if (!open) return { error: "No Message button on this profile; nothing was sent." };
    press(open);
    const box = await waitFor(() => Array.from(document.querySelectorAll(".msg-form__contenteditable[contenteditable=true], [role=textbox][contenteditable=true]")).find(visible), 8000);
    if (!box) return { error: "LinkedIn's message box did not open; nothing was sent." };
    box.focus();
    document.execCommand("insertText", false, message);
    box.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: message }));
    const head = message.slice(0, 40);
    const matching = () =>
      Array.from(document.querySelectorAll(".msg-s-event-listitem__body, .msg-s-message-list__event, [data-event-urn]")).filter((m) =>
        text(m).includes(head)
      ).length;
    // The thread may already hold this text (an earlier send): require a new one.
    const before = matching();
    const form = box.closest("form") || document;
    const send = await waitFor(() => {
      const b = form.querySelector("button.msg-form__send-button, button[type=submit]") || find(form, /^send\b/i);
      return b && !b.disabled ? b : null;
    }, 4000);
    if (!send) return { error: "LinkedIn's Send button stayed disabled; nothing was sent." };
    press(send);
    const shown = await waitFor(() => matching() > before, 10000);
    return shown ? { delivery: "messaged" } : { reason: "unconfirmed" };
  }

  // Invite with a note.
  let connect = find(top, /\b(invite .* to connect|^connect)\b/i);
  if (!connect) {
    const more = find(top, /^more( actions)?\b/i);
    if (!more) return { error: "No Connect option on this profile; nothing was sent." };
    press(more);
    connect = await waitFor(() => find(document, /\b(invite .* to connect|^connect)\b/i, "[role=menuitem], div[role=button], li button, .artdeco-dropdown__item"), 4000);
    if (!connect) return { error: "No Connect option on this profile; nothing was sent." };
  }
  press(connect);
  const modal = await waitFor(dialog, 6000);
  if (!modal) return { error: "LinkedIn's invitation dialog did not open; nothing was sent." };
  if (/enter (their|the member.s) email|how do you know/i.test(text(modal))) {
    dismiss();
    return { error: "LinkedIn asks for this person's email to connect; nothing was sent." };
  }
  if (LIMIT_RE.test(text(modal))) {
    dismiss();
    return { reason: "limit" };
  }
  const addNote = find(modal, /^add (a )?note\b/i);
  if (addNote) press(addNote);
  const note = await waitFor(() => {
    const d = dialog();
    return d && Array.from(d.querySelectorAll("textarea")).find(visible);
  }, 5000);
  if (!note) {
    const d = dialog();
    const limited = d && LIMIT_RE.test(text(d));
    dismiss();
    // Free accounts run out of notes; never send a bare request instead.
    return limited ? { reason: "limit" } : { error: "LinkedIn did not offer a note; nothing was sent." };
  }
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  setter.call(note, message);
  note.dispatchEvent(new Event("input", { bubbles: true }));
  note.dispatchEvent(new Event("change", { bubbles: true }));
  const send = await waitFor(() => {
    const d = dialog();
    const b = d && find(d, /^send( invitation| now)?\b/i);
    return b && !b.disabled ? b : null;
  }, 4000);
  if (!send) {
    dismiss();
    return { error: "LinkedIn's Send button stayed disabled; nothing was sent." };
  }
  press(send);
  const outcome = await waitFor(() => {
    const d = dialog();
    if (d && LIMIT_RE.test(text(d))) return "limit";
    const c = card();
    if (!d && c && find(c, /\bpending\b/i)) return "invited";
    if (/invitation sent/i.test(text(document.querySelector("[role=alert], .artdeco-toast-item")))) return "invited";
    return null;
  }, 10000);
  if (outcome === "invited") return { delivery: "invited" };
  if (outcome === "limit") {
    dismiss();
    return { reason: "limit" };
  }
  return { reason: "unconfirmed" };
}
