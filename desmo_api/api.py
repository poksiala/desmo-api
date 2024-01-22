from fastapi import FastAPI, Header, HTTPException
from typing import Dict, Annotated, Union, List
from .fsm import JailStateMachine
import logging
import sys
import asyncio
import statemachine.exceptions
from contextlib import asynccontextmanager
from .hcloud_dns import HCloudDNS
import os
from . import db, models
import random
import secrets

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("api")

STATE_MACHINES: Dict[str, JailStateMachine] = {}

DNS_CLIENT = HCloudDNS(os.environ["HCLOUD_DNS_KEY"])
database = db.DB(os.environ["DATABASE_DSN"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Running migrations")
    await database.migrate()
    logger.info("Loading servers from database")
    jails = await database.get_jails()
    logger.info("Loaded %s jails from database", len(jails))
    logger.info(jails)
    for jail in jails:
        logger.info("Loading server %s with state %s", jail.name, jail.state)
        _fsm = JailStateMachine(DNS_CLIENT, database, jail.name)
        _fsm.current_state_value = jail.state
        _fsm.start_on_enter_task()
        STATE_MACHINES[jail.name] = _fsm
    yield
    logger.info("Closing asyncio clients")
    await DNS_CLIENT.close()
    await database.close()
    logger.info("Closed asyncio clients")


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def read_root():
    return {"Hello": "World"}


@app.post(
    "/jails",
    status_code=201,
)
async def create_jail(
    req: models.CreateJailRequest,
    x_api_key: Annotated[Union[str, None], Header()] = None,
) -> models.FullJailInfo:
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    first_part = secrets.token_hex(2)
    second_part = secrets.token_hex(2)
    name = f"{req.name}-{first_part}-{second_part}"
    ip = os.environ["NETWORK_PREFIX"] + "::" + first_part + ":" + second_part
    hosts = os.environ["RUNNER_HOSTS"].split(",")
    host = random.choice(hosts)
    state = "uninitialized"

    await database.insert_jail(name, host, ip, state)
    for package in req.packages:
        await database.insert_jail_package(name, package)

    for i, command in enumerate(req.commands):
        await database.insert_jail_command(name, command, i)

    STATE_MACHINES[name] = JailStateMachine(DNS_CLIENT, database, name)
    STATE_MACHINES[name].initialize()
    await asyncio.sleep(1)
    return models.FullJailInfo(
        name=name,
        state=state,
        ip=ip,
        host=host,
        packages=req.packages,
        commands=req.commands,
    )


@app.get("/jails")
async def get_servers() -> List[models.JailInfo]:
    jails = await database.get_jails()
    return jails


@app.get("/jails/{name}")
async def get_server(name: str) -> models.FullJailInfo | Dict[str, str]:
    if name not in STATE_MACHINES:
        return {"error": "server does not exist"}
    jail = await database.get_jail(name)
    packages = await database.get_jail_packages(name)
    commands = await database.get_jail_commands(name)
    return models.FullJailInfo(
        name=jail.name,
        state=jail.state,
        ip=jail.ip,
        host=jail.host,
        packages=packages,
        commands=commands,
    )


@app.delete("/jails/{name}")
async def delete_server(
    name: str, x_api_key: Annotated[Union[str, None], Header()] = None
) -> Dict[str, str]:
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if name not in STATE_MACHINES:
        return {"error": "jail does not exist"}
    try:
        STATE_MACHINES[name].remove_jail()
    except statemachine.exceptions.TransitionNotAllowed:
        return {"error": "jail is not ready"}
    await asyncio.sleep(1)
    return {"status": "ok"}
