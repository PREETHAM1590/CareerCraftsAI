"""Run inside the relay container; only synthetic users and fixture secrets."""

import concurrent.futures
import json
import os
import statistics
import time
import uuid

import httpx

client = httpx.Client(
    base_url="http://127.0.0.1:4302",
    timeout=75,
    headers={"Authorization": "Bearer " + os.environ["SANDBOX_RELAY_TOKEN"]},
)
users = []
proof = {}


def call(user, operation, parameters=None, actor="human", snapshot=None, expected=200):
    body = {"operation": operation, "parameters": parameters or {}, "actor": actor}
    if snapshot:
        body.update(
            computer_run=snapshot["computer_run"],
            approved_snapshot=snapshot["snapshotId"],
        )
    result = client.post(f"/users/{user}/action", json=body)
    assert result.status_code == expected, (
        operation,
        result.status_code,
        result.text[:180],
    )
    return result.json()


def start():
    user = str(uuid.uuid4())
    users.append(user)
    response = client.post(f"/users/{user}/start")
    assert response.status_code == 200, response.text
    return user


def fields(snapshot):
    return {item["name"]: item["ref"] for item in snapshot["elements"]}


try:
    user = start()
    call(user, "navigate", {"url": "http://test-portal/"})
    shot = call(user, "screenshot")
    assert len(shot["base64"]) > 1000
    snap = call(user, "snapshot", actor="agent")
    for name, text in [
        ("Full name", "Synthetic Test User"),
        ("Email", "test@example.com"),
        ("Cover letter", "Fixture application only"),
    ]:
        call(
            user,
            "type",
            {"ref": fields(snap)[name], "snapshotId": snap["snapshotId"], "text": text},
            "agent",
            snap,
        )
        snap = call(user, "snapshot", actor="agent")
    call(
        user,
        "click",
        {
            "ref": fields(snap)["Submit test application"],
            "snapshotId": snap["snapshotId"],
        },
        "agent",
        snap,
    )
    page = call(user, "read", actor="agent")
    assert "Test application received. Count: 1" in json.dumps(page)
    proof["fixture_submission"] = "one confirmed receipt"
    call(
        user,
        "click",
        {
            "ref": fields(snap)["Submit test application"],
            "snapshotId": snap["snapshotId"],
        },
        "agent",
        {**snap, "computer_run": "stale"},
        409,
    )
    fresh = call(user, "snapshot", actor="agent")
    call(
        user,
        "click",
        {
            "ref": fields(snap)["Submit test application"],
            "snapshotId": snap["snapshotId"],
        },
        "agent",
        snap,
        409,
    )
    proof["stale_run_and_snapshot"] = "refused before click"
    for op in ["credentials/fill", "screenshot", "human/type"]:
        call(user, op, actor="agent", expected=403)
    proof["agent_private_capabilities"] = "denied"
    call(
        user,
        "files/write",
        {"path": "proof.txt", "contents": "only owner one"},
        "agent",
    )
    other = start()
    listing = call(other, "files/list", actor="agent")
    assert "proof.txt" not in json.dumps(listing)
    proof["workspace_isolation"] = True
    client.post(f"/users/{other}/stop").raise_for_status()
    call(user, "navigate", {"url": "http://test-portal/login.html"})
    login = call(user, "snapshot")
    bad = {
        "origin": "https://wrong.example",
        "username": "fixture@example.com",
        "password": "synthetic-private-password",
        "usernameRef": fields(login)["Email"],
        "passwordRef": fields(login)["Password"],
        "snapshotId": login["snapshotId"],
    }
    call(user, "credentials/fill", bad, snapshot=login, expected=403)
    call(
        user,
        "credentials/fill",
        {**bad, "origin": "http://test-portal"},
        snapshot=login,
    )
    call(user, "read", actor="agent", expected=409)
    call(user, "snapshot", actor="agent", expected=409)
    call(
        user,
        "navigate",
        {"url": "http://test-portal/"},
        actor="agent",
        snapshot=login,
        expected=409,
    )
    proof["secret_page_model_access"] = "blocked"
    client.post(f"/users/{user}/stop").raise_for_status()
    client.post(f"/users/{user}/start").raise_for_status()
    call(user, "read", actor="agent", expected=409)
    recovery = call(user, "control/request", {"reason": "Recover after restart"})
    recovery_id = recovery["request"]["id"]
    call(user, "control/take", {"requestId": recovery_id})
    call(user, "human/navigate", {"url": "http://test-portal/"})
    call(user, "privacy/release")
    call(user, "control/release", {"requestId": recovery_id})
    call(user, "snapshot", actor="agent")
    call(user, "read", actor="agent")
    assert "only owner one" in json.dumps(
        call(user, "files/read", {"path": "proof.txt"}, "agent")
    )
    proof["private_marker_and_workspace_restart"] = True
    # Fixture receipt is in persistent localStorage; show it through the UI.
    call(user, "navigate", {"url": "http://test-portal/"})
    snap = call(user, "snapshot")
    takeover = call(user, "control/request", {"reason": "Synthetic takeover proof"})
    request_id = takeover["request"]["id"]
    call(user, "control/take", {"requestId": request_id})
    call(
        user,
        "click",
        {
            "ref": fields(snap)["Submit test application"],
            "snapshotId": snap["snapshotId"],
        },
        "agent",
        snap,
        409,
    )
    call(user, "control/release", {"requestId": request_id})
    call(
        user,
        "click",
        {
            "ref": fields(snap)["Submit test application"],
            "snapshotId": snap["snapshotId"],
        },
        "agent",
        snap,
        409,
    )
    proof["takeover_invalidates_approval"] = True
    # A blocked target may render Squid's deny page or cause a browser error.
    for target in ["http://169.254.169.254/", "http://supervisor:4300/"]:
        result = client.post(
            f"/users/{user}/action",
            json={"operation": "navigate", "parameters": {"url": target}},
        )
        if result.status_code == 200:
            assert "Access Denied" in json.dumps(result.json()) or "ERR_" in json.dumps(
                result.json()
            )
        else:
            assert result.status_code in {400, 403, 502, 409}, result.text
    proof["private_network_egress"] = "blocked"
    client.post(f"/users/{user}/stop").raise_for_status()
    benchmark = []
    for count in [1, 4, 8]:
        group = [start() for _ in range(count)]

        def workload(member):
            began = time.monotonic()
            call(member, "navigate", {"url": "http://test-portal/"})
            call(member, "snapshot", actor="agent")
            return time.monotonic() - began

        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            latency = list(pool.map(workload, group))
        if count == 8:
            rejected = client.post(f"/users/{uuid.uuid4()}/start")
            assert rejected.status_code == 429
            proof["ninth_computer"] = "429 busy response, no allocation"
        # Observer reads Docker stats on the host while these browsers stay live.
        print(json.dumps({"phase": "browsers_live", "count": count}), flush=True)
        time.sleep(12)
        started = time.monotonic()
        for _ in range(3):
            with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                list(
                    pool.map(
                        lambda member: call(member, "snapshot", actor="agent"), group
                    )
                )
        benchmark.append(
            {
                "browsers": count,
                "cold_load_snapshot_seconds_mean": round(statistics.mean(latency), 3),
                "cold_load_snapshot_seconds_max": round(max(latency), 3),
                "three_snapshot_rounds_seconds": round(time.monotonic() - started, 3),
            }
        )
        for member in group:
            client.post(f"/users/{member}/stop").raise_for_status()
    proof["benchmark"] = benchmark
    # Screen polling must neither allocate nor retain a lease.
    idle = start()
    call(idle, "navigate", {"url": "http://test-portal/"})
    for _ in range(26):
        state = client.get(f"/users/{idle}").json()
        if not state["running"]:
            break
        # The reaper may stop it between the status read and this screenshot.
        shot = client.post(
            f"/users/{idle}/action",
            json={"operation": "screenshot", "parameters": {}, "actor": "human"},
        )
        if shot.status_code == 409:
            break
        assert shot.status_code == 200, shot.text[:180]
        time.sleep(4)
    assert not client.get(f"/users/{idle}").json()["running"]
    call(idle, "screenshot", expected=409)
    proof["screenshots_do_not_prevent_idle_stop"] = True
    print(json.dumps({"proof": proof}, indent=2), flush=True)
finally:
    for user in users:
        client.post(f"/users/{user}/stop")
