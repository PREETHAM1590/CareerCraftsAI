"""FastAPI dependencies for authentication and database access."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clerk_auth import (
    get_or_provision_user,
    subject_from_payload,
    verify_auth_jwt,
)
from app.core.database import get_db
from app.models.db import User

logger = logging.getLogger(__name__)

# Endpoints a user must be able to reach even before agreeing to the
# Terms of Service / Privacy Policy: checking their own status, recording
# agreement itself, exporting their data, and deleting their account. A
# data-access or erasure right can't be legally conditioned on first
# accepting new terms, so those two stay reachable regardless. Every other
# endpoint is blocked until policy_accepted_at is set, whether the request
# came through the sign-up form or hit the API directly.
_CONSENT_EXEMPT_PATHS = {
    ("GET", "/api/v1/users/me"),
    ("POST", "/api/v1/users/me/consent"),
    ("GET", "/api/v1/users/me/export"),
    ("DELETE", "/api/v1/users/me"),
    ("POST", "/api/v1/users/me/cancel-deletion"),
}


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and verify the Bearer token, then fetch or provision the user."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = auth_header.removeprefix("Bearer ").strip()
    # The JWT middleware already verified this header and kept the claims;
    # verify again only where it did not run, and off the event loop, since
    # a JWKS cache miss is a blocking HTTP fetch.
    payload = getattr(request.state, "user", None)
    if not isinstance(payload, dict):
        payload = await asyncio.to_thread(verify_auth_jwt, token)

    # Clerk's `sub` is a text id (user_2abc...), stored in users.clerk_user_id.
    # Provisioning (including the concurrent-first-request race) lives in
    # app.core.clerk_auth so middleware and routers share one code path.
    auth_subject = subject_from_payload(payload)
    user = await get_or_provision_user(db, auth_subject, payload)

    require_eligible_owner(user, exempt=(request.method, request.url.path) in _CONSENT_EXEMPT_PATHS)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_eligible_owner(user: User, *, exempt: bool = False) -> None:
    """Common eligibility policy for session and device credentials."""
    deletion_due = getattr(user, "deletion_scheduled_for", None)
    if deletion_due is not None and deletion_due <= datetime.now(UTC) and not exempt:
        raise HTTPException(status_code=410, detail="Account erasure is in progress")

    if user.policy_accepted_at is None and not exempt:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "policy_consent_required",
                "message": "Agree to the Terms of Service and Privacy Policy to continue.",
            },
        )
