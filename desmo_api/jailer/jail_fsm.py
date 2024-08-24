import asyncio
import logging
from tilakone import StateMachine, StateChart
from typing import Awaitable, Callable, Dict
import os

from .. import actions
from .. import db
from .. import models
from ..enums import JailState, JailEvent
import asyncpg
from PgQueuer.db import AsyncpgDriver
from PgQueuer.qm import QueueManager
from PgQueuer.models import Job as PgQueuerJob
from .. import log
from ..hcloud_dns import HCloudDNS
from ..desmo_api_client import DesmoApiClient

logger = log.get_logger(__name__)


state_chart = StateChart[JailState, JailEvent](
    {
        JailState.uninitialized: {
            "initial": True,
            "on": {JailEvent.initialize: JailState.jail_provisioning},
        },
        JailState.jail_provisioning: {
            "on": {
                JailEvent.jail_provisioning_failed: JailState.jail_provisioning,
                JailEvent.jail_provisioned: JailState.dns_provisioning,
                JailEvent.remove_jail: JailState.jail_removal,
            }
        },
        JailState.dns_provisioning: {
            "on": {
                JailEvent.dns_provisioning_failed: JailState.dns_provisioning,
                JailEvent.dns_provisioned: JailState.jail_setup,
                JailEvent.remove_jail: JailState.jail_removal,
            }
        },
        JailState.jail_setup: {
            "on": {
                JailEvent.jail_setup_failed: JailState.jail_setup,
                JailEvent.jail_setup_done: JailState.jail_ready,
                JailEvent.remove_jail: JailState.jail_removal,
            }
        },
        JailState.jail_ready: {"on": {JailEvent.remove_jail: JailState.jail_removal}},
        JailState.jail_removal: {
            "on": {
                JailEvent.jail_removal_failed: JailState.jail_removal,
                JailEvent.jail_removed: JailState.dns_deprovisioning,
            }
        },
        JailState.dns_deprovisioning: {
            "on": {
                JailEvent.dns_deprovisioning_failed: JailState.dns_deprovisioning,
                JailEvent.dns_deprovisioned: JailState.terminated,
            }
        },
        JailState.terminated: {},
    }
)

action_mapping: Dict[JailState, Callable[[actions.Clients, str], Awaitable[None]]] = {
    JailState.jail_provisioning: actions.start_jail_provisioning,
    JailState.dns_provisioning: actions.start_dns_provisioning,
    JailState.jail_setup: actions.start_jail_setup,
    JailState.jail_ready: actions.start_jail_watch,
    JailState.jail_removal: actions.start_jail_removal,
    JailState.dns_deprovisioning: actions.start_dns_deprovisioning,
}


async def process_jail_event(
    clients: actions.Clients, jail_name: str, event: JailEvent
) -> None:
    jail = await clients.api.get_jail_info(jail_name)
    logger.info("Processing event %s for jail %s.", event, jail_name)
    if jail is None:
        logger.warning("Event %s received for non existing jail %s", event, jail_name)
        return
    fsm = StateMachine(state_chart, initial_state=jail.state)
    transitioned = fsm.send(event)
    if not transitioned:
        logging.warning(
            "Discarding irrelevant event `%s` for jail `%s` in state `%s`.",
            event,
            jail_name,
            fsm.current_state,
        )
    else:
        new_state = fsm.current_state
        await clients.api.set_jail_state(jail_name, new_state)
        action = action_mapping.get(new_state)
        if action is not None:
            logger.info(
                "Starting action for jail `%s` in state `%s`.", jail_name, new_state
            )
            await action(clients, jail_name)


class JailEventWorker:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._qm: QueueManager | None = None

    async def _process(self):
        logger.info("JailEventWorkerStarted")
        conn = await asyncpg.connect(os.environ["DATABASE_DSN"])
        driver = AsyncpgDriver(conn)
        qm = QueueManager(driver)
        self._qm = qm
        lock = asyncio.Lock()
        clients = actions.Clients(dns=HCloudDNS(), api=DesmoApiClient())

        @qm.entrypoint(db.JAIL_EVENT_QUEUE)
        async def process_message(job: PgQueuerJob) -> None:
            logger.info("Processing job %s", job.model_dump_json())
            payload = job.payload
            if payload is None:
                logger.warning("Job %s payload was none", job.id)
            else:
                job_payload = models.JailEventQueueObject.model_validate_json(
                    payload.decode()
                )
                async with lock:  # Only process one task at a time at this point
                    await process_jail_event(
                        clients, job_payload.name, job_payload.event
                    )

        await qm.run()
        await clients.stop()

    async def run(self):
        await self._process()

    def start(self):
        logger.info("Starting JailEventWorker")
        self._task = asyncio.create_task(self._process())

    async def stop(self):
        logger.info("Shutting down JailEventWorker")
        if self._qm is not None:
            logger.info("Setting alive to false")
            self._qm.alive = False
        if self._task is not None:
            logger.info("awaiting task")
            await self._task

    def start_shutdown(self):
        if self._qm is not None:
            self._qm.alive = False
