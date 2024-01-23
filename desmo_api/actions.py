from __future__ import annotations

import asyncio
import logging
import os

import ansible_runner
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .fsm import JailStateMachine

from . import hcloud_dns, db, models

logger = logging.getLogger(__name__)


def get_inventory(vars: Optional[dict] = None) -> dict:
    runner_hosts = os.environ["RUNNER_HOSTS"].split(",")
    hosts = {host: {} for host in runner_hosts}
    return {
        "all": {
            "vars": {
                "ansible_user": "automation",
                "ansible_python_interpreter": "/usr/local/bin/python",
                "jails_path": "/usr/local/jails",
                "media_path": "/usr/local/jails/media",
                "containers_path": "/usr/local/jails/containers",
                "ansible_ssh_common_args": "-o StrictHostKeyChecking=no",
                **(vars or {}),
            },
            "children": {"bsd_servers": {"hosts": hosts}},
        }
    }


def run_ansible_playbook(name, playbook: str, inventory: dict):
    data_dir = f"/tmp/ansible-isolation/{name}"
    os.makedirs(data_dir, exist_ok=True)
    with open(os.environ["SSH_KEY_FILE"], "r") as f:
        ssh_key = f.read()
    return ansible_runner.run_async(
        private_data_dir=data_dir,
        project_dir=os.environ.get("ANSIBLE_PROJECT_DIR")
        or f"{os.getcwd()}/ansible/project",
        playbook=playbook,
        inventory=inventory,
        ssh_key=ssh_key,
    )


async def prepare_runners():
    inventory = get_inventory()
    _thread, runner = run_ansible_playbook("runners", "prepare_runners.yaml", inventory)
    while runner.rc is None:
        await asyncio.sleep(1)
    if runner.rc != 0:
        raise Exception(f"Ansible failed with {runner.rc}")


async def jail_provisioning(jail_info: models.JailInfo):
    vars = {
        "jail_host": jail_info.host,
        "jail_name": jail_info.name,
        "jail_ipv6": jail_info.ip,
    }
    inventory = get_inventory(vars)
    _thread, runner = run_ansible_playbook(
        jail_info.name, "create_jail.yaml", inventory
    )
    while runner.rc is None:
        await asyncio.sleep(1)
    if runner.rc != 0:
        raise Exception(f"Ansible failed with {runner.rc}")


async def start_jail_provisioning(fsm: JailStateMachine, database: db.DB, name: str):
    logger.info("Starting provisioning for jail %s", name)
    try:
        jail_info = await database.get_jail(name)
        await jail_provisioning(jail_info=jail_info)
        fsm.jail_provisioned()
    except Exception as e:
        logger.error("Jail provisioning failed for %s", name, exc_info=e)
        fsm.jail_provisioning_failed()


async def dns_provisioning(
    dns_client: hcloud_dns.HCloudDNS, jail_info: models.JailInfo
):
    zone_id = os.environ["HCLOUD_DNS_ZONE_ID"]
    name = jail_info.name
    ipv6 = jail_info.ip
    records = await dns_client.get_records_by_name(zone_id, name)
    for record in records:
        logger.info("Deleting existing record %s for jail %s", record.id, name)
        await dns_client.delete_record(record.id)

    logger.info("Creating AAAA record for jail %s", name)
    await dns_client.create_record(
        zone_id=zone_id,
        name=f"{name}.jail",
        record_type="AAAA",
        value=ipv6,
    )


async def start_dns_provisioning(
    fsm: JailStateMachine,
    database: db.DB,
    dns_client: hcloud_dns.HCloudDNS,
    name: str,
):
    logger.info("Starting DNS provisioning for jail %s", name)
    try:
        jail_info = await database.get_jail(name)
        await dns_provisioning(dns_client, jail_info)
        fsm.dns_provisioned()
    except Exception as e:
        logger.error("DNS provisioning failed for server %s", name, exc_info=e)
        fsm.dns_provisioning_failed()


async def jail_setup(
    jail_info: models.JailInfo, packages: list[str], commands: list[str]
):
    vars = {
        "jail_host": jail_info.host,
        "jail_name": jail_info.name,
        "jail_packages": packages,
        "jail_commands": commands,
    }
    inventory = get_inventory(vars)
    _thread, runner = run_ansible_playbook(jail_info.name, "setup_jail.yaml", inventory)
    while runner.rc is None:
        await asyncio.sleep(1)
    if runner.rc != 0:
        raise Exception(f"Ansible failed with {runner.rc}")


async def start_jail_setup(fsm: JailStateMachine, database: db.DB, name: str):
    logger.info("Starting jail setup for jail %s", name)
    try:
        jail_info = await database.get_jail(name)
        packages = await database.get_jail_packages(name)
        commands = await database.get_jail_commands(name)
        await jail_setup(jail_info=jail_info, packages=packages, commands=commands)
        fsm.jail_setup_done()
    except Exception as e:
        logger.error("Jail setup failed for jail %s", name, exc_info=e)
        fsm.jail_setup_failed()


async def start_jail_watch(fsm: JailStateMachine, database: db.DB, name: str):
    logger.info("Starting healtcheck for jail %s", name)
    try:
        logger.info("not implemented lol")
    except Exception as e:
        logger.error("Server healtcheck failed for jail %s", name, exc_info=e)


async def jail_removal(jail_info: models.JailInfo):
    vars = {
        "jail_host": jail_info.host,
        "jail_name": jail_info.name,
    }
    inventory = get_inventory(vars)
    _thread, runner = run_ansible_playbook(
        jail_info.name, "delete_jail.yaml", inventory
    )
    while runner.rc is None:
        await asyncio.sleep(1)
    if runner.rc != 0:
        raise Exception(f"Ansible failed with {runner.rc}")


async def start_jail_removal(fsm: JailStateMachine, database: db.DB, name: str):
    logger.info("Starting removal of jail %s", name)
    try:
        jail_info = await database.get_jail(name)
        await jail_removal(jail_info)
        fsm.jail_removed()
    except Exception as e:
        logger.error("Stop server failed for server %s, ignoring", name, exc_info=e)
        fsm.jail_removal_failed()


async def dns_deprovisioning(dns_client: hcloud_dns.HCloudDNS, name: str):
    zone_id = os.environ["HCLOUD_DNS_ZONE_ID"]

    records = await dns_client.get_records_by_name(zone_id, f"{name}.jail")
    for record in records:
        logger.info("Deleting record %s for jail %s", record.id, name)
        await dns_client.delete_record(record.id)


async def start_dns_deprovisioning(
    fsm: JailStateMachine, dns_client: hcloud_dns.HCloudDNS, database: db.DB, name: str
):
    logger.info("Starting DNS deprovisioning for jail %s", name)
    try:
        await dns_deprovisioning(dns_client, name)
        await database.set_jail_state(
            name, "terminated"
        )  # Have to do this here because the tasks will be cancelled
        fsm.dns_deprovisioned()
    except Exception as e:
        logger.error("DNS deprovisioning failed for jail %s", name, exc_info=e)
        fsm.dns_deprovisioning_failed()
