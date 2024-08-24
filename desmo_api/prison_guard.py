from . import db, hcloud_dns
from typing import List, Optional, Set
import secrets
import os
import random
import asyncio

from .enums import JailEvent
from . import log

logger = log.get_logger(__name__)


class PrisonGuard:
    def __init__(self, database: db.DB, dns_client: hcloud_dns.HCloudDNS):
        self._db = database
        self._dns_client = dns_client
        self._tasks: Set[asyncio.Task] = set()

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
        packages: List[str],
        commands: List[str],
        prison: Optional[str] = None,
        preferred_host: Optional[str] = None,
    ) -> str:
        first_part = secrets.token_hex(2)
        second_part = secrets.token_hex(2)
        name = f"{name_prefix}-{first_part}-{second_part}"
        ip = os.environ["NETWORK_PREFIX"] + "::" + first_part + ":" + second_part
        host = self.select_host(preferred_host)
        state = "uninitialized"

        # TODO: Transactions
        await self._db.insert_jail(name, host, ip, state, base, prison=prison)

        for package in packages:
            await self._db.insert_jail_package(name, package)

        for i, command in enumerate(commands):
            await self._db.insert_jail_command(name, command, i)

        await self._db.queue_jail_event(name, JailEvent.initialize)
        return name

    async def create_prison(
        self,
        name: str,
        base: str,
        replicas: int,
        packages: List[str],
        commands: List[str],
    ) -> None:
        await self._db.insert_prison(name, base, replicas)
        for package in packages:
            await self._db.insert_prison_package(name, package)
        for i, command in enumerate(commands):
            await self._db.insert_prison_command(name, command, i)

    async def remove_jail(self, name: str) -> None:
        await self._db.queue_jail_event(name, JailEvent.remove_jail)

    async def reconcile_prison(self, name: str):
        logger.info("Reconciling prison {}", name)
        prison = await self._db.get_prison_or_raise(name)
        jails = await self._db.get_prison_jails(name)
        live_jails = [j for j in jails if j.state != "terminated"]
        if len(live_jails) < prison.replicas:
            logger.info(
                "Creating {} jails for prison {}",
                prison.replicas - len(live_jails),
                name,
            )
            packages = await self._db.get_prison_packages(name)
            commands = await self._db.get_prison_commands(name)
            for i in range(prison.replicas - len(live_jails)):
                await self.create_jail(
                    name_prefix=name,
                    base=prison.base,
                    packages=packages,
                    commands=commands,
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
                await self.remove_jail(ready_jails[i].name)

    async def reconcile_prisons(self):
        try:
            logger.info("Reconciling prisons")
            prisons = await self._db.get_prisons()
            for prison in prisons:
                await self.reconcile_prison(prison.name)
        except Exception as e:
            logger.error("Prison reconciliation failed", exc_info=e)
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
