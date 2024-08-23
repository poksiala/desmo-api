from fastapi import FastAPI, Header, HTTPException, UploadFile, File
from fastapi.responses import RedirectResponse
from typing import Dict, Annotated, Union, List
from contextlib import asynccontextmanager

from desmo_api.enums import JailEvent
from .hcloud_dns import HCloudDNS
import os
from . import db, models
from .prison_guard import PrisonGuard
from tarfile import TarFile
import json
from .jail_fsm import JailEventWorker
from . import log
import signal
import uvicorn


logger = log.get_logger(__name__)

DNS_CLIENT = HCloudDNS()
database = db.DB(os.environ["DATABASE_DSN"])

GUARD = PrisonGuard(database, DNS_CLIENT)

JAIL_EVENT_WORKER = JailEventWorker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Running migrations")
    await database.migrate()
    logger.info("Cleaning terminated jails from database")
    await database.clean_jails()
    logger.info("Preparing runners")
    # await prepare_runners()
    logger.info("Loading prisons from database")
    await GUARD.initialize()
    yield
    logger.info("Stopping Guard")
    GUARD.stop()
    logger.info("Closing asyncio clients")
    await DNS_CLIENT.close()
    await database.close()
    logger.info("Closed asyncio clients")


app = FastAPI(lifespan=lifespan)


@app.get("/stop")
def stop():
    os.kill(os.getpid(), signal.SIGKILL)
    return "ok"


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
    assert jail_info is not None
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


@app.post("/jails/{name}/events", status_code=201)
async def create_jail_event(
    name: str,
    req: models.CreateJailEventRequest,
    x_api_key: Annotated[Union[str, None], Header()] = None,
):
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        _ = await database.get_jail_or_raise(name)
        await database.queue_jail_event(name, req.event)
    except KeyError:
        raise HTTPException(status_code=404, detail="Jail not found")
    return {"status": "ok"}


@app.patch("/jails/{name}")
async def update_jail(
    name: str,
    req: models.UpdateJailRequest,
    x_api_key: Annotated[Union[str, None], Header()] = None,
):

    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        _ = await database.get_jail_or_raise(name)
        await database.set_jail_state(name, req.state)
    except KeyError:
        raise HTTPException(status_code=404, detail="Jail not found")
    return {"status": "ok"}


@app.get("/jails")
async def get_jails() -> List[models.JailInfo]:
    jails = await database.get_jails()
    return jails


@app.get("/jails/{name}")
async def get_jail(name: str) -> models.FullJailInfoResponse:
    jail = await database.get_jail(name)
    if jail is None:
        raise HTTPException(status_code=404, detail="Jail does not exist")
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
    try:
        _ = await database.get_jail_or_raise(name)
        await database.queue_jail_event(name, JailEvent.remove_jail)
    except KeyError:
        raise HTTPException(status_code=404, detail="Jail not found")
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
async def get_prison(name: str) -> models.PrisonInfoResponse | Dict[str, str]:
    prison = await database.get_prison(name)
    if prison is None:
        return {"error": "Prison does not exist"}
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
    if prison is None:
        return {"error": "prison does not exist"}
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
    with TarFile.open(fileobj=image.file, mode="r") as tar:
        members = tar.getmembers()
        try:
            manifest_json = tar.extractfile("./__desmometa/manifest.json")
        except KeyError:
            raise HTTPException(400, "manifest.json is missing")
    if manifest_json is None:
        raise HTTPException(400, "Invalid manifest.json")

    manifest = models.PrisonManifest(**json.load(manifest_json))

    names = [member.name for member in members]
    return {"filename": image.filename, "members": names, "manifest": manifest}


async def main():

    config = uvicorn.Config("desmo_api.api:app", port=8080, log_level="info")
    server = uvicorn.Server(config)

    def handle_signal(sig, frame) -> None:
        print("handling signal")
        server.handle_exit(sig, frame)

    signal.signal(signal.SIGINT, handle_signal)
    await server.serve()
