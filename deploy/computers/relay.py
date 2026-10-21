"""Private single-host OpenBot relay: bounded allocation, idle release, no shell.

Only CareerCraft's authenticated backend holds RELAY_TOKEN. Run one process;
SQLite owns persistent enablement and this process owns the host's slot budget.
Secrets and typed input are never written to the state database or access logs.
"""

import asyncio
import hashlib
import hmac
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

TOKEN = os.environ["SANDBOX_RELAY_TOKEN"]
MASTER = os.environ["COMPUTER_TOKEN"]
SUPERVISOR_TOKEN = os.environ["COMPUTER_SUPERVISOR_TOKEN"]
MAX_SESSIONS = int(os.getenv("SANDBOX_MAX_SESSIONS", "8"))
IDLE_SECONDS = int(os.getenv("SANDBOX_IDLE_SECONDS", "90"))
DB_PATH = os.getenv("SANDBOX_STATE_PATH", "/state/sessions.db")
allocation_lock = asyncio.Lock()
locks: dict[str, asyncio.Lock] = {}


def database():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def identity(user_id: str):
    # Docker's embedded DNS requires each hostname label to fit 63 bytes.
    return "u-" + uuid.UUID(user_id).hex


def user_lock(user_id: str):
    return locks.setdefault(user_id, asyncio.Lock())


async def authorized(request: Request):
    offered = request.headers.get("authorization", "")
    if not hmac.compare_digest(offered, f"Bearer {TOKEN}"):
        raise HTTPException(401, "Unauthorized")


async def upstream(
    method, url, token, body=None, bot=None, actor="human", snapshot=None
):
    headers = {"Authorization": f"Bearer {token}"}
    if bot:
        headers["x-openbot-bot-id"] = bot
        headers["x-careercraft-actor"] = actor
        if snapshot is not None:
            headers["x-careercraft-snapshot"] = str(snapshot)
    try:
        async with httpx.AsyncClient(timeout=65, trust_env=False) as client:
            response = await client.request(method, url, headers=headers, json=body)
        result = response.json()
        if response.is_error:
            # Do not forward a trace or raw authenticated URL.
            detail = "Computer action refused; refresh the page or take control"
            if response.status_code == 409:
                detail = "Browser state changed or human control is active; refresh before continuing"
            raise HTTPException(response.status_code, detail)
        return result
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Sandbox computer unavailable") from exc


async def supervisor(bot, verb):
    return await upstream(
        "POST", f"http://supervisor:4300/computers/{bot}/{verb}", SUPERVISOR_TOKEN
    )


async def ensure(user_id, enable=False):
    bot = identity(user_id)
    # The global lock covers only the slot check and reservation. Container
    # start-up and the readiness wait run outside it: held across them, every
    # user's every action queued behind one lock (latency grew linearly with
    # concurrent users).
    async with allocation_lock:
        with database() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE user_id=?", (user_id,)
            ).fetchone()
            if row and row["purged"]:
                raise HTTPException(410, "Computer owner has been erased")
            if not enable and (not row or not row["enabled"]):
                raise HTTPException(409, "Start your computer before using it")
            count = db.execute(
                "SELECT count(*) FROM sessions WHERE running=1"
            ).fetchone()[0]
            reserved = not row or not row["running"]
            was_enabled = bool(row and row["enabled"])
            if reserved and count >= MAX_SESSIONS:
                raise HTTPException(
                    429, "All sandbox slots are busy; retry when a slot is available"
                )
            # Reserve before releasing the lock, so concurrent starts count it.
            db.execute(
                "INSERT INTO sessions(user_id,enabled,running,touched) VALUES (?,1,1,?) ON CONFLICT(user_id) DO UPDATE SET enabled=1,running=1,touched=excluded.touched",
                (user_id, time.time()),
            )
    try:
        state = await supervisor(bot, "ensure")
        expected = f"http://careercraft-sandbox-computer-{bot}:4100"
        if state.get("url") != expected:
            raise HTTPException(
                503, "Supervisor returned an unexpected computer address"
            )
        # Container creation returns before its HTTP listener is ready. Retry
        # only this read; never retry a dispatched browser action.
        token = hmac.new(
            MASTER.encode(), ("opendots-computer:" + bot).encode(), hashlib.sha256
        ).hexdigest()
        for attempt in range(30):
            try:
                await upstream("GET", expected + "/run", token, bot=bot)
                break
            except HTTPException as exc:
                if exc.status_code != 503 or attempt == 29:
                    raise
                await asyncio.sleep(0.5)
    except BaseException:
        if reserved:
            # The supervisor may have created a container that never became
            # ready: stop it before freeing the slot, or it runs uncounted.
            # If the stop fails the slot stays reserved (the reaper retries).
            await supervisor(bot, "stop")
            with database() as db:
                db.execute(
                    "UPDATE sessions SET running=0,enabled=? WHERE user_id=?",
                    (int(was_enabled), user_id),
                )
        raise
    if reserved:
        # A slow start must not count against the idle lease it just got.
        with database() as db:
            db.execute(
                "UPDATE sessions SET touched=? WHERE user_id=?", (time.time(), user_id)
            )
    return bot


async def stop(user_id, disable):
    # Callers hold this user's lock. No global lock: freeing a slot cannot
    # overshoot MAX_SESSIONS, and a stop (profile flush) takes seconds.
    await supervisor(identity(user_id), "stop")
    with database() as db:
        db.execute(
            "UPDATE sessions SET running=0,enabled=CASE WHEN ? THEN 0 ELSE enabled END WHERE user_id=?",
            (disable, user_id),
        )


async def reap():
    while True:
        await asyncio.sleep(10)
        with database() as db:
            rows = db.execute(
                "SELECT user_id FROM sessions WHERE running=1 AND touched<?",
                (time.time() - IDLE_SECONDS,),
            ).fetchall()
        for row in rows:
            user = row["user_id"]
            async with user_lock(user):
                with database() as db:
                    current = db.execute(
                        "SELECT touched FROM sessions WHERE user_id=?", (user,)
                    ).fetchone()
                if current and current["touched"] < time.time() - IDLE_SECONDS:
                    try:
                        await stop(user, False)
                    except HTTPException:
                        pass  # Keep the occupied slot until stop is confirmed.


@asynccontextmanager
async def lifespan(app):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with database() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS sessions(user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, running INTEGER NOT NULL, touched REAL NOT NULL, purged INTEGER NOT NULL DEFAULT 0)"
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        if "purged" not in columns:
            db.execute(
                "ALTER TABLE sessions ADD COLUMN purged INTEGER NOT NULL DEFAULT 0"
            )
    task = asyncio.create_task(reap())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan, dependencies=[Depends(authorized)])


class Operation(str, Enum):
    upload = "upload"
    navigate = "navigate"
    read = "read"
    snapshot = "snapshot"
    screenshot = "screenshot"
    click = "click"
    type = "type"
    scroll = "scroll"
    files_list = "files/list"
    files_read = "files/read"
    files_write = "files/write"
    help = "control/request"
    control = "control"
    take = "control/take"
    release = "control/release"
    secret_request = "control/secret"
    secret = "human/secret"
    human_click = "human/click"
    human_type = "human/type"
    human_key = "human/key"
    human_scroll = "human/scroll"
    human_navigate = "human/navigate"
    credential_fill = "credentials/fill"
    privacy_release = "privacy/release"


class Action(BaseModel):
    operation: Operation
    parameters: dict = Field(default_factory=dict)
    computer_run: str | None = None
    approved_snapshot: int | None = None
    actor: Literal["human", "agent"] = "human"


@app.get("/health")
async def health():
    with database() as db:
        active = db.execute("SELECT count(*) FROM sessions WHERE running=1").fetchone()[
            0
        ]
    return {
        "status": "ok",
        "active": active,
        "capacity": MAX_SESSIONS,
        "idle_seconds": IDLE_SECONDS,
    }


@app.get("/users/{user_id}")
async def status(user_id: uuid.UUID):
    with database() as db:
        row = db.execute(
            "SELECT enabled,running FROM sessions WHERE user_id=?", (str(user_id),)
        ).fetchone()
    return {
        "enabled": bool(row and row["enabled"]),
        "running": bool(row and row["running"]),
    }


@app.post("/users/{user_id}/start")
async def start(user_id: uuid.UUID):
    async with user_lock(str(user_id)):
        await ensure(str(user_id), True)
    return {"enabled": True, "running": True}


@app.post("/users/{user_id}/stop")
async def shutdown(user_id: uuid.UUID):
    async with user_lock(str(user_id)):
        await stop(str(user_id), True)
    return {"enabled": False, "running": False}


@app.delete("/users/{user_id}")
async def purge(user_id: uuid.UUID):
    user = str(user_id)
    async with user_lock(user), allocation_lock:
        # Disable first, even when remote deletion fails. The retained row
        # keeps its occupied slot until the next purge succeeds.
        with database() as db:
            db.execute(
                "INSERT INTO sessions(user_id,enabled,running,touched,purged) VALUES (?,0,0,?,1) "
                "ON CONFLICT(user_id) DO UPDATE SET enabled=0,purged=1",
                (user, time.time()),
            )
        result = await supervisor(identity(user), "purge")
        if result.get("purged") is not True:
            raise HTTPException(503, "Computer erasure was not confirmed")
        with database() as db:
            # A minimal owner tombstone rejects late in-flight start calls.
            # It contains no cookies, files or browser history.
            db.execute(
                "UPDATE sessions SET running=0,touched=0 WHERE user_id=?", (user,)
            )
    return {"purged": True}


@app.post("/users/{user_id}/action")
async def action(user_id: uuid.UUID, payload: Action):
    user = str(user_id)
    async with user_lock(user):
        op = payload.operation.value
        if payload.actor == "agent" and op not in {
            "navigate",
            "read",
            "snapshot",
            "click",
            "type",
            "scroll",
            "upload",
            "files/list",
            "files/read",
            "files/write",
        }:
            raise HTTPException(403, "This operation is not an agent capability")
        # Screen polling must not allocate a computer or prevent idle release.
        if op in {"screenshot", "control"}:
            with database() as db:
                row = db.execute(
                    "SELECT running FROM sessions WHERE user_id=?", (user,)
                ).fetchone()
            if not row or not row["running"]:
                raise HTTPException(409, "Computer is sleeping; choose Resume")
            bot = identity(user)
        else:
            bot = await ensure(user)
        token = hmac.new(
            MASTER.encode(), ("opendots-computer:" + bot).encode(), hashlib.sha256
        ).hexdigest()
        url = f"http://careercraft-sandbox-computer-{bot}:4100"
        current = await upstream("GET", url + "/run", token, bot=bot)
        mutation = op == "upload" or (
            payload.actor == "agent" and op in {"navigate", "click", "type", "scroll"}
        )
        if (
            mutation or op in {"click", "type", "credentials/fill"}
        ) and payload.computer_run != current["run"]:
            raise HTTPException(
                409, "Computer restarted; take a new snapshot and review again"
            )
        if mutation and not payload.approved_snapshot:
            raise HTTPException(422, "A reviewed snapshot is required")
        params = payload.parameters
        if op in {"navigate", "human/navigate"}:
            parsed = urlsplit(str(params.get("url", "")))
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise HTTPException(
                    422, "Use an HTTP(S) URL without embedded credentials"
                )
        method = "GET" if op in {"read", "screenshot", "control"} else "POST"
        result = await upstream(
            method,
            url + "/" + op,
            token,
            params if method == "POST" else None,
            bot,
            payload.actor,
            payload.approved_snapshot if mutation else None,
        )
        if op == "snapshot":
            result["computer_run"] = current["run"]
        return result
