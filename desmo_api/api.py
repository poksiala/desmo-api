from fastapi import FastAPI, Header, HTTPException, UploadFile, File
from fastapi.responses import RedirectResponse
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
from .actions import prepare_runners
from .prison_guard import PrisonGuard
from tarfile import TarFile


logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("api")

DNS_CLIENT = HCloudDNS(os.environ["HCLOUD_DNS_KEY"])
database = db.DB(os.environ["DATABASE_DSN"])

GUARD = PrisonGuard(database, DNS_CLIENT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Running migrations")
    await database.migrate()
    logger.info("Preparing runners")
    await prepare_runners()
    logger.info("Loading jails from database")
    await GUARD.initialize()
    yield
    GUARD.stop()
    logger.info("Closing asyncio clients")
    await DNS_CLIENT.close()
    await database.close()
    logger.info("Closed asyncio clients")


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def read_root():
    return RedirectResponse("/docs")


@app.post(
    "/jails",
    status_code=201,
)
async def create_jail(
    req: models.CreateJailRequest,
    x_api_key: Annotated[Union[str, None], Header()] = None,
) -> models.FullJailInfoResponse:
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    name = await GUARD.create_jail(req.name, req.base, req.packages, req.commands)
    jail_info = await database.get_jail(name)
    return models.FullJailInfoResponse(
        name=jail_info.name,
        state=jail_info.state,
        ip=jail_info.ip,
        host=jail_info.host,
        base=jail_info.base,
        packages=req.packages,
        commands=req.commands,
        dns=f"{name}.jail.{os.environ['DNS_ZONE']}",
    )


@app.get("/jails")
async def get_jails() -> List[models.JailInfo]:
    jails = await database.get_jails()
    return jails


@app.get("/jails/{name}")
async def get_jail(name: str) -> models.FullJailInfoResponse | Dict[str, str]:
    if name not in GUARD.state_machines:
        return {"error": "server does not exist"}
    jail = await database.get_jail(name)
    packages = await database.get_jail_packages(name)
    commands = await database.get_jail_commands(name)
    return models.FullJailInfoResponse(
        name=jail.name,
        state=jail.state,
        ip=jail.ip,
        host=jail.host,
        base=jail.base,
        packages=packages,
        commands=commands,
        dns=f"{name}.jail.{os.environ['DNS_ZONE']}",
    )


@app.delete("/jails/{name}")
async def delete_jail(
    name: str, x_api_key: Annotated[Union[str, None], Header()] = None
) -> Dict[str, str]:
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if name not in GUARD.state_machines:
        return {"error": "jail does not exist"}
    try:
        GUARD.state_machines[name].remove_jail()
    except statemachine.exceptions.TransitionNotAllowed:
        return {"error": "jail is not ready"}
    await asyncio.sleep(1)
    return {"status": "ok"}


@app.post("/prisons", status_code=201)
async def create_prison(
    req: models.CreatePrisonRequest,
    x_api_key: Annotated[Union[str, None], Header()] = None,
) -> models.PrisonInfoResponse:
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    await GUARD.create_prison(
        req.name, req.base, req.replicas, req.packages, req.commands
    )
    return models.PrisonInfoResponse(
        name=req.name,
        base=req.base,
        replicas=req.replicas,
        dns=f"{req.name}.prison.{os.environ['DNS_ZONE']}",
        jails=[],
    )


@app.get("/prisons")
async def get_prisons() -> List[models.PrisonInfo]:
    prisons = await database.get_prisons()
    return prisons


@app.get("/prisons/{name}")
async def get_prison(name: str) -> models.PrisonInfoResponse:
    prison = await database.get_prison(name)
    jails = await database.get_prison_jails(name)
    return models.PrisonInfoResponse(
        name=prison.name,
        base=prison.base,
        replicas=prison.replicas,
        dns=f"{name}.prison.{os.environ['DNS_ZONE']}",
        jails=jails,
    )


@app.delete("/prisons/{name}")
async def delete_prison(
    name: str, x_api_key: Annotated[Union[str, None], Header()] = None
) -> Dict[str, str]:
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    prison = await database.get_prison(name)
    if prison.replicas > 0:
        return {"error": "prison is not empty"}
    await database.delete_prison(name)
    return {"status": "ok"}


@app.patch("/prisons/{name}")
async def update_prison(
    name: str,
    req: models.UpdatePrisonRequest,
    x_api_key: Annotated[Union[str, None], Header()] = None,
):
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if req.replicas is not None:
        await GUARD.update_prison_replicas(name, req.replicas)
    return {"status": "ok"}


@app.post("/v1/prisons")
async def create_prisonf_from_file(
    image: UploadFile = File(...),
    x_api_key: Annotated[Union[str, None], Header()] = None,
):
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    tar = TarFile.open(fileobj=image.file, mode="r:gz")
    members = tar.getmembers()
    names = [member.name for member in members]
    return {"filename": image.filename, "members": names}
