from typing import List, Optional
import asyncio
import asyncpg
from PgQueuer.db import AsyncpgDriver
from PgQueuer.queries import Queries

from . import models
from . import enums
from . import log

logger = log.get_logger(__name__)

JAIL_EVENT_QUEUE = "JAIL_EVENT"

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
    [
        """
        CREATE TYPE pgqueuer_status AS ENUM ('queued', 'picked');
        """,
        """
        CREATE TABLE pgqueuer (
            id SERIAL PRIMARY KEY,
            priority INT NOT NULL,
            created TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            updated TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            status pgqueuer_status NOT NULL,
            entrypoint TEXT NOT NULL,
            payload BYTEA
        );
        """,
        """
        CREATE INDEX pgqueuer_priority_id_id1_idx ON pgqueuer (priority ASC, id DESC)
            INCLUDE (id) WHERE status = 'queued';
        """,
        """
        CREATE INDEX pgqueuer_updated_id_id1_idx ON pgqueuer (updated ASC, id DESC)
            INCLUDE (id) WHERE status = 'picked';
        """,
        """
        CREATE TYPE pgqueuer_statistics_status AS ENUM ('exception', 'successful');
        """,
        """
        CREATE TABLE pgqueuer_statistics (
            id SERIAL PRIMARY KEY,
            created TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT DATE_TRUNC('sec', NOW() at time zone 'UTC'),
            count BIGINT NOT NULL,
            priority INT NOT NULL,
            time_in_queue INTERVAL NOT NULL,
            status pgqueuer_statistics_status NOT NULL,
            entrypoint TEXT NOT NULL
        );
        """,
        """
        CREATE UNIQUE INDEX pgqueuer_statistics_unique_count ON pgqueuer_statistics (
            priority,
            DATE_TRUNC('sec', created at time zone 'UTC'),
            DATE_TRUNC('sec', time_in_queue),
            status,
            entrypoint
        );
        """,
        """
        CREATE FUNCTION fn_pgqueuer_changed() RETURNS TRIGGER AS $$
        DECLARE
            to_emit BOOLEAN := false;  -- Flag to decide whether to emit a notification
        BEGIN
            -- Check operation type and set the emit flag accordingly
            IF TG_OP = 'UPDATE' AND OLD IS DISTINCT FROM NEW THEN
                to_emit := true;
            ELSIF TG_OP = 'DELETE' THEN
                to_emit := true;
            ELSIF TG_OP = 'INSERT' THEN
                to_emit := true;
            ELSIF TG_OP = 'TRUNCATE' THEN
                to_emit := true;
            END IF;

            -- Perform notification if the emit flag is set
            IF to_emit THEN
                PERFORM pg_notify(
                    'ch_pgqueuer',
                    json_build_object(
                        'channel', 'ch_pgqueuer',
                        'operation', lower(TG_OP),
                        'sent_at', NOW(),
                        'table', TG_TABLE_NAME,
                        'type', 'table_changed_event'
                    )::text
                );
            END IF;

            -- Return appropriate value based on the operation
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                RETURN OLD;
            ELSE
                RETURN NULL; -- For TRUNCATE and other non-row-specific contexts
            END IF;

        END;
        $$ LANGUAGE plpgsql;
        """,
        """
        CREATE TRIGGER tg_pgqueuer_changed
        AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pgqueuer
        EXECUTE FUNCTION fn_pgqueuer_changed();
        """,
    ],
    [
        """DROP TABLE prison_command;""",
        """DROP TABLE prison_package;""",
        """DROP TABLE jail_command;""",
        """DROP TABLE jail_package;""",
    ],
    [
        """
        CREATE TABLE image (
            digest TEXT PRIMARY KEY,
            data BYTEA,
            desmofile TEXT
        );
        """,
        """
        ALTER TABLE jail ADD COLUMN image_digest text DEFAULT NULL;
        """,
        """
        ALTER TABLE prison ADD COLUMN image_digest text DEFAULT NULL;
        """,
    ],
]


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
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT base, name, state, ip, host, image_digest FROM jail;"
        )
        return [models.JailInfo(**row) for row in rows]

    async def get_jail(self, name: str) -> models.JailInfo | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
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
        pool = await self._get_pool()
        await pool.execute("DELETE FROM jail WHERE name = $1;", name)

    async def set_jail_state(self, name: str, state: str) -> None:
        pool = await self._get_pool()
        await pool.execute("UPDATE jail SET state = $1 WHERE name = $2;", state, name)

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
        pool = await self._get_pool()
        await pool.execute(
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
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT name, base, replicas, image_digest FROM prison;"
        )
        return [models.PrisonInfo(**row) for row in rows]

    async def insert_prison(
        self, name: str, base: str, replicas: int, image_digest: str
    ) -> None:
        pool = await self._get_pool()
        await pool.execute(
            "INSERT INTO prison (name, base, replicas, image_digest) VALUES ($1, $2, $3, $4);",
            name,
            base,
            replicas,
            image_digest,
        )

    async def get_prison(self, name: str) -> models.PrisonInfo | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
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
        pool = await self._get_pool()
        await pool.execute("DELETE FROM prison WHERE name = $1;", name)

    async def get_prison_jails(self, name: str) -> List[models.JailInfo]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT base, name, state, ip, host, image_digest FROM jail WHERE prison_name = $1;",
            name,
        )
        return [models.JailInfo(**row) for row in rows]

    async def update_prison_replicas(self, name: str, replicas: int) -> None:
        pool = await self._get_pool()
        await pool.execute(
            "UPDATE prison SET replicas = $1 WHERE name = $2;", replicas, name
        )

    async def clean_jails(self):
        pool = await self._get_pool()
        await pool.execute("DELETE FROM jail WHERE state = $1;", "terminated")

    async def insert_image(self, digest: str, data: bytes, desmofile: str):
        pool = await self._get_pool()
        try:
            await pool.execute(
                "INSERT INTO image (digest, data, desmofile) VALUES ($1, $2, $3)",
                digest,
                data,
                desmofile,
            )
        except asyncpg.exceptions.UniqueViolationError:
            logger.info("image with digest {} already exists", digest)

    async def get_image_data(self, digest: str) -> bytes | None:
        pool = await self._get_pool()
        return await pool.fetchval("SELECT data FROM image WHERE digest = $1;", digest)

    async def get_desmofile(self, digest: str) -> str | None:
        pool = await self._get_pool()
        return await pool.fetchval(
            "SELECT desmofile FROM image WHERE digest = $1;", digest
        )

    async def queue_jail_event(self, jail_name: str, event: enums.JailEvent):
        logger.info("Queueing jail event")
        event_data = (
            models.JailEventQueueObject(name=jail_name, event=event)
            .model_dump_json()
            .encode()
        )
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            driver = AsyncpgDriver(conn)
            queries = Queries(driver)
            await queries.enqueue(JAIL_EVENT_QUEUE, event_data)
        logger.info("JailEvent Queued")
