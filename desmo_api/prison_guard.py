import asyncio
import os
import random
import secrets
from typing import Iterable, Optional, Set

from desmo_api.models import JailInfo

from . import db, hcloud_dns, log
from .enums import JailEvent, JailState
from .jailer.jail_fsm import JailEventWriter

logger = log.get_logger(__name__)


async def update_prison_headless_dns(
    dns_client: hcloud_dns.HCloudDNS, prison_name: str, jails: Iterable[JailInfo]
):
    zone_id = os.environ["HCLOUD_DNS_ZONE_ID"]
    records = await dns_client.get_records_by_name(zone_id, "{prison_name}.svc")
    live_jails_ips = [jail.ip for jail in jails if jail.state == JailState.jail_ready]
    records_to_delete = [
        record.id for record in records if record.value not in live_jails_ips
    ]
    records_to_create = [  # O(n^2)
        ip for ip in live_jails_ips if ip not in [record.value for record in records]
    ]

    for record_id in records_to_delete:
        logger.info("Deleting dns record {} for prison {}", record_id, prison_name)
        await dns_client.delete_record(record_id=record_id)

    for ip in records_to_create:
        logger.info("Creating dns record for prison {} (ip: {})", prison_name, ip)
        await dns_client.create_record(
            zone_id=zone_id,
            name="{prison_name}.svc",
            record_type="AAAA",
            value=ip,
            ttl=300,
        )


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
        live_jails = [j for j in jails if j.state != JailState.terminated]
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

        # TODO: remove possibly removed jails from the list of live jails.
        await update_prison_headless_dns(self._dns_client, name, live_jails)

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
