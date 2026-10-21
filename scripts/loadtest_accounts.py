#!/usr/bin/env python3
"""Synthetic signed-in accounts for tests/load/k6_users.js.

Run it yourself against the Clerk instance whose tokens the API trusts. It reads
CLERK_SECRET_KEY and NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY from the env files given
(values are never printed) and writes one bearer token per line.

    sudo python3 scripts/loadtest_accounts.py create --count 200 \
        --env-file /opt/careercraft-secrets/backend.env \
        --env-file /opt/careercraft-secrets/public.env --out ~/loadtest/tokens.txt
    sudo python3 scripts/loadtest_accounts.py delete \
        --env-file /opt/careercraft-secrets/backend.env \
        --env-file /opt/careercraft-secrets/public.env

create   idempotent: makes loadtest+NNNN@<domain> users (public_metadata.loadtest),
         a "loadtest" JWT template (1 h, email claim) if missing, signs each user in
         with a one-time ticket, mints a template token and records policy consent
         through the API (which also provisions the app's user row). Re-run it to
         get fresh tokens; existing users are reused.
delete   asks the API to schedule erasure of each account (DELETE /users/me, the
         app's own path; 15-day grace period). The Clerk users are NOT deleted
         here: the backend reaper (reap_expired_account_deletions) removes them
         once the grace period has passed. Only users tagged
         public_metadata.loadtest=true are touched. If create reuses a user
         whose erasure is already due (consent returns 410), it cancels the
         pending erasure (POST /users/me/cancel-deletion) and retries consent.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLERK_API = "https://api.clerk.com/v1"
TEMPLATE = "loadtest"
# Clerk's Cloudflare front rejects urllib's default "Python-urllib" agent (1010).
USER_AGENT = "careercraft-loadtest-accounts/1.0"


def read_env(paths):
    values = {}
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    values[key.strip()] = value.strip().strip("'\"")
    return values


def call(method, url, *, token=None, body=None, form=None, headers=None):
    """JSON request with retry on 429/5xx; returns (status, json, response headers)."""
    data, all_headers = None, {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        **(headers or {}),
    }
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data, all_headers["Content-Type"] = (
            json.dumps(body).encode(),
            "application/json",
        )
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        all_headers["Content-Type"] = "application/x-www-form-urlencoded"
    for attempt in range(6):
        request = urllib.request.Request(
            url, data=data, method=method, headers=all_headers
        )
        try:
            with urllib.request.urlopen(
                request, timeout=30
            ) as response:  # nosec B310 - fixed https Clerk/API URLs
                raw = response.read()
                return (
                    response.status,
                    (json.loads(raw) if raw else {}),
                    response.headers,
                )
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt < 5:
                time.sleep(float(exc.headers.get("Retry-After") or 2**attempt))
                continue
            raw = exc.read()
            try:
                return exc.code, json.loads(raw), exc.headers
            except ValueError:
                return (
                    exc.code,
                    {"raw": raw[:300].decode(errors="replace")},
                    exc.headers,
                )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt < 5:  # HTTPError is handled above
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"{method} {url}: {exc}") from exc
    raise RuntimeError(f"{method} {url}: retries exhausted")


def frontend_api(publishable_key):
    encoded = publishable_key.split("_", 2)[2]
    return "https://" + base64.b64decode(
        encoded + "=" * (-len(encoded) % 4)
    ).decode().rstrip("$")


def loadtest_users(secret):
    users, offset = [], 0
    while True:
        query = urllib.parse.urlencode(
            {"query": "loadtest+", "limit": 100, "offset": offset}
        )
        status, page, _ = call("GET", f"{CLERK_API}/users?{query}", token=secret)
        if status != 200:
            sys.exit(f"listing users failed: HTTP {status} {page}")
        users += [
            u for u in page if (u.get("public_metadata") or {}).get("loadtest") is True
        ]
        if len(page) < 100:
            return users
        offset += 100


def ensure_template(secret):
    status, templates, _ = call("GET", f"{CLERK_API}/jwt_templates", token=secret)
    if status == 200 and any(t.get("name") == TEMPLATE for t in templates):
        return
    status, body, _ = call(
        "POST",
        f"{CLERK_API}/jwt_templates",
        token=secret,
        body={
            "name": TEMPLATE,
            "claims": {"email": "{{user.primary_email_address}}"},
            "lifetime": 3600,
            "allowed_clock_skew": 5,
        },
    )
    if status not in (200, 201):
        sys.exit(f"creating the JWT template failed: HTTP {status} {body}")


def session_token(secret, fapi, user_id):
    status, ticket, _ = call(
        "POST",
        f"{CLERK_API}/sign_in_tokens",
        token=secret,
        body={"user_id": user_id, "expires_in_seconds": 300},
    )
    if status not in (200, 201):
        raise RuntimeError(f"sign-in token: HTTP {status} {ticket}")
    # Native-client sign-in: no cookies, the client token comes back in a header.
    status, signin, _ = call(
        "POST",
        f"{fapi}/v1/client/sign_ins?_is_native=true",
        form={"strategy": "ticket", "ticket": ticket["token"]},
    )
    session_id = (signin.get("response") or {}).get("created_session_id")
    if status != 200 or not session_id:
        raise RuntimeError(f"ticket sign-in: HTTP {status} {str(signin)[:300]}")
    status, minted, _ = call(
        "POST",
        f"{CLERK_API}/sessions/{session_id}/tokens/{TEMPLATE}",
        token=secret,
        body={},
    )
    if status != 200 or not minted.get("jwt"):
        raise RuntimeError(f"template token: HTTP {status} {minted}")
    return minted["jwt"]


def create(args, env):
    secret, fapi = env["CLERK_SECRET_KEY"], frontend_api(
        env["NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"]
    )
    ensure_template(secret)
    existing = {
        u["email_addresses"][0]["email_address"]: u["id"]
        for u in loadtest_users(secret)
    }
    tokens, failures = [], 0
    for n in range(1, args.count + 1):
        email = f"loadtest+{n:04d}@{args.domain}"
        try:
            user_id = existing.get(email)
            if not user_id:
                status, user, _ = call(
                    "POST",
                    f"{CLERK_API}/users",
                    token=secret,
                    body={
                        "email_address": [email],
                        "first_name": "Load",
                        "last_name": f"Test {n:04d}",
                        "skip_password_requirement": True,
                        "public_metadata": {"loadtest": True},
                    },
                )
                if status not in (200, 201):
                    raise RuntimeError(f"create user: HTTP {status} {user}")
                user_id = user["id"]
            jwt = session_token(secret, fapi, user_id)
            status, consent, _ = call(
                "POST", f"{args.api}/users/me/consent", token=jwt, body={}
            )
            if status == 410:  # reused user whose erasure grace period has passed
                call("POST", f"{args.api}/users/me/cancel-deletion", token=jwt)
                status, consent, _ = call(
                    "POST", f"{args.api}/users/me/consent", token=jwt, body={}
                )
            if status != 200:
                raise RuntimeError(f"consent: HTTP {status} {consent}")
            tokens.append(jwt)
        except RuntimeError as exc:
            failures += 1
            print(f"{email}: {exc}", file=sys.stderr)
            if failures >= 5 and not tokens:
                sys.exit("stopping: the first accounts all failed")
        print(f"\r{len(tokens)} ready, {failures} failed", end="", file=sys.stderr)
        time.sleep(0.2)  # Clerk Backend API: stay well under 100 requests / 10 s
    # Owner-only: these are live bearer tokens for an hour.
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(args.out, 0o600)  # an existing file keeps its old mode otherwise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(tokens) + "\n")
    print(f"\nwrote {len(tokens)} tokens to {args.out} (valid 1 hour)", file=sys.stderr)


def delete(args, env):
    secret, fapi = env["CLERK_SECRET_KEY"], frontend_api(
        env["NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"]
    )
    users = loadtest_users(secret)
    for done, user in enumerate(users, 1):
        try:
            jwt = session_token(secret, fapi, user["id"])
            status, body, _ = call("DELETE", f"{args.api}/users/me", token=jwt)
            if status not in (200, 202, 204):
                raise RuntimeError(f"HTTP {status} {body}")
        except RuntimeError as exc:
            print(
                f"{user['id']}: erasure not scheduled, user kept in Clerk ({exc})",
                file=sys.stderr,
            )
        print(f"\r{done}/{len(users)} erasure scheduled", end="", file=sys.stderr)
        time.sleep(0.2)
    print(file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["create", "delete"])
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--domain", default="careercraftsai.me")
    parser.add_argument("--api", default="https://api.careercraftsai.me/api/v1")
    parser.add_argument("--env-file", action="append", required=True)
    parser.add_argument("--out", default="tokens.txt")
    args = parser.parse_args()
    env = read_env(args.env_file)
    missing = {"CLERK_SECRET_KEY", "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"} - env.keys()
    if missing:
        sys.exit(f"missing in env files: {', '.join(sorted(missing))}")
    (create if args.action == "create" else delete)(args, env)


if __name__ == "__main__":
    main()
