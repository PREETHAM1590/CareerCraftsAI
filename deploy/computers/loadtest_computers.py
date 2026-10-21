"""Concurrent-user load test for the computers relay. Synthetic users only.

Run inside the relay container (it has httpx and the relay token):

    docker compose --env-file ENV cp loadtest_computers.py relay:/tmp/
    docker compose --env-file ENV exec -e STEPS=4,8,12 relay python /tmp/loadtest_computers.py

Each simulated user behaves like a member on the Copilot page: start (wake)
their computer, open a real careers page, poll a screenshot every 4 s as the
panel does, read the accessibility snapshot as the agent does, browse a couple
more pages, then let it sit. A step holds N users at once for HOLD_SECONDS and
reports start/action latency and failures. Sites rotate so no third party sees
more than a few page loads a minute. Users are stopped and purged at the end.
"""

import concurrent.futures
import json
import os
import random
import threading
import time
import uuid

import httpx

RELAY = os.getenv("RELAY", "http://127.0.0.1:4302")
STEPS = [int(n) for n in os.getenv("STEPS", "4,8,12,16").split(",")]
HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "90"))
POLL_SECONDS = 4
SITES = [
    "https://boards.greenhouse.io/gitlab",
    "https://jobs.lever.co/spotify",
    "https://jobs.ashbyhq.com/notion",
    "https://www.workatastartup.com/jobs",
    "https://remoteok.com/remote-dev-jobs",
    "https://weworkremotely.com/categories/remote-programming-jobs",
    "https://news.ycombinator.com/jobs",
    "https://example.com/",
]

client = httpx.Client(
    base_url=RELAY,
    timeout=90,
    headers={"Authorization": "Bearer " + os.environ["SANDBOX_RELAY_TOKEN"]},
)


def timed(fn):
    began = time.monotonic()
    try:
        response = fn()
        return response.status_code, (time.monotonic() - began) * 1000
    except httpx.HTTPError as exc:
        return type(exc).__name__, (time.monotonic() - began) * 1000


def act(user, operation, parameters=None, actor="human"):
    body = {"operation": operation, "parameters": parameters or {}, "actor": actor}
    return timed(lambda: client.post(f"/users/{user}/action", json=body))


def member(user, deadline, record):
    status, ms = timed(lambda: client.post(f"/users/{user}/start"))
    record("start", status, ms)
    if status != 200:
        return
    sites = random.sample(SITES, 3)
    record("navigate", *act(user, "navigate", {"url": sites[0]}))
    next_browse = time.monotonic() + random.uniform(20, 35)
    browsed = 1
    while time.monotonic() < deadline:
        record("screenshot", *act(user, "screenshot"))
        if browsed < len(sites) and time.monotonic() >= next_browse:
            record("snapshot", *act(user, "snapshot", actor="agent"))
            record("navigate", *act(user, "navigate", {"url": sites[browsed]}))
            browsed += 1
            next_browse = time.monotonic() + random.uniform(20, 35)
        time.sleep(POLL_SECONDS)


def percentile(values, q):
    return (
        sorted(values)[min(len(values) - 1, int(len(values) * q))] if values else None
    )


def run_step(n):
    samples, lock = [], threading.Lock()

    def record(kind, status, ms):
        with lock:
            samples.append((kind, status, ms))

    users = [str(uuid.uuid4()) for _ in range(n)]
    deadline = time.monotonic() + HOLD_SECONDS
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda u: member(u, deadline, record), users))
    for user in users:  # free the slots and storage for the next step
        timed(lambda user=user: client.delete(f"/users/{user}"))
    report = {"users": n}
    for kind in ("start", "navigate", "screenshot", "snapshot"):
        rows = [s for s in samples if s[0] == kind]
        ok = [ms for _, status, ms in rows if status == 200]
        failed = [status for _, status, _ in rows if status != 200]
        report[kind] = {
            "n": len(rows),
            "ok": len(ok),
            "failed": {str(s): failed.count(s) for s in set(failed)},
            "p50_ms": round(percentile(ok, 0.5) or 0),
            "p95_ms": round(percentile(ok, 0.95) or 0),
        }
    return report


if __name__ == "__main__":
    print(json.dumps(client.get("/health").json()))
    for n in STEPS:
        result = run_step(n)
        print(json.dumps(result), flush=True)
        starts = result["start"]
        if starts["ok"] < n * 0.9 or (starts["p95_ms"] or 0) > 60000:
            print(json.dumps({"stopped": f"{n} users: starts failing or >60 s"}))
            break
        time.sleep(20)
