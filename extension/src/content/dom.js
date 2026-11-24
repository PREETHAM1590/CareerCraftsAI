// CareerCraft AI — DOM utilities shared by every driver.
// Classic script (not a module) — exposes window.CareerCraftDOM.
// Guarded against double-injection: re-running this file on the same
// document (e.g. a duplicate "complete" event) is a safe no-op.
(function () {
  if (window.__ccDomLoaded) return;
  window.__ccDomLoaded = true;

  const FIELD_ATTR = "data-cc-field";

  function textOf(node) {
    if (!node) return "";
    const t = "innerText" in node ? node.innerText : node.textContent;
    return (t || "").replace(/\s+/g, " ").trim();
  }

  function isVisible(el) {
    if (!el) return false;
    if (el.closest("[hidden]")) return false;
    let style;
    try {
      style = window.getComputedStyle(el);
    } catch (e) {
      return false;
    }
    if (!style) return false;
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (parseFloat(style.opacity || "1") === 0) return false;
    return el.getClientRects().length > 0;
  }

  function nextFieldId() {
    if (typeof window.__ccFieldCounter !== "number") window.__ccFieldCounter = 0;
    window.__ccFieldCounter += 1;
    return "cc-" + window.__ccFieldCounter;
  }

  function ensureId(el) {
    if (el.id) return el.id;
    let existing = el.getAttribute(FIELD_ATTR);
    if (!existing) {
      existing = nextFieldId();
      el.setAttribute(FIELD_ATTR, existing);
    }
    return existing;
  }

  // querySelectorAll that also looks inside open shadow roots. Some ATS
  // front ends (SmartRecruiters' web components) keep their whole form there.
  function deepQueryAll(root, selector) {
    const found = Array.from(root.querySelectorAll(selector));
    if (root.shadowRoot) found.push(...deepQueryAll(root.shadowRoot, selector));
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) found.push(...deepQueryAll(el.shadowRoot, selector));
    }
    return found;
  }

  function shadowTexts(root, out) {
    for (const el of root.querySelectorAll("*")) {
      if (!el.shadowRoot) continue;
      for (const child of el.shadowRoot.children) out.push(textOf(child));
      shadowTexts(el.shadowRoot, out);
    }
    return out;
  }

  // The element a snapshot field id points at: its own id, or the marker
  // ensureId put on an element that had none.
  function elementFor(id) {
    return (
      document.getElementById(id) ||
      deepQueryAll(document, `#${CSS.escape(id)}, [${FIELD_ATTR}="${CSS.escape(id)}"]`)[0] ||
      null
    );
  }

  // el.labels?.[0]?.innerText || aria-label || aria-labelledby text ||
  // (structural fallback, useful on LinkedIn's custom markup) || placeholder || name
  function getLabel(el) {
    let text = "";
    try {
      if (el.labels && el.labels[0]) text = textOf(el.labels[0]);
    } catch (e) {
      /* labels not supported on this element type */
    }
    if (!text) text = (el.getAttribute("aria-label") || "").trim();
    if (!text) {
      const labelledBy = el.getAttribute("aria-labelledby");
      if (labelledBy) {
        text = labelledBy
          .split(/\s+/)
          .map((id) => textOf(document.getElementById(id)))
          .filter(Boolean)
          .join(" ")
          .trim();
      }
    }
    if (!text) {
      // Nearest label / legend / span[aria-hidden=true] inside the field's
      // container — LinkedIn rarely uses a real <label for="…">.
      const container =
        el.closest("[class*='form-element'], [class*='fb-'], li, fieldset, div") || el.parentElement;
      if (container) {
        const candidate = container.querySelector("label, legend, span[aria-hidden='true']");
        if (candidate && candidate !== el && !candidate.contains(el)) {
          text = textOf(candidate);
        }
      }
    }
    if (!text) text = (el.getAttribute("placeholder") || "").trim();
    if (!text) text = el.name || "";
    return text.trim();
  }

  function getGroupLabel(el) {
    const fieldset = el.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend) {
        const t = textOf(legend);
        if (t) return t;
      }
      const aria = (fieldset.getAttribute("aria-label") || "").trim();
      if (aria) return aria;
    }
    const group = el.closest("[role='radiogroup'], [role='group']");
    if (group) {
      const aria = (group.getAttribute("aria-label") || "").trim();
      if (aria) return aria;
      const labelledBy = group.getAttribute("aria-labelledby");
      if (labelledBy) {
        const t = labelledBy
          .split(/\s+/)
          .map((id) => textOf(document.getElementById(id)))
          .filter(Boolean)
          .join(" ")
          .trim();
        if (t) return t;
      }
    }
    return "";
  }

  function inputTypeOf(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "select") return "select";
    if (tag === "textarea") return "textarea";
    return (el.type || "text").toLowerCase();
  }

  function currentValue(el, type) {
    if (type === "select") {
      if (!el.value) return "";
      const opt = el.selectedOptions && el.selectedOptions[0];
      return opt ? opt.text.trim() : "";
    }
    if (type === "file") {
      return Array.from(el.files || [])
        .map((f) => f.name)
        .join(",");
    }
    if (type === "checkbox" || type === "radio") return el.value;
    return el.value || "";
  }

  // Snapshot every fillable field under `root` (document by default),
  // skipping password/hidden inputs. Returns { fields, byId, byName } where
  // byId/byName let a driver find the live elements again after planning.
  // ── Custom widgets ──────────────────────────────────────────────────────
  // Workday, Greenhouse's newer boards and Ashby render dropdowns as buttons
  // or comboboxes with a separate option popup; native <select> handling
  // never sees them. The snapshot reports them as fields with a `widget`
  // kind so drivers can fill them the way a person would.
  const LISTBOX_BUTTON = "button[aria-haspopup='listbox']";
  const COMBO_INPUT = "input[role='combobox'], input[data-uxi-widget-type='selectinput']";
  const MULTI_CONTAINER = "[data-automation-id='multiSelectContainer'], [data-automation-id='multiselectInputContainer']";
  const OPTION_SEL = "[role='option'], [data-automation-id='promptOption']";
  const DATE_PART_SEL = "input[data-automation-id^='dateSection']";
  const PLACEHOLDER_RE = /^(select one|select\.*|choose( one)?\.*|please select\.*|-+.*-+|none selected)$/i;

  function containerLabel(el) {
    const box = el.closest("[data-automation-id^='formField-'], fieldset, [role='group']");
    if (!box) return "";
    const label = box.querySelector("label, legend");
    return label && !label.contains(el) ? textOf(label) : "";
  }

  function isRequired(el, label) {
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    if (/\*\s*$/.test(label || "")) return true;
    return el.tagName === "BUTTON" && /\brequired\b/i.test(el.getAttribute("aria-label") || "");
  }

  function isMulti(el) {
    return !!el.closest(MULTI_CONTAINER);
  }

  function widgetOf(el) {
    if (el.tagName === "BUTTON") return "listbox";
    if (el.tagName === "INPUT" && el.matches(COMBO_INPUT)) return isMulti(el) ? "multiselect" : "combobox";
    return null;
  }

  function widgetValue(el, widget) {
    if (widget === "listbox") {
      const t = textOf(el);
      return PLACEHOLDER_RE.test(t) ? "" : t;
    }
    if (widget === "multiselect") {
      const box = el.closest(MULTI_CONTAINER);
      return Array.from(box.querySelectorAll("[data-automation-id='selectedItem']")).map(textOf).filter(Boolean).join(", ");
    }
    return el.value || "";
  }

  // A resume input is often visually hidden behind a styled drop zone or
  // "Attach" button; the person can still use it, so it counts as visible.
  function fileInputVisible(el) {
    if (isVisible(el)) return true;
    if (Array.from(el.labels || []).some(isVisible)) return true;
    const zone = el.closest(
      "[data-automation-id='file-upload-drop-zone'], [data-automation-id='resumeSection'], [data-automation-id^='formField-'], label, fieldset, [class*='upload' i], [class*='dropzone' i], [class*='attach' i]"
    );
    return !!zone && isVisible(zone);
  }

  function dateParts(wrap) {
    const parts = {};
    for (const el of wrap.querySelectorAll(DATE_PART_SEL)) {
      const kind = el.getAttribute("data-automation-id") || "";
      if (/Month/i.test(kind)) parts.m = el;
      else if (/Day/i.test(kind)) parts.d = el;
      else if (/Year/i.test(kind)) parts.y = el;
    }
    return parts;
  }

  function dateWrapper(el) {
    return el.closest("[data-automation-id='dateInputWrapper']") || el.closest("[data-automation-id^='formField-']") || el.parentElement;
  }

  // Snapshot every fillable field under `root` (document by default),
  // skipping password/hidden inputs. Returns { fields, byId, byName } where
  // byId/byName let a driver find the live elements again after planning.
  function snapshot(root) {
    root = root || document;
    const nodes = deepQueryAll(root, `input,select,textarea,${LISTBOX_BUTTON}`).filter(
      (e) => e.type !== "password" && e.type !== "hidden"
    );
    const fields = [];
    const byId = new Map();
    const byName = new Map();
    const widgets = new Map();
    const dateWraps = new Set();

    for (const el of nodes) {
      // Workday splits a date into month/day/year inputs: one field per date.
      if (el.matches(DATE_PART_SEL)) {
        const wrap = dateWrapper(el);
        if (dateWraps.has(wrap)) continue;
        dateWraps.add(wrap);
        const parts = dateParts(wrap);
        const all = Object.values(parts);
        const id = ensureId(wrap);
        const label = containerLabel(el) || getLabel(el);
        const value = [parts.m, parts.d, parts.y].filter(Boolean).map((p) => p.value).filter(Boolean).join("/");
        fields.push({
          name: "", id, type: "date", widget: "date", label, group_label: "", value, checked: false,
          required: all.some((p) => isRequired(p, label)), options: [],
          visible: all.some(isVisible), disabled: all.every((p) => p.disabled),
        });
        byId.set(id, wrap);
        widgets.set(id, "date");
        continue;
      }
      const widget = widgetOf(el);
      const type = widget === "listbox" ? "select" : inputTypeOf(el);
      const id = ensureId(el);
      const label = widget ? containerLabel(el) || getLabel(el) : getLabel(el);
      const group_label = type === "radio" || type === "checkbox" ? getGroupLabel(el) : "";
      const options = type === "select" && !widget ? Array.from(el.options).map((o) => o.text.trim()).filter(Boolean) : [];
      const visible = type === "file" ? fileInputVisible(el) : isVisible(el);
      const disabled = !!el.disabled || el.getAttribute("aria-disabled") === "true";

      const field = {
        name: el.name || "",
        id,
        type,
        label,
        group_label,
        value: widget ? widgetValue(el, widget) : currentValue(el, type),
        checked: !!el.checked,
        required: isRequired(el, label),
        options,
        visible,
        disabled,
      };
      if (widget) {
        field.widget = widget;
        widgets.set(id, widget);
      }
      fields.push(field);

      byId.set(id, el);
      if ((type === "radio" || type === "checkbox") && el.name) {
        if (!byName.has(el.name)) byName.set(el.name, []);
        byName.get(el.name).push({ el, label });
      }
    }

    return { fields, byId, byName, widgets };
  }

  function waitUntil(fn, ms) {
    const end = Date.now() + ms;
    return new Promise((resolve) => {
      (function tick() {
        let value = null;
        try {
          value = fn();
        } catch (e) {
          value = null;
        }
        if (value || Date.now() >= end) return resolve(value || null);
        setTimeout(tick, 100);
      })();
    });
  }

  function pressKey(el, key) {
    let accepted = true;
    for (const type of ["keydown", "keyup"]) {
      const ok = el.dispatchEvent(new KeyboardEvent(type, { key, code: key, bubbles: true, cancelable: true }));
      if (type === "keydown") accepted = ok;
    }
    return accepted;
  }

  function optionText(option) {
    return (option.getAttribute("data-automation-label") || textOf(option)).trim();
  }

  function popupScope(el) {
    const id = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
    const own = id && document.getElementById(id);
    return own && isVisible(own) ? own : document;
  }

  function visibleOptions(scope) {
    return deepQueryAll(scope, OPTION_SEL).filter((o) => isVisible(o) && o.getAttribute("aria-disabled") !== "true");
  }

  // Handlers usually sit on the option or an ancestor; clicking the deepest
  // element lets the event bubble through all of them.
  function innermost(el) {
    let target = el;
    while (target.firstElementChild && !/^(INPUT|SVG)$/i.test(target.firstElementChild.tagName)) target = target.firstElementChild;
    return target;
  }

  async function closePopup(el) {
    if (!visibleOptions(document).length) return;
    pressKey(document.activeElement || el, "Escape");
    if (await waitUntil(() => !visibleOptions(document).length, 600)) return;
    if (el.tagName === "BUTTON" && el.getAttribute("aria-expanded") === "true") clickLike(el);
  }

  async function openOptions(el) {
    const current = visibleOptions(popupScope(el));
    if (current.length) return current;
    el.focus();
    clickLike(el);
    let found = await waitUntil(() => {
      const o = visibleOptions(popupScope(el));
      return o.length ? o : null;
    }, 1500);
    if (!found && el.tagName === "INPUT") {
      pressKey(el, "ArrowDown");
      found = await waitUntil(() => {
        const o = visibleOptions(popupScope(el));
        return o.length ? o : null;
      }, 1000);
    }
    return found;
  }

  async function readOptions(el) {
    const options = (await openOptions(el)) || [];
    const texts = options.map(optionText).filter((t) => t && !PLACEHOLDER_RE.test(t));
    await closePopup(el);
    return Array.from(new Set(texts));
  }

  // Options of a custom dropdown exist only while it is open: open each one
  // briefly so the plan can match saved answers against the real choices.
  // Multi-select prompts stay free text (their first level is categories).
  async function loadWidgetOptions(snap, limit) {
    let opened = 0;
    for (const field of snap.fields) {
      if (opened >= (limit || 40)) break;
      if (!field.widget || field.options.length || !field.visible || field.disabled) continue;
      if (field.widget !== "listbox" && field.widget !== "combobox") continue;
      const el = snap.byId.get(field.id);
      if (!el) continue;
      opened += 1;
      const options = await readOptions(el);
      if (options.length) {
        field.options = options;
        field.type = "select";
      }
    }
    return snap;
  }

  async function fillListbox(button, text) {
    if (normalize(widgetValue(button, "listbox")) === normalize(text)) return true;
    const options = await openOptions(button);
    if (!options) return false;
    const match = matchOption(options, text, optionText);
    if (!match) {
      await closePopup(button);
      return false;
    }
    if (match.scrollIntoView) match.scrollIntoView({ block: "nearest" });
    clickLike(innermost(match));
    const chosen = normalize(optionText(match));
    const ok = await waitUntil(() => normalize(textOf(button)).includes(chosen), 1500);
    await closePopup(button);
    return !!ok;
  }

  // Type-to-search dropdowns (react-select, Workday prompts): type the
  // answer, then click the matching suggestion. Multi-selects take a comma
  // separated list.
  async function fillCombobox(input, value) {
    const multi = isMulti(input);
    const values = Array.isArray(value) ? value : multi ? String(value).split(/[;,]/) : [value];
    let all = true;
    for (const raw of values.map((v) => String(v).trim()).filter(Boolean)) {
      if (multi && normalize(widgetValue(input, "multiselect")).includes(normalize(raw))) continue;
      input.focus();
      clickLike(input);
      setNativeValue(input, raw);
      input.dispatchEvent(new InputEvent("input", { bubbles: true, data: raw, inputType: "insertText" }));
      const pick = () => {
        const o = visibleOptions(popupScope(input));
        return o.length ? matchOption(o, raw, optionText) : null;
      };
      let match = await waitUntil(pick, 2000);
      if (!match) {
        pressKey(input, "Enter"); // Workday prompts search on Enter
        match = await waitUntil(pick, 2000);
      }
      if (!match) {
        all = false;
        // A stray search term in a multi-select would block the next one;
        // a single combobox keeps the typed text for the person to see.
        if (multi) {
          setNativeValue(input, "");
          fireEvents(input, ["input"]);
        }
        await closePopup(input);
        continue;
      }
      clickLike(innermost(match));
      await delay(200, 350);
    }
    if (!multi) await closePopup(input);
    input.blur();
    return all;
  }

  function parseDate(value) {
    const s = String(value || "").trim();
    let m;
    if ((m = /^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?/.exec(s))) return { y: m[1], m: m[2], d: m[3] };
    if ((m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(s))) return { m: m[1], d: m[2], y: m[3] };
    if ((m = /^(\d{1,2})\/(\d{4})$/.exec(s))) return { m: m[1], y: m[2] };
    if ((m = /^(\d{4})$/.exec(s))) return { y: m[1] };
    return null;
  }

  // Workday's date sections are spin-button inputs that react to keystrokes;
  // when the page handles a key itself (keydown cancelled) it is not typed twice.
  async function typeInto(el, text) {
    el.focus();
    clickLike(el);
    setNativeValue(el, "");
    for (const ch of text) {
      const handled = !el.dispatchEvent(new KeyboardEvent("keydown", { key: ch, bubbles: true, cancelable: true }));
      if (!handled) {
        setNativeValue(el, el.value + ch);
        el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
      }
      el.dispatchEvent(new KeyboardEvent("keyup", { key: ch, bubbles: true }));
    }
    fireEvents(el, ["change"]);
    el.blur();
  }

  // ── Date-picker popups ──────────────────────────────────────────────────
  // Some date inputs ignore typing and only take a click in their calendar
  // popup (Workday's calendar icon). Open it, step month by month (or year by
  // year for a month picker) to the target, then click the day or month.
  const DATE_ICON = "[data-automation-id='dateIcon'], button[aria-label*='calendar' i], button[aria-label*='date picker' i]";
  const PICKER = "[data-automation-id*='datePicker'], [data-automation-id*='monthPicker'], [role='dialog'][aria-label*='date' i]";
  const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

  function shownMonthYear(picker) {
    const header = picker.querySelector("[data-automation-id*='Header'], [data-automation-id*='MonthYear'], [aria-live], [role='heading'], h2, h3");
    const text = textOf(header || picker).slice(0, 200).toLowerCase();
    const both = /\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{4})\b/.exec(text);
    if (both) return { y: Number(both[2]), m: MONTHS.indexOf(both[1]) + 1 };
    const year = /\b(19|20)\d{2}\b/.exec(text);
    return year ? { y: Number(year[0]), m: null } : null;
  }

  function pickerButton(picker, direction) {
    const re = direction > 0 ? /next|forward|›|»|>/i : /prev|back|‹|«|</i;
    return deepQueryAll(picker, "button, [role='button']").find((b) => isVisible(b) && re.test(`${b.getAttribute("aria-label") || ""} ${b.getAttribute("data-automation-id") || ""} ${textOf(b)}`));
  }

  async function pickDate(wrap, date) {
    const icon = wrap.querySelector(DATE_ICON) || (wrap.parentElement && wrap.parentElement.querySelector(DATE_ICON));
    if (!icon || !date.y) return false;
    clickLike(icon);
    const picker = await waitUntil(() => deepQueryAll(document, PICKER).find(isVisible), 1500);
    if (!picker) return false;
    const target = { y: Number(date.y), m: date.m ? Number(date.m) : null };
    for (let i = 0; i < 600; i++) {
      const shown = shownMonthYear(picker);
      if (!shown) break;
      const diff = shown.m && target.m ? (target.y - shown.y) * 12 + (target.m - shown.m) : target.y - shown.y;
      if (diff === 0) break;
      const step = pickerButton(picker, diff);
      if (!step) break;
      clickLike(step);
      await delay(20, 40);
    }
    const want = date.d ? String(Number(date.d)) : target.m ? MONTHS[target.m - 1] : String(target.y);
    const cells = deepQueryAll(picker, "[role='gridcell'], [role='option'], td, button").filter(
      (c) => isVisible(c) && c.getAttribute("aria-disabled") !== "true" && !/outside|othermonth|other-month/i.test(`${c.className} ${c.getAttribute("data-automation-id") || ""}`)
    );
    const cell = cells.find((c) => normalize(textOf(c)) === want) || (!date.d && cells.find((c) => normalize(textOf(c)).startsWith(want)));
    if (!cell) {
      pressKey(document.activeElement || icon, "Escape");
      return false;
    }
    clickLike(innermost(cell));
    await waitUntil(() => !picker.isConnected || !isVisible(picker), 1000);
    return true;
  }

  async function fillDateGroup(wrap, value) {
    const date = parseDate(value);
    if (!date) return false;
    const parts = dateParts(wrap);
    const strip = (v) => String(v || "").replace(/^0+(?=\d)/, "");
    const matches = () => ["m", "d", "y"].every((key) => !parts[key] || !date[key] || strip(parts[key].value) === strip(date[key]));
    for (const key of ["m", "d", "y"]) {
      if (!parts[key] || !date[key]) continue;
      await typeInto(parts[key], key === "y" ? date[key] : date[key].padStart(2, "0"));
    }
    if (matches()) return true;
    return (await pickDate(wrap, date)) && matches();
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    if (descriptor && descriptor.set) {
      descriptor.set.call(el, value);
    } else {
      el.value = value;
    }
  }

  function setNativeSelectValue(el, optionValue) {
    const descriptor = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value");
    if (descriptor && descriptor.set) {
      descriptor.set.call(el, optionValue);
    } else {
      el.value = optionValue;
    }
  }

  function fireEvents(el, types) {
    for (const type of types) {
      el.dispatchEvent(new Event(type, { bubbles: true }));
    }
  }

  function normalize(s) {
    return (s == null ? "" : s).toString().replace(/\s+/g, " ").trim().toLowerCase();
  }

  function escapeRe(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // Exact text first, then the answer as a whole word at the start, then
  // anywhere. Whole words only: "No" must never pick "Not applicable", nor
  // "Male" pick "Female".
  function matchOption(options, text, getText) {
    getText = getText || ((o) => (typeof o === "string" ? o : o.text != null ? o.text : o.label || ""));
    const target = normalize(text);
    if (!target) return null;
    const texts = options.map((o) => normalize(getText(o)));
    const word = new RegExp("(^|[^\\p{L}\\p{N}])" + escapeRe(target) + "($|[^\\p{L}\\p{N}])", "u");
    let i = texts.indexOf(target);
    if (i < 0) i = texts.findIndex((t) => t.startsWith(target) && word.test(t));
    if (i < 0) i = texts.findIndex((t) => word.test(t));
    return i < 0 ? null : options[i];
  }

  function findOption(options, text) {
    return matchOption(options, text, (o) => o.text);
  }

  async function fillText(el, value) {
    el.focus();
    setNativeValue(el, value);
    fireEvents(el, ["input", "change"]);
    el.blur();
    fireEvents(el, ["blur"]);
  }

  async function fillSelect(el, text) {
    const options = Array.from(el.options);
    const match = findOption(options, text);
    if (!match) return false;
    el.focus();
    setNativeSelectValue(el, match.value);
    fireEvents(el, ["input", "change"]);
    el.blur();
    return true;
  }

  function clickLike(el) {
    el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
    el.click();
  }

  async function fillRadioGroup(elements, text) {
    const match = matchOption(elements, text, (e) => e.label);
    if (!match) return false;
    if (!match.el.checked) clickLike(match.el);
    return true;
  }

  async function fillCheckboxGroup(elements, values) {
    const wanted = new Set((Array.isArray(values) ? values : [values]).map(normalize));
    const labels = new Set(elements.map(({ label }) => normalize(label)));
    // An answer that is none of the boxes would silently tick nothing.
    if ([...wanted].some((w) => !labels.has(w))) return false;
    for (const { el, label } of elements) {
      const shouldCheck = wanted.has(normalize(label));
      if (!!el.checked !== shouldCheck) clickLike(el);
    }
    return true;
  }

  async function fillSingleCheckbox(el, value) {
    const shouldCheck = value === true || normalize(value) === "true" || normalize(value) === "yes";
    if (!!el.checked !== shouldCheck) clickLike(el);
    return true;
  }

  function base64ToBytes(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  async function attachFile(el, base64, filename, mime) {
    const bytes = base64ToBytes(base64);
    const file = new File([bytes], filename || "resume.pdf", { type: mime || "application/pdf" });
    const dt = new DataTransfer();
    dt.items.add(file);
    el.files = dt.files;
    fireEvents(el, ["input", "change"]);
    return el.files && el.files.length === 1;
  }

  function delay(min, max) {
    min = typeof min === "number" ? min : 250;
    max = typeof max === "number" ? max : 800;
    const ms = min + Math.random() * (max - min);
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function findByText(root, selector, regex) {
    const nodes = Array.from(root.querySelectorAll(selector));
    return nodes.find((n) => regex.test(textOf(n)) || regex.test(n.getAttribute("aria-label") || ""));
  }

  function visibleText(root, limit) {
    const base = root && root.body ? root.body : root || document.body;
    const text = [textOf(base), ...shadowTexts(base, [])].join(" ");
    return limit ? text.slice(0, limit) : text;
  }

  window.CareerCraftDOM = {
    isVisible,
    elementFor,
    deepQueryAll,
    getLabel,
    getGroupLabel,
    snapshot,
    setNativeValue,
    fillText,
    fillSelect,
    fillRadioGroup,
    fillCheckboxGroup,
    fillSingleCheckbox,
    attachFile,
    delay,
    clickLike,
    findByText,
    textOf,
    normalize,
    visibleText,
    matchOption,
    waitUntil,
    pressKey,
    readOptions,
    loadWidgetOptions,
    fillListbox,
    fillCombobox,
    fillDateGroup,
    parseDate,
    fileInputVisible,
  };
})();
