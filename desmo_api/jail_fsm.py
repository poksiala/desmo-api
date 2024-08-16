import asyncio
import logging
from tilakone import StateMachine, StateChart
from typing import Awaitable, Callable, Dict


from . import actions
from . import db
from .enums import JailState, JailEvent


logger = logging.getLogger(__name__)


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

action_mapping: Dict[JailState, Callable[[db.DB, str], Awaitable[None]]] = {
    JailState.jail_provisioning: actions.start_jail_provisioning,
    JailState.dns_provisioning: actions.start_dns_provisioning,
    JailState.jail_setup: actions.start_jail_setup,
    JailState.jail_ready: actions.start_jail_watch,
    JailState.jail_removal: actions.start_jail_removal,
    JailState.dns_deprovisioning: actions.start_dns_deprovisioning,
}


async def process_jail_event(database: db.DB, jail_name: str, event: JailEvent) -> None:
    jail = await database.get_jail(jail_name)
    initial_state = None if jail is None else JailState(jail.state)
    fsm = StateMachine(state_chart, initial_state=initial_state)
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
        await database.set_jail_state(jail_name, new_state)
        action = action_mapping.get(new_state)
        if action is not None:
            logging.info(
                "Starting action for jail `%s` in state `%s`.", jail_name, new_state
            )
            await action(database, jail_name)


class JailEventWorker:
    def __init__(self, database: db.DB):
        self._db = database
        self._shutting_down = False
        self._task: asyncio.Task | None = None

    async def _process(self):
        while not self._shutting_down:
            job = self._db.get_jail_event_no_wait()
            if job is None:
                await asyncio.sleep(1)
            else:
                await process_jail_event(self._db, job.name, job.event)

    def start(self):
        self._task = asyncio.create_task(self._process())

    async def stop(self):
        self._shutting_down = True
        if self._task:
            await self._task
