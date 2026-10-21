# Copilot computers in production: launch and capacity report

**Date:** 2026-10-22. **Host:** `24gb1`, Oracle A1, 4 ARM cores, 24 GB RAM, Hyderabad.
**Path tested:** careercraftsai.me backend (APP VM, Mumbai) → restricted SSH tunnel → relay → one gVisor browser per user.

## Answer

| Concurrent users with an active computer | Experience |
|---|---|
| **up to 12** | Comfortable: screen refresh ~0.6 s, page reads ~1–4 s, no failures |
| **16 (production cap)** | Works with no failures; at full load screens refresh in ~0.2–3 s and pages take ~15–25 s to open |
| 20+ | Page loads start failing; screens lag 6–15 s |

The cap is `SANDBOX_MAX_SESSIONS=16`. A 17th simultaneous user gets "All sandbox slots are busy; retry when a slot is available" (429) and nothing is allocated. Computers sleep after 90 s without an action; every real action renews that lease, but screen and control polling don't. A hidden browser tab stops fetching screen frames and only checks status every 15 s. So far more than 16 members can use the feature over a day; 16 is how many can drive a browser *at the same moment*.

## End to end through the production app

Driven through the real `/computer` routes inside the production backend container, following the panel's exact steps (start, take control, open, release, screen, agent snapshot, stop):

| Step | Result |
|---|---|
| Start (cold, gVisor) | 200 in 5.6 s |
| Take control → open GitLab's job board | 200, page loaded in 2.5 s |
| Screen frame | 188 ms, JPEG, 33.6 KB, correct URL |
| Agent snapshot | 358 ms, 61 elements |
| Open `169.254.169.254` (cloud metadata) | refused by the egress proxy |
| Stop | 2.4 s |

Every action except screen and control polling (fetched every few seconds by design) is written to `agent_runs`.

**16 users at once through the same path:** 368 of 368 actions succeeded. Start p50 6.9 s, take control p50 1.1 s, open page p50 15.4 s / p95 22.9 s, screen refresh p50 0.23 s / p95 2.1 s, agent snapshot p50 1.3 s / p95 7.1 s.

## Load test on the computers host

`deploy/computers/loadtest_computers.py`: N simulated members at once. Each starts a computer and opens a real job board (Greenhouse, Lever, Ashby and others). It then polls a screen frame every 4 s like the panel, and makes at most two more navigations, 20–35 s apart, each preceded by an agent snapshot. Most of each 90 s hold is screen polling: a member reading a page, not continuous browsing.

| Users | Navigate p50 | Screen frame p50 / p95 | Snapshot p95 | Failures |
|---|---|---|---|---|
| 12 | 15 s | 0.6 s / 3.8 s | 3.8 s | 0 |
| 16 | 16–26 s | 0.9–3.2 s / 4.7–9.3 s | 11–17 s | 0 |
| 20 | 32 s | 5.8 s / 16 s | 24 s | 4 |
| 24 | 29 s | 7.0 s / 18 s | 34 s | 10 |

- **Limit:** CPU. All 4 cores sit at 100% from about 12 active browsers. Memory is about 400 MB per browser. The first ramp, with PNG frames, reached 32 browsers at 13 GB of 24 GB.
- **Cost of gVisor:** at 12 users, plain `runc` gave navigate p50 2.7 s and screen frames 0.09 s. gVisor's system-call interception costs roughly 4–6× in latency, because there's no KVM acceleration on A1. gVisor stays on: these browsers open arbitrary sites and hold users' saved portal logins.

## What was fixed to get here

1. **Every navigation failed under gVisor.** Docker's internal DNS doesn't answer inside gVisor containers. Browsers now reach the egress proxy by a pinned IP.
2. **The relay made all users wait in one line.** A global lock covered container start-up and every action. Now it covers only the slot check plus a reservation. 8 parallel starts went from 28 s median / 45 s p95 to 7.5 s in total, and the cap is still exact.
3. **Screen frames were full-resolution PNGs every 4 s.** They're now JPEG q70 (about 34 KB), which cut screen-frame latency at 16 users from 2.7 s to 0.9 s.
4. **Chromium background work is trimmed:** translate, media routing, optimisation hints and sync are off. Safe Browsing and certificate revocation updates stay on.
5. **Hidden tabs stop pulling screen frames,** and refresh immediately when shown again.
6. **Failed starts clean up:** the half-started container is stopped, the previous state restored, and the slot freed only after the stop. A slow start no longer loses its idle lease.
7. **Disk:** root was mounted with online `discard` on both VMs, which stalled the disk after every deploy. It's now weekly `fstrim`.

## Security checks (all passing under gVisor)

- **Verify suites pass:** core flow, privacy, saved profile across stop/start, keyboard input, file upload.
- **Egress blocked:** private networks and the cloud metadata endpoint are refused, and the test portal is absent in production.
- **Tunnel account is locked down:** the SSH tunnel uses `sandboxtunnel` (`nologin`). It can only open a local forward to `127.0.0.1:14312`. Remote forwards, other ports and shells are refused, and each was tested.
- **Secrets stay on the servers:** they live in root-only files, and the relay token travelled server to server without being printed.

## Not covered / next

- **No real browser session tested:** there was no test with an actual signed-in member in a real browser, since creating accounts in the live Clerk instance is off limits for me. The app path was exercised through the real routes with synthetic `loadtest-NNNN` account ids.
- **No Grafana coverage yet:** `24gb1` isn't in Grafana. Its own dashboard, kept separate like APP and INFRA, is the natural next step.
- **More capacity needs more CPU:** about 3–4 more comfortable users per extra core. Otherwise, users would have to accept slower screens.
