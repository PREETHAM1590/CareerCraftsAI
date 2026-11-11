"""Limits on how fast and how often applications go out.

Job boards flag accounts that apply in bursts, and a member's reputation is
the thing at stake, so every application passes the same three checks: a
rolling daily cap, a pause since the previous application, and (for
automatic applications) a minimum match score.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.config import settings

_MAX_PACING_WAIT_S = 15 * 60


def daily_cap_error(started_last_24h: int, cap: int | None = None) -> str | None:
    cap = settings.APPLY_DAILY_CAP if cap is None else cap
    if started_last_24h >= cap:
        return f"Daily application limit of {cap} reached. It frees up as earlier ones age out."
    return None


def auto_submit_cap_error(auto_approved_last_24h: int, cap: int) -> str | None:
    if auto_approved_last_24h >= cap:
        return (
            f"Auto-submit limit of {cap} a day reached; approve this one yourself. "
            "It frees up as earlier ones age out."
        )
    return None


# Their terms forbid (LinkedIn) or were not reviewed for (Naukri) automated
# submission: always a person's click, whatever the member's setting.
NO_AUTO_SUBMIT_HOSTS = ("linkedin.com", "naukri.com")


def auto_submit_host_blocked(*hosts: str | None) -> bool:
    for host in hosts:
        host = (host or "").lower().rstrip(".")
        if any(host == h or host.endswith("." + h) for h in NO_AUTO_SUBMIT_HOSTS):
            return True
    return False


def score_error(score: int | None, minimum: int | None = None) -> str | None:
    minimum = settings.AUTO_APPLY_MIN_SCORE if minimum is None else minimum
    if score is None or score < minimum:
        shown = "unscored" if score is None else str(score)
        return f"Match score {shown} is below the automatic-apply threshold of {minimum}."
    return None


def pacing_wait_seconds(
    last_started: datetime | None, now: datetime, gap_s: int | None = None
) -> int:
    """Seconds to wait so this application starts `gap_s` after the last one."""
    gap_s = settings.APPLY_MIN_GAP_SECONDS if gap_s is None else gap_s
    if last_started is None:
        return 0
    remaining = (last_started + timedelta(seconds=gap_s) - now).total_seconds()
    return int(min(max(remaining, 0), _MAX_PACING_WAIT_S))
