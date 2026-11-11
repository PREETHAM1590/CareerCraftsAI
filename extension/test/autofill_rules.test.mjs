// Pure helpers behind autofill and auto-apply: option matching, date
// parsing and the local profile rules (no browser needed).
//   node --test extension/test/autofill_rules.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const window = {};
const context = { window, document: {}, CSS: { escape: (s) => s }, console, setTimeout, MouseEvent: class {} };
const load = (file) => vm.runInNewContext(fs.readFileSync(new URL(`../src/content/${file}`, import.meta.url), "utf8"), context);
load("dom.js");
window.CareerCraftPanel = {};
window.CareerCraftDrivers = { _shared: { isEmpty: (v) => v == null || v === "" || (Array.isArray(v) && !v.length) } };
load("autofill.js");
const dom = window.CareerCraftDOM;
const autofill = window.CareerCraftAutofill;

test("options match whole words, never a longer word", () => {
  assert.equal(dom.matchOption(["Not applicable", "No", "Yes"], "No"), "No");
  assert.equal(dom.matchOption(["Not applicable", "Yes"], "No"), null);
  assert.equal(dom.matchOption(["Female", "Male"], "male"), "Male");
  assert.equal(dom.matchOption(["Female", "Male (he/him)"], "Male"), "Male (he/him)");
  assert.equal(dom.matchOption(["Yes, I am authorized", "No"], "Yes"), "Yes, I am authorized");
  assert.equal(dom.matchOption(["India", "Indiana"], "india"), "India");
  assert.equal(dom.matchOption(["Yes", "No"], ""), null);
  assert.equal(dom.matchOption([{ label: "C++ (expert)" }], "c++", (o) => o.label).label, "C++ (expert)");
});

test("a checkbox answer that is none of the boxes is reported, not ignored", async () => {
  const box = (label) => ({ label, el: { checked: false, dispatchEvent() {}, click() { this.checked = !this.checked; } } });
  assert.equal(await dom.fillCheckboxGroup([box("Remote"), box("Hybrid")], ["On-site"]), false);
  const boxes = [box("Remote"), box("Hybrid")];
  assert.equal(await dom.fillCheckboxGroup(boxes, ["remote"]), true);
  assert.deepEqual(boxes.map((b) => b.el.checked), [true, false]);
});

test("dates in the formats plans and profiles use", () => {
  assert.deepEqual(JSON.parse(JSON.stringify(dom.parseDate("2026-12-01"))), { y: "2026", m: "12", d: "01" });
  assert.deepEqual(JSON.parse(JSON.stringify(dom.parseDate("03/2024"))), { m: "03", y: "2024" });
  assert.deepEqual(JSON.parse(JSON.stringify(dom.parseDate("12/31/2025"))), { m: "12", d: "31", y: "2025" });
  assert.equal(dom.parseDate("next month"), null);
});

test("local autofill answers only what the profile holds", () => {
  const profile = { firstName: "Ada", lastName: "Lovelace", email: "ada@example.com", phone: "900", country: "India", gender: "", requiresSponsorship: "" };
  assert.equal(autofill.answerFor("First Name*", profile), "Ada");
  assert.equal(autofill.answerFor("Legal Name", profile), "Ada Lovelace");
  assert.equal(autofill.answerFor("Phone Device Type*", profile), "Mobile");
  assert.equal(autofill.answerFor("Country Phone Code", profile), null);
  assert.equal(autofill.answerFor("Country/Territory", profile), "India");
  assert.equal(autofill.answerFor("Gender", profile), null);
  assert.equal(autofill.answerFor("Will you require visa sponsorship?", profile), null);
  assert.equal(autofill.answerFor("Will you require visa sponsorship?", { requiresSponsorship: "No" }), "No");
});

test("local plan never overwrites, never ticks consent, and lists what is left", () => {
  const fields = [
    { id: "fn", type: "text", label: "First Name*", value: "", visible: true, required: true },
    { id: "ln", type: "text", label: "Last Name*", value: "Byron", visible: true, required: true },
    { id: "why", type: "textarea", label: "Why us?", value: "", visible: true, required: true },
    { id: "c", name: "consent", type: "checkbox", label: "I agree*", value: "on", checked: false, visible: true, required: true },
    { id: "t", type: "text", label: "Work Experience 1: Job Title", value: "", visible: true, required: true },
  ];
  const { plan, missing } = JSON.parse(JSON.stringify(autofill.planLocally(fields, { firstName: "Ada", lastName: "Lovelace", currentTitle: "Engineer" })));
  assert.deepEqual(plan.map((p) => [p.field_id, p.value]), [["fn", "Ada"]]);
  assert.deepEqual(missing, ["Why us?", "I agree*"]);
});
