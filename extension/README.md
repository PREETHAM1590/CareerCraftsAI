# CareerCraft AI — Apply Assistant (Chrome extension)

Applies to jobs **in your own browser**, where you are already signed in to
LinkedIn, Naukri and company career sites. CareerCraft never runs a browser
for you on its servers and never sees your job-site passwords or cookies.

By default every application ends with **you** pressing **Submit
application** in the extension's review panel and then **Submit this
application** in the popup. Nothing is submitted before that, unless you turn
on **Auto-submit** in CareerCraft's settings (see below).

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
code is shown once and only its hash is stored on the server. A browser's
token expires after 90 days; the extension swaps it for a fresh one on its
own during the last 30 days. At most five browsers stay connected: connecting
a sixth disconnects the one used least recently.

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
   must never guess stay empty unless you saved an answer for them in
   **Settings → Applying** (work authorization, visa sponsorship, notice
   period, salary expectation, relocation, start date, and optional
   voluntary disclosures). Consent checkboxes are always left to you.
   With **Remember my answers** on, what you type is reused next time.
4. You press **Submit application** in the panel. Nothing is sent yet: the
   toolbar icon shows **1** and the popup opens (Chrome 127+) with the final
   form. Press **Submit this application** there. Only then does the
   extension click the site's submit control, read the confirmation, and
   report back. CareerCraft marks the job *Applied* and schedules day-5 and
   day-12 follow-up drafts (which also wait for your approval). The
   application's details in CareerCraft show who approved it (you or your
   auto-submit setting), when, and the exact answers submitted, plus a
   screenshot of the reviewed form when Chrome allowed one (it does only
   after you clicked the toolbar icon on that tab, because the extension
   does not ask for access to every site).
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
  as *How did you hear about us* and skills, split month/day/year dates
  (through the calendar popup when typing is ignored) and the hidden resume
  upload. On My Experience it uploads the resume first so Workday parses
  work history and education; values already on the page are never
  overwritten, and repeated blocks are labelled *Work Experience 1: Job
  Title* so answers are not mixed up. When the jobs and schools you entered
  in CareerCraft's resume gap fixer are more than Workday parsed, it presses
  **Add** for each missing one and fills it from those entries only (never
  a drafted job title or description). Voluntary disclosures (gender,
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

## Auto-submit (opt-in, off by default)

An account setting, not an extension one: turn it on and set its daily limit
(1–50, default 10) in CareerCraft **Settings → Applying**; the popup only
shows its state. When an application needs nothing from you, the extension
skips the review panel and the popup click and asks the server to approve
that one reviewed form as *auto-approved*. The server decides, and refuses
(the popup approval then takes over, saying why) when:
- auto-submit is off or today's limit is used up;
- the job is on **LinkedIn** or **Naukri** — never auto-submitted (LinkedIn's
  terms forbid automation; Naukri's were not reviewed), checked by both the
  extension and the server;
- any answer on the form came from anything but your saved answers or your
  profile, such as an AI draft.

It also still stops and hands the tab back to you for: sign-in, account
creation, verification codes, CAPTCHAs, required questions it could not
answer or the page refused, validation errors, consent/legal checkboxes, and
any submit button it only guessed. Extensions older than 0.6.0 cannot submit
at all until updated, because they could not say who approved.

## Autofill this page (no CareerCraft task needed)

**Autofill this page** in the popup fills the application form on the
current tab (every frame, so embedded Greenhouse forms work too) from the
profile and resume saved in **Autofill profile & settings** (stored only in
this browser). On Workday it fills the current step. It never navigates,
never ticks consent boxes and never submits; work authorization, sponsorship
and voluntary disclosures are used only if you chose an answer there.

## Decisions on the page

When plain text matching is not enough — which button advances this form,
does this page confirm the submission — the extension asks the backend
(`POST /api/v1/extension/device/decide`), which answers with built-in
heuristics. Matching a saved answer to a dropdown's options happens when the
step is planned (exact text first, then the configured decision model,
TypeSafe **Jev** or a self-hosted **Laya**, above a confidence threshold);
an answer that matches no option is left for you, never forced.

## Please read

Automating job applications may breach some sites' terms of service —
LinkedIn's User Agreement, for example, restricts automated activity. You
stay responsible for how you use this: every submission is approved by you
(or by the auto-submit setting you turned on, never on LinkedIn or Naukri),
the extension paces its actions like a person, and it never solves CAPTCHAs.

## Permissions

- `storage`, `alarms` — pairing, the active application and a 30-second poll.
- `tabs`, `scripting` — open the job tab and inject the assistant into it.
- `activeTab` — the review screenshot, only after you click the toolbar icon.
- `tabGroups` — keep the CareerCraft agent's tab grouped and labelled.
- Host access to CareerCraft and the supported job sites listed in
  `manifest.json`; any other site is asked for, one site at a time, when an
  application moves there. `http://localhost` and `127.0.0.1` are for a
  CareerCraft running on your own machine.

## Development

- `manifest.json` is the production manifest and grants nothing on
  `localhost`/`127.0.0.1`. For a local CareerCraft or the test harnesses, run
  `node scripts/build.mjs --dev --out /tmp/cc-ext` and load that folder.
- Sites outside the built-in job/ATS list need a one-time approval in the popup;
  switching the paired account from a web page needs a popup confirmation.
- `src/background.js` — service worker: pairing, polling, all network calls.
- `src/content/*.js` — injected into the job tab: DOM helpers, review panel
  (closed shadow DOM), platform drivers, and the runner.
- `src/content/workday.js` — the Workday steps; `src/content/autofill.js` —
  the task-free "Autofill this page"; `options.html` — the autofill profile.
- `test/fixtures/` — Greenhouse-, LinkedIn- and Naukri-like pages, plus
  `workday/full.html`, a multi-step Workday reconstruction (listboxes,
  prompts, split and calendar-only dates, hidden resume input, parsing,
  Add for work experience and education, validation, disclosures). It is
  modelled on Workday's markup, not captured from a live tenant.
- `test/autofill_rules.test.mjs` — option matching, dates, local autofill
  rules (`node --test test/autofill_rules.test.mjs`).
- `test/e2e_apply.mjs` — the whole apply flow in a real Chromium against
  local fixture job sites (Greenhouse, Lever, Workday, SmartRecruiters, Workable, an in-page Apply button the extension must leave for the person, a company page with an embedded Greenhouse form) and a fake API that
  follows the backend's status rules. Run `node test/e2e_apply.mjs`.
- `test/e2e_live.py` — end-to-end run against a local stack (see its
  docstring): real API, Temporal worker and this extension in Chromium.

CI syntax-checks every script and the manifest and runs both node tests.
