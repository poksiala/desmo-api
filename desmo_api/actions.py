from __future__ import annotations

import asyncio
import os
from typing import Optional

import ansible_runner

from . import desmo_api_client, hcloud_dns, log, models
from .enums import JailEvent

logger = log.get_logger(__name__)


class Clients:
    def __init__(self, dns: hcloud_dns.HCloudDNS, api: desmo_api_client.DesmoApiClient):
        self.dns: hcloud_dns.HCloudDNS = dns
        self.api: desmo_api_client.DesmoApiClient = api

    async def stop(self):
        await self.dns.close()
        await self.api.close()


def get_inventory(vars: Optional[dict] = None) -> dict:
    runner_hosts = os.environ["RUNNER_HOSTS"].split(",")
    hosts = {host: {} for host in runner_hosts}
    return {
        "all": {
            "vars": {
                "ansible_user": "automation",
                "ansible_python_interpreter": "/usr/local/bin/python3",
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
        "jail_base": jail_info.base,
    }
    inventory = get_inventory(vars)
    _thread, runner = run_ansible_playbook(
        jail_info.name, "create_jail.yaml", inventory
    )
    while runner.rc is None:
        await asyncio.sleep(1)
    if runner.rc != 0:
        raise Exception(f"Ansible failed with {runner.rc}")


async def start_jail_provisioning(clients: Clients, name: str) -> None:
    logger.info("Starting provisioning for jail {}", name)
    try:
        jail_info = await clients.api.get_jail_or_raise(name)
        await jail_provisioning(jail_info=jail_info)
        await clients.api.create_jail_event(name, JailEvent.jail_provisioned)
    except Exception as e:
        logger.error("Jail provisioning failed for {}", name, exc_info=e)
        await clients.api.create_jail_event(name, JailEvent.jail_provisioning_failed)


async def dns_provisioning(
    dns_client: hcloud_dns.HCloudDNS, jail_info: models.JailInfo
):
    zone_id = os.environ["HCLOUD_DNS_ZONE_ID"]
    name = jail_info.name
    ipv6 = jail_info.ip
    records = await dns_client.get_records_by_name(zone_id, name)
    for record in records:
        logger.info("Deleting existing record {} for jail {}", record.id, name)
        await dns_client.delete_record(record.id)

    logger.info("Creating AAAA record for jail {}", name)
    await dns_client.create_record(
        zone_id=zone_id,
        name=f"{name}.jail",
        record_type="AAAA",
        value=ipv6,
    )


async def start_dns_provisioning(clients: Clients, name: str) -> None:
    logger.info("Starting DNS provisioning for jail {}", name)
    try:
        jail_info = await clients.api.get_jail_or_raise(name)
        await dns_provisioning(clients.dns, jail_info)
        await clients.api.create_jail_event(name, JailEvent.dns_provisioned)
    except Exception as e:
        logger.error("DNS provisioning failed for server {}", name, exc_info=e)
        await clients.api.create_jail_event(name, JailEvent.dns_provisioning_failed)


async def run_jail_cmd(jail_info: models.JailInfo, command: str):
    vars = {
        "jail_host": jail_info.host,
        "jail_name": jail_info.name,
        "jail_command": command,
    }
    inventory = get_inventory(vars)
    _thread, runner = run_ansible_playbook(jail_info.name, "run_CMD.yaml", inventory)
    while runner.rc is None:
        await asyncio.sleep(1)
    if runner.rc != 0:
        raise Exception(f"Ansible failed with {runner.rc}")


async def run_jail_pkg(jail_info: models.JailInfo, packages: list[str]):
    vars = {
        "jail_host": jail_info.host,
        "jail_name": jail_info.name,
        "jail_packages": packages,
    }
    inventory = get_inventory(vars)
    _thread, runner = run_ansible_playbook(jail_info.name, "run_PKG.yaml", inventory)
    while runner.rc is None:
        await asyncio.sleep(1)
    if runner.rc != 0:
        raise Exception(f"Ansible failed with {runner.rc}")


async def run_jail_copy(
    jail_info: models.JailInfo,
    image_url: str,
    image_digest: str,
    copy_src: str,
    copy_dest: str,
):
    vars = {
        "jail_host": jail_info.host,
        "jail_name": jail_info.name,
        "image_url": image_url,
        "image_digest": image_digest,
        "copy_src": copy_src,
        "copy_dest": copy_dest,
    }
    inventory = get_inventory(vars)
    _thread, runner = run_ansible_playbook(jail_info.name, "run_COPY.yaml", inventory)
    while runner.rc is None:
        await asyncio.sleep(1)
    if runner.rc != 0:
        raise Exception(f"Ansible failed with {runner.rc}")


async def jail_setup(jail_info: models.JailInfo, clients: Clients):
    assert jail_info.image_digest is not None, "Image digest does not exist!"
    desmofile = await clients.api.get_desmofile(jail_info.image_digest)
    for line in desmofile.split("\n"):
        logger.info("Processing line {}", line)
        parts = line.split(" ")
        if len(parts) == 1 and parts[0] == "":
            continue
        elif parts[0] == "PKG":
            await run_jail_pkg(jail_info=jail_info, packages=parts[1:])
        elif parts[0] == "CMD" or parts[0] == "ENTRYPOINT":
            await run_jail_cmd(jail_info=jail_info, command=" ".join(parts[1:]))
        elif parts[0] == "COPY":
            image_url = clients.api.format_image_dl_link(jail_info.image_digest)
            assert len(parts) == 3, "Wrong number of parts for COPY command"
            await run_jail_copy(
                jail_info=jail_info,
                image_url=image_url,
                image_digest=jail_info.image_digest,
                copy_src=parts[1],
                copy_dest=parts[2],
            )
        else:
            raise ValueError(f"Invalid command {parts[0]}")


async def start_jail_setup(clients: Clients, name: str) -> None:
    logger.info("Starting jail setup for jail {}", name)
    try:
        jail_info = await clients.api.get_jail_or_raise(name)
        await jail_setup(jail_info=jail_info, clients=clients)
        await clients.api.create_jail_event(name, JailEvent.jail_setup_done)
    except Exception as e:
        logger.error("Jail setup failed for jail {}", name, exc_info=e)
        await clients.api.create_jail_event(name, JailEvent.jail_setup_failed)


async def start_jail_watch(clients: Clients, name: str) -> None:
    logger.info("Starting healtcheck for jail {}", name)
    try:
        logger.info("not implemented lol")
    except Exception as e:
        logger.error("Server healtcheck failed for jail {}", name, exc_info=e)


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


async def start_jail_removal(clients: Clients, name: str) -> None:
    logger.info("Starting removal of jail {}", name)
    try:
        jail_info = await clients.api.get_jail_or_raise(name)
        await jail_removal(jail_info)
        await clients.api.create_jail_event(name, JailEvent.jail_removed)
    except Exception as e:
        logger.error("Stop server failed for server {}, ignoring", name, exc_info=e)
        await clients.api.create_jail_event(name, JailEvent.jail_removal_failed)


async def dns_deprovisioning(dns_client: hcloud_dns.HCloudDNS, name: str):
    zone_id = os.environ["HCLOUD_DNS_ZONE_ID"]

    records = await dns_client.get_records_by_name(zone_id, f"{name}.jail")
    for record in records:
        logger.info("Deleting record {} for jail {}", record.id, name)
        await dns_client.delete_record(record.id)


async def start_dns_deprovisioning(clients: Clients, name: str) -> None:
    logger.info("Starting DNS deprovisioning for jail {}", name)
    try:
        await dns_deprovisioning(clients.dns, name)
        await clients.api.create_jail_event(name, JailEvent.dns_deprovisioned)
    except Exception as e:
        logger.error("DNS deprovisioning failed for jail {}", name, exc_info=e)
        await clients.api.create_jail_event(name, JailEvent.dns_deprovisioning_failed)
