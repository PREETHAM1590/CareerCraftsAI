"""A throwaway local PostgreSQL for hermetic end-to-end tests.

No Docker and no credentials: it runs `initdb`/`pg_ctl` from an installed
PostgreSQL (GitHub's ubuntu runners and most dev machines have one), creates the
ORM schema with `Base.metadata.create_all`, and is torn down with the session.
Set `HERMETIC_PG_BIN` to the server's bin directory to override discovery.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
from pathlib import Path


def find_pg_bin() -> Path | None:
    candidates = [os.environ.get("HERMETIC_PG_BIN", "")]
    candidates += sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    candidates += sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin"), reverse=True)
    for candidate in candidates:
        if candidate and (Path(candidate) / "initdb").exists():
            return Path(candidate)
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EphemeralPostgres:
    def __init__(self, root: Path, bin_dir: Path):
        self.root = root
        self.bin = bin_dir
        self.port = _free_port()
        self.database = "careercraft_e2e"

    @property
    def async_url(self) -> str:
        return f"postgresql+asyncpg://cc@127.0.0.1:{self.port}/{self.database}"

    @property
    def sync_url(self) -> str:
        return f"postgresql+psycopg://cc@127.0.0.1:{self.port}/{self.database}"

    def start(self) -> None:
        data = self.root / "data"
        run = dict(check=True, capture_output=True, text=True, timeout=120)
        subprocess.run(
            [str(self.bin / "initdb"), "-D", str(data), "-U", "cc", "-A", "trust", "--no-sync"],
            **run,
        )
        options = (
            f"-p {self.port} -k {self.root} -c listen_addresses=127.0.0.1 "
            "-c fsync=off -c synchronous_commit=off -c full_page_writes=off"
        )
        subprocess.run(
            [
                str(self.bin / "pg_ctl"),
                "-D",
                str(data),
                "-o",
                options,
                "-l",
                str(self.root / "pg.log"),
                "-w",
                "start",
            ],
            **run,
        )
        subprocess.run(
            [
                str(self.bin / "createdb"),
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                "cc",
                self.database,
            ],
            **run,
        )

    def stop(self) -> None:
        subprocess.run(
            [str(self.bin / "pg_ctl"), "-D", str(self.root / "data"), "-m", "immediate", "stop"],
            capture_output=True,
            timeout=60,
            check=False,
        )

    def create_schema(self) -> None:
        from sqlalchemy import create_engine

        from app.models.db import Base

        engine = create_engine(self.sync_url)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
