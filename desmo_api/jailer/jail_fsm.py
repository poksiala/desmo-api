import asyncio
import time
from typing import Awaitable, Callable, Dict, Optional

from femtoqueue import FemtoQueue
from pydantic import ValidationError
from tilakone import StateChart, StateMachine

from .. import actions, log, models
from ..desmo_api_client import DesmoApiClient
from ..enums import JailEvent, JailState
from ..hcloud_dns import HCloudDNS

logger = log.get_logger(__name__)


TASK_TIMEOUT = 5 * 60 * 1000  # Five minutes in milliseconds

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
    logger.info("Processing event {} for jail {}.", event, jail_name)
    if jail is None:
        logger.warning("Event {} received for non existing jail {}", event, jail_name)
        return
    fsm = StateMachine(state_chart, initial_state=jail.state)
    transitioned = fsm.send(event)
    if not transitioned:
        logger.warning(
            "Discarding irrelevant event `{}` for jail `{}` in state `{}`.",
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
                "Starting action for jail `{}` in state `{}`.", jail_name, new_state
            )
            await action(clients, jail_name)


class JailEventWorker:
    def __init__(self, node_id: str, queue_data_dir: str = "task_data"):
        self._task: asyncio.Task | None = None
        self._qm = FemtoQueue(
            data_dir=queue_data_dir, node_id=node_id, timeout_stale_ms=TASK_TIMEOUT
        )
        self._alive = True

    async def _process(self):
        logger.info("JailEventWorkerStarted")
        lock = asyncio.Lock()
        clients = actions.Clients(dns=HCloudDNS(), api=DesmoApiClient())

        while self._alive:
            task = self._qm.pop()
            if task is None:
                await asyncio.sleep(1)
                continue
            logger.info("Processing task {}", task.id)
            try:
                payload = models.JailEventQueueObject.model_validate_json(
                    task.data.decode()
                )
                async with lock:
                    await process_jail_event(clients, payload.name, payload.event)
                self._qm.done(task)
            except ValidationError as e:
                logger.error(
                    "Event decode failed for event {}. Discarding event.",
                    task.id,
                    exec_info=e,
                )
            except Exception as e:
                logger.error(
                    "Event processing failed for event {}", task.id, exec_info=e
                )
                await asyncio.sleep(1)

        await clients.stop()

    async def run(self):
        await self._process()

    def start(self):
        logger.info("Starting JailEventWorker")
        self._task = asyncio.create_task(self._process())

    async def stop(self):
        logger.info("Shutting down JailEventWorker")
        self._alive = False
        if self._task is not None:
            logger.info("awaiting task")
            await self._task

    def start_shutdown(self):
        self._alive = False


class JailEventWriter:
    # Write only worker
    def __init__(self, queue_data_dir: str = "task_data"):
        self._qm = FemtoQueue(
            data_dir=queue_data_dir, node_id="writer", timeout_stale_ms=TASK_TIMEOUT
        )

    def push(
        self, jail_name: str, event: JailEvent, delay_seconds: Optional[int] = None
    ) -> str:
        ts_us = (
            int((time.time() + delay_seconds) * 1_000_000) if delay_seconds else None
        )

        logger.info("Queueing jail event")
        event_data = (
            models.JailEventQueueObject(name=jail_name, event=event)
            .model_dump_json()
            .encode()
        )

        return self._qm.push(data=event_data, time_us=ts_us)
