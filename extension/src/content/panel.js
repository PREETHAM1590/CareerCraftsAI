// CareerCraft AI — review panel UI, rendered inside a closed Shadow DOM so
// the host page's CSS can never leak in (or our styles leak out).
// Classic script — exposes window.CareerCraftPanel.
(function () {
  if (window.__ccPanelLoaded) return;
  window.__ccPanelLoaded = true;

  const CSS = `
    :host { all: initial; }
    .cc-wrap {
      position: fixed; top: 4vh; right: 12px; width: 380px; max-height: 90vh;
      overflow-y: auto; pointer-events: auto; box-sizing: border-box;
      border-radius: 14px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 13px; line-height: 1.45; box-shadow: 0 12px 40px rgba(0,0,0,.28);
      background: #ffffff; color: #16181d; border: 1px solid #e2e5ea;
    }
    @media (prefers-color-scheme: dark) {
      .cc-wrap { background: #1c1f26; color: #eef0f3; border-color: #33384a; }
    }
    .cc-header {
      padding: 14px 16px 10px; border-bottom: 1px solid rgba(127,127,127,.2);
      position: sticky; top: 0; background: inherit; border-radius: 14px 14px 0 0;
    }
    .cc-title { font-weight: 700; font-size: 13px; margin: 0 0 2px; letter-spacing: .1px; }
    .cc-sub { opacity: .75; font-size: 12px; margin: 0; }
    .cc-body { padding: 10px 16px 4px; }
    .cc-msg { margin: 4px 0 12px; }
    .cc-field { margin: 0 0 12px; padding-bottom: 10px; border-bottom: 1px dashed rgba(127,127,127,.2); }
    .cc-field:last-child { border-bottom: none; }
    .cc-flabel { font-weight: 600; font-size: 12.5px; display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
    .cc-badge {
      font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .3px;
      padding: 2px 6px; border-radius: 999px; white-space: nowrap; flex: none;
    }
    .cc-badge.saved { background: #dcf5e3; color: #146c2e; }
    .cc-badge.profile { background: #dbeafe; color: #1e40af; }
    .cc-badge.draft { background: #fef3c7; color: #92400e; }
    .cc-badge.unresolved { background: #fde2e1; color: #9f1a13; }
    @media (prefers-color-scheme: dark) {
      .cc-badge.saved { background: #103b1e; color: #7be79a; }
      .cc-badge.profile { background: #122b4d; color: #92bdfa; }
      .cc-badge.draft { background: #4a3a0c; color: #f7cf6b; }
      .cc-badge.unresolved { background: #4a1614; color: #ff9d97; }
    }
    .cc-fvalue { margin-top: 4px; opacity: .85; word-break: break-word; }
    .cc-reason { margin-top: 4px; font-size: 11.5px; opacity: .7; font-style: italic; }
    .cc-input, .cc-select, .cc-textarea {
      width: 100%; box-sizing: border-box; margin-top: 6px; padding: 7px 8px; border-radius: 8px;
      border: 1px solid rgba(127,127,127,.4); background: transparent; color: inherit; font: inherit;
    }
    .cc-textarea { min-height: 60px; resize: vertical; }
    .cc-checks { display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
    .cc-checks label { display: flex; gap: 6px; align-items: center; font-weight: 400; }
    .cc-footer { padding: 12px 16px 16px; position: sticky; bottom: 0; background: inherit; border-radius: 0 0 14px 14px; }
    .cc-remember { display: flex; align-items: center; gap: 6px; margin: 4px 0 12px; opacity: .9; }
    .cc-btnrow { display: flex; gap: 8px; }
    .cc-btn {
      flex: 1; padding: 10px 12px; border-radius: 9px; border: 1px solid rgba(127,127,127,.35);
      background: rgba(127,127,127,.08); color: inherit; font: inherit; font-weight: 600; cursor: pointer;
    }
    .cc-btn:hover { background: rgba(127,127,127,.16); }
    .cc-btn.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
    .cc-btn.primary:hover { background: #1d4ed8; }
    .cc-btn.danger-outline { color: inherit; }
    .cc-spinner {
      width: 14px; height: 14px; border-radius: 50%; border: 2px solid rgba(127,127,127,.35);
      border-top-color: currentColor; display: inline-block; animation: cc-spin 0.8s linear infinite; margin-right: 8px;
      vertical-align: -2px;
    }
    @keyframes cc-spin { to { transform: rotate(360deg); } }
  `;

  const BADGE_LABEL = {
    user: ["Saved", "saved"],
    profile: ["Profile", "profile"],
    resume: ["Profile", "profile"],
    generated: ["Draft — review", "draft"],
    unresolved: ["Unresolved", "unresolved"],
    page: ["On page", "profile"],
  };

  let host, shadow, wrap, pending;

  function ensureMounted() {
    if (host && document.documentElement.contains(host)) return;
    host = document.createElement("div");
    host.id = "careercraft-panel-host";
    Object.assign(host.style, {
      all: "initial",
      position: "fixed",
      top: "0",
      left: "0",
      width: "0",
      height: "0",
      zIndex: "2147483647",
      pointerEvents: "none",
    });
    document.documentElement.appendChild(host);
    shadow = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);
    wrap = document.createElement("div");
    wrap.className = "cc-wrap";
    shadow.appendChild(wrap);
  }

  function h(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function settle(result) {
    const resolve = pending;
    pending = null;
    if (resolve) resolve(result);
  }

  function mount() {
    ensureMounted();
  }

  function hide() {
    settle({ action: "cancel", reason: "closed" });
    if (host && host.parentNode) host.parentNode.removeChild(host);
    host = shadow = wrap = null;
  }

  function showStatus(text, sub) {
    ensureMounted();
    wrap.innerHTML = "";
    wrap.appendChild(
      h(`
      <div class="cc-header">
        <p class="cc-title"><span class="cc-spinner"></span>CareerCraft</p>
        <p class="cc-sub">${escapeHtml(sub || "")}</p>
      </div>
      <div class="cc-body"><div class="cc-msg">${escapeHtml(text || "Working…")}</div></div>
    `)
    );
  }

  // A finished message (no spinner) with a close button.
  function showNotice(text, sub) {
    ensureMounted();
    wrap.innerHTML = "";
    wrap.appendChild(
      h(`
      <div class="cc-header">
        <p class="cc-title">CareerCraft</p>
        <p class="cc-sub">${escapeHtml(sub || "")}</p>
      </div>
      <div class="cc-body"><div class="cc-msg">${escapeHtml(text || "")}</div></div>
      <div class="cc-footer"><div class="cc-btnrow"><button class="cc-btn" data-role="close">Close</button></div></div>
    `)
    );
    wrap.querySelector("[data-role='close']").addEventListener("click", () => hide());
  }

  function fieldOptions(field, optionsByFieldId) {
    return (optionsByFieldId && optionsByFieldId[field.field_id]) || [];
  }

  function renderFieldEditor(field, optionsByFieldId) {
    const opts = fieldOptions(field, optionsByFieldId);
    const current = Array.isArray(field.value) ? field.value : field.value == null ? "" : field.value;
    if (field.input_type === "checkbox" && opts.length <= 1) {
      return `<div class="cc-checks" data-role="editor" data-kind="single-check">
        <label><input type="checkbox" ${current === true ? "checked" : ""}/> Yes</label>
      </div>`;
    }
    if (field.input_type === "checkbox") {
      const wanted = new Set((Array.isArray(current) ? current : [current]).map((v) => String(v)));
      return `<div class="cc-checks" data-role="editor" data-kind="checks">
        ${opts
          .map(
            (o) =>
              `<label><input type="checkbox" value="${escapeHtml(o)}" ${wanted.has(o) ? "checked" : ""}/> ${escapeHtml(o)}</label>`
          )
          .join("")}
      </div>`;
    }
    if (opts.length > 0) {
      return `<select class="cc-select" data-role="editor">
        <option value="">— choose —</option>
        ${opts.map((o) => `<option value="${escapeHtml(o)}" ${o === current ? "selected" : ""}>${escapeHtml(o)}</option>`).join("")}
      </select>`;
    }
    if (field.input_type === "textarea") {
      return `<textarea class="cc-textarea" data-role="editor" placeholder="Type your answer">${escapeHtml(
        typeof current === "string" ? current : ""
      )}</textarea>`;
    }
    if (field.input_type === "file") {
      return `<div class="cc-reason" data-role="editor" data-kind="none">Attach this file yourself in the page, then continue.</div>`;
    }
    return `<input class="cc-input" data-role="editor" type="text" placeholder="Type your answer" value="${escapeHtml(
      typeof current === "string" ? current : ""
    )}"/>`;
  }

  function readEditor(container, field) {
    const editor = container.querySelector("[data-role='editor']");
    if (!editor) return field.value;
    if (editor.dataset.kind === "none") return field.value;
    if (editor.dataset.kind === "checks") {
      return Array.from(editor.querySelectorAll("input[type=checkbox]"))
        .filter((c) => c.checked)
        .map((c) => c.value);
    }
    if (editor.dataset.kind === "single-check") {
      return editor.querySelector("input[type=checkbox]").checked;
    }
    return editor.value;
  }

  // Sites often put the required asterisk in the label text already.
  function displayLabel(field) {
    const label = String(field.label || field.field_id).trim();
    if (!field.required || /\*\s*$/.test(label)) return label;
    return label + " *";
  }

  function fieldBlock(field, optionsByFieldId, editable) {
    const [badgeText, badgeClass] = BADGE_LABEL[field.source] || [field.source || "?", "unresolved"];
    const needsEditor = editable && !field.readonly && (field.requires_review || field.source === "unresolved" || field.source === "generated");
    const valueLine =
      !needsEditor && field.value != null && field.value !== ""
        ? `<div class="cc-fvalue">${escapeHtml(Array.isArray(field.value) ? field.value.join(", ") : field.value)}</div>`
        : "";
    const reason = field.missing_reason ? `<div class="cc-reason">${escapeHtml(field.missing_reason)}</div>` : "";
    return `
      <div class="cc-field" data-field-id="${escapeHtml(field.field_id)}">
        <div class="cc-flabel"><span>${escapeHtml(displayLabel(field))}</span><span class="cc-badge ${badgeClass}">${escapeHtml(badgeText)}</span></div>
        ${valueLine}
        ${needsEditor ? renderFieldEditor(field, optionsByFieldId) : ""}
        ${reason}
      </div>`;
  }

  function collectTyped(container, fields) {
    const typed = {};
    for (const field of fields) {
      const block = container.querySelector(`[data-field-id="${CSS_escape(field.field_id)}"]`);
      if (!block) continue;
      const editor = block.querySelector("[data-role='editor']");
      if (!editor) continue;
      typed[field.field_id] = readEditor(block, field);
    }
    return typed;
  }

  function CSS_escape(s) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function renderFormPanel(opts) {
    // opts: { title, subtitle, message, fields, optionsByFieldId, primaryLabel,
    //         secondaryLabel, allowRemember }
    ensureMounted();
    settle({ action: "cancel", reason: "superseded" });
    wrap.innerHTML = "";
    const fields = opts.fields || [];
    wrap.appendChild(
      h(`
      <div class="cc-header">
        <p class="cc-title">${escapeHtml(opts.title)}</p>
        <p class="cc-sub">${escapeHtml(opts.subtitle || "")}</p>
      </div>
      <div class="cc-body">
        ${opts.message ? `<div class="cc-msg">${escapeHtml(opts.message)}</div>` : ""}
        ${fields.map((f) => fieldBlock(f, opts.optionsByFieldId, true)).join("")}
      </div>
      <div class="cc-footer">
        ${
          opts.allowRemember
            ? `<label class="cc-remember"><input type="checkbox" data-role="remember" checked/> Remember my answers</label>`
            : ""
        }
        <div class="cc-btnrow">
          <button class="cc-btn" data-role="cancel">Cancel</button>
          <button class="cc-btn primary" data-role="primary">${escapeHtml(opts.primaryLabel)}</button>
        </div>
      </div>
    `)
    );

    return new Promise((resolve) => {
      pending = resolve;
      wrap.querySelector("[data-role='cancel']").addEventListener("click", () => {
        settle({ action: "cancel" });
      });
      wrap.querySelector("[data-role='primary']").addEventListener("click", () => {
        const typed = collectTyped(wrap, fields);
        const rememberEl = wrap.querySelector("[data-role='remember']");
        settle({ action: opts.primaryAction, typed, remember: rememberEl ? rememberEl.checked : false });
      });
    });
  }

  function showReview({ company, role, fields, optionsByFieldId, message }) {
    return renderFormPanel({
      title: "CareerCraft — review before submitting",
      subtitle: [company, role].filter(Boolean).join(" · ") || "Review your application",
      message,
      fields: fields || [],
      optionsByFieldId,
      primaryLabel: "Submit application",
      primaryAction: "submit",
      allowRemember: true,
    });
  }

  function showNeedsInput({ company, role, message, fields, optionsByFieldId }) {
    return renderFormPanel({
      title: "CareerCraft — needs your input",
      subtitle: [company, role].filter(Boolean).join(" · "),
      message: message || "A few answers need your input before continuing.",
      fields,
      optionsByFieldId,
      primaryLabel: "Continue",
      primaryAction: "continue",
      allowRemember: true,
    });
  }

  function showLoginRequired({ company, role, message }) {
    return renderFormPanel({
      title: "CareerCraft — sign in required",
      subtitle: [company, role].filter(Boolean).join(" · "),
      message: message || "Sign in, then press Continue.",
      fields: [],
      primaryLabel: "Continue",
      primaryAction: "continue",
      allowRemember: false,
    });
  }

  function showCaptcha({ company, role, message }) {
    return renderFormPanel({
      title: "CareerCraft — action needed",
      subtitle: [company, role].filter(Boolean).join(" · "),
      message: message || "Complete the CAPTCHA, then press Continue in the CareerCraft panel",
      fields: [],
      primaryLabel: "Continue",
      primaryAction: "continue",
      allowRemember: false,
    });
  }

  window.CareerCraftPanel = {
    mount,
    hide,
    showStatus,
    showNotice,
    showReview,
    showNeedsInput,
    showLoginRequired,
    showCaptcha,
  };
})();
