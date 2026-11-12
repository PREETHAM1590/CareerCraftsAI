"""
conftest.py — E2E test fixtures for CareerCraft AI Playwright browser tests.

Requirements before running:
    - Full Docker Compose stack running (frontend, backend, worker, redis)
    - pip install pytest-playwright playwright httpx
    - playwright install chromium
    - Environment variables set: TEST_EMAIL, TEST_PASSWORD, TEST_JWT, TEST_JOB_URL
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page

E2E_DIR = Path(__file__).parent
SCREENSHOT_DIR = E2E_DIR / "screenshots"
VIDEO_DIR = E2E_DIR / "videos"
AUTH_DIR = E2E_DIR / ".auth"
AUTH_STATE_PATH = AUTH_DIR / "state.json"
ARTIFACT_DIR = E2E_DIR / ".artifacts"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# Vars required for a real (RUN_LIVE_E2E=1) authenticated live run, distinct
# from the legacy RUN_E2E prerequisite check above.
REQUIRED_LIVE_VARS = ("TEST_JWT", "TEST_EMAIL", "TEST_PASSWORD", "WEB_URL", "API_URL")


def _is_live_run() -> bool:
    return os.getenv("RUN_LIVE_E2E") == "1"


def _missing_live_vars() -> list[str]:
    return [var for var in REQUIRED_LIVE_VARS if not os.getenv(var)]


# ── Environment Validation ──────────────────────────────────────────────────

def _check_env_vars() -> list[str]:
    missing = []
    for var in ("TEST_EMAIL", "TEST_PASSWORD", "TEST_JWT"):
        if not os.getenv(var):
            missing.append(var)
    return missing

def _check_backend_health() -> tuple[bool, str]:
    api_url = os.getenv("API_URL", "http://localhost:8000/api/v1")
    # /health is served at the app root, not under the /api/v1 prefix API_URL
    # includes (backend/app/main.py's @app.get("/health") has no router
    # prefix) — strip the suffix before appending /health.
    health_url = f"{api_url.removesuffix('/api/v1')}/health"
    try:
        r = httpx.get(health_url, timeout=10.0)
        if r.status_code == 200 and r.json().get("status") == "ok":
            return True, ""
        return False, f"/health returned {r.status_code}: {r.text[:200]}"
    except httpx.ConnectError:
        return False, f"Backend not reachable at {health_url}. Run: docker compose up -d"
    except Exception as e:
        return False, str(e)

def pytest_configure(config):
    E2E_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    VIDEO_DIR.mkdir(exist_ok=True)
    ARTIFACT_DIR.mkdir(exist_ok=True)


def pytest_collection_modifyitems(config, items):
    if config.option.collectonly:
        return
    run_e2e = os.getenv("RUN_E2E", "").lower() in ("1", "true", "yes")
    if not run_e2e:
        missing = _check_env_vars()
        health_ok, health_msg = (False, "Prerequisites missing") if missing else _check_backend_health()

        if missing or not health_ok:
            reasons = []
            if missing:
                reasons.append(f"Missing env vars: {', '.join(missing)}")
            if not health_ok:
                reasons.append(f"Backend: {health_msg}")

            skip_msg = "E2E prerequisite check failed:\n  " + "\n  ".join(reasons)
            skip_msg += "\n\nSet environment variables and start services: docker compose up -d"
            skip_msg += "\nThen run with: RUN_E2E=1 pytest backend/tests/e2e/ -v -s --headed"

            for item in items:
                # test_harness_contract.py verifies the harness itself (route
                # manifest completeness, safety-fixture defaults) and must run
                # without live credentials or a running backend/browser.
                if "test_harness_contract" in str(item.fspath):
                    continue
                # test_hermetic_* run the real routes against stubs and a local
                # throwaway PostgreSQL: no credentials, no running stack.
                if "test_hermetic_" in str(item.fspath):
                    continue
                if "e2e" in str(item.fspath) or any(
                    marker.name in ("e2e", "browser", "integration")
                    for marker in item.own_markers
                ):
                    item.add_marker(pytest.mark.skip(reason=skip_msg))


# ── API Client ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def api_client():
    jwt = os.getenv("TEST_JWT", "")
    api_url = os.getenv("API_URL", "http://localhost:8000/api/v1")
    client = httpx.Client(
        base_url=api_url,
        headers={
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
        },
        timeout=120.0,
    )
    yield client
    client.close()


# ── Live-run Safety Gate ─────────────────────────────────────────────────────
# Human-in-the-loop protection: any test that would send an email, message a
# recruiter, or submit a job application must check `live_safety` first and
# skip/no-op the destructive step unless the operator opted in explicitly.

@dataclass(frozen=True)
class LiveSafety:
    allow_external_writes: bool


@pytest.fixture(scope="session")
def live_safety() -> LiveSafety:
    return LiveSafety(allow_external_writes=os.getenv("ALLOW_LIVE_SENDS") == "1")


# ── Agent Run Polling ────────────────────────────────────────────────────────

def _wait_for_terminal(client: httpx.Client, run_id: str, timeout_s: int = 120) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(f"/agents/runs/{run_id}")
        response.raise_for_status()
        data = response.json()
        if data["status"] in {"completed", "awaiting_approval", "failed", "expired"}:
            return data
        time.sleep(1)
    raise AssertionError(f"run {run_id} did not reach terminal status within {timeout_s}s")


@pytest.fixture
def wait_for_run():
    """Callable fixture: wait_for_run(api_client, run_id, timeout_s=120) -> dict."""
    return _wait_for_terminal


# ── Artifact Directory ───────────────────────────────────────────────────────

@pytest.fixture
def artifact_dir(request) -> Path:
    """Per-test directory (gitignored) for screenshots/videos/traces to land in."""
    safe_name = re.sub(r"[^\w.-]", "_", request.node.name)
    path = ARTIFACT_DIR / safe_name
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── Playwright Browser ──────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def browser_context(playwright):
    headless = os.getenv("HEADLESS", "0").lower() in ("1", "true", "yes")
    slow_mo = int(os.getenv("SLOW_MO", "500"))
    browser = playwright.chromium.launch(
        headless=headless,
        slow_mo=slow_mo,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        record_video_dir=str(VIDEO_DIR),
        record_video_size={"width": 1920, "height": 1080},
    )
    yield context
    context.close()
    browser.close()


@pytest.fixture(scope="function")
def page(browser_context, request) -> Page:
    p = browser_context.new_page()
    p.set_default_timeout(60000)
    yield p
    p.close()


# ── Authenticated Session ────────────────────────────────────────────────────
# Signs in once per test session through the real Clerk UI (see
# scripts/e2e_browser_login.py for the interactive reference script) and
# persists the storage state so later tests reuse it instead of re-logging in.

def _clerk_login(page: Page, web_url: str, email: str, password: str, otp_code: str) -> None:
    page.goto(f"{web_url}/login", wait_until="domcontentloaded", timeout=60000)
    page.fill("input[type=email]", email)

    password_input = page.query_selector("input[type=password]")
    if password_input:
        password_input.fill(password)
    page.click("button[type=submit]")
    page.wait_for_timeout(3000)

    code_input = (
        page.query_selector("input[name=code]")
        or page.query_selector("input[inputmode=numeric]")
        or page.query_selector("input[maxlength='8']")
    )
    if code_input:
        code_input.fill(otp_code)
        submit_button = page.query_selector("button[type=submit]")
        if submit_button and submit_button.is_enabled():
            submit_button.click()
        else:
            page.keyboard.press("Enter")

    page.wait_for_function("!!(window.Clerk && window.Clerk.user)", timeout=30000)


@pytest.fixture(scope="session")
def _auth_state(playwright) -> Path:
    if not _is_live_run():
        pytest.skip("Set RUN_LIVE_E2E=1 to run authenticated live E2E tests")

    missing = _missing_live_vars()
    if missing:
        pytest.fail(f"RUN_LIVE_E2E=1 but missing required env vars: {', '.join(missing)}")

    web_url = os.environ["WEB_URL"]
    otp_code = os.getenv("TEST_OTP_CODE", "424242")
    headless = os.getenv("HEADLESS", "1").lower() in ("1", "true", "yes")

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    login_page = context.new_page()
    try:
        _clerk_login(login_page, web_url, os.environ["TEST_EMAIL"], os.environ["TEST_PASSWORD"], otp_code)
        context.storage_state(path=str(AUTH_STATE_PATH))
    finally:
        context.close()
        browser.close()

    return AUTH_STATE_PATH


@pytest.fixture(scope="session")
def _authenticated_context(playwright, _auth_state):
    headless = os.getenv("HEADLESS", "1").lower() in ("1", "true", "yes")
    browser = playwright.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        storage_state=str(_auth_state),
        viewport={"width": 1920, "height": 1080},
        record_video_dir=str(VIDEO_DIR),
    )
    yield context
    context.close()
    browser.close()


@pytest.fixture
def authenticated_page(_authenticated_context) -> Page:
    p = _authenticated_context.new_page()
    p.set_default_timeout(60000)
    yield p
    p.close()


@pytest.fixture
def authenticated_mobile_page(_authenticated_context) -> Page:
    """Same authenticated session as `authenticated_page`, at a mobile
    viewport (390x844) for responsive screen smoke coverage."""
    p = _authenticated_context.new_page()
    p.set_default_timeout(60000)
    p.set_viewport_size({"width": 390, "height": 844})
    yield p
    p.close()


# ── Screenshot Helper ──────────────────────────────────────────────────────

def screenshot(page: Page, test_name: str, step: str) -> None:
    path = SCREENSHOT_DIR / test_name
    path.mkdir(exist_ok=True)
    filepath = str(path / f"{step}.png")
    page.screenshot(path=filepath, full_page=True)
    print(f"📸 Screenshot: {test_name}/{step}.png")


# ── SSE Stream Helper ──────────────────────────────────────────────────────

def wait_for_sse_complete(
    api_client: httpx.Client,
    run_id: str,
    timeout_s: int = 120,
) -> dict:
    checkpoint_seen = False
    events: list[dict] = []
    result: dict = {}
    start = time.time()

    with api_client.stream("GET", f"/agents/{run_id}/stream") as response:
        for line in response.iter_lines():
            if time.time() - start > timeout_s:
                raise TimeoutError(
                    f"Agent run {run_id} did not complete within {timeout_s}s"
                )

            if line.startswith("event: "):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data: "):
                payload_str = line.split(":", 1)[1].strip()
                try:
                    payload = __import__("json").loads(payload_str)
                except Exception:
                    payload = {"raw": payload_str}
                events.append({"type": event_type, "payload": payload})
                print(f"  SSE [{event_type}]: {str(payload)[:120]}")

                if event_type == "complete":
                    result = payload
                    break
                if event_type == "checkpoint":
                    checkpoint_seen = True
                    result = {"checkpoint": payload, "run_id": run_id}
                    break
                if event_type == "error":
                    raise RuntimeError(
                        f"Agent error: {payload.get('message', 'Unknown error')}"
                    )

    return {"result": result, "events": events, "checkpoint_seen": checkpoint_seen}
