// CareerCraft AI — runs one application task in this tab.
// Classic script — injected last, after dom.js, panel.js and drivers.js.
// Every network call goes through the background worker (chrome.runtime
// messages); this file only orchestrates the driver and reports outcomes.
(function () {
  if (window.__ccRunnerLoaded) return;
  window.__ccRunnerLoaded = true;

  const TERMINAL = new Set(["submitted", "failed", "cancelled"]);

  // Thrown when the server says the task is no longer active (cancelled
  // from the web app, expired, or claimed elsewhere): stop quietly.
  class StopTask extends Error {}

  let runningTaskId = null;

  function send(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (response) => {
          const lastError = chrome.runtime.lastError;
          resolve(response || { error: lastError ? lastError.message : "no_response" });
        });
      } catch (e) {
        // The extension was reloaded or updated under this page.
        resolve({ error: "extension_context_invalidated" });
      }
    });
  }

  function inactive(response) {
    return response && (response.active === false || response.status === 409 || (response.data && response.data.active === false));
  }

  function makeApi(task, state) {
    return {
      async event(stage, extra) {
        const response = await send({ type: "CC_EVENT", taskId: task.id, stage, ...(extra || {}) });
        if (response.error) throw new Error(response.error);
        if (TERMINAL.has(stage)) {
          state.finished = true;
          state.outcome = stage;
          return response;
        }
        if (inactive(response)) throw new StopTask("Task is no longer active");
        if (response.error) throw new Error(response.error);
        return response;
      },
      async plan(url, fields) {
        const response = await send({ type: "CC_PLAN", taskId: task.id, url, fields });
        if (inactive(response)) throw new StopTask("Task is no longer active");
        return response.data || null;
      },
      async resume() {
        const response = await send({ type: "CC_RESUME", taskId: task.id });
        if (response.error) throw new Error(response.error);
        return response.data || null;
      },
      async answer(label, value, questionKey) {
        const response = await send({ type: "CC_ANSWER", taskId: task.id, label, value, question_key: questionKey });
        return response.data || null;
      },
      async markSubmitting(root, submit, opts) {
        const dom = window.CareerCraftDOM;
        async function snapshot() {
          if (!submit?.isConnected || submit.disabled || !dom.isVisible(submit)) throw new Error("The submit control changed. Review the form again.");
          const fields = [];
          for (const field of dom.snapshot(root).fields.filter((field) => field.visible)) {
            const el = dom.elementFor(field.id);
            let digest = null;
            if (field.type === "file" && el?.files?.length) {
              const hashes = [];
              for (const file of el.files) {
                if (file.size > 10 * 1024 * 1024) throw new Error("Review files must be smaller than 10 MB.");
                hashes.push(Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()))).map((b) => b.toString(16).padStart(2, "0")).join(""));
              }
              digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(hashes.join(","))))).map((b) => b.toString(16).padStart(2, "0")).join("");
            }
            const types = ["text", "textarea", "email", "tel", "url", "number", "date", "select", "checkbox", "radio", "file"];
            fields.push({ id: field.id.slice(0, 200), label: field.label.slice(0, 500), type: types.includes(field.type) ? field.type : "other", value: field.type === "checkbox" || field.type === "radio" ? field.checked : field.value, digest });
          }
          return { url: location.href, fields, submit_control: JSON.stringify([submit.tagName, submit.id, submit.name, submit.type, dom.textOf(submit), submit.getAttribute("aria-label"), submit.form?.action || ""]) };
        }
        const reviewed = await snapshot();
        const auto = !!(opts && opts.auto);
        const response = await send({ type: "CC_REVIEW", taskId: task.id, snapshot: reviewed, url: location.href, auto });
        if (response.error) throw new Error(response.error);
        window.CareerCraftPanel.showStatus(
          response.data && response.data.autoApproved
            ? "Auto-apply: submitting this application…"
            : "Last step: click the CareerCraft icon in your browser toolbar and press Submit this application. Nothing is sent until you do."
        );
        const deadline = Date.now() + 5 * 60 * 1000;
        while (Date.now() < deadline) {
          await dom.delay(800, 1200);
          const status = await send({ type: "CC_SUBMIT_STATUS", taskId: task.id });
          if (status.error) throw new Error(status.error);
          if (!status.approved) continue;
          await dom.delay(300, 800);
          const current = await snapshot();
          if (JSON.stringify(current) !== JSON.stringify(reviewed)) throw new Error("The form changed after review. Nothing was clicked; verify before retrying.");
          const claim = await send({ type: "CC_SUBMIT_STATUS", taskId: task.id, consume: true, snapshot: current });
          if (!claim.ok) throw new Error(claim.error || "Submission approval is unavailable.");
          return;
        }
        throw new Error("Review expired without submission approval.");
      },
      async navigate(to) {
        const response = await send({ type: "CC_NAVIGATE", taskId: task.id, url: location.href, to });
        if (response.error) throw new Error(response.error);
        return response;
      },
      async decide(stateText, questions) {
        const response = await send({ type: "CC_DECIDE", taskId: task.id, state: stateText, questions });
        return response.data || null;
      },
    };
  }

  const FINAL_MESSAGES = {
    submitted: "Application submitted. You can close this tab.",
    failed: "This application could not be completed — see CareerCraft for details.",
    cancelled: "Application cancelled.",
  };

  async function run(task, submitting, autoApply) {
    const dom = window.CareerCraftDOM;
    const panel = window.CareerCraftPanel;
    const drivers = window.CareerCraftDrivers;
    const state = { finished: false, outcome: null };
    const api = makeApi(task, state);
    const ctx = { task, api, panel, autoApply: !!autoApply, delay: () => dom.delay(250, 800) };
    const subtitle = [task.company, task.role].filter(Boolean).join(" · ");

    panel.showStatus("Preparing your application…", subtitle);
    try {
      // Let client-rendered job pages (LinkedIn, Naukri) finish rendering.
      await dom.delay(1200, 1800);
      let outcome;
      if (submitting) {
        await drivers.confirmAfterNavigation(ctx);
      } else {
        outcome = await drivers.select(task).run(ctx);
      }
      if (outcome === drivers.NAVIGATING) {
        // Heading to the application form; the next page picks it up.
        panel.showStatus("Opening the application form…", subtitle);
        return;
      }
      if (!state.finished) {
        await api.event("failed", { error: "The application flow ended without a result" });
      }
    } catch (e) {
      if (e instanceof StopTask) {
        panel.hide();
        return;
      }
      if (!state.finished) {
        const reason = (e && e.message ? e.message : String(e)).slice(0, 300);
        try { await api.event("failed", { error: reason }); } catch { /* Preserve the original error for the user. */ }
        panel.showStatus(reason, subtitle);
        return;
      }
    }

    if (state.outcome === "submitted") {
      panel.showStatus(FINAL_MESSAGES.submitted, subtitle);
      setTimeout(() => panel.hide(), 6000);
    } else if (state.outcome) {
      panel.showStatus(FINAL_MESSAGES[state.outcome], subtitle);
      setTimeout(() => panel.hide(), 4000);
    } else {
      panel.hide();
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== "RUN_TASK" || !message.task) return false;
    // One run per document: a duplicate "complete" event re-sends RUN_TASK.
    if (runningTaskId === message.task.id) {
      sendResponse({ ok: true, alreadyRunning: true });
      return false;
    }
    runningTaskId = message.task.id;
    window.__ccTaskRunning = true; // autofill.js stays out of a running application
    sendResponse({ ok: true });
    run(message.task, !!message.submitting, !!message.autoApply).finally(() => {
      runningTaskId = null;
      window.__ccTaskRunning = false;
    });
    return false;
  });
})();
