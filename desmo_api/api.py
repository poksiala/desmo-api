import hashlib
import json
import os
import signal
from contextlib import asynccontextmanager
from io import BytesIO
from tarfile import TarFile
from typing import Annotated, Dict, List, Union

import pydantic
import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse

from desmo_api.enums import JailEvent

from . import db, log, models
from .hcloud_dns import HCloudDNS
from .jailer.jail_fsm import JailEventWriter
from .prison_guard import PrisonGuard

logger = log.get_logger(__name__)

DNS_CLIENT = HCloudDNS()
# database = db.DB(os.environ["DATABASE_DSN"])
database = db.SqliteDB()
JAIL_EVENT_WRITER = JailEventWriter()
GUARD = PrisonGuard(database, DNS_CLIENT, JAIL_EVENT_WRITER)


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


@app.get("/desmofile/{digest}")
async def get_desmofile(digest: str) -> models.DesmofileResponse:
    content = await database.get_desmofile(digest)
    if content is None:
        raise HTTPException(404, "Desmofile not found")
    return models.DesmofileResponse(content=content)


@app.get("/image/{digest}.tar.gz")
async def get_image_data(digest: str) -> StreamingResponse:
    data = await database.get_image_data(digest)
    if data is None:
        raise HTTPException(404, "File not found")
    file_obj = BytesIO(data)
    return StreamingResponse(
        file_obj,
        media_type="application/gzip",
        headers={"Content-Disposition": f"attachment; filename={digest}tar.gz"},
    )


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
    name = await GUARD.create_jail(req.name, req.base, req.image_digest)
    jail_info = await database.get_jail(name)
    assert jail_info is not None
    return models.FullJailInfoResponse(
        name=jail_info.name,
        state=jail_info.state,
        ip=jail_info.ip,
        host=jail_info.host,
        base=jail_info.base,
        image_digest=jail_info.image_digest,
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
        JAIL_EVENT_WRITER.push(name, req.event)
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
    return models.FullJailInfoResponse(
        name=jail.name,
        state=jail.state,
        ip=jail.ip,
        host=jail.host,
        base=jail.base,
        image_digest=jail.image_digest,
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
        JAIL_EVENT_WRITER.push(name, JailEvent.remove_jail)
    except KeyError:
        raise HTTPException(status_code=404, detail="Jail not found")
    return {"status": "ok"}


def calculate_sha256(file_obj) -> str:
    file_obj.seek(0)
    sha256_hash = hashlib.sha256()
    for chunk in iter(lambda: file_obj.read(4096), b""):
        sha256_hash.update(chunk)
    file_obj.seek(0)
    return sha256_hash.hexdigest()


@app.post("/prisons")
async def create_prison_from_file(
    name: str,
    replicas: int | None = None,
    base: str | None = None,
    image: UploadFile = File(...),
    x_api_key: Annotated[Union[str, None], Header()] = None,
):
    if not x_api_key or x_api_key != os.environ["API_KEY"]:
        raise HTTPException(status_code=401, detail="Unauthorized")

    digest = calculate_sha256(image.file)

    try:
        req = models.CreatePrisonRequest(
            name=name, image_digest=digest
        )  # Todo: there is probably a better way to this
        req = models.CreatePrisonRequest(
            name=name,
            image_digest=digest,
            replicas=replicas or req.replicas,
            base=base or req.base,
        )
    except pydantic.ValidationError as exc:
        raise HTTPException(400, json.loads(exc.json()))

    with TarFile.open(fileobj=image.file, mode="r:gz") as tar:
        try:
            file_obj = tar.extractfile("Desmofile")
        except KeyError:
            raise HTTPException(400, "Desmofile is missing")
        if file_obj is not None:
            content = file_obj.read().decode()
        else:
            raise HTTPException(400, "Invalid Desmofile")

    image.file.seek(0)
    await database.insert_image(digest, image.file.read(), content)
    await GUARD.create_prison(req.name, req.base, req.replicas, digest)
    return models.PrisonInfoResponse(
        name=req.name,
        base=req.base,
        replicas=req.replicas,
        dns=f"{req.name}.prison.{os.environ['DNS_ZONE']}",
        headless_svc=f"{req.name}.svc.{os.environ['DNS_ZONE']}",
        jails=[],
        image_digest=digest,
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
        headless_svc=f"{name}.svc.{os.environ['DNS_ZONE']}",
        jails=jails,
        image_digest=prison.image_digest,
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


async def main():
    config = uvicorn.Config("desmo_api.api:app", port=8080, log_level="info")
    server = uvicorn.Server(config)

    def handle_signal(sig, frame) -> None:
        print("handling signal")
        server.handle_exit(sig, frame)

    signal.signal(signal.SIGINT, handle_signal)
    await server.serve()
