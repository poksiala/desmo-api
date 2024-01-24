import asyncio
from collections.abc import Coroutine
import logging
from statemachine import StateMachine, State
from typing import Set


from . import actions
from . import hcloud_dns
from . import db


logger = logging.getLogger(__name__)


class JailStateMachine(StateMachine):
    def __init__(
        self,
        dns_client: hcloud_dns.HCloudDNS,
        database: db.DB,
        name: str,
    ):
        self._dns_client = dns_client
        self._name = name
        self._db = database
        self._tasks: Set[asyncio.Task] = set()

        super().__init__()

    uninitialized = State(initial=True)
    jail_provisioning = State()
    dns_provisioning = State()
    jail_setup = State()
    jail_ready = State()
    jail_removal = State()
    dns_deprovisioning = State()
    terminated = State()

    initialize = uninitialized.to(jail_provisioning) | terminated.to(jail_provisioning)
    jail_provisioned = jail_provisioning.to(dns_provisioning)
    jail_provisioning_failed = jail_provisioning.to(jail_provisioning)
    dns_provisioned = dns_provisioning.to(jail_setup)
    dns_provisioning_failed = dns_provisioning.to(dns_provisioning)
    jail_setup_done = jail_setup.to(jail_ready)
    jail_setup_failed = jail_setup.to(jail_setup)
    remove_jail = jail_ready.to(jail_removal)
    jail_removal_failed = jail_removal.to(jail_removal)
    jail_removed = jail_removal.to(dns_deprovisioning)
    dns_deprovisioned = dns_deprovisioning.to(terminated)
    dns_deprovisioning_failed = dns_deprovisioning.to(dns_deprovisioning)

    def start_on_enter_task(self):
        state = self.current_state.id
        switch = {
            "jail_provisioning": self.on_enter_jail_provisioning,
            "dns_provisioning": self.on_enter_dns_provisioning,
            "jail_setup": self.on_enter_jail_setup,
            "jail_ready": self.on_enter_jail_ready,
            "jail_removal": self.on_enter_jail_removal,
            "dns_deprovisioning": self.on_enter_dns_deprovisioning,
        }
        func = switch.get(state)
        if func is not None:
            func()

    async def _store_state_and_run(self, awaitable: Coroutine):
        await self._db.set_jail_state(self._name, self.current_state.id)
        await awaitable

    def on_enter_jail_provisioning(self):
        task = asyncio.create_task(
            self._store_state_and_run(
                actions.start_jail_provisioning(self, self._db, self._name)
            )
        )
        self._tasks.add(task)

    def on_enter_dns_provisioning(self):
        task = asyncio.create_task(
            self._store_state_and_run(
                actions.start_dns_provisioning(
                    self, self._db, self._dns_client, self._name
                )
            )
        )
        self._tasks.add(task)

    def on_enter_jail_setup(self):
        task = asyncio.create_task(
            self._store_state_and_run(
                actions.start_jail_setup(self, self._db, self._name)
            )
        )
        self._tasks.add(task)

    def on_enter_jail_ready(self):
        task = asyncio.create_task(
            self._store_state_and_run(
                actions.start_jail_watch(self, self._db, self._name)
            )
        )
        self._tasks.add(task)

    def on_enter_jail_removal(self):
        task = asyncio.create_task(
            self._store_state_and_run(
                actions.start_jail_removal(self, self._db, self._name)
            )
        )
        self._tasks.add(task)

    def on_enter_dns_deprovisioning(self):
        task = asyncio.create_task(
            self._store_state_and_run(
                actions.start_dns_deprovisioning(
                    self, self._dns_client, self._db, self._name
                )
            )
        )
        self._tasks.add(task)

    def on_enter_terminated(self):
        logger.info("Jail %s terminated", self._name)
        for task in self._tasks:
            task.cancel()

    def stop(self):
        for task in self._tasks:
            task.cancel()
