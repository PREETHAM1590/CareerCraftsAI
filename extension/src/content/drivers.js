// CareerCraft AI — per-platform application drivers.
// Classic script — exposes window.CareerCraftDrivers. Depends on
// window.CareerCraftDOM and window.CareerCraftPanel (loaded first).
(function () {
  if (window.__ccDriversLoaded) return;
  window.__ccDriversLoaded = true;

  const dom = window.CareerCraftDOM;

  const CAPTCHA_SRC_RE = /recaptcha|hcaptcha|turnstile/i;
  const GENERIC_SUCCESS_RE =
    /application (has been |was )?(successfully )?submitted|thank you for applying|we have received your application|application received/i;
  const SUBMIT_TEXT_RE = /submit( your)? application|send application|submit|apply/i;
  const LINKEDIN_LOGIN_URL_RE = /^\/(login|authwall|checkpoint|uas)(\/|$)/i;
  const LINKEDIN_SUBMIT_RE = /submit application/i;
  const LINKEDIN_NEXT_RE = /^(continue to next step|next|review your application|review)$/i;
  const LINKEDIN_CONFIRM_RE = /your application was sent|application sent|you applied|application submitted/i;
  const NAUKRI_LOGIN_RE = /\/nlogin/i;
  const NAUKRI_SUCCESS_RE = /successfully applied|you have applied|application sent/i;
  // Controls that lead from a job description to its application form.
  const APPLY_LINK_RE = /^(apply|apply now|apply here|apply for this (job|position|role)|apply to this job|apply online|start (your )?application|i'?m interested)$/i;
  // Hosted application forms that company career sites embed in an iframe.
  const ATS_FRAME_RE = /(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|jobvite\.com|icims\.com|bamboohr\.com|recruitee\.com|myworkdayjobs\.com|myworkdaysite\.com|taleo\.net|successfactors\.(com|eu)|darwinbox\.(in|com)|keka\.com)$/i;

  // Returned by a driver that sent the tab to another page: the runner stops
  // quietly and the background re-runs it once that page has loaded.
  const NAVIGATING = "navigating";

  // Workday tenants on a company domain still carry these markers.
  const WORKDAY_MARKERS =
    "[data-automation-id=\"adventureButton\"], [data-automation-id=\"jobPostingHeader\"], [data-automation-id=\"applyFlowPage\"], [data-automation-id=\"bottom-navigation-next-button\"], [data-automation-id=\"pageFooterNextButton\"]";

  // ── Small shared helpers ────────────────────────────────────────────────

  function waitFor(fn, timeoutMs, intervalMs) {
    intervalMs = intervalMs || 300;
    return new Promise((resolve) => {
      const start = Date.now();
      (function tick() {
        let value;
        try {
          value = fn();
        } catch (e) {
          value = null;
        }
        if (value) return resolve(value);
        if (Date.now() - start >= timeoutMs) return resolve(null);
        setTimeout(tick, intervalMs);
      })();
    });
  }

  // Only a challenge the user can see counts. Many application forms embed
  // an invisible reCAPTCHA (size=invisible) that never needs a person.
  function detectCaptchaFrame(root) {
    const frames = Array.from((root || document).querySelectorAll("iframe"));
    return frames.find((f) => {
      const src = f.getAttribute("src") || "";
      if (!CAPTCHA_SRC_RE.test(src) || /size=invisible/i.test(src)) return false;
      const rect = f.getBoundingClientRect();
      return dom.isVisible(f) && rect.width >= 60 && rect.height >= 40;
    });
  }

  // While a visible CAPTCHA is on the page, ask the user to solve it.
  // Returns true when the user cancelled (the caller must stop), false once
  // there is no CAPTCHA left and the flow can continue.
  async function guardCaptcha(ctx, root) {
    for (let round = 0; round < 5 && detectCaptchaFrame(root); round++) {
      const message = "Complete the CAPTCHA, then press Continue in the CareerCraft panel";
      await ctx.api.event("needs_input", { message });
      const res = await ctx.panel.showCaptcha({ company: ctx.task.company, role: ctx.task.role, message });
      if (res.action !== "continue") {
        await ctx.api.event("cancelled", { message: "Cancelled at the CAPTCHA" });
        return true;
      }
      await dom.delay(400, 600);
    }
    return false;
  }

  function isEmpty(value) {
    return value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0);
  }

  // Required fields the plan could not answer and the user left empty.
  function missingRequired(planFields, typed) {
    return planFields.filter((f) => {
      if (!f.required || f.input_type === "file") return false;
      const value = typed && Object.prototype.hasOwnProperty.call(typed, f.field_id) ? typed[f.field_id] : f.value;
      return isEmpty(value);
    });
  }

  function buildOptionsMap(rawFields) {
    const map = {};
    const byName = {};
    for (const f of rawFields) {
      if (f.type === "select") map[f.id] = f.options;
      if ((f.type === "radio" || f.type === "checkbox") && f.name) {
        (byName[f.name] = byName[f.name] || []).push({ type: f.type, label: f.label });
      }
    }
    for (const [name, items] of Object.entries(byName)) {
      // A lone checkbox is a yes/no box, not a one-option group.
      if (items[0].type === "checkbox" && items.length === 1) continue;
      map[name] = items.map((item) => item.label);
    }
    return map;
  }

  function mergeFields(rawFields, plan, opts) {
    opts = opts || {};
    const fields = (plan.fields || []).map((f) => {
      if (f.input_type === "file" && f.value === "__resume__") {
        if (opts.resumeFilename) return { ...f, value: `Resume: ${opts.resumeFilename}` };
        return { ...f, value: null, source: "unresolved", requires_review: true, missing_reason: "The resume could not be attached; attach it yourself in the page." };
      }
      return f;
    });
    return { fields, optionsByFieldId: buildOptionsMap(rawFields) };
  }

  async function applyOne(snap, fieldId, inputType, value) {
    const group = snap.byName.get(fieldId);
    if (group) {
      if (inputType === "radio") return dom.fillRadioGroup(group, value);
      if (group.length === 1 || typeof value === "boolean") return dom.fillSingleCheckbox(group[0].el, value);
      return dom.fillCheckboxGroup(group, value);
    }
    const el = snap.byId.get(fieldId);
    if (!el) return false;
    const widget = snap.widgets && snap.widgets.get(fieldId);
    if (widget === "listbox") return dom.fillListbox(el, value);
    if (widget === "combobox" || widget === "multiselect") return dom.fillCombobox(el, value);
    if (widget === "date") return dom.fillDateGroup(el, value);
    if (inputType === "select") return dom.fillSelect(el, value);
    if (inputType === "checkbox") return dom.fillSingleCheckbox(el, value);
    if (inputType === "radio") {
      if (!el.checked) dom.clickLike(el);
      return true;
    }
    return dom.fillText(el, value);
  }

  // Fills every planned field into the live DOM, pacing each fill. `typed`
  // (field_id -> value) overrides plan values for fields the user edited in
  // the panel. File inputs are skipped here — see attachResumeIfNeeded.
  // Returns the ids of fields whose value could not be put on the page (no
  // matching option, a widget that did not take it) so callers can ask the
  // person instead of assuming they were filled.
  async function applyPlan(ctx, snap, planFields, typed, opts) {
    const failed = [];
    for (const field of planFields) {
      if (field.input_type === "file") continue;
      const hasOverride = typed && Object.prototype.hasOwnProperty.call(typed, field.field_id);
      const value = hasOverride ? typed[field.field_id] : field.value;
      if (value === null || value === undefined || value === "") continue;
      if (!hasOverride && opts && opts.skip && opts.skip(field)) continue;
      await ctx.delay();
      let ok = false;
      try {
        ok = await applyOne(snap, field.field_id, field.input_type, value);
      } catch (e) {
        ok = false;
      }
      if (ok === false) failed.push(field.field_id);
    }
    return failed;
  }

  // Required fields still empty after filling: what the plan could not
  // answer plus required answers the page refused.
  function unresolvedIds(plan, failed) {
    flagFailed(plan.fields, failed);
    const required = new Set(plan.fields.filter((f) => f.required).map((f) => f.field_id));
    return Array.from(new Set([...(plan.unresolved_required || []), ...failed.filter((id) => required.has(id))]));
  }

  // Marks fields the page did not accept so they show up as unresolved.
  function flagFailed(planFields, failed) {
    const ids = new Set(failed);
    for (const f of planFields) {
      if (!ids.has(f.field_id)) continue;
      f.requires_review = true;
      f.source = "unresolved";
      f.missing_reason = `Could not set "${Array.isArray(f.value) ? f.value.join(", ") : f.value}" on the page; choose it yourself.`;
    }
  }

  // ── Auto-apply ──────────────────────────────────────────────────────────
  // With the member's opt-in, a form that needs nothing from them is
  // submitted without the review panel or the popup approval. Anything a
  // person should look at (an empty required answer, an AI draft, a value the
  // page refused) still stops at the review panel.
  // Answers auto-submit may rely on: the member's saved answers ("user"),
  // their profile, and values the site itself already shows ("page").
  const AUTO_SOURCES = new Set(["user", "profile", "page"]);
  const NO_AUTO_HOST_RE = /(^|\.)(linkedin|naukri)\.com\.?$/i;

  function needsAttention(fields, typed) {
    return fields.filter((f) => {
      if (f.readonly) return false;
      const value = typed && Object.prototype.hasOwnProperty.call(typed, f.field_id) ? typed[f.field_id] : f.value;
      if (f.required && isEmpty(value)) return true;
      if (!isEmpty(value) && f.source && !AUTO_SOURCES.has(f.source)) return true;
      return !!f.requires_review && !isEmpty(value);
    });
  }

  async function reviewOrAuto(ctx, opts) {
    // canAuto=false: the submit control was only guessed, so a person confirms.
    if (ctx.autoApply && opts.canAuto !== false && !NO_AUTO_HOST_RE.test(location.hostname)) {
      const blocking = needsAttention(opts.fields || []);
      if (!blocking.length) {
        ctx.panel.showStatus("Auto-apply is on: everything is filled, submitting now…", [ctx.task.company, ctx.task.role].filter(Boolean).join(" · "));
        return { action: "submit", typed: {}, remember: false, auto: true };
      }
      opts = {
        ...opts,
        message: [opts.message, `Auto-apply paused: ${blocking.map((f) => f.label || f.field_id).slice(0, 6).join(", ")} need${blocking.length === 1 ? "s" : ""} your review.`].filter(Boolean).join(" "),
      };
    }
    return ctx.panel.showReview(opts);
  }

  async function attachResumeIfNeeded(ctx, snap, planFields) {
    const fileField = planFields.find((f) => f.input_type === "file" && f.value === "__resume__");
    if (!fileField) return null;
    const el = snap.byId.get(fileField.field_id);
    if (!el) return null;
    if (el.files && el.files.length > 0) return { filename: el.files[0].name };
    const resume = await ctx.api.resume();
    if (!resume) return null;
    await ctx.delay();
    const ok = await dom.attachFile(el, resume.base64, resume.filename, resume.mime);
    return ok ? { filename: resume.filename } : null;
  }

  async function rememberAnswers(ctx, fields, typed) {
    if (!typed) return;
    for (const field of fields) {
      if (!Object.prototype.hasOwnProperty.call(typed, field.field_id)) continue;
      const value = typed[field.field_id];
      if (isEmpty(value)) continue;
      // Consent boxes are ticked per application, never remembered.
      if (field.input_type === "checkbox" && typeof value === "boolean") continue;
      try {
        // No question_key: the server derives the canonical key from the
        // label, which is what makes the answer reusable on the next form.
        await ctx.api.answer(field.label || field.field_id, value);
      } catch (e) {
        /* best-effort — one bad save shouldn't stop the application */
      }
    }
  }

  function snippetAround(text, index, length) {
    const start = Math.max(0, index - 60);
    return text.slice(start, index + length + 60).trim();
  }

  // Polls the visible page text for `regex`; if nothing matches within
  // timeoutMs, falls back to asking the decision engine whether the page
  // confirms submission (a "noul" question over the first ~3000 chars).
  async function waitForConfirmation(ctx, regex, timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const text = dom.visibleText(document, 4000);
      const match = regex.exec(text);
      if (match) return { confirmed: true, text: snippetAround(text, match.index, match[0].length) };
      await dom.delay(450, 550);
    }
    const text = dom.visibleText(document, 3000);
    const decision = await ctx.api.decide(text, {
      confirmed: { type: "noul", instructions: "Does this page confirm the job application was submitted?" },
    });
    const answer = decision && decision.answers && decision.answers.confirmed;
    const noul = answer && typeof answer.noul === "number" ? answer.noul : null;
    if (noul !== null) {
      if (noul >= 0.8) return { confirmed: true, text: text.slice(0, 300) };
      if (noul <= 0.2) return { confirmed: false, text: null };
    }
    return { confirmed: "unknown", text: null };
  }

  async function reportConfirmationResult(ctx, result, notConfirmedMessage) {
    if (result.confirmed === true) {
      await ctx.api.event("submitted", { confirmation_text: result.text || undefined, confirmation_url: location.href });
    } else if (result.confirmed === false) {
      await ctx.api.event("failed", { error: notConfirmedMessage });
    } else {
      await ctx.api.event("failed", {
        error: "Could not confirm whether the application was submitted — please check the site.",
      });
    }
  }

  // Lets the decision engine pick the advancing button when text heuristics
  // find nothing (state = visible button texts; act only if confidence >= .6).
  async function decideAdvanceButton(ctx, buttons) {
    if (!buttons.length) return null;
    const criteria = {};
    const texts = [];
    buttons.forEach((b, i) => {
      const key = "b" + i;
      const text = dom.textOf(b) || b.getAttribute("aria-label") || "button " + i;
      criteria[key] = text;
      texts.push(text);
    });
    let decision;
    try {
      decision = await ctx.api.decide(texts, {
        advance: {
          type: "choice",
          instructions: "Pick the button that advances this job application form to its next step, or submits it.",
          criteria,
        },
      });
    } catch (e) {
      return null;
    }
    const answer = decision && decision.answers && decision.answers.advance;
    if (!answer || typeof answer.confidence !== "number" || answer.confidence < 0.6) return null;
    const idx = Object.keys(criteria).indexOf(answer.choice);
    return idx >= 0 ? buttons[idx] : null;
  }

  function externalApplyError() {
    return "This job applies on the company site — open it from CareerCraft to apply there";
  }

  // ── Generic ATS driver (Greenhouse, Lever, Ashby, Workday, Indeed, …) ──

  function findSubmitControl(form) {
    let el = form.querySelector('button[type="submit"], input[type="submit"]');
    if (el) return el;
    const candidates = Array.from(form.querySelectorAll("button, input[type=button], a[role=button], a.button, a[class*=button]"));
    return candidates.find((c) => dom.isVisible(c) && (SUBMIT_TEXT_RE.test(dom.textOf(c)) || SUBMIT_TEXT_RE.test(c.value || "")));
  }

  // A job page also has search boxes, newsletter sign-ups and cookie forms.
  // Only a form that asks for a resume, an email, or several personal fields
  // is an application; anything else must never reach the review panel.
  function looksLikeApplication(form) {
    const role = (form.getAttribute("role") || "").toLowerCase();
    const action = (form.getAttribute("action") || "").toLowerCase();
    if (role === "search" || /search|newsletter|subscribe|login|signin/.test(action)) return false;
    const inputs = Array.from(form.querySelectorAll("input,select,textarea")).filter(
      (e) => !["hidden", "password", "search", "submit", "button"].includes((e.type || "").toLowerCase()) && dom.isVisible(e)
    );
    if (inputs.some((e) => e.type === "file")) return true;
    const hasEmail = inputs.some((e) => e.type === "email" || /e-?mail/i.test(dom.getLabel(e)));
    return hasEmail && inputs.length >= 3;
  }

  // Single-page ATS front ends (Ashby, some Greenhouse and Lever boards)
  // render the application without a <form> element.
  const FORMLESS_CONTAINERS =
    ".ashby-application-form-container, [class*='application-form' i], [id*='application-form' i], [data-testid*='application' i], [data-ui='application-form'], main, [role='main']";

  function findApplicationForm() {
    const forms = Array.from(document.forms);
    if (!forms.some(looksLikeApplication)) {
      const container = Array.from(document.querySelectorAll(FORMLESS_CONTAINERS)).find((el) => !el.closest("form") && looksLikeApplication(el));
      if (container) {
        const submit = findSubmitControl(container);
        if (submit) return { form: container, submit, inputs: container.querySelectorAll("input,select,textarea").length };
      }
    }
    const scored = forms
      .filter(looksLikeApplication)
      .map((f) => ({ form: f, submit: findSubmitControl(f), inputs: f.querySelectorAll("input,select,textarea").length }));
    if (!scored.length) return null;
    const withSubmit = scored.filter((s) => s.submit);
    const pool = withSubmit.length ? withSubmit : scored;
    pool.sort((a, b) => b.inputs - a.inputs);
    return pool[0];
  }

  function httpsHref(value) {
    try {
      const url = new URL(value, location.href);
      return url.protocol === "https:" ? url.href : null;
    } catch (e) {
      return null;
    }
  }

  function findAtsFrame() {
    return Array.from(document.querySelectorAll("iframe[src]")).find((f) => {
      const href = httpsHref(f.getAttribute("src"));
      return href && ATS_FRAME_RE.test(new URL(href).hostname) && dom.isVisible(f);
    });
  }

  function findApplyControl() {
    const controls = Array.from(document.querySelectorAll("a[href], button, [role=button], input[type=button], input[type=submit]"));
    return controls.find((c) => {
      if (!dom.isVisible(c) || c.disabled) return false;
      // A submit control inside some other form (a search filter's "Apply")
      // is not the way to the application.
      if (c.type === "submit" && c.closest("form")) return false;
      const text = dom.textOf(c) || c.value || c.getAttribute("aria-label") || "";
      return APPLY_LINK_RE.test(text.trim());
    });
  }

  // On a job description page, get to the application form: follow an
  // embedded ATS iframe or an Apply link in this tab, or press an in-page
  // Apply button that reveals the form. Returns the form, NAVIGATING, or null.
  async function reachApplicationForm(ctx) {
    const frame = findAtsFrame();
    if (frame) {
      await ctx.api.navigate(httpsHref(frame.getAttribute("src")));
      return NAVIGATING;
    }
    const control = findApplyControl();
    if (!control) return null;
    // Unknown standalone Apply buttons can submit immediately on a signed-in
    // portal. Only follow an actual link here; a human must operate buttons
    // whose effects cannot be established before a form review exists.
    const href = control.tagName === "A" ? httpsHref(control.getAttribute("href")) : null;
    if (!href || href.split("#")[0] === location.href.split("#")[0]) {
      throw new Error("Open the application form manually, then continue CareerCraft. This Apply control may submit immediately and requires your review.");
    }
    return clickThrough(ctx, control, findApplicationForm);
  }

  // Presses a control that leads towards the form, in this tab. A link goes
  // through the background (target=_blank would open a tab nobody watches);
  // anything else is clicked, and we wait for `ready()` to turn truthy or
  // for the page to start leaving (the background then re-runs the driver
  // there). Anything else, like a popup window or a dead button, ends null.
  async function clickThrough(ctx, control, ready) {
    const href = control.tagName === "A" ? httpsHref(control.getAttribute("href")) : null;
    if (href && href.split("#")[0] !== location.href.split("#")[0]) {
      await ctx.api.navigate(href);
      return NAVIGATING;
    }
    await ctx.api.navigate(null);
    let leaving = false;
    window.addEventListener("beforeunload", () => (leaving = true), { once: true });
    await ctx.delay();
    dom.clickLike(control);
    return waitFor(() => ready() || (leaving ? NAVIGATING : null), 15000);
  }

  const genericDriver = {
    async run(ctx) {
      if (await guardCaptcha(ctx)) return;

      let found = findApplicationForm();
      if (!found) {
        found = await reachApplicationForm(ctx);
        if (found === NAVIGATING) return NAVIGATING;
      }
      if (!found) throw new Error("Could not find an application form on this page");
      const { form, submit } = found;

      await ctx.api.event("filling", { message: "Filling the application" });
      const snap = await dom.loadWidgetOptions(dom.snapshot(form));
      const plan = await ctx.api.plan(location.href, snap.fields);
      if (!plan) throw new Error("Could not reach CareerCraft to plan this form");

      flagFailed(plan.fields, await applyPlan(ctx, snap, plan.fields));
      const resumeInfo = await attachResumeIfNeeded(ctx, snap, plan.fields);

      if (await guardCaptcha(ctx, form)) return;

      const merged = mergeFields(snap.fields, plan, { resumeFilename: resumeInfo && resumeInfo.filename });
      await ctx.api.event("review", { message: "Ready for your review" });
      let decision;
      let message;
      for (;;) {
        const reviewOpts = {
          company: ctx.task.company,
          role: ctx.task.role,
          message,
          fields: merged.fields,
          optionsByFieldId: merged.optionsByFieldId,
          canAuto: !!submit,
        };
        decision = message ? await ctx.panel.showReview(reviewOpts) : await reviewOrAuto(ctx, reviewOpts);
        if (decision.action !== "submit") {
          await ctx.api.event("cancelled", { message: "Cancelled before submitting" });
          return;
        }
        const missing = missingRequired(plan.fields, decision.typed);
        if (!missing.length) break;
        message = "Answer the required questions first: " + missing.map((f) => f.label || f.field_id).join(", ");
      }
      await applyPlan(ctx, snap, plan.fields, decision.typed);
      if (decision.remember) await rememberAnswers(ctx, merged.fields, decision.typed);

      let submitEl = submit;
      if (!submitEl) {
        const buttons = Array.from(document.querySelectorAll("button, input[type=submit], input[type=button]")).filter((b) =>
          dom.isVisible(b)
        );
        submitEl = await decideAdvanceButton(ctx, buttons);
      }
      if (!submitEl) throw new Error("Could not find the submit control for this application");

      await ctx.delay();
      await ctx.api.markSubmitting(form, submitEl, { auto: !!decision.auto });
      dom.clickLike(submitEl);

      const result = await waitForConfirmation(ctx, GENERIC_SUCCESS_RE, 15000);
      await reportConfirmationResult(ctx, result, "The page does not show a submission confirmation.");
    },
  };

  // ── LinkedIn Easy Apply driver ──────────────────────────────────────────

  function linkedinLooksSignedOut() {
    if (LINKEDIN_LOGIN_URL_RE.test(location.pathname)) return true;
    const hasNav = !!document.querySelector(".global-nav, #global-nav");
    const hasLoginForm = !!document.querySelector('form[action*="login"], #login-form, input[name="session_password"], input[name="session_key"]');
    return hasLoginForm && !hasNav;
  }

  function findEasyApplyButton() {
    let btn = document.querySelector("button.jobs-apply-button");
    if (btn && dom.isVisible(btn)) return btn;
    const all = Array.from(document.querySelectorAll("button"));
    return all.find((b) => dom.isVisible(b) && (/easy apply/i.test(dom.textOf(b)) || /easy apply/i.test(b.getAttribute("aria-label") || "")));
  }

  function findExternalApplyButton() {
    const all = Array.from(document.querySelectorAll("button, a"));
    return all.find((b) => dom.isVisible(b) && /^apply$/i.test(dom.textOf(b)) && !/easy apply/i.test(dom.textOf(b)));
  }

  function findModal() {
    return document.querySelector("[role='dialog'], .jobs-easy-apply-modal");
  }

  function findModalPrimaryButton(modal) {
    const buttons = Array.from(modal.querySelectorAll("button")).filter((b) => dom.isVisible(b) && !b.disabled);
    const submitBtn = buttons.find((b) => LINKEDIN_SUBMIT_RE.test(dom.textOf(b)));
    if (submitBtn) return { el: submitBtn, kind: "submit" };
    const nextBtn = buttons.find((b) => LINKEDIN_NEXT_RE.test(dom.textOf(b)));
    if (nextBtn) return { el: nextBtn, kind: "next" };
    return { el: null, kind: null, buttons };
  }

  function findInlineErrors(modal) {
    return Array.from(modal.querySelectorAll(".artdeco-inline-feedback--error, [aria-invalid='true']"));
  }

  const linkedinDriver = {
    async run(ctx) {
      for (let attempt = 0; attempt < 5 && linkedinLooksSignedOut(); attempt++) {
        await ctx.api.event("login_required", { message: "Sign in to LinkedIn in this tab, then press Continue." });
        const res = await ctx.panel.showLoginRequired({
          company: ctx.task.company,
          role: ctx.task.role,
          message: attempt === 0 ? "Sign in to LinkedIn in this tab, then press Continue." : "Still signed out — finish signing in, then press Continue.",
        });
        if (res.action !== "continue") {
          await ctx.api.event("cancelled", { message: "Cancelled while signing in" });
          return;
        }
      }
      if (linkedinLooksSignedOut()) {
        throw new Error("Could not detect a signed-in LinkedIn session");
      }

      if (await guardCaptcha(ctx)) return;

      const easyApply = findEasyApplyButton();
      if (!easyApply) {
        if (findExternalApplyButton()) {
          await ctx.api.event("failed", { error: externalApplyError() });
          return;
        }
        throw new Error("Could not find the Easy Apply button on this job page");
      }

      await ctx.api.event("filling", { message: "Opening Easy Apply" });
      await ctx.delay();
      dom.clickLike(easyApply);
      const modal = await waitFor(findModal, 8000);
      if (!modal) throw new Error("The Easy Apply modal did not open");

      for (let step = 0; step < 15; step++) {
        const m = findModal();
        if (!m) throw new Error("The Easy Apply modal closed unexpectedly");

        if (await guardCaptcha(ctx, m)) return;

        const snap = await dom.loadWidgetOptions(dom.snapshot(m));
        const plan = await ctx.api.plan(location.href, snap.fields);
        if (!plan) throw new Error("Could not reach CareerCraft to plan this step");
        const unresolved = unresolvedIds(plan, await applyPlan(ctx, snap, plan.fields));
        const resumeInfo = await attachResumeIfNeeded(ctx, snap, plan.fields);
        const merged = mergeFields(snap.fields, plan, { resumeFilename: resumeInfo && resumeInfo.filename });

        if (unresolved.length) {
          await ctx.api.event("needs_input", { message: "A few required questions need your answer." });
          const res = await ctx.panel.showNeedsInput({
            company: ctx.task.company,
            role: ctx.task.role,
            fields: merged.fields.filter((f) => unresolved.includes(f.field_id) || f.requires_review),
            optionsByFieldId: merged.optionsByFieldId,
          });
          if (res.action !== "continue") {
            await ctx.api.event("cancelled", { message: "Cancelled by user" });
            return;
          }
          await applyPlan(ctx, snap, plan.fields, res.typed);
          if (res.remember) await rememberAnswers(ctx, merged.fields, res.typed);
        }

        let primary = findModalPrimaryButton(m);
        if (!primary.el) {
          const chosen = await decideAdvanceButton(ctx, primary.buttons || []);
          if (chosen && LINKEDIN_SUBMIT_RE.test(dom.textOf(chosen))) primary = { el: chosen, kind: "submit" };
        }
        if (!primary.el) throw new Error("Could not find a button to advance the Easy Apply modal");

        if (primary.kind === "submit") {
          await ctx.api.event("review", { message: "Ready for your review" });
          const decision = await reviewOrAuto(ctx, {
            company: ctx.task.company,
            role: ctx.task.role,
            fields: merged.fields,
            optionsByFieldId: merged.optionsByFieldId,
            canAuto: LINKEDIN_SUBMIT_RE.test(dom.textOf(primary.el)),
          });
          if (decision.action !== "submit") {
            await ctx.api.event("cancelled", { message: "Cancelled before submitting" });
            return;
          }
          await applyPlan(ctx, snap, plan.fields, decision.typed);
          if (decision.remember) await rememberAnswers(ctx, merged.fields, decision.typed);

          await ctx.delay();
          await ctx.api.markSubmitting(m, primary.el, { auto: !!decision.auto });
          dom.clickLike(primary.el);
          const result = await waitForConfirmation(ctx, LINKEDIN_CONFIRM_RE, 20000);
          await reportConfirmationResult(ctx, result, "LinkedIn did not show a submission confirmation.");
          return;
        }

        await ctx.delay();
        dom.clickLike(primary.el);
        await dom.delay(700, 900);

        const afterModal = findModal();
        if (afterModal && findInlineErrors(afterModal).length) {
          await ctx.api.event("needs_input", { message: "LinkedIn flagged some answers — fix them, then press Continue." });
          const res = await ctx.panel.showNeedsInput({
            company: ctx.task.company,
            role: ctx.task.role,
            message: "LinkedIn flagged some answers — fix them in the form, then press Continue.",
            fields: [],
            optionsByFieldId: {},
          });
          if (res.action !== "continue") {
            await ctx.api.event("cancelled", { message: "Cancelled by user" });
            return;
          }
        }
      }
      throw new Error("Easy Apply had too many steps");
    },
  };

  // ── Naukri driver ───────────────────────────────────────────────────────

  function naukriLooksSignedOut() {
    if (NAUKRI_LOGIN_RE.test(location.href)) return true;
    return !!document.querySelector("#login_Layer, .login-modal, [class*='loginModal' i]");
  }

  function findNaukriApplyButton() {
    let btn = document.getElementById("apply-button");
    if (btn && dom.isVisible(btn)) return btn;
    const all = Array.from(document.querySelectorAll("button, a"));
    return all.find((b) => dom.isVisible(b) && /^apply$/i.test(dom.textOf(b)));
  }

  function findNaukriExternalApply() {
    const all = Array.from(document.querySelectorAll("button, a"));
    return all.find((b) => dom.isVisible(b) && /apply on company site/i.test(dom.textOf(b)));
  }

  function findChatbotDrawer() {
    const all = Array.from(document.querySelectorAll("div, section, aside"));
    return all.find((el) => /chatbot|drawer/i.test(el.className || "") && dom.isVisible(el));
  }

  const naukriDriver = {
    async run(ctx) {
      for (let attempt = 0; attempt < 5 && naukriLooksSignedOut(); attempt++) {
        await ctx.api.event("login_required", { message: "Sign in to Naukri in this tab, then press Continue." });
        const res = await ctx.panel.showLoginRequired({
          company: ctx.task.company,
          role: ctx.task.role,
          message: attempt === 0 ? "Sign in to Naukri in this tab, then press Continue." : "Still signed out — finish signing in, then press Continue.",
        });
        if (res.action !== "continue") {
          await ctx.api.event("cancelled", { message: "Cancelled while signing in" });
          return;
        }
      }
      if (naukriLooksSignedOut()) throw new Error("Could not detect a signed-in Naukri session");

      if (await guardCaptcha(ctx)) return;

      if (findNaukriExternalApply() && !findNaukriApplyButton()) {
        await ctx.api.event("failed", { error: externalApplyError() });
        return;
      }
      const applyBtn = findNaukriApplyButton();
      if (!applyBtn) throw new Error("Could not find the Apply button on this Naukri page");
      if (/company site/i.test(dom.textOf(applyBtn))) {
        await ctx.api.event("failed", { error: externalApplyError() });
        return;
      }

      // Naukri's own Apply button submits immediately — the review panel
      // must appear *before* we click it, never after.
      await ctx.api.event("review", { message: "Ready to apply on Naukri" });
      const decision = await reviewOrAuto(ctx, {
        company: ctx.task.company,
        role: ctx.task.role,
        message: "Naukri applies with your Naukri profile — submit?",
        fields: [],
        optionsByFieldId: {},
      });
      if (decision.action !== "submit") {
        await ctx.api.event("cancelled", { message: "Cancelled before applying" });
        return;
      }

      await ctx.delay();
      await ctx.api.markSubmitting(document, applyBtn, { auto: !!decision.auto });
      dom.clickLike(applyBtn);

      await dom.delay(900, 1200);
      if (await guardCaptcha(ctx)) return;
      if (findChatbotDrawer()) {
        throw new Error("Naukri opened additional questions after Apply. Complete them manually and verify the outcome; CareerCraft will not click a second submit.");
      }

      const result = await waitForConfirmation(ctx, NAUKRI_SUCCESS_RE, 15000);
      await reportConfirmationResult(ctx, result, "Naukri did not show a submission confirmation.");
    },
  };

  // ── Multi-step portals: Workday, SmartRecruiters, Workable ─────────────
  // One wizard loop, three small configurations. Each pass reads the current
  // step from the page (so a re-injection after any navigation resumes
  // correctly), fills it from the plan, asks the user about what CareerCraft
  // never guesses, then presses Next. Only the final Submit goes through the
  // review panel and the popup approval; accounts and passwords are never
  // touched (password inputs are not snapshotted).

  const SUBMIT_BTN_RE = /^(submit( (your )?application)?|send application)$/i;
  const NEXT_BTN_RE = /^(next|continue|save and continue|save & continue|continue to next step|review)$/i;
  const ATS_SUCCESS_RE =
    /application (has been |was )?(successfully )?(submitted|sent|received)|thank(s| you) for applying|we.ve received your application|we have received your application|you have successfully applied|successfully applied for/i;

  function visibleOne(selector, root) {
    return dom.deepQueryAll(root || document, selector).find((e) => dom.isVisible(e)) || null;
  }

  function buttonLabel(b) {
    return (dom.textOf(b) || b.value || b.getAttribute("aria-label") || "").trim();
  }

  function primaryFrom(buttons) {
    const enabled = buttons.filter((b) => dom.isVisible(b) && !b.disabled);
    const submit = enabled.find((b) => SUBMIT_BTN_RE.test(buttonLabel(b)));
    if (submit) return { el: submit, kind: "submit" };
    const next = enabled.find((b) => NEXT_BTN_RE.test(buttonLabel(b)));
    if (next) return { el: next, kind: "next" };
    return { el: null, kind: null, buttons: enabled };
  }

  function stepErrors(root) {
    return dom
      .deepQueryAll(root, "[aria-invalid='true'], .artdeco-inline-feedback--error, [data-automation-id='errorBanner'], [data-automation-id='errorMessage']")
      .filter((e) => dom.isVisible(e));
  }

  const ANY_BUTTON = "button, input[type=submit], input[type=button], [role=button]";

  const smartRecruitersCfg = {
    label: "SmartRecruiters",
    success: ATS_SUCCESS_RE,
    stepDelay: [900, 1300],
    signedOut: () => false,
    findRoot() {
      if (!/\/oneclick-ui\//i.test(location.pathname)) return null;
      const root = document.querySelector("form") || document.body;
      return dom.deepQueryAll(root, "input,select,textarea").some((e) => e.type !== "hidden" && dom.isVisible(e)) ? root : null;
    },
    findPrimary: (root) => primaryFrom(dom.deepQueryAll(root, ANY_BUTTON)),
    async enter(ctx) {
      const apply = visibleOne('a[href*="/oneclick-ui/"], [data-test="apply-button"]') || findApplyControl();
      return apply ? clickThrough(ctx, apply, smartRecruitersCfg.findRoot) : null;
    },
  };

  const workableCfg = {
    label: "Workable",
    success: ATS_SUCCESS_RE,
    stepDelay: [900, 1300],
    signedOut: () => false,
    findRoot: () => visibleOne('form[data-ui="application-form"]') || Array.from(document.forms).find(looksLikeApplication) || null,
    findPrimary(root) {
      const found = primaryFrom(dom.deepQueryAll(root, ANY_BUTTON));
      return found.el ? found : { ...found, el: visibleOne('button[data-ui="apply-button"], button[type="submit"]', root), kind: "submit" };
    },
    async enter(ctx) {
      const apply = visibleOne('[data-ui="overview-apply"], a[href$="/apply/"], a[href$="/apply"]') || findApplyControl();
      return apply ? clickThrough(ctx, apply, workableCfg.findRoot) : null;
    },
  };


  // ── Wizard portals without a Workday-style marker set ───────────────────
  // iCIMS, Taleo, SuccessFactors, Darwinbox and Keka share one shape: a job
  // page with an Apply control, then a form (one page or several steps) that
  // the wizard loop fills. Hosted-tenant selectors only differ in how the
  // Apply control is found, so they share a factory. Sign-in is detected by
  // a visible password field and handed to the member, never filled.
  function formPortalCfg(label, applySelector) {
    const cfg = {
      label,
      success: ATS_SUCCESS_RE,
      stepDelay: [1200, 1700],
      loginMessage: `Sign in or create your ${label} account in this tab, then press Continue. CareerCraft never sees your password.`,
      signedOut: () => !!visibleOne("input[type=password]") && !cfg.findRoot(),
      findRoot() {
        const forms = Array.from(document.forms).filter((f) => dom.isVisible(f) || f.querySelector("input,select,textarea"));
        return forms.find(looksLikeApplication) || null;
      },
      findPrimary: (root) => primaryFrom(dom.deepQueryAll(root, ANY_BUTTON)),
      async enter(ctx) {
        const apply = visibleOne(applySelector) || findApplyControl();
        return apply ? clickThrough(ctx, apply, () => cfg.findRoot() || cfg.signedOut()) : null;
      },
    };
    return cfg;
  }

  const icimsCfg = formPortalCfg("iCIMS", 'a[href*="mode=apply"], a.iCIMS_Anchor[href*="apply"], a[href*="/apply"]');
  const taleoCfg = formPortalCfg("Taleo", 'a[href*="apply"], [id*="applyButton"], input[value*="Apply"]');
  const successFactorsCfg = formPortalCfg("SuccessFactors", '[id*="applyButton"], a[href*="/apply"], button[data-testid*="apply"]');
  const darwinboxCfg = formPortalCfg("Darwinbox", 'a[href*="apply"], button[class*="apply"]');
  const kekaCfg = formPortalCfg("Keka", 'a[href*="apply"], button[class*="apply"]');

  async function ensureSignedIn(ctx, cfg) {
    for (let attempt = 0; attempt < 5 && cfg.signedOut(); attempt++) {
      await ctx.api.event("login_required", { message: cfg.loginMessage });
      const res = await ctx.panel.showLoginRequired({
        company: ctx.task.company,
        role: ctx.task.role,
        message: attempt === 0 ? cfg.loginMessage : "Still signed out — finish signing in, then press Continue.",
      });
      if (res.action !== "continue") {
        await ctx.api.event("cancelled", { message: "Cancelled while signing in" });
        return false;
      }
      await dom.delay(600, 900);
    }
    if (cfg.signedOut()) throw new Error(`Could not detect a signed-in ${cfg.label} session`);
    return true;
  }

  // Optional cfg hooks (Workday uses them all):
  //   snapshot(root)            async snapshot of the current step
  //   stepName()                the step's title, for messages
  //   restrict(plan, stepName)  drops answers a step must not get automatically
  //   keepFilled                never overwrite a value the page already shows
  //   prepare(ctx, root)        runs before planning (e.g. resume upload first)
  //   expand(ctx, root, plan)   adds blocks the plan asks for; true = re-plan
  //   blocked()                 a reason this application cannot go on
  //   stepErrors(root)          visible validation messages
  //   signature()               changes whenever the step changes
  function makeAtsDriver(cfg) {
    const errorsOf = cfg.stepErrors || ((root) => stepErrors(root).map((e) => dom.textOf(e) || "A field is marked invalid"));
    const signature =
      cfg.signature ||
      (() => {
        const root = cfg.findRoot();
        return root ? dom.textOf(root).slice(0, 600) : "";
      });
    const subtitle = (ctx) => [ctx.task.company, ctx.task.role].filter(Boolean).join(" · ");

    async function handBack(ctx, message) {
      await ctx.api.event("needs_input", { message: message.slice(0, 480) });
      const res = await ctx.panel.showNeedsInput({ company: ctx.task.company, role: ctx.task.role, message, fields: [], optionsByFieldId: {} });
      if (res.action !== "continue") {
        await ctx.api.event("cancelled", { message: "Cancelled by user" });
        return false;
      }
      return true;
    }

    return {
      cfg,
      async run(ctx) {
        if (!(await ensureSignedIn(ctx, cfg))) return;
        if (await guardCaptcha(ctx)) return;

        if (!cfg.findRoot()) {
          if ((await cfg.enter(ctx)) === NAVIGATING) return NAVIGATING;
          await waitFor(() => cfg.signedOut() || cfg.findRoot(), 15000);
          if (!(await ensureSignedIn(ctx, cfg))) return;
          await waitFor(cfg.findRoot, 15000);
        }
        if (!cfg.findRoot()) throw new Error(`Could not find the ${cfg.label} application form on this page`);

        await ctx.api.event("filling", { message: `Filling the ${cfg.label} application` });
        const answered = new Map(); // what earlier steps filled, for the final review
        let stuck = 0;
        for (let step = 0; step < 25; step++) {
          // Workday can ask for an account (or a verification code) mid-flow.
          if (cfg.signedOut() && !(await ensureSignedIn(ctx, cfg))) return;
          const blocked = cfg.blocked && cfg.blocked();
          if (blocked) {
            await ctx.api.event("failed", { error: blocked });
            return;
          }
          const m = await waitFor(cfg.findRoot, 10000);
          if (!m) throw new Error(`The ${cfg.label} form closed unexpectedly`);
          if (await guardCaptcha(ctx, m)) return;
          const stepName = cfg.stepName ? cfg.stepName() : "";
          const where = stepName ? ` (${stepName})` : "";
          ctx.panel.showStatus(`Filling ${cfg.label}${where}…`, subtitle(ctx));

          if (cfg.prepare) await cfg.prepare(ctx, m);
          let snap;
          let plan;
          for (let pass = 0; pass < 2; pass++) {
            snap = cfg.snapshot ? await cfg.snapshot(m) : await dom.loadWidgetOptions(dom.snapshot(m));
            plan = snap.fields.length ? await ctx.api.plan(location.href, snap.fields) : { fields: [], unresolved_required: [] };
            if (!plan) throw new Error("Could not reach CareerCraft to plan this step");
            // The page may need more repeated blocks (work history, education)
            // than it shows: add them once, then plan the step again.
            if (pass > 0 || !cfg.expand || !(await cfg.expand(ctx, m, plan))) break;
          }
          plan.unresolved_required = plan.unresolved_required || [];
          if (cfg.restrict) cfg.restrict(plan, stepName);

          let skip = null;
          if (cfg.keepFilled) {
            const onPage = new Map(snap.fields.filter((f) => !isEmpty(f.value) && f.type !== "radio" && f.type !== "checkbox" && f.type !== "file").map((f) => [f.id, f.value]));
            skip = (f) => onPage.has(f.field_id);
            for (const f of plan.fields) {
              if (!onPage.has(f.field_id)) continue;
              Object.assign(f, { value: onPage.get(f.field_id), source: "page", requires_review: false, missing_reason: null });
              plan.unresolved_required = plan.unresolved_required.filter((id) => id !== f.field_id);
            }
          }
          const unresolved = unresolvedIds(plan, await applyPlan(ctx, snap, plan.fields, null, { skip }));
          const resumeInfo = await attachResumeIfNeeded(ctx, snap, plan.fields);
          const merged = mergeFields(snap.fields, plan, { resumeFilename: resumeInfo && resumeInfo.filename });

          if (unresolved.length) {
            await ctx.api.event("needs_input", { message: `A few required questions need your answer${where}.` });
            const res = await ctx.panel.showNeedsInput({
              company: ctx.task.company,
              role: ctx.task.role,
              message: ctx.autoApply ? `Auto-apply paused${where}: these need your answer.` : undefined,
              fields: merged.fields.filter((f) => unresolved.includes(f.field_id) || f.requires_review),
              optionsByFieldId: merged.optionsByFieldId,
            });
            if (res.action !== "continue") {
              await ctx.api.event("cancelled", { message: "Cancelled by user" });
              return;
            }
            const failed = await applyPlan(ctx, snap, plan.fields, res.typed);
            if (res.remember) await rememberAnswers(ctx, merged.fields, res.typed);
            for (const f of merged.fields) {
              if (res.typed && Object.prototype.hasOwnProperty.call(res.typed, f.field_id)) f.value = res.typed[f.field_id];
            }
            if (failed.length) {
              const labels = merged.fields.filter((f) => failed.includes(f.field_id)).map((f) => f.label || f.field_id);
              if (!(await handBack(ctx, `Set these yourself in the page, then press Continue: ${labels.join(", ")}`))) return;
            }
          }

          let primary = cfg.findPrimary(m);
          if (!primary.el) {
            const chosen = await decideAdvanceButton(ctx, primary.buttons || []);
            // An unrecognised button is treated as the final step unless it is
            // plainly "Next": a "Finish" or "Apply" click must go through review.
            if (chosen) primary = { el: chosen, kind: NEXT_BTN_RE.test(buttonLabel(chosen)) ? "next" : "submit", guessed: true };
          }
          if (!primary.el) throw new Error(`Could not find the Next or Submit button in ${cfg.label}`);

          if (primary.kind === "submit") {
            // Earlier steps are already saved by the site: show them, but only
            // this step's answers can still be edited here.
            const current = new Set(merged.fields.map((f) => f.field_id));
            const earlier = Array.from(answered.values()).filter((f) => !current.has(f.field_id)).map((f) => ({ ...f, readonly: true, requires_review: false }));
            await ctx.api.event("review", { message: "Ready for your review" });
            const decision = await reviewOrAuto(ctx, {
              company: ctx.task.company,
              role: ctx.task.role,
              fields: [...earlier, ...merged.fields],
              optionsByFieldId: merged.optionsByFieldId,
              canAuto: !primary.guessed && SUBMIT_BTN_RE.test(buttonLabel(primary.el)),
            });
            if (decision.action !== "submit") {
              await ctx.api.event("cancelled", { message: "Cancelled before submitting" });
              return;
            }
            await applyPlan(ctx, snap, plan.fields, decision.typed);
            if (decision.remember) await rememberAnswers(ctx, merged.fields, decision.typed);

            await ctx.delay();
            await ctx.api.markSubmitting(m, primary.el, { auto: !!decision.auto });
            dom.clickLike(primary.el);
            const result = await waitForConfirmation(ctx, cfg.success, 20000);
            await reportConfirmationResult(ctx, result, `${cfg.label} did not show a submission confirmation.`);
            return;
          }
          for (const f of merged.fields) if (!isEmpty(f.value)) answered.set(f.field_id, f);

          const before = signature();
          await ctx.delay();
          dom.clickLike(primary.el);
          // Wait for the site to save the step: a new step, validation errors
          // or a sign-in prompt. A fixed sleep re-filled slow steps and could
          // press Next twice.
          const moved = await waitFor(() => {
            if (cfg.signedOut()) return "signin";
            const root = cfg.findRoot();
            if (root && errorsOf(root).length) return "errors";
            return signature() !== before ? "moved" : null;
          }, cfg.stepTimeout || 12000, 300);
          await dom.delay(...cfg.stepDelay);

          const after = cfg.findRoot();
          const errors = after ? errorsOf(after) : [];
          if (errors.length) {
            stuck = 0;
            const list = Array.from(new Set(errors)).slice(0, 5).join("; ");
            const ok = await handBack(ctx, `${cfg.label} flagged some answers${where}: ${list}. Fix them in the form (dropdowns and date pickers included), then press Continue.`);
            if (!ok) return;
            continue;
          }
          if (!moved && ++stuck >= 2) {
            stuck = 0;
            if (!(await handBack(ctx, `${cfg.label} did not move past this step${where}. Finish it in the page, then press Continue.`))) return;
          } else if (moved) {
            stuck = 0;
          }
        }
        throw new Error(`${cfg.label} had too many steps`);
      },
    };
  }

  const smartRecruitersDriver = makeAtsDriver(smartRecruitersCfg);
  const workableDriver = makeAtsDriver(workableCfg);
  const icimsDriver = makeAtsDriver(icimsCfg);
  const taleoDriver = makeAtsDriver(taleoCfg);
  const successFactorsDriver = makeAtsDriver(successFactorsCfg);
  const darwinboxDriver = makeAtsDriver(darwinboxCfg);
  const kekaDriver = makeAtsDriver(kekaCfg);

  // ── Selection ────────────────────────────────────────────────────────────

  // Hostname first (also covers an ATS opened from a company page), then the
  // platform the server detected, then page markers for Workday tenants on a
  // company's own domain.
  function atsKey(task) {
    const host = location.hostname;
    const platform = (task && task.platform) || "";
    if (/(^|\.)myworkdayjobs\.com$/.test(host) || /(^|\.)myworkday(site)?\.com$/.test(host) || platform === "workday" || document.querySelector(WORKDAY_MARKERS)) return "workday";
    if (/(^|\.)smartrecruiters\.com$/.test(host) || platform === "smartrecruiters") return "smartrecruiters";
    if (/(^|\.)workable\.com$/.test(host) || platform === "workable") return "workable";
    if (/(^|\.)icims\.com$/.test(host) || platform === "icims") return "icims";
    if (/(^|\.)taleo\.net$/.test(host) || platform === "taleo") return "taleo";
    if (/(^|\.)successfactors\.(com|eu)$/.test(host) || platform === "successfactors") return "successfactors";
    if (/(^|\.)darwinbox\.(in|com)$/.test(host) || platform === "darwinbox") return "darwinbox";
    if (/(^|\.)keka\.com$/.test(host) || platform === "keka") return "keka";
    return null;
  }

  const ATS_DRIVERS = {
    smartrecruiters: smartRecruitersDriver,
    workable: workableDriver,
    icims: icimsDriver,
    taleo: taleoDriver,
    successfactors: successFactorsDriver,
    darwinbox: darwinboxDriver,
    keka: kekaDriver,
  };

  function select(task) {
    const platform = (task && task.platform) || "";
    if (platform === "linkedin" || /(^|\.)linkedin\.com$/.test(location.hostname)) return linkedinDriver;
    if (platform === "naukri" || /naukri\.com$/.test(location.hostname)) return naukriDriver;
    const key = atsKey(task);
    // workday.js loads after this file and registers its driver.
    if (key === "workday" && window.CareerCraftDrivers.workdayDriver) return window.CareerCraftDrivers.workdayDriver;
    return ATS_DRIVERS[key] || genericDriver;
  }

  // The submit click navigated to a new page (common on ATS forms): the
  // re-injected runner only has to read the outcome, never fill again.
  async function confirmAfterNavigation(ctx) {
    const platform = (ctx.task && ctx.task.platform) || "";
    const regex =
      platform === "linkedin" ? LINKEDIN_CONFIRM_RE : platform === "naukri" ? NAUKRI_SUCCESS_RE : atsKey(ctx.task) ? ATS_SUCCESS_RE : GENERIC_SUCCESS_RE;
    const result = await waitForConfirmation(ctx, regex, 15000);
    await reportConfirmationResult(ctx, result, "The page after submitting does not show a confirmation.");
  }

  window.CareerCraftDrivers = {
    select,
    confirmAfterNavigation,
    atsKey,
    genericDriver,
    linkedinDriver,
    naukriDriver,
    smartRecruitersDriver,
    workableDriver,
    icimsDriver,
    taleoDriver,
    successFactorsDriver,
    darwinboxDriver,
    kekaDriver,
    NAVIGATING,
    // For workday.js and autofill.js (loaded after this file).
    _shared: {
      makeAtsDriver,
      ensureSignedIn,
      visibleOne,
      primaryFrom,
      buttonLabel,
      clickThrough,
      findApplyControl,
      findApplicationForm,
      waitFor,
      applyPlan,
      unresolvedIds,
      isEmpty,
      ATS_SUCCESS_RE,
      ANY_BUTTON,
    },
  };
})();
