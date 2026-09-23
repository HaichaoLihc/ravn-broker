"""One process, one serialized connection, cancellation-safe units of work."""

import asyncio
import fcntl
import os
import stat
from contextlib import asynccontextmanager
from importlib.resources import files

import aiosqlite

from ravn.common import identifier, now
from ravn.config import Config
from ravn.crypto import Cipher


async def finish(awaitable):
    """Wait for a queued SQLite operation to finish even if its waiter is cancelled."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def one(db, sql: str, params=()):
    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


async def rows(db, sql: str, params=()):
    async with db.execute(sql, params) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def event(
    db, principal, kind: str, subject: str, *, actor_kind=None, actor_id=None, request_id=None
):
    if actor_kind is None:
        actor_kind = "runtime_session" if principal.session_id else "application_key"
        actor_id = principal.session_id or principal.key_id
    await db.execute(
        "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            identifier("evt"),
            principal.app,
            principal.tenant,
            principal.user,
            kind,
            subject,
            now(),
            actor_kind,
            actor_id,
            kind.split(".")[0],
            request_id,
        ),
    )


class Store:
    def __init__(self, config: Config, cipher: Cipher):
        self.config, self.cipher = config, cipher
        self.lock = asyncio.Lock()
        self.db = None
        self.lock_fd = None

    async def open(self):
        path = self.config.storage.sqlite_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError("Database directory must be owned by this user and mode 0700")
        fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise ValueError("Another RAVN process owns this database") from None
        self.lock_fd = fd
        try:
            if path.is_symlink():
                raise ValueError("Database cannot be a symlink")
            if path.exists() and not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("Database must be a regular file")
            self.db = await aiosqlite.connect(path, isolation_level=None)
            os.chmod(path, 0o600)
            self.db.row_factory = aiosqlite.Row
            for pragma in [
                "journal_mode=WAL",
                "foreign_keys=ON",
                "busy_timeout=5000",
                "synchronous=FULL",
            ]:
                await self.db.execute("PRAGMA " + pragma)
            version = await one(self.db, "PRAGMA user_version")
            if version["user_version"] == 0:
                schema = files("ravn").joinpath("schema.sql").read_text()
                await self.db.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
            elif version["user_version"] != 9:
                raise ValueError(
                    "Unsupported demo database layout; initialize a new state directory"
                )
            async with self.transaction() as db:
                check = await one(db, "SELECT value FROM metadata WHERE key='key-check'")
                if check:
                    if self.cipher.decrypt(check["value"], "", "", "key-check") != "ravn-v1":
                        raise ValueError("Key check failed")
                    key_id = await one(db, "SELECT value FROM metadata WHERE key='key-id'")
                    if key_id["value"] != self.cipher.key_id:
                        raise ValueError(
                            "Changing the key ID cannot rotate existing encrypted data"
                        )
                else:
                    await db.execute(
                        "INSERT INTO metadata VALUES('key-check',?)",
                        (self.cipher.encrypt("ravn-v1", "", "", "key-check"),),
                    )
                    await db.execute(
                        "INSERT INTO metadata VALUES('key-id',?)", (self.cipher.key_id,)
                    )
                await db.execute(
                    "UPDATE calls SET status='unknown',error_code='outcome_unknown',"
                    "updated_at=?,completed_at=? WHERE status='running'",
                    (now(), now()),
                )
                # A persisted attempt can have rotated remotely before a crash.
                # Never reuse its old refresh token after restarting.
                pending = await rows(
                    db,
                    "SELECT * FROM connections WHERE refresh_attempt IS NOT NULL AND status='active'",
                )
                from ravn.common import Principal

                for conn in pending:
                    await db.execute(
                        "UPDATE connections SET status='reconnect_required',refresh_attempt=NULL,revision=revision+1,updated_at=? WHERE app_id=? AND tenant_id=? AND id=?",
                        (now(), conn["app_id"], conn["tenant_id"], conn["id"]),
                    )
                    await event(
                        db,
                        Principal(conn["app_id"], conn["tenant_id"], conn["user_id"]),
                        "credential.refresh_uncertain",
                        conn["id"],
                        actor_kind="system",
                        actor_id="restart",
                    )
                await db.execute(
                    "UPDATE connect_sessions SET status='failed',failure_code='exchange_interrupted',ticket_hash=NULL,state_hash=NULL,cookie_hash=NULL,verifier=NULL,staged=NULL,completion_hash=NULL,updated_at=? WHERE status='exchanging'",
                    (now(),),
                )
        except BaseException:
            await self.close()
            raise

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            if self.db is None:
                raise RuntimeError("Store is not open")
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                await finish(self.db.commit())
            except BaseException:
                await finish(self.db.rollback())
                raise

    async def close(self):
        if self.db is not None:
            await finish(self.db.close())
            self.db = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
