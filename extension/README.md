# CareerCraft AI — Apply Assistant (Chrome extension)

Applies to jobs **in your own browser**, where you are already signed in to
LinkedIn, Naukri and company career sites. CareerCraft never runs a browser
for you on its servers and never sees your job-site passwords or cookies.

By default every application ends with **you** pressing **Submit
application** in the extension's review panel and then **Submit this
application** in the popup. Nothing is submitted before that, unless you turn
on **Auto-apply** (see below).

## Install (unpacked, for now)

1. In CareerCraft open **Settings → Integrations → Browser extension** and
   click **Download extension**, then unzip it (you get a
   `careercraft-extension` folder). From a checkout you can use this
   `extension/` folder directly.
2. Open `chrome://extensions`, turn on **Developer mode**, click
   **Load unpacked** and pick that folder.
3. Pin **CareerCraft AI — Apply Assistant** to the toolbar.

Chrome 110 or newer (Edge and Brave work too).

## Connect it to your account

1. Reload the CareerCraft page after loading the extension.
2. In **Settings → Integrations → Browser extension**, click **Connect this
   browser**. The hosted site and localhost pair automatically.
3. For other CareerCraft deployments, connect from the extension popup with a
   connection code. Chrome asks once for permission to talk to that site.

Each connected browser appears in Settings, where you can revoke it. The
code is shown once and only its hash is stored on the server.

## How applying works

1. Press **Apply** on a saved job in CareerCraft. The server starts a
   durable Temporal workflow and queues the application for your browser.
2. The extension picks it up within seconds (it also checks every 30 s),
   opens the job page in a new tab and finds the application form. When the
   job page is only a description, it follows the page's **Apply** link or
   button, or the company's embedded Greenhouse/Lever/Ashby/Workable… form,
   in the same tab (at most four hops). Then it fills what it can:
   - your saved answers and profile (name, email, phone, links…),
   - your resume PDF on upload fields,
   - a **draft** for free-text questions ("Why do you want to join?"),
     written by your own configured model and marked *Draft — review*.
   Search boxes, job-alert sign-ups and other non-application forms are
   never filled or submitted.
3. A review panel lists every answer with where it came from. Questions it
   must never guess stay empty for you: visa sponsorship, work
   authorization, salary, notice period, and consent checkboxes.
   With **Remember my answers** on, what you type is reused next time.
4. You press **Submit application** in the panel. Nothing is sent yet: the
   toolbar icon shows **1** and the popup opens (Chrome 127+) with the final
   form. Press **Submit this application** there. Only then does the
   extension click the site's submit control, read the confirmation, and
   report back. CareerCraft marks the job *Applied* and schedules day-5 and
   day-12 follow-up drafts (which also wait for your approval).
5. If the application moves to a site the extension has no access to yet,
   the icon shows **!** and the popup asks you to allow that site; the same
   tab continues once you do.

Dedicated handlers (step-by-step loop: fill the step, ask about what is
never guessed, press Next, repeat; only the final Submit goes through the
review and popup approval):
- **Workday** (`*.myworkdayjobs.com`, `*.myworkdaysite.com`, and tenants on
  company domains, recognised by Workday's `data-automation-id` markup) —
  job page → Apply → *Apply Manually* → your account → My Information → My
  Experience → Application Questions → Voluntary Disclosures / Self Identify
  → Review → Submit (`src/content/workday.js`). It fills text fields,
  listbox dropdowns (options are read by opening them), search prompts such
  as *How did you hear about us* and skills, split month/day/year dates and
  the hidden resume upload. On My Experience it uploads the resume first so
  Workday parses work history and education; values already on the page are
  never overwritten, and repeated blocks are labelled *Work Experience 1:
  Job Title* so answers are not mixed up. Voluntary disclosures (gender,
  race/ethnicity, veteran, disability) are filled **only** from answers you
  saved yourself; otherwise they stay blank and are flagged. After each Next
  it waits for the step to change and hands Workday's validation messages
  back to you. Sign-in, account creation, verification codes and the legal
  notice are always left to you; passwords are never read. "Already
  applied" ends the task.
- **SmartRecruiters** — *I'm interested* → the application, including forms
  that live inside open shadow roots.
- **Workable** — *Apply for this job* → the single-page form.

Supported flows: LinkedIn **Easy Apply** (multi-step), **Naukri** (the review
panel appears *before* Naukri's one-click Apply) and single-page ATS forms
(Greenhouse, Lever, Ashby, Workday, most company career pages). Jobs that
send you to an external site from LinkedIn/Naukri are reported as such.

If you are signed out, a CAPTCHA appears, or a question needs you, the
panel says so and waits. Closing the tab cancels the application.

## Auto-apply (opt-in, off by default)

Turn it on in the popup or in **Autofill profile & settings**. When an
application needs nothing from you, the extension skips the review panel and
the popup approval and submits it; your opt-in is sent as the confirmation
for that one reviewed form. It still stops and hands the tab back to you for:
sign-in, account creation, verification codes, CAPTCHAs, required questions
it could not answer or the page refused, AI-drafted answers, validation
errors, consent/legal checkboxes, and any submit button it only guessed.

## Autofill this page (no CareerCraft task needed)

**Autofill this page** in the popup fills the application form on the
current tab (every frame, so embedded Greenhouse forms work too) from the
profile and resume saved in **Autofill profile & settings** (stored only in
this browser). On Workday it fills the current step. It never navigates,
never ticks consent boxes and never submits; work authorization, sponsorship
and voluntary disclosures are used only if you chose an answer there.

## Decisions on the page

When plain text matching is not enough — which button advances this form,
which dropdown option matches your saved answer, does this page confirm the
submission — the extension asks the backend's decision engine
(`POST /api/v1/extension/device/decide`). The server answers with a fast
typed-decision model: TypeSafe **Jev** (`TYPESAFE_API_KEY`) or a
self-hosted **Laya** (`LAYA_URL`, see `deploy/laya/`), falling back to
built-in heuristics. It acts only above a confidence threshold; anything
less is left to you.

## Please read

Automating job applications may breach some sites' terms of service —
LinkedIn's User Agreement, for example, restricts automated activity. You
stay responsible for how you use this: every submission is approved by you,
the extension paces its actions like a person, and it never solves CAPTCHAs.

## Development

- `src/background.js` — service worker: pairing, polling, all network calls.
- `src/content/*.js` — injected into the job tab: DOM helpers, review panel
  (closed shadow DOM), platform drivers, and the runner.
- `src/content/workday.js` — the Workday steps; `src/content/autofill.js` —
  the task-free "Autofill this page"; `options.html` — autofill profile and
  the auto-apply switch.
- `test/fixtures/` — Greenhouse-, LinkedIn- and Naukri-like pages, plus
  `workday/full.html`, a multi-step Workday reconstruction (listboxes,
  prompts, split dates, hidden resume input, parsing, validation,
  disclosures).
- `test/autofill_rules.test.mjs` — option matching, dates, local autofill
  rules (`node --test test/autofill_rules.test.mjs`).
- `test/e2e_apply.mjs` — the whole apply flow in a real Chromium against
  local fixture job sites (Greenhouse, Lever, Workday, SmartRecruiters, Workable, an in-page Apply button the extension must leave for the person, a company page with an embedded Greenhouse form) and a fake API that
  follows the backend's status rules. Run `node test/e2e_apply.mjs`.
- `test/e2e_live.py` — end-to-end run against a local stack (see its
  docstring): real API, Temporal worker and this extension in Chromium.

CI syntax-checks every script and the manifest and runs both node tests.
