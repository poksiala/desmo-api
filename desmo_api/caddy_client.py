import asyncio
import os
from typing import Any, Dict, Iterable, List, Optional

import aiohttp
from pydantic import BaseModel

from .models import PrisonInfo


class Matcher(BaseModel):
    host: Optional[List[str]]


class DynamicUpstream(BaseModel):
    source: str
    name: str
    port: str


class Handler(BaseModel):
    handler: str
    dynamic_upstreams: DynamicUpstream


class Route(BaseModel):
    match: List[Matcher]
    handle: List[Handler]
    terminal: bool = False
    group: Optional[str] = None


class CaddyClient:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(CaddyClient, cls).__new__(cls)
        return cls._instance

    def __init__(self, api_domain="http://localhost:2019", server_name="desmo"):
        self.api_domain = api_domain
        self.server_name = server_name
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                }
            )
        return self._session

    async def close(self):
        if self._session is not None:
            await self._session.close()
            await asyncio.sleep(0.250)

    async def _post(self, url: str, json: Dict[str, Any]):
        session = self._get_session()
        async with session.post(url=url, json=json) as resp:
            resp.raise_for_status()

    async def _initialize_config(self):
        url = f"{self.api_domain}/load"
        data = {
            "apps": {
                "http": {
                    "servers": {
                        self.server_name: {
                            "automatic_https": {"disable": True},
                            "listen": [":2015"],
                            "routes": [],
                        }
                    }
                }
            }
        }
        await self._post(url=url, json=data)

    async def ensure_server(self):
        url = f"{self.api_domain}/config/apps/http/servers/{self.server_name}"
        session = self._get_session()
        async with session.get(url=url) as resp:
            if resp.status == 400:
                await self._initialize_config()

    async def get_routes(self) -> Iterable[Route]:
        await self.ensure_server()
        url = f"{self.api_domain}/config/apps/http/servers/{self.server_name}/routes"
        session = self._get_session()
        async with session.get(url=url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if data is None:
            return []
        return [Route.model_validate(entry) for entry in data]

    async def post_route(self, route: Route, index: Optional[int] = None):
        url = f"{self.api_domain}/config/apps/http/servers/{self.server_name}/routes/"
        url_with_index = f"{url}/{index}" if index is not None else url
        print(f"posting {route.model_dump_json()} to {url}")
        await self._post(url=url_with_index, json=route.model_dump())

    async def add_or_update_prison(self, prison: PrisonInfo, http_port: int):
        route_host = f"{prison.name}.prison.{os.environ['DNS_ZONE']}"
        route_upstream = f"{prison.name}.svc.{os.environ['DNS_ZONE']}"
        routes = await self.get_routes()
        route_index: Optional[int] = None
        for index, route in enumerate(routes):
            for matcher in route.match:
                if matcher.host and route_host in matcher.host:
                    route_index = index
            if route_index is not None:
                break

        await self.post_route(
            index=route_index,
            route=Route(
                match=[Matcher(host=[route_host])],
                handle=[
                    Handler(
                        handler="reverse_proxy",
                        dynamic_upstreams=DynamicUpstream(
                            source="a", name=route_upstream, port=str(http_port)
                        ),
                    )
                ],
                terminal=True,
            ),
        )

    async def delete_route(self, index: int):
        url = f"{self.api_domain}/config/apps/http/servers/{self.server_name}/routes/{index}"
        session = self._get_session()
        async with session.delete(url) as resp:
            resp.raise_for_status()

    async def delete_prison(self, prison: PrisonInfo):
        route_host = f"{prison.name}.prison.{os.environ['DNS_ZONE']}"
        routes = await self.get_routes()
        for index, route in enumerate(routes):
            for matcher in route.match:
                if matcher.host == route_host:
                    await self.delete_route(index)
                    return
