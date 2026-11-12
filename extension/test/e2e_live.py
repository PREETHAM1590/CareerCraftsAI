"""Live end-to-end check: real API + Temporal worker + this unpacked extension.

    python extension/test/e2e_live.py <greenhouse|linkedin|naukri> <application_id>

Needs a running local stack (web app on CC_APP_URL, API, Temporal server and
`python -m app.temporal_worker`), and:
  * a Chrome with remote debugging on CC_USER_CDP (default :9222) signed in
    to the web app — the script calls the API as that user;
  * the fixtures served on 127.0.0.1:8765:
        python -m http.server 8765 --bind 127.0.0.1 --directory extension/test/fixtures
    (the LinkedIn/Naukri fixtures live under linkedin.com/ and naukri.com/ so
    the backend picks the matching driver from the URL);
  * a saved job application of that user whose job_url is a fixture URL and
    whose resume_id is an uploaded PDF resume.

The review panel lives in a closed shadow root, so it is driven through the
Chrome DevTools Protocol exactly where a user would click. Nothing is ever
submitted before the panel's Submit button is pressed; the script asserts it.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

# manifest.json is the production manifest; the live run pairs with localhost.
EXT = str(Path(tempfile.mkdtemp(prefix="cc-ext-")) / "extension")
subprocess.run(
    [
        "node",
        str(Path(__file__).resolve().parents[1] / "scripts" / "build.mjs"),
        "--out",
        EXT,
        "--dev",
    ],
    check=True,
)
CHROME = os.environ.get(
    "CC_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
)
APP = os.environ.get("CC_APP_URL", "http://localhost:3000")
USER_CDP = os.environ.get("CC_USER_CDP", "http://127.0.0.1:9222")
FLOW, APPLICATION_ID = sys.argv[1], sys.argv[2]


def log(*a):
    print("[e2e]", *a, flush=True)


# ── API as the signed-in user (through the Clerk session on :9222) ──────
async def user_api(pw, method, path, body=None):
    browser = await pw.chromium.connect_over_cdp(USER_CDP)
    page = browser.contexts[0].pages[0]
    if not page.url.startswith(APP):
        await page.goto(APP + "/dashboard", wait_until="networkidle")
    status, text = await page.evaluate(
        """async ([m, p, b]) => {
            const t = await window.Clerk.session.getToken();
            const r = await fetch('/api/v1' + p, {method: m,
              headers: {'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json'},
              body: b ? JSON.stringify(b) : undefined});
            return [r.status, await r.text()];
        }""",
        [method, path, body],
    )
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


# ── Closed-shadow-DOM panel access through CDP ───────────────────────────
def walk(node):
    yield node
    for key in ("children", "shadowRoots"):
        for child in node.get(key, []) or []:
            yield from walk(child)


def attrs(node):
    a = node.get("attributes") or []
    return dict(zip(a[::2], a[1::2]))


async def panel(page, cdp):
    doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
    host = next(
        (
            n
            for n in walk(doc["root"])
            if attrs(n).get("id") == "careercraft-panel-host"
        ),
        None,
    )
    return host


async def panel_text(page, cdp):
    host = await panel(page, cdp)
    if not host:
        return ""
    obj = (
        await cdp.send("DOM.resolveNode", {"nodeId": host["shadowRoots"][0]["nodeId"]})
        if host.get("shadowRoots")
        else None
    )
    if not obj:
        return ""
    res = await cdp.send(
        "Runtime.callFunctionOn",
        {
            "objectId": obj["object"]["objectId"],
            "functionDeclaration": "function(){ const w = this.querySelector('.cc-wrap'); return w ? w.innerText.replace(/\\s+/g,' ') : '' }",
            "returnByValue": True,
        },
    )
    return res["result"].get("value") or ""


async def panel_call(cdp, selector, fn):
    """Run fn (a JS function body using `el`) on the first match inside the panel."""
    doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
    host = next(
        (
            n
            for n in walk(doc["root"])
            if attrs(n).get("id") == "careercraft-panel-host"
        ),
        None,
    )
    shadow = await cdp.send(
        "DOM.resolveNode", {"nodeId": host["shadowRoots"][0]["nodeId"]}
    )
    res = await cdp.send(
        "Runtime.callFunctionOn",
        {
            "objectId": shadow["object"]["objectId"],
            "functionDeclaration": f"function(){{ const el = this.querySelector({json.dumps(selector)}); if (!el) return 'missing'; {fn}; return 'ok'; }}",
            "returnByValue": True,
        },
    )
    return res["result"].get("value")


async def click_panel(page, cdp, selector):
    doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
    host = next(
        (
            n
            for n in walk(doc["root"])
            if attrs(n).get("id") == "careercraft-panel-host"
        ),
        None,
    )
    found = await cdp.send(
        "DOM.querySelector",
        {"nodeId": host["shadowRoots"][0]["nodeId"], "selector": selector},
    )
    box = await cdp.send("DOM.getBoxModel", {"nodeId": found["nodeId"]})
    q = box["model"]["content"]
    await page.mouse.click((q[0] + q[4]) / 2, (q[1] + q[5]) / 2)


async def wait_panel(page, cdp, needle, timeout=90):
    for _ in range(timeout * 2):
        text = await panel_text(page, cdp)
        if needle in text:
            return text
        await asyncio.sleep(0.5)
    raise AssertionError(f"panel never showed {needle!r}; last: {text[:500]!r}")


async def wait_panel_or_page(page, cdp, panel_needles, page_needle, timeout=90):
    """Whichever comes first: one of panel_needles in the panel, or
    page_needle in the page (the flow may finish without asking — e.g. when
    a saved answer from an earlier run fills the question)."""
    for _ in range(timeout * 2):
        text = await panel_text(page, cdp)
        for needle in panel_needles:
            if needle in text:
                return needle
        if await page.evaluate(
            "(n) => document.body.innerText.includes(n)", page_needle
        ):
            return page_needle
        await asyncio.sleep(0.5)
    raise AssertionError(f"neither {panel_needles!r} nor {page_needle!r} appeared")


async def main():
    async with async_playwright() as pw:
        status, paired = await user_api(
            pw, "POST", "/extension/pair", {"name": "E2E Chromium"}
        )
        assert status == 200, paired
        log("paired device", paired["device_id"])

        ctx = await pw.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="cc-ext-"),
            executable_path=CHROME,
            headless=False,
            args=[
                "--headless=new",
                f"--disable-extensions-except={EXT}",
                f"--load-extension={EXT}",
                "--no-sandbox",
            ],
            viewport={"width": 1400, "height": 900},
        )
        sw = (
            ctx.service_workers[0]
            if ctx.service_workers
            else await ctx.wait_for_event("serviceworker")
        )
        ext_id = sw.url.split("/")[2]
        popup = await ctx.new_page()
        await popup.goto(f"chrome-extension://{ext_id}/popup.html")
        await popup.fill("#app-origin", APP)
        await popup.fill("#token", paired["token"])
        await popup.click("#connect")
        await popup.wait_for_selector("#who:not(:empty)", timeout=15000)
        log("popup:", await popup.inner_text("#who"))

        status, started = await user_api(
            pw, "POST", f"/jobs/applications/{APPLICATION_ID}/prepare-apply", {}
        )
        log("prepare-apply", status, started)
        assert status == 200, started
        run_id = started["run_id"]

        await popup.click("#check")
        job = None
        for _ in range(60):
            job = next((p for p in ctx.pages if "127.0.0.1:8765" in p.url), None)
            if job:
                break
            await asyncio.sleep(0.5)
        assert job, "extension never opened the job page"
        await job.wait_for_load_state("load")
        log("opened", job.url)
        cdp = await ctx.new_cdp_session(job)

        if FLOW == "greenhouse":
            text = await wait_panel(job, cdp, "Submit application")
            log("review panel:", text[:700])
            state = await job.evaluate("""() => ({submits: window.submits,
                first: document.getElementById('first_name').value, last: document.getElementById('last_name').value,
                email: document.getElementById('email').value, resume: document.getElementById('resume').files.length,
                why: document.getElementById('why').value.slice(0, 80)})""")
            log("page before submit:", state)
            assert state["submits"] == 0, "submitted before the user approved"
            assert (
                state["email"] == "cc-sanity+clerk_test@example.com"
                and state["resume"] == 1
            )
            # The user answers what CareerCraft must never guess.
            # "missing" = already answered from a saved answer of an earlier run.
            assert await panel_call(
                cdp, '[data-field-id="sponsorship"] select', "el.value='No'"
            ) in {"ok", "missing"}
            # Consent is never remembered: the user ticks it every time.
            assert (
                await panel_call(
                    cdp,
                    '[data-field-id="consent"] input[type=checkbox]',
                    "el.checked=true",
                )
                == "ok"
            )
            await click_panel(job, cdp, '[data-role="primary"]')
            await job.wait_for_function(
                "document.body.innerText.includes('Thank you for applying')",
                timeout=30000,
            )
            sent = await job.evaluate("window.submittedData")
            log(
                "submitted form data:",
                {k: (v[:40] if isinstance(v, str) else v) for k, v in sent.items()},
            )
            assert (
                sent["sponsorship"] == "no"
                and sent["consent"] == "on"
                and sent["resume"]
            )
            assert await job.evaluate("window.submits") == 1, "submitted more than once"
        elif FLOW == "linkedin":
            seen = await wait_panel_or_page(
                job, cdp, ["needs your input", "Submit application"], "\0"
            )
            if seen == "needs your input":  # no saved answer yet for this question
                assert (
                    await panel_call(
                        cdp, '[data-field-id="yrs"] select', "el.value='3'"
                    )
                    == "ok"
                )
                await click_panel(job, cdp, '[data-role="primary"]')
                await wait_panel(job, cdp, "Submit application")
            assert (
                await job.evaluate("window.submits") == 0
            ), "submitted before the user approved"
            await click_panel(job, cdp, '[data-role="primary"]')
            await job.wait_for_function(
                "document.body.innerText.includes('Your application was sent')",
                timeout=30000,
            )
            assert await job.evaluate("window.submits") == 1
        elif FLOW == "naukri":
            await wait_panel(job, cdp, "Naukri applies with your Naukri profile")
            assert (
                await job.evaluate("window.submits") == 0
            ), "Naukri Apply clicked before approval"
            await click_panel(job, cdp, '[data-role="primary"]')
            seen = await wait_panel_or_page(
                job, cdp, ["needs your input"], "successfully applied"
            )
            if seen == "needs your input":
                assert (
                    await panel_call(
                        cdp, '[data-field-id="np"] input', "el.value='30 days'"
                    )
                    == "ok"
                )
                await click_panel(job, cdp, '[data-role="primary"]')
            await job.wait_for_function(
                "document.body.innerText.includes('successfully applied')",
                timeout=30000,
            )

        for _ in range(60):
            status, run = await user_api(pw, "GET", f"/agents/runs/{run_id}")
            if run.get("status") not in ("queued", "running"):
                break
            await asyncio.sleep(1)
        log("run:", run["status"], json.dumps(run.get("output"))[:400])
        await ctx.close()
        assert run["status"] == "completed", run
        assert run["output"]["outcome"] == "submitted", run
        log("PASS", FLOW)


asyncio.run(main())
