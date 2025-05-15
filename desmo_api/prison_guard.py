import asyncio
import os
import random
import secrets
from typing import Optional, Set

from . import db, hcloud_dns, log
from .enums import JailEvent
from .jailer.jail_fsm import JailEventWriter

logger = log.get_logger(__name__)


class PrisonGuard:
    def __init__(
        self,
        database: db.DB,
        dns_client: hcloud_dns.HCloudDNS,
        jail_event_writer: JailEventWriter,
    ):
        self._db = database
        self._dns_client = dns_client
        self._tasks: Set[asyncio.Task] = set()
        self._ew = jail_event_writer

    async def initialize(self):
        task = asyncio.create_task(self.reconcile_prisons())
        self._tasks.add(task)

    def select_host(self, preferred_host: Optional[str]) -> str:
        if preferred_host is not None:
            return preferred_host
        else:
            hosts = os.environ["RUNNER_HOSTS"].split(",")
            return random.choice(hosts)

    async def create_jail(
        self,
        name_prefix: str,
        base: str,
        image_digest: str,
        prison: Optional[str] = None,
        preferred_host: Optional[str] = None,
    ) -> str:
        first_part = secrets.token_hex(2)
        second_part = secrets.token_hex(2)
        name = f"{name_prefix}-{first_part}-{second_part}"
        ip = os.environ["NETWORK_PREFIX"] + "::" + first_part + ":" + second_part
        host = self.select_host(preferred_host)
        state = "uninitialized"

        await self._db.insert_jail(
            name, host, ip, state, base, image_digest, prison=prison
        )

        self._ew.push(name, JailEvent.initialize)
        return name

    async def create_prison(
        self,
        name: str,
        base: str,
        replicas: int,
        image_digest: str,
    ) -> None:
        await self._db.insert_prison(name, base, replicas, image_digest)

    def remove_jail(self, name: str) -> None:
        self._ew.push(name, JailEvent.remove_jail)

    async def reconcile_prison(self, name: str):
        logger.info("Reconciling prison {}", name)
        prison = await self._db.get_prison_or_raise(name)
        assert prison.image_digest is not None, "Image digest should not be none"
        jails = await self._db.get_prison_jails(name)
        live_jails = [j for j in jails if j.state != "terminated"]
        if len(live_jails) < prison.replicas:
            logger.info(
                "Creating {} jails for prison {}",
                prison.replicas - len(live_jails),
                name,
            )
            for i in range(prison.replicas - len(live_jails)):
                await self.create_jail(
                    name_prefix=name,
                    base=prison.base,
                    image_digest=prison.image_digest,
                    prison=name,
                )
        elif len(live_jails) > prison.replicas:
            remove_count = len(live_jails) - prison.replicas
            logger.info("Trying to remove {} jails for prison {}", remove_count, name)
            ready_jails = [j for j in live_jails if j.state == "jail_ready"]
            if len(ready_jails) < remove_count:
                logger.info("Not enough ready jails to remove for prison {}", name)
                return

            random.shuffle(ready_jails)
            for i in range(remove_count):
                self.remove_jail(ready_jails[i].name)

    async def reconcile_prisons(self):
        try:
            logger.info("Reconciling prisons")
            prisons = await self._db.get_prisons()
            for prison in prisons:
                await self.reconcile_prison(prison.name)
        except Exception as e:
            logger.error("Prison reconciliation failed: {}", e)
        finally:
            # There must be a better way to do this
            await asyncio.sleep(10)
            task = asyncio.create_task(self.reconcile_prisons())
            self._tasks.add(task)

    async def update_prison_replicas(self, name: str, replicas: int):
        prison = await self._db.get_prison_or_raise(name)
        if prison.replicas == replicas:
            return
        await self._db.update_prison_replicas(name, replicas)

    def stop(self):
        for task in self._tasks:
            task.cancel()
