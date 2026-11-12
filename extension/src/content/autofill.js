// CareerCraft AI — one-click "Autofill this page" from the popup.
// Classic script, injected after workday.js. Fills the application form in
// this frame from the autofill profile saved in the extension's options. It
// never navigates, never ticks consent boxes and never submits: the person
// reviews and continues themselves. Works without a CareerCraft task.
(function () {
  if (window.__ccAutofillLoaded) return;
  window.__ccAutofillLoaded = true;

  const dom = window.CareerCraftDOM;
  const panel = window.CareerCraftPanel;
  const drivers = window.CareerCraftDrivers;
  const shared = drivers._shared;

  const SECTION_RE = /^(work experience|education|language|certification|websites) \d+:/i;

  // Most specific rules first. Voluntary disclosures and work-authorization
  // answers are only used when the person saved them in the options page.
  const RULES = [
    [/phone device type/, () => "Mobile"],
    [/(country|phone) (phone )?code|phone extension|\bextension\b/, () => null],
    [/first name|given name|forename/, (p) => p.firstName],
    [/last name|family name|surname/, (p) => p.lastName],
    [/middle name/, () => null],
    // "Name of current employer" / "Name of school" are not the person's name.
    [/^(full |legal |your |candidate )?name\b(?! of\b)/, (p) => [p.firstName, p.lastName].filter(Boolean).join(" ")],
    [/e-?mail/, (p) => p.email],
    [/phone|mobile|telephone|contact number/, (p) => p.phone],
    [/address line ?2|apartment|suite/, () => null],
    [/address line ?1|street|^address\b|home address/, (p) => p.addressLine1],
    [/\bcity\b|\btown\b/, (p) => p.city],
    [/postal|zip/, (p) => p.postalCode],
    [/country|nation of residence/, (p) => p.country],
    [/\bstate\b|province|county|region/, (p) => p.state],
    [/linkedin/, (p) => p.linkedin],
    [/github/, (p) => p.github],
    [/portfolio|website|personal (site|url)|blog|\burl\b/, (p) => p.website],
    [/current (company|employer)|most recent (company|employer)|^employer\b/, (p) => p.currentCompany],
    [/current (job )?title|most recent (job )?title|^job title\b/, (p) => p.currentTitle],
    [/years of (relevant |professional |total )?experience/, (p) => p.yearsExperience],
    [/hear about|how did you (hear|find|learn)|referral source|^source\b/, (p) => p.howDidYouHear],
    // "Authorized to work ... without sponsorship?" asks about authorization.
    [/without\b.{0,40}sponsor/, (p) => p.workAuthorized],
    [/sponsor/, (p) => p.requiresSponsorship],
    [/authori[sz]ed to work|legally (authori[sz]ed|eligible|able) to work|right to work|work authori[sz]ation|eligible to work/, (p) => p.workAuthorized],
    [/gender|\bsex\b/, (p) => p.gender],
    [/\brace\b|ethnic|hispanic|latin[oax]/, (p) => p.ethnicity],
    [/veteran/, (p) => p.veteran],
    [/disab/, (p) => p.disability],
  ];

  function answerFor(label, profile) {
    const text = dom.normalize(label).replace(/\*/g, "").trim();
    if (!text) return null;
    for (const [re, pick] of RULES) {
      if (re.test(text)) return pick(profile || {}) || null;
    }
    return null;
  }

  function planLocally(fields, profile) {
    const plan = [];
    const missing = [];
    const seenGroups = new Set();
    for (const f of fields) {
      if (!f.visible || f.disabled || f.type === "file") continue;
      if (f.type === "checkbox") {
        // Consent and attestation boxes are never ticked for the person.
        if (f.required && !f.checked) missing.push(f.label || f.id);
        continue;
      }
      let id = f.id;
      let label = f.label;
      if (f.type === "radio") {
        if (!f.name || seenGroups.has(f.name)) continue;
        seenGroups.add(f.name);
        id = f.name;
        label = f.group_label || f.label;
        if (fields.some((g) => g.name === f.name && g.checked)) continue;
      } else if (!shared.isEmpty(f.value)) {
        continue; // already answered on the page; never overwrite it
      }
      if (SECTION_RE.test(label || "")) continue;
      const value = answerFor(label, profile);
      if (shared.isEmpty(value)) {
        if (f.required) missing.push(label || id);
        continue;
      }
      const inputType = f.type === "radio" ? "radio" : f.type === "select" ? "select" : f.type === "date" ? "date" : "text";
      plan.push({ field_id: id, label, input_type: inputType, required: !!f.required, value });
    }
    return { plan, missing };
  }

  async function attachResume(snap, resume) {
    if (!resume || !resume.base64) return false;
    const files = snap.fields.filter((f) => f.type === "file" && f.visible && !f.disabled && !f.value);
    const target = files.find((f) => /resume|cv|curriculum/i.test(f.label || "")) || (files.length === 1 ? files[0] : null);
    if (!target) return false;
    const el = snap.byId.get(target.id);
    return el ? dom.attachFile(el, resume.base64, resume.filename, resume.mime) : false;
  }

  function findRoot() {
    if (drivers.atsKey({}) === "workday" && drivers.workdayCfg) return { root: drivers.workdayCfg.findRoot(), workday: true };
    const found = shared.findApplicationForm();
    return { root: found ? found.form : null, workday: false };
  }

  async function run({ profile, resume }) {
    const host = location.host;
    if (window.__ccTaskRunning) return { ok: false, host, error: "A CareerCraft application is already running in this tab." };
    const { root, workday } = findRoot();
    if (!root) {
      return {
        ok: true,
        host,
        filled: 0,
        message: workday ? "Open the Workday form first (Apply → Apply Manually, then sign in)." : "No application form found here.",
      };
    }
    panel.showStatus("Autofilling this form…", host);
    const snap = workday ? await drivers.workdayCfg.snapshot(root) : await dom.loadWidgetOptions(dom.snapshot(root));
    const { plan, missing } = planLocally(snap.fields, profile);
    const failed = await shared.applyPlan({ delay: () => dom.delay(60, 160) }, snap, plan);
    const attached = await attachResume(snap, resume);
    const failedLabels = plan.filter((p) => failed.includes(p.field_id)).map((p) => p.label || p.field_id);
    const filled = plan.length - failed.length;
    const left = [...missing, ...failedLabels];
    const summary =
      `Filled ${filled} field${filled === 1 ? "" : "s"}${attached ? " and attached your resume" : ""}.` +
      (left.length ? ` Still needs you: ${left.slice(0, 8).join(", ")}${left.length > 8 ? "…" : ""}.` : "") +
      " Review the form, then continue or submit it yourself.";
    panel.showNotice(summary, host);
    return { ok: true, host, filled, attached, missing: left, message: summary };
  }

  window.CareerCraftAutofill = { run, planLocally, answerFor };
})();
