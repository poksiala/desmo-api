import asyncio
from typing import List, Optional

import asyncpg

from . import log, models
from .migrations import MIGRATIONS

logger = log.get_logger(__name__)

JAIL_EVENT_QUEUE = "JAIL_EVENT"

ArgType = str | int | bytes | None


class DB:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        pool = await asyncpg.create_pool(self.dsn)
        if pool is not None:
            self._pool = pool
            return pool
        logger.warning("Failed to aquire pool. Trying again after 1 second")
        await asyncio.sleep(1)
        return await self._get_pool()

    async def _execute(self, query: str, *args: ArgType) -> None:
        pool = await self._get_pool()

        await pool.execute(query, *args)

    async def _fetchrow(self, query: str, *args: ArgType) -> asyncpg.Record | None:
        pool = await self._get_pool()
        return await pool.fetchrow(query, *args)

    async def _fetch(self, query: str, *args: ArgType) -> List[asyncpg.Record]:
        pool = await self._get_pool()
        return await pool.fetch(query, *args)

    async def _fetchval(self, query: str, *args: ArgType):
        pool = await self._get_pool()
        return await pool.fetchval(query, *args)

    async def migrate(self):
        pool = await self._get_pool()
        # get version from meta table
        try:
            version = await pool.fetchval("SELECT MAX(version) FROM meta")
        except asyncpg.UndefinedTableError:
            version = -1

        assert type(version) is int, "version must be an integer"

        logger.info("Current database version: {}", version)
        async with pool.acquire() as conn:
            for i, migration in enumerate(MIGRATIONS[version + 1 :]):  # noqa: E203
                async with conn.transaction():
                    for query in migration:
                        logger.info("Running migration: {}", query)
                        await conn.execute(query)
                    await conn.execute(
                        "INSERT INTO meta (version) VALUES ($1) ON CONFLICT DO NOTHING",
                        version + i + 1,
                    )

    async def get_jails(self) -> List[models.JailInfo]:
        rows = await self._fetch(
            "SELECT base, name, state, ip, host, image_digest FROM jail;"
        )
        return [models.JailInfo(**row) for row in rows]

    async def get_jail(self, name: str) -> models.JailInfo | None:
        row = await self._fetchrow(
            "SELECT base, name, state, ip, host, image_digest FROM jail WHERE name = $1;",
            name,
        )
        return None if row is None else models.JailInfo(**row)

    async def get_jail_or_raise(self, name: str) -> models.JailInfo:
        res = await self.get_jail(name=name)
        if res is None:
            raise KeyError(f"Jail {name} does not exist in database")
        return res

    async def delete_jail(self, name: str) -> None:
        await self._execute("DELETE FROM jail WHERE name = $1;", name)

    async def set_jail_state(self, name: str, state: str) -> None:
        await self._execute("UPDATE jail SET state = $1 WHERE name = $2;", state, name)

    async def insert_jail(
        self,
        name,
        host,
        ip,
        state,
        base,
        image_digest: str,
        prison: Optional[str] = None,
    ) -> None:
        await self._execute(
            """INSERT INTO jail (
                    name, host, ip, state, base, prison_name, image_digest
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7
                );""",
            name,
            host,
            ip,
            state,
            base,
            prison,
            image_digest,
        )

    async def get_prisons(self) -> List[models.PrisonInfo]:
        rows = await self._fetch(
            "SELECT name, base, replicas, image_digest FROM prison;"
        )
        return [models.PrisonInfo(**row) for row in rows]

    async def insert_prison(
        self, name: str, base: str, replicas: int, image_digest: str
    ) -> None:
        await self._execute(
            "INSERT INTO prison (name, base, replicas, image_digest) VALUES ($1, $2, $3, $4);",
            name,
            base,
            replicas,
            image_digest,
        )

    async def get_prison(self, name: str) -> models.PrisonInfo | None:
        row = await self._fetchrow(
            "SELECT name, base, replicas, image_digest FROM prison WHERE name = $1;",
            name,
        )
        return None if row is None else models.PrisonInfo(**row)

    async def get_prison_or_raise(self, name: str) -> models.PrisonInfo:
        res = await self.get_prison(name=name)
        if res is None:
            raise KeyError(f"Prison {name} does not exist in database")
        return res

    async def delete_prison(self, name: str) -> None:
        await self._execute("DELETE FROM prison WHERE name = $1;", name)

    async def get_prison_jails(self, name: str) -> List[models.JailInfo]:
        rows = await self._fetch(
            "SELECT base, name, state, ip, host, image_digest FROM jail WHERE prison_name = $1;",
            name,
        )
        return [models.JailInfo(**row) for row in rows]

    async def update_prison_replicas(self, name: str, replicas: int) -> None:
        await self._execute(
            "UPDATE prison SET replicas = $1 WHERE name = $2;", replicas, name
        )

    async def clean_jails(self):
        await self._execute("DELETE FROM jail WHERE state = $1;", "terminated")

    async def insert_image(self, digest: str, data: bytes, desmofile: str):
        try:
            await self._execute(
                "INSERT INTO image (digest, data, desmofile) VALUES ($1, $2, $3)",
                digest,
                data,
                desmofile,
            )
        except asyncpg.exceptions.UniqueViolationError:
            logger.info("image with digest {} already exists", digest)

    async def get_image_data(self, digest: str) -> bytes | None:
        return await self._fetchval("SELECT data FROM image WHERE digest = $1;", digest)

    async def get_desmofile(self, digest: str) -> str | None:
        return await self._fetchval(
            "SELECT desmofile FROM image WHERE digest = $1;", digest
        )
