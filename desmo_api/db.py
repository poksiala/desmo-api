from typing import List, Optional

import logging
import asyncpg

from . import models

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
]


class DB:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: Optional[asyncpg.Connection] = None

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def _get_conn(self) -> asyncpg.Connection:
        if self._conn is None:
            self._conn = await asyncpg.connect(self.dsn)
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
                    "INSERT INTO meta (version) VALUES ($1)", version + i + 1
                )

    async def get_jails(self) -> List[models.JailInfo]:
        conn = await self._get_conn()
        rows = await conn.fetch("SELECT name, state, ip, host FROM jail;")
        return [models.JailInfo(**row) for row in rows]

    async def get_jail(self, name: str) -> models.JailInfo:
        conn = await self._get_conn()
        row = await conn.fetchrow(
            "SELECT name, state, ip, host FROM jail WHERE name = $1;", name
        )
        return models.JailInfo(**row)

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

    async def insert_jail(self, name, host, ip, state) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO jail (name, host, ip, state) VALUES ($1, $2, $3, $4);",
            name,
            host,
            ip,
            state,
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
