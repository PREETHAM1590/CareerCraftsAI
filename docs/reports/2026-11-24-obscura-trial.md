# Obscura vs Chromium trial

**Date:** 2026-11-24.
**What:** [Obscura](https://github.com/h4ckf0r0day/obscura) v0.2.7 (Apache-2.0, Rust), rendering build, stealth **off**, compared with Playwright's Chromium headless shell 153.
**Where:** the dev VM, not 24gb1.
- 2 ARM cores and 11 GB, so half the CPU of 24gb1.
- No gVisor, which on 24gb1 adds 4 to 6 times the latency.
- Kept off 24gb1 so live members' 16 slots weren't slowed.

**Verdict:**
- **Use Obscura for job search and reading job pages.** It uses about 4 times less memory, and about half the CPU for text-only work.
- **Keep Chromium for Nova's computer** (sign-in, forms, live view). Obscura can't upload a resume, breaks the page structure on LinkedIn, and is slower at screenshots and the page snapshots Nova reads.

## 1. Can it do what Nova does? (local fixture)

`deploy/computers/fixtures`, driven through Playwright exactly as Nova would:

| Step | Chromium | Obscura |
|---|---|---|
| Fill name and email | ✅ | ✅ |
| Type into the cover letter (keyboard events) | ✅ | ✅ |
| **Upload resume** (`set_input_files`) | ✅ | ❌ `ReferenceError: DataTransfer is not defined` |
| Submit form, receipt shown | ✅ | ✅ |
| Login form, Enter submits (Nova's `sign_in`) | ✅ | ✅ |
| Accessibility snapshot with textboxes and heading | ✅ | ✅ |

Resume upload is required for almost every application.

## 2. Real job sites

One page load each per engine, Chromium first.

| Site | Chromium | Obscura | Notes |
|---|---|---|---|
| Greenhouse board + job page | ✅ | ✅ same text, links, form fields | Screenshot nearly identical |
| Lever board + job page | ✅ | ✅ identical | |
| Ashby board + job page | ✅ | ✅ identical | |
| SmartRecruiters | ✅ | ✅ identical | |
| Workday (NVIDIA) | ⚠️ only 350 chars after 4 s | ✅ 2,539 chars, 23 job links | Chromium was still loading |
| Internshala list + detail | ✅ | ✅ same text, 4× slower on the list (21 s vs 5 s) | Missing icon font, sign-up popup drawn over the page |
| LinkedIn job page | ✅ | ⚠️ text OK, but the DOM had **27 `<body>` elements**, so the snapshot failed | The LinkedIn list returned 429 to the second engine (same IP, rate limit) |
| Naukri | ❌ 403 Access Denied | ❌ empty challenge page | Both blocked |
| foundit | ❌ 403 | ✅ results page | The headless-shell build is easier to spot as a bot |
| Indeed | ❌ blocked | ❌ "Just a moment..." | Both blocked |

## 3. Capacity

One browser process per user, as in production. Each user loads 3 real job pages (Greenhouse, Lever, Ashby, SmartRecruiters rotating).

**Live mode** (page + JPEG screenshot + accessibility snapshot, like the computer panel and Nova):

| Engine | Users | Memory per user | CPU | Navigate p50 / p95 | Screenshot p50 / p95 | Snapshot p95 | Failures |
|---|---|---|---|---|---|---|---|
| Chromium | 4 | 554 MB | 94% | 1.4 / 3.3 s | 0.12 / 0.5 s | 1.2 s | 0 |
| Chromium | 8 | 506 MB | 100% | 2.5 / 3.6 s | 1.6 / 5.4 s | 2.4 s | 0 |
| Obscura | 4 | 157 MB | 83% | 1.7 / 8.9 s | 0.6 / 1.8 s | 5.6 s | 0 |
| Obscura | 8 | 153 MB | 99% | 3.0 / 4.3 s | 1.7 / 5.2 s | 10.5 s | 0 |
| Obscura | 16 | 146 MB | 100% | 5.0 / 11.4 s | 5.1 / 11.8 s | 22.4 s | 0 |
| Obscura | 32 | 122 MB | 81% | 6.2 / 16.5 s | 5.9 / 7.9 s | 25.8 s | screenshots time out (30 s) |

**Text mode** (page + text only, like job search):

| Engine | Users | Memory per user | CPU | Navigate p50 / p95 |
|---|---|---|---|---|
| Chromium | 8 | 501 MB | 98% | 2.7 / 4.3 s |
| Obscura | 8 | 126 MB | 59% | 2.1 / 4.8 s |
| Obscura | 16 | 124 MB | 96% | 4.6 / 9.5 s |

**What this means for 24gb1:**
- Production is limited by **CPU** (about 12 browsers fill 4 cores), not memory.
- In live mode Obscura fills the CPU at the same number of users as Chromium. Its snapshots, which Nova needs, are slower. So it would **not** raise the 16-slot cap for the computer.
- In text mode it does about twice the work per core and a quarter of the memory. That makes it a good fit for job-search scraping.

## 4. Security notes

- **Download:** the release has no published checksums. Pin a version and verify the Docker image digest.
- **Built-in protection:** it blocks private and internal addresses by default, and the Docker image is distroless and non-root.
- **Engine risk:**
  - The engine is new (first release April 2026, 277 open issues).
  - Its DOM, network and layout code is its own, not Chromium's, with far less security hardening.
  - Run it under gVisor and the Squid egress proxy like the current computers. It must never hold members' sign-ins.
- **Stealth mode:** fingerprint spoofing. It was not used here and shouldn't be used against portals where members have accounts.

## 5. Caveats

- Memory is summed RSS across each engine's processes, which counts shared pages more than once. The same method was used for both engines.
- These are single runs on a 2-core VM over the public internet, so site speed and rate limits add noise.
- No gVisor here. Expect about 4 to 6 times slower actions on 24gb1 for both engines.
- The trial scripts are not in the repo. They were scratch files: `common.py`, `compat.py` and `capacity.py`.
