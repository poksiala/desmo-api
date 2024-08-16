from typing import List, Optional
import asyncio
import logging
import asyncpg

from . import models
from . import enums

logger = logging.getLogger(__name__)

MIGRATIONS = [
    [
        """
        CREATE TABLE meta (
            version integer PRIMARY KEY
        );
        """,
        "INSERT INTO meta (version) VALUES (0)",
    ],
    [
        """
        CREATE TABLE jail (
            name text PRIMARY KEY,
            host text NOT NULL,
            ip text NOT NULL UNIQUE,
            state text NOT NULL
        );
        """,
        """
        CREATE TABLE jail_package (
            jail_name text NOT NULL REFERENCES jail(name) ON DELETE CASCADE,
            name text NOT NULL
        );
        """,
        """
        CREATE TABLE jail_command (
            jail_name text NOT NULL REFERENCES jail(name) ON DELETE CASCADE,
            command text NOT NULL,
            order_no integer NOT NULL
        );
        """,
    ],
    [
        """
        CREATE TABLE prison (
            name text PRIMARY KEY,
            base text NOT NULL,
            replicas integer NOT NULL
        );
        """,
        """
        CREATE TABLE prison_package (
            prison_name text NOT NULL REFERENCES prison(name) ON DELETE CASCADE,
            name text NOT NULL
        );
        """,
        """
        CREATE TABLE prison_command (
            prison_name text NOT NULL REFERENCES prison(name) ON DELETE CASCADE,
            command text NOT NULL,
            order_no integer NOT NULL
        );
        """,
        """
        ALTER TABLE jail ADD COLUMN base text NOT NULL DEFAULT '14.0-RELEASE-base';
        """,
        """
        ALTER TABLE jail ADD COLUMN prison_name text REFERENCES prison(name) ON DELETE CASCADE DEFAULT NULL;
        """,
    ],
    [
        """CREATE table namespace (name text PRIMARY KEY);""",
        """INSERT INTO namespace (name) VALUES ('default') ON CONFLICT DO NOTHING;""",
        """ALTER TABLE jail ADD COLUMN namespace text REFERENCES namespace(name) ON DELETE RESTRICT DEFAULT 'default';""",
        """ALTER TABLE prison ADD COLUMN namespace text REFERENCES namespace(name) ON DELETE RESTRICT DEFAULT 'default';""",
    ],
]


class DB:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: Optional[asyncpg.Connection] = None
        self._fake_queue = asyncio.Queue[
            models.JailEventQueueObject
        ]()  # Temporary until persisted queue exists

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def _get_conn(self) -> asyncpg.Connection:
        if self._conn is None:
            conn = await asyncpg.connect(self.dsn)
            self._conn = conn
            return conn
        else:
            return self._conn

    async def migrate(self):
        conn = await self._get_conn()
        # get version from meta table
        try:
            version = await conn.fetchval("SELECT MAX(version) FROM meta")
        except asyncpg.UndefinedTableError:
            version = -1

        assert type(version) is int, "version must be an integer"

        logger.info("Current database version: %s", version)

        for i, migration in enumerate(MIGRATIONS[version + 1 :]):
            async with conn.transaction():
                for query in migration:
                    logger.info("Running migration: %s", query)
                    await conn.execute(query)
                await conn.execute(
                    "INSERT INTO meta (version) VALUES ($1) ON CONFLICT DO NOTHING",
                    version + i + 1,
                )

    async def get_jails(self) -> List[models.JailInfo]:
        conn = await self._get_conn()
        rows = await conn.fetch("SELECT base, name, state, ip, host FROM jail;")
        return [models.JailInfo(**row) for row in rows]

    async def get_jail(self, name: str) -> models.JailInfo | None:
        conn = await self._get_conn()
        row = await conn.fetchrow(
            "SELECT base, name, state, ip, host FROM jail WHERE name = $1;", name
        )
        return None if row is None else models.JailInfo(**row)

    async def get_jail_or_raise(self, name: str) -> models.JailInfo:
        res = await self.get_jail(name=name)
        if res is None:
            raise KeyError(f"Jail {name} does not exist in database")
        return res

    async def get_jail_packages(self, name: str) -> List[str]:
        conn = await self._get_conn()
        rows = await conn.fetch(
            "SELECT name FROM jail_package WHERE jail_name = $1;", name
        )
        return [row["name"] for row in rows]

    async def get_jail_commands(self, name: str) -> List[str]:
        conn = await self._get_conn()
        rows = await conn.fetch(
            "SELECT command FROM jail_command WHERE jail_name = $1 ORDER BY order_no;",
            name,
        )
        return [row["command"] for row in rows]

    async def delete_jail(self, name: str) -> None:
        conn = await self._get_conn()
        await conn.execute("DELETE FROM jail WHERE name = $1;", name)

    async def set_jail_state(self, name: str, state: str) -> None:
        conn = await self._get_conn()
        await conn.execute("UPDATE jail SET state = $1 WHERE name = $2;", state, name)

    async def insert_jail(
        self, name, host, ip, state, base, prison: Optional[str] = None
    ) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO jail (name, host, ip, state, base, prison_name) VALUES ($1, $2, $3, $4, $5, $6);",
            name,
            host,
            ip,
            state,
            base,
            prison,
        )

    async def insert_jail_package(self, name: str, package: str) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO jail_package (jail_name, name) VALUES ($1, $2);", name, package
        )

    async def insert_jail_command(self, name: str, command: str, order: int) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO jail_command (jail_name, command, order_no) VALUES ($1, $2, $3);",
            name,
            command,
            order,
        )

    async def get_prisons(self) -> List[models.PrisonInfo]:
        conn = await self._get_conn()
        rows = await conn.fetch("SELECT name, base, replicas FROM prison;")
        return [models.PrisonInfo(**row) for row in rows]

    async def get_prison_packages(self, name: str) -> List[str]:
        conn = await self._get_conn()
        rows = await conn.fetch(
            "SELECT name FROM prison_package WHERE prison_name = $1;", name
        )
        return [row["name"] for row in rows]

    async def get_prison_commands(self, name: str) -> List[str]:
        conn = await self._get_conn()
        rows = await conn.fetch(
            "SELECT command FROM prison_command WHERE prison_name = $1 ORDER BY order_no;",
            name,
        )
        return [row["command"] for row in rows]

    async def insert_prison(self, name: str, base: str, replicas: int) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO prison (name, base, replicas) VALUES ($1, $2, $3);",
            name,
            base,
            replicas,
        )

    async def insert_prison_package(self, name: str, package: str) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO prison_package (prison_name, name) VALUES ($1, $2);",
            name,
            package,
        )

    async def insert_prison_command(self, name: str, command: str, order: int) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO prison_command (prison_name, command, order_no) VALUES ($1, $2, $3);",
            name,
            command,
            order,
        )

    async def get_prison(self, name: str) -> models.PrisonInfo | None:
        conn = await self._get_conn()
        row = await conn.fetchrow(
            "SELECT name, base, replicas FROM prison WHERE name = $1;", name
        )
        return None if row is None else models.PrisonInfo(**row)

    async def get_prison_or_raise(self, name: str) -> models.PrisonInfo:
        res = await self.get_prison(name=name)
        if res is None:
            raise KeyError(f"Prison {name} does not exist in database")
        return res

    async def delete_prison(self, name: str) -> None:
        conn = await self._get_conn()
        await conn.execute("DELETE FROM prison WHERE name = $1;", name)

    async def get_prison_jails(self, name: str) -> List[models.JailInfo]:
        conn = await self._get_conn()
        rows = await conn.fetch(
            "SELECT base, name, state, ip, host FROM jail WHERE prison_name = $1;",
            name,
        )
        return [models.JailInfo(**row) for row in rows]

    async def update_prison_replicas(self, name: str, replicas: int) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "UPDATE prison SET replicas = $1 WHERE name = $2;", replicas, name
        )

    async def queue_jail_event(self, jail_name: str, event: enums.JailEvent):
        await self._fake_queue.put(
            models.JailEventQueueObject(name=jail_name, event=event)
        )

    async def get_jail_event(self) -> models.JailEventQueueObject:
        return await self._fake_queue.get()

    def get_jail_event_no_wait(self) -> models.JailEventQueueObject | None:
        try:
            return self._fake_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
