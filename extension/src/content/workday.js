// CareerCraft AI — Workday driver.
// Classic script, injected after drivers.js; registers
// window.CareerCraftDrivers.workdayDriver.
//
// Workday (*.myworkdayjobs.com, *.myworkdaysite.com and company-hosted
// tenants) is a multi-step wizard built on data-automation-id attributes:
// job page → Apply → Apply Manually → sign in / create account → My
// Information → My Experience → Application Questions → Voluntary
// Disclosures → Self Identify → Review → Submit. Dropdowns are listbox
// buttons, "How did you hear about us" and skills are search prompts, dates
// are month/day/year spin inputs — dom.js fills those widgets; this file
// knows the steps.
(function () {
  if (window.__ccWorkdayLoaded) return;
  window.__ccWorkdayLoaded = true;

  const dom = window.CareerCraftDOM;
  const drivers = window.CareerCraftDrivers;
  const { makeAtsDriver, visibleOne, primaryFrom, clickThrough, findApplyControl, waitFor, ATS_SUCCESS_RE, ANY_BUTTON } = drivers._shared;
  const NAVIGATING = drivers.NAVIGATING;

  const aid = (...ids) => ids.map((v) => `[data-automation-id="${v}"]`).join(", ");
  const NEXT = aid("bottom-navigation-next-button", "pageFooterNextButton");
  const FLOW = aid("applyFlowPage");
  const ACCOUNT = aid(
    "signInContent",
    "signInSubmitButton",
    "createAccountSubmitButton",
    "createAccountCheckbox",
    "verifyPassword",
    "signInFormo"
  );
  const LEGAL_NOTICE = aid("legalNoticeAcceptButton");
  const UPLOADED = aid("file-upload-successful", "delete-file", "file-upload-item");
  const REPEATING = "[data-automation-id^='workExperience-'], [data-automation-id^='education-'], [data-automation-id^='language-'], [data-automation-id^='certification-'], [data-automation-id^='websites-']";
  const VERIFY_RE = /verify your (email|account)|verification code|one[- ]time (pass)?code|two[- ]factor|enter the code/i;
  const SENSITIVE_STEP_RE = /voluntary|self[- ]?identif|disclosure|diversity|equal (employment )?opportunit|\beeo\b/i;
  const SENSITIVE_LABEL_RE = /gender|\bsex\b|\brace\b|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation|lgbt|transgender|pronoun/i;
  const SUCCESS_RE = new RegExp(`${ATS_SUCCESS_RE.source}|application submitted|you('ve| have) applied`, "i");

  function verificationPrompt() {
    const dialog = visibleOne("[role='dialog'], [data-automation-id='verifyEmail'], " + FLOW);
    return !!dialog && VERIFY_RE.test(dom.textOf(dialog).slice(0, 2000)) && !!visibleOne("input[type=text], input[type=number], input:not([type])", dialog);
  }

  function signedOut() {
    return !!visibleOne(ACCOUNT) || !!visibleOne("input[type=password]") || !!visibleOne(LEGAL_NOTICE) || verificationPrompt();
  }

  function findRoot() {
    const flow = visibleOne(FLOW);
    if (flow && visibleOne(NEXT)) return flow;
    const next = visibleOne(NEXT);
    return next ? next.closest(`${FLOW}, main, [role="main"]`) || document.body : null;
  }

  function stepName() {
    const active = visibleOne(aid("progressBarActiveStep"));
    if (active) {
      const text = dom.textOf(active).replace(/^current step \d+ of \d+\s*/i, "").trim();
      if (text) return text;
    }
    const root = findRoot();
    const heading = root && Array.from(root.querySelectorAll("h1, h2, h3")).find((h) => dom.isVisible(h));
    return heading ? dom.textOf(heading).slice(0, 80) : "";
  }

  function signature() {
    const root = findRoot();
    const fields = root ? root.querySelectorAll("input, textarea, button[aria-haspopup='listbox']").length : 0;
    return `${stepName()}|${location.pathname}|${fields}`;
  }

  function stepErrors(root) {
    const messages = dom
      .deepQueryAll(root, aid("errorMessage", "inputAlert") + ", [role='alert']")
      .filter((e) => dom.isVisible(e))
      .map((e) => dom.textOf(e))
      .filter(Boolean);
    for (const el of dom.deepQueryAll(root, "[aria-invalid='true']")) {
      if (dom.isVisible(el)) messages.push(`${dom.getLabel(el) || "A field"} needs an answer`);
    }
    const banner = visibleOne(aid("errorBanner"), root);
    if (banner && !messages.length) messages.push(dom.textOf(banner) || "Errors found");
    return Array.from(new Set(messages));
  }

  function blocked() {
    if (visibleOne(aid("alreadyApplied"))) return "Workday says you have already applied to this job.";
    return null;
  }

  function resumeUploaded(root) {
    return !!visibleOne(UPLOADED, root);
  }

  function sectionName(section) {
    const id = section.getAttribute("data-automation-id") || "";
    const m = /^([a-zA-Z]+)-(\d+)$/.exec(id);
    if (!m) return id;
    const words = m[1].replace(/([a-z])([A-Z])/g, "$1 $2");
    return `${words[0].toUpperCase()}${words.slice(1)} ${m[2]}`;
  }

  // Labels inside repeated Work Experience / Education blocks are identical
  // ("Job Title"); prefix them so the plan and the person can tell them apart.
  async function snapshot(root) {
    const snap = dom.snapshot(root);
    const uploaded = resumeUploaded(root);
    snap.fields = snap.fields.filter((f) => !(uploaded && f.type === "file"));
    for (const f of snap.fields) {
      const el = snap.byId.get(f.id);
      const section = el && el.closest(REPEATING);
      if (section) f.label = `${sectionName(section)}: ${f.label}`;
    }
    return dom.loadWidgetOptions(snap);
  }

  // Upload the resume before anything else on My Experience: Workday parses
  // it into work history and education, which are then kept as filled.
  async function prepare(ctx, root) {
    if (!ctx.task.has_resume || resumeUploaded(root)) return;
    const input = dom.deepQueryAll(root, "input[type=file][data-automation-id='file-upload-input-ref'], input[type=file]").find((el) => dom.fileInputVisible(el));
    if (!input || (input.files && input.files.length)) return;
    let resume = null;
    try {
      resume = await ctx.api.resume();
    } catch (e) {
      return;
    }
    if (!resume) return;
    await dom.attachFile(input, resume.base64, resume.filename, resume.mime);
    if (await waitFor(() => resumeUploaded(root), 15000, 400)) await dom.delay(1500, 2500);
  }

  function unresolve(plan, field, reason) {
    Object.assign(field, { value: null, source: "unresolved", confidence: 0, requires_review: true, missing_reason: reason });
    if (field.required && !plan.unresolved_required.includes(field.field_id)) plan.unresolved_required.push(field.field_id);
  }

  // Voluntary disclosures and self-identification are answered only from an
  // answer the member saved themselves; anything else is left blank and
  // flagged. Phone device type defaults to Mobile, the overwhelmingly common
  // answer, which is not a personal disclosure.
  function restrict(plan, step) {
    const sensitiveStep = SENSITIVE_STEP_RE.test(step || "");
    for (const f of plan.fields) {
      const sensitive = SENSITIVE_LABEL_RE.test(f.label || "") || (sensitiveStep && f.input_type !== "text" && f.input_type !== "textarea");
      if (sensitive && f.source !== "user" && f.value != null && f.value !== "") {
        unresolve(plan, f, "Voluntary disclosure: CareerCraft only uses an answer you saved yourself. Choose one (or a decline option) to continue.");
      } else if (sensitive && f.source !== "user") {
        f.requires_review = true;
        f.missing_reason = f.missing_reason || "Voluntary disclosure: answer it yourself if you wish.";
      }
      // A generic "phone" answer is the number itself, never a device type.
      if (/phone device type/i.test(f.label || "") && f.source !== "user") {
        Object.assign(f, { value: "Mobile", source: "profile", confidence: 0.9, requires_review: false, missing_reason: null });
        plan.unresolved_required = plan.unresolved_required.filter((id) => id !== f.field_id);
      }
    }
  }

  function findPrimary(root) {
    const nav = dom.deepQueryAll(document, NEXT).filter((b) => dom.isVisible(b) && !b.disabled);
    if (nav.length) {
      const found = primaryFrom(nav);
      // An unrecognised label (a localised tenant) might be Submit: treat it
      // as the final step, which always goes through review.
      return found.el ? found : { el: nav[0], kind: "submit", guessed: true };
    }
    return primaryFrom(dom.deepQueryAll(root, ANY_BUTTON));
  }

  const cfg = {
    label: "Workday",
    success: SUCCESS_RE,
    stepDelay: [700, 1100],
    stepTimeout: 15000,
    keepFilled: true,
    get loginMessage() {
      if (visibleOne(LEGAL_NOTICE)) return "Read and accept Workday's legal notice yourself, then press Continue.";
      if (verificationPrompt()) return "Enter the Workday verification code yourself, then press Continue.";
      return "Sign in or create your Workday account in this tab, then press Continue. CareerCraft never sees your password.";
    },
    signedOut,
    findRoot,
    findPrimary,
    stepName,
    signature,
    stepErrors,
    blocked,
    snapshot,
    prepare,
    restrict,
    // Job page → Apply → "Apply Manually" (never the resume auto-parse
    // start, so answers come from CareerCraft's plan) → account → form.
    async enter(ctx) {
      const manualSel = aid("applyManually");
      const ready = () => visibleOne(manualSel) || findRoot() || signedOut();
      if (!visibleOne(manualSel)) {
        const apply = visibleOne(aid("adventureButton", "applyButton")) || findApplyControl();
        if (!apply) return null;
        if ((await clickThrough(ctx, apply, ready)) === NAVIGATING) return NAVIGATING;
      }
      const manual = visibleOne(manualSel);
      if (!manual) return null;
      return clickThrough(ctx, manual, () => findRoot() || signedOut());
    },
  };

  drivers.workdayDriver = makeAtsDriver(cfg);
  drivers.workdayCfg = cfg;
})();
