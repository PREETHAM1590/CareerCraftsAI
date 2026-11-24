"""Real MV3 popup/background approval check; controlled API, no applications."""

import asyncio
import json
import os
import subprocess
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

approvals = []


class API(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"device_name":"fixture","user":{"name":"Test"}}')

    def do_POST(self):
        if self.path.endswith("approve-submit"):
            approvals.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "submission_token": "fixture-permit",
                        "expires_at": (
                            datetime.now(UTC) + timedelta(minutes=5)
                        ).isoformat(),
                    }
                ).encode()
            )
        else:
            self.send_response(204)
            self.end_headers()


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), API)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = Path(__file__).resolve().parents[1]
    extension = Path(tempfile.mkdtemp(prefix="cc-ext-")) / "extension"
    # manifest.json is the production manifest; the gate pairs with localhost.
    await asyncio.to_thread(
        subprocess.run,
        ["node", str(root / "scripts" / "build.mjs"), "--out", str(extension), "--dev"],
        check=True,
    )
    chrome = (
        Path(os.environ["LOCALAPPDATA"])
        / "ms-playwright/chromium-1243/chrome-win64/chrome.exe"
    )
    if not chrome.exists():
        chrome = next(
            (Path(os.environ["LOCALAPPDATA"]) / "ms-playwright").glob(
                "chromium-*/chrome-win64/chrome.exe"
            )
        )
    with tempfile.TemporaryDirectory() as profile:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                profile,
                executable_path=str(chrome),
                headless=False,
                args=[
                    f"--disable-extensions-except={extension}",
                    f"--load-extension={extension}",
                    "--remote-debugging-port=9228",
                ],
            )
            worker = (
                context.service_workers[0]
                if context.service_workers
                else await context.wait_for_event("serviceworker")
            )
            eid = worker.url.split("/")[2]
            await context.route(
                "https://jobs.lever.co/**",
                lambda route: route.fulfill(
                    body='<input id="name" value="Test"><button id="submit">Apply</button>'
                ),
            )
            job = await context.new_page()
            await job.goto("https://jobs.lever.co/fixture/job")
            tabs = await worker.evaluate("chrome.tabs.query({})")
            tabid = next(t["id"] for t in tabs if t.get("url") == job.url)
            snapshot = {
                "url": job.url,
                "fields": [{"id": "name", "label": "Name", "value": "Test"}],
            }
            active = {
                "taskId": "fixture",
                "tabId": tabid,
                "stage": "awaiting_approval",
                "task": {"job_url": job.url, "company": "Fixture", "role": "Engineer"},
                "review": {
                    "review_hash": "hash",
                    "expires_at": (
                        datetime.now(UTC) + timedelta(minutes=5)
                    ).isoformat(),
                    "snapshot": snapshot,
                },
            }
            await worker.evaluate(
                """async ({pairing,active,tabid})=>{
                await chrome.storage.local.set({pairing}); await chrome.storage.session.set({activeTask:active});
                await chrome.scripting.executeScript({target:{tabId:tabid},func:()=>chrome.runtime.onMessage.addListener((msg,sender,reply)=>{if(msg.test){chrome.runtime.sendMessage(msg.test).then(reply);return true;}})});
            }""",
                {
                    "pairing": {
                        "appOrigin": f"http://localhost:{server.server_port}",
                        "token": "ccx_fixture",
                    },
                    "active": active,
                    "tabid": tabid,
                },
            )

            async def content(msg):
                return await worker.evaluate(
                    "({tabid,msg})=>chrome.tabs.sendMessage(tabid,{test:msg})",
                    {"tabid": tabid, "msg": msg},
                )

            assert (
                await content(
                    {
                        "type": "CC_SUBMIT_STATUS",
                        "taskId": "fixture",
                        "consume": True,
                        "snapshot": snapshot,
                    }
                )
            ).get("waiting")
            assert "error" in await content(
                {"type": "CC_APPROVE_SUBMIT", "taskId": "fixture", "reviewHash": "hash"}
            )
            assert not approvals
            await worker.evaluate("chrome.action.openPopup()")
            import httpx
            import websockets

            targets = (
                await asyncio.to_thread(httpx.get, "http://127.0.0.1:9228/json/list")
            ).json()
            popup_target = next(
                t
                for t in targets
                if t.get("url") == f"chrome-extension://{eid}/popup.html"
            )
            async with websockets.connect(
                popup_target["webSocketDebuggerUrl"]
            ) as socket:
                counter = 0

                async def cdp(method, params):
                    nonlocal counter
                    counter += 1
                    await socket.send(
                        json.dumps({"id": counter, "method": method, "params": params})
                    )
                    while True:
                        result = json.loads(await socket.recv())
                        if result.get("id") == counter:
                            return result.get("result", {})

                await asyncio.sleep(1)
                rect = await cdp(
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify(document.querySelector('#approve-submit').getBoundingClientRect().toJSON())",
                        "returnByValue": True,
                    },
                )
                rect = json.loads(rect["result"]["value"])
                assert rect["width"] > 0
                point = {
                    "x": rect["x"] + rect["width"] / 2,
                    "y": rect["y"] + rect["height"] / 2,
                    "button": "left",
                    "clickCount": 1,
                }
                await cdp("Input.dispatchMouseEvent", {"type": "mousePressed", **point})
                await cdp(
                    "Input.dispatchMouseEvent", {"type": "mouseReleased", **point}
                )
                await asyncio.sleep(1)
            assert len(approvals) == 1
            assert "error" in await content(
                {
                    "type": "CC_SUBMIT_STATUS",
                    "taskId": "fixture",
                    "consume": True,
                    "snapshot": {"url": job.url, "fields": []},
                }
            )
            assert (
                await content(
                    {
                        "type": "CC_SUBMIT_STATUS",
                        "taskId": "fixture",
                        "consume": True,
                        "snapshot": snapshot,
                    }
                )
            ).get("ok")
            assert "error" in await content(
                {
                    "type": "CC_SUBMIT_STATUS",
                    "taskId": "fixture",
                    "consume": True,
                    "snapshot": snapshot,
                }
            )
            await context.close()
    server.shutdown()
    print(
        "PASS: actual MV3 content cannot approve; trusted popup approves once; changed form and replay blocked."
    )


if __name__ == "__main__":
    asyncio.run(main())
