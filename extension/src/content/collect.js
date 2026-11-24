// CareerCraft AI — read-only job-listing capture for Naukri, Instahyre and
// LinkedIn. Runs on the job board, parses the listing open in the member's
// browser, and offers it to the background worker. Nothing is typed, clicked
// or submitted: capture only.
(function () {
  if (window.__ccCollectLoaded) return;
  window.__ccCollectLoaded = true;

  const BOARD = (() => {
    const host = location.hostname.replace(/^www\./, "");
    if (host.endsWith("naukri.com")) return "naukri";
    if (host.endsWith("instahyre.com")) return "instahyre";
    if (host.endsWith("linkedin.com")) return "linkedin";
    return null;
  })();
  if (!BOARD) return;

  function text(el) {
    return el ? el.textContent.replace(/\s+/g, " ").trim() : "";
  }

  function first(selectors) {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  // The listing URL the member is actually looking at, with tracking noise
  // (utm_*, jobId=… / jobId- paths kept minimal) stripped where safe.
  function listingUrl() {
    const url = new URL(location.href);
    for (const key of [...url.searchParams.keys()]) {
      if (/^(utm_|ref|src)/i.test(key)) url.searchParams.delete(key);
    }
    return url.toString();
  }

  function parseNaukri() {
    const title = text(first(["h1.job-title-heading", 'h1[class*="job-title"]', "h1"]));
    const company = text(
      first(['a[href*="/careers/"] .comp-name', ".comp-name", 'a[href*="jobs-badge"] .comp-name'])
    );
    const location = text(first(['.loc', '.location', '[class*="job-location"]']));
    const description = text(first(['.job-desc', '[class*="job-description"]', "#job_description"]));
    const salary = text(first(['.salary', '[class*="salary"]']));
    const posted = text(first(['.publish-on', '[class*="posted"]', '[class*="job-posted"]']));
    const skills = [...document.querySelectorAll('.key-skill span, [class*="keyword"] li')]
      .map((el) => text(el))
      .filter(Boolean)
      .slice(0, 30);
    return { title, company, location, description, salary, posted_at: posted, skills };
  }

  function parseInstahyre() {
    const title = text(first(["h1.title", ".job-title", "h1"]));
    const company = text(first([".company-name a", ".company-name", ".company a"]));
    const location = text(first([".job-location", ".location"]));
    const description = text(first([".job-details", ".description", "#description"]));
    const posted = text(first([".job-date", ".posted-date", ".date"]));
    const skills = [...document.querySelectorAll(".skills .skill, .skill-tags span")]
      .map((el) => text(el))
      .filter(Boolean)
      .slice(0, 30);
    return { title, company, location, description, posted_at: posted, skills };
  }

  function parseLinkedin() {
    const title = text(first(["h1.top-card-layout__title", ".job-details-jobs-unified-top-card__job-title", "h1"]));
    const company = text(
      first([".topcard__org-name-link", ".job-details-jobs-unified-top-card__company-name a"])
    );
    const location = text(
      first([".topcard__flavor--bullet", ".job-details-jobs-unified-top-card__bullet"])
    );
    const description = text(first([".description__text", ".jobs-description__content"]));
    const posted = text(first([".job-details-jobs-unified-top-card__posted-date", ".posted-time-ago__text"]));
    return { title, company, location, description, posted_at: posted, skills: [] };
  }

  const PARSERS = { naukri: parseNaukri, instahyre: parseInstahyre, linkedin: parseLinkedin };

  function scrape() {
    const payload = PARSERS[BOARD]();
    payload.url = listingUrl();
    payload.board = BOARD;
    return payload;
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === "CC_COLLECT_JOB") {
      const payload = scrape();
      sendResponse(payload.title ? { ok: true, job: payload } : { ok: false, error: "No job listing found on this page" });
    }
    return undefined;
  });
})();
