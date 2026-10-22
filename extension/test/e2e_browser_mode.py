"""Full-chain browser-mode check: real backend + real extension, no fake API.

computer_service.dispatch/act (this process) -> Redis -> uvicorn device routes
-> the extension's command loop in Playwright Chromium -> the agent tab.

Needs Docker (disposable Postgres/Redis from docker-compose.test.yml), the
backend venv and a Playwright Chromium. Opens real public sites
(example.com, boards.greenhouse.io/gitlab, iana.org).

    backend/.venv/Scripts/python extension/test/e2e_browser_mode.py [--keep-infra]
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PY = sys.executable
DB_URL = "postgresql://workflow_test:workflow_test@127.0.0.1:55439/applications_test"
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "careercraft-workflow-tests",
    "-f",
    str(ROOT / "docker-compose.test.yml"),
]

ENV = {
    "APP_ENV": "development",
    "APP_SECRET_KEY": "e2e-browser-mode-not-a-secret-0123456789abcdef",
    "DATABASE_URL": DB_URL.replace("postgresql://", "postgresql+asyncpg://"),
    "REDIS_URL": "redis://127.0.0.1:56379/0",
    "TEMPORAL_ENABLED": "false",
    "NANGO_ENABLED": "false",
    "SANDBOX_RELAY_URL": "",
    "SANDBOX_RELAY_TOKEN": "",
}
# Before any app import: settings and the DB engine are built at import time.
# The working directory must not hold backend/.env (pydantic loads ./.env).
os.environ.update(ENV)
sys.path.insert(0, str(BACKEND))

results = []


def record(step, outcome, ms=None):
    results.append((step, outcome, ms))
    print(
        f"{step:<44} {f'{ms:>6} ms' if ms is not None else '         '}  {outcome}",
        flush=True,
    )


def infra_up():
    subprocess.run([*COMPOSE, "up", "-d", "postgres", "redis"], check=True)
    for _ in range(40):
        if (
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "careercraft-workflow-tests-postgres-1",
                    "pg_isready",
                    "-U",
                    "workflow_test",
                ],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        ):
            break
        time.sleep(2)
    subprocess.run(
        [
            "docker",
            "exec",
            "careercraft-workflow-tests-postgres-1",
            "psql",
            "-U",
            "workflow_test",
            "-c",
            "CREATE DATABASE applications_test",
        ],
        capture_output=True,
        check=False,
    )  # exists on rerun
    subprocess.run(
        [PY, str(ROOT / "scripts/migrate.py"), "--database-url", DB_URL, "--bootstrap"],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def extension_copy(tmp):
    """The extension with https://example.com granted up front: optional host
    permissions need a user gesture, which a harness cannot give."""
    dst = Path(tmp) / "extension"
    shutil.copytree(ROOT / "extension", dst, ignore=shutil.ignore_patterns("test"))
    manifest = json.loads((dst / "manifest.json").read_text(encoding="utf8"))
    manifest["host_permissions"].append("https://example.com/*")
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    return dst


async def timed(coro):
    started = time.monotonic()
    try:
        return await coro, None, int((time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - record every failure
        return None, exc, int((time.monotonic() - started) * 1000)


def describe(exc):
    return f"{getattr(exc, 'status_code', type(exc).__name__)} {getattr(exc, 'detail', exc)}"


async def main():
    import logging

    from app.core.database import AsyncSessionLocal
    from app.models.db import ExtensionDevice, User
    from app.services import computer_service as cs
    from app.services.computer_service import ComputerAction
    from app.services.extension_service import pair_device
    from playwright.async_api import async_playwright
    from sqlalchemy import update

    logging.getLogger("sqlalchemy.engine.Engine").setLevel(
        logging.WARNING
    )  # APP_ENV=development echoes SQL

    failures = []

    def expect(step, ok, outcome, ms=None):
        record(step, ("PASS " if ok else "FAIL ") + outcome, ms)
        if not ok:
            failures.append(step)

    # ── user + paired device (the backend's own pairing) ──
    async with AsyncSessionLocal() as db:
        user = User(
            email=f"browser-mode-{int(time.time())}@example.com",
            full_name="E2E",
            computer_mode="browser",
            policy_accepted_at=datetime.now(UTC),
            policy_version="2026-09-26",
        )
        db.add(user)
        await db.commit()
        user_id = user.id
        device, token = await pair_device(db, user_id, "e2e Chromium")
        device_id = device.id

    port = free_port()
    workdir = tempfile.mkdtemp()
    # Blocking setup calls are fine here: they run once, before any browser work.
    log_path = Path(workdir) / "uvicorn.log"
    log = open(log_path, "wb")  # noqa: ASYNC230, SIM115 - closed below
    server = subprocess.Popen(  # noqa: ASYNC220
        [
            PY,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            str(BACKEND),
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=workdir,
        env={**os.environ, **ENV},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    print(f"uvicorn log: {Path(workdir) / 'uvicorn.log'}")
    try:
        import httpx

        for _ in range(60):
            try:
                health = f"http://127.0.0.1:{port}/health/live"
                if httpx.get(health, timeout=1).status_code == 200:  # noqa: ASYNC210
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)

        offline = await cs.dispatch(user_id, "status")
        expect(
            "status before the browser connects",
            offline.get("running") is False and offline.get("backend") == "browser",
            json.dumps(offline),
        )
        _, exc, ms = await timed(cs.dispatch(user_id, "start"))
        expect(
            "start while offline -> 503",
            getattr(exc, "status_code", 0) == 503
            and "isn't connected" in str(exc.detail),
            describe(exc),
            ms,
        )

        chrome = next(
            (Path(os.environ["LOCALAPPDATA"]) / "ms-playwright").glob(
                "chromium-*/chrome-win64/chrome.exe"
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            ext = extension_copy(tmp)
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    str(Path(tmp) / "profile"),
                    executable_path=str(chrome),
                    headless=False,
                    args=[
                        f"--disable-extensions-except={ext}",
                        f"--load-extension={ext}",
                    ],
                )
                worker = (
                    context.service_workers[0]
                    if context.service_workers
                    else await context.wait_for_event("serviceworker")
                )
                await worker.evaluate(
                    "p => chrome.storage.local.set({pairing: p})",
                    {"appOrigin": f"http://127.0.0.1:{port}", "token": token},
                )
                # Fire the poll alarm now: it (re)starts the command loop.
                await worker.evaluate(
                    "chrome.alarms.create('poll', {when: Date.now() + 100, periodInMinutes: 0.5})"
                )
                started = time.monotonic()
                while (await cs.computer_backend(user_id))[
                    "device"
                ] is None and time.monotonic() - started < 20:
                    await asyncio.sleep(0.25)
                status = await cs.dispatch(user_id, "status")
                expect(
                    "status: backend=browser, running",
                    status
                    == {
                        "available": True,
                        "backend": "browser",
                        "running": True,
                        "enabled": True,
                    },
                    json.dumps(status),
                    int((time.monotonic() - started) * 1000),
                )

                out, exc, ms = await timed(cs.dispatch(user_id, "start"))
                expect(
                    "start",
                    out == {"running": True, "enabled": True},
                    json.dumps(out) if out else describe(exc),
                    ms,
                )

                async def act(
                    operation, actor="agent", snap=None, approved=True, **parameters
                ):
                    action = ComputerAction(
                        operation=operation,
                        parameters=parameters,
                        computer_run=snap and snap["computer_run"],
                        approved_snapshot=snap and approved and snap["snapshotId"],
                    )
                    return await timed(cs.act(user_id, action, actor=actor))

                snap, exc, ms = await act("snapshot")
                expect(
                    "snapshot on about:blank (empty)",
                    bool(snap)
                    and snap["elements"] == []
                    and snap["url"] == "about:blank",
                    (
                        f"snapshotId={snap and snap['snapshotId']} elements={snap and len(snap['elements'])}"
                        if snap
                        else describe(exc)
                    ),
                    ms,
                )

                out, exc, ms = await act(
                    "navigate", snap=snap, url="https://example.com/"
                )
                expect(
                    "navigate (agent) https://example.com",
                    bool(out)
                    and out["url"].startswith("https://example.com")
                    and "Example Domain" in out["title"],
                    (
                        f"{out['url']} title={out['title']!r} text={len(out['text'])} chars"
                        if out
                        else describe(exc)
                    ),
                    ms,
                )

                snap, exc, ms = await act("snapshot")
                expect(
                    "snapshot example.com",
                    bool(snap) and any(e["role"] == "link" for e in snap["elements"]),
                    (
                        f"snapshotId={snap['snapshotId']} {snap['elements']}"
                        if snap
                        else describe(exc)
                    ),
                    ms,
                )

                out, exc, ms = await act("read")
                expect(
                    "read example.com",
                    bool(out) and "documentation examples" in out["text"],
                    f"{len(out['text'])} chars" if out else describe(exc),
                    ms,
                )

                out, exc, ms = await act(
                    "navigate",
                    snap=snap,
                    url="https://www.iana.org/help/example-domains",
                )
                expect(
                    "navigate ungranted origin -> 403 permission_required",
                    getattr(exc, "status_code", 0) == 403
                    and "https://www.iana.org" in str(exc.detail),
                    describe(exc) if exc else json.dumps(out),
                    ms,
                )
                needed = await worker.evaluate(
                    "chrome.storage.session.get('hostPermissionNeeded')"
                )
                expect(
                    "  permission prompt flow armed",
                    needed.get("hostPermissionNeeded") == "www.iana.org",
                    json.dumps(needed),
                )

                out, exc, ms = await act(
                    "navigate", snap=snap, url="https://boards.greenhouse.io/gitlab"
                )
                expect(
                    "navigate (agent) boards.greenhouse.io/gitlab",
                    bool(out) and "greenhouse.io" in out["url"],
                    (
                        f"{out['url']} title={out['title']!r} text={len(out['text'])} chars"
                        if out
                        else describe(exc)
                    ),
                    ms,
                )

                snap, exc, ms = await act("snapshot")
                links = [
                    e
                    for e in (snap or {}).get("elements", [])
                    if e["role"] == "link" and e["name"]
                ]
                expect(
                    "snapshot job board with refs",
                    len(links) > 3,
                    (
                        f"snapshotId={snap['snapshotId']} elements={len(snap['elements'])} e.g. {links[:3]}"
                        if snap
                        else describe(exc)
                    ),
                    ms,
                )

                target = next(
                    (e for e in links if len(e["name"]) > 12),
                    links[0] if links else None,
                )
                if not target or not snap:
                    raise SystemExit("no snapshot of the job board; cannot continue")
                out, exc, ms = await act(
                    "click", snap=snap, ref=target["ref"], snapshotId=snap["snapshotId"]
                )
                expect(
                    f"click {target['ref']} {target['name'][:30]!r}",
                    bool(out) and out.get("ok") is True,
                    json.dumps(out) if out else describe(exc),
                    ms,
                )

                out, exc, ms = await act(
                    "click", snap=snap, ref=target["ref"], snapshotId=snap["snapshotId"]
                )
                expect(
                    "click again on the spent snapshot -> 409",
                    getattr(exc, "status_code", 0) == 409
                    and "take a new snapshot" in str(exc.detail),
                    describe(exc) if exc else json.dumps(out),
                    ms,
                )

                _, exc, ms = await timed(
                    cs.dispatch(
                        user_id,
                        "action",
                        {"operation": "screenshot", "parameters": {}, "actor": "human"},
                    )
                )
                expect(
                    "screenshot -> 409 contract text",
                    getattr(exc, "status_code", 0) == 409
                    and exc.detail == cs.BROWSER_UNSUPPORTED,
                    describe(exc),
                    ms,
                )
                await asyncio.sleep(1.5)  # let the click's navigation settle
                page = next((p for p in context.pages if "greenhouse" in p.url), None)
                banner = page and await page.evaluate(
                    "!!document.getElementById('careercraft-agent-banner')"
                )
                expect(
                    "banner visible in the agent tab",
                    bool(banner),
                    f"{page and page.url}",
                )

                out, exc, ms = await timed(cs.dispatch(user_id, "stop"))
                expect(
                    "stop",
                    out == {"running": False, "enabled": False},
                    json.dumps(out) if out else describe(exc),
                    ms,
                )
                out, exc, ms = await act("read")
                expect(
                    "read after stop -> 409 tab closed",
                    getattr(exc, "status_code", 0) == 409
                    and "closed" in str(exc.detail),
                    describe(exc) if exc else json.dumps(out),
                    ms,
                )
                await context.close()

        # Browser gone, but agent_seen_at is still fresh (< 90 s): the command
        # waits for a reply that never comes.
        out, exc, ms = await timed(cs.dispatch(user_id, "start"))
        expect(
            "browser closed, still 'online' -> 504",
            getattr(exc, "status_code", 0) == 504
            and "didn't respond" in str(exc.detail),
            describe(exc) if exc else json.dumps(out),
            ms,
        )
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ExtensionDevice)
                .where(ExtensionDevice.id == device_id)
                .values(agent_seen_at=datetime.now(UTC) - timedelta(seconds=120))
            )
            await db.commit()
        snapless = ComputerAction(operation="read")
        out, exc, ms = await timed(cs.act(user_id, snapless, actor="agent"))
        expect(
            "device offline (> 90 s) -> 503",
            getattr(exc, "status_code", 0) == 503 and exc.detail == cs.BROWSER_OFFLINE,
            describe(exc) if exc else json.dumps(out),
            ms,
        )
        status = await cs.dispatch(user_id, "status")
        expect("status offline", status.get("running") is False, json.dumps(status))
    finally:
        server.terminate()
        server.wait(10)
        log.close()
    return failures


if __name__ == "__main__":
    keep = "--keep-infra" in sys.argv
    infra_up()
    try:
        failed = asyncio.run(main())
    finally:
        if not keep:
            subprocess.run([*COMPOSE, "down"], check=False)
    print(
        f"\n{len(results) - len(failed)}/{len(results)} passed"
        + (f"; FAILED: {failed}" if failed else "")
    )
    sys.exit(1 if failed else 0)
