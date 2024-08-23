import asyncio
import aiohttp

import os
from . import enums
from . import models
from . import log

logger = log.get_logger(__name__)


class DesmoApiClient:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DesmoApiClient, cls).__new__(cls)
        return cls._instance

    def __init__(self, api: str = "http://localhost:8000"):
        self._api = api
        self._token = os.environ["API_KEY"]
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={
                    "X-Api-Key": self._token,
                    "Content-Type": "application/json; charset=utf-8",
                }
            )
        return self._session

    async def close(self):
        if self._session is not None:
            await self._session.close()
            await asyncio.sleep(0.250)

    async def get_jail_info(self, name: str) -> models.FullJailInfoResponse | None:
        session = self._get_session()
        async with session.get(f"{self._api}/jails/{name}") as resp:
            if resp.status == 200:
                data = await resp.json()
                return models.FullJailInfoResponse.model_validate(data)
            elif resp.status == 404:
                return None
            else:
                logger.error(
                    "Unknown status code for jail info request %s", resp.status
                )
                raise ValueError("Unknown status code for jail info request")

    async def get_jail_or_raise(self, name: str) -> models.FullJailInfoResponse:
        res = await self.get_jail_info(name)
        if res is None:
            raise ValueError("No jail found")
        return res

    async def create_jail_event(self, name: str, event: enums.JailEvent):
        session = self._get_session()
        req = models.CreateJailEventRequest(event=event)
        async with session.post(
            f"{self._api}/jails/{name}/events", json=req.model_dump()
        ) as resp:
            resp.raise_for_status()

    async def set_jail_state(self, name: str, state: enums.JailState):
        session = self._get_session()
        req = models.UpdateJailRequest(state=state)
        async with session.patch(
            f"{self._api}/jails/{name}", json=req.model_dump()
        ) as resp:
            resp.raise_for_status()
