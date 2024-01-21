import asyncio
import aiohttp
from typing import Dict, List, Optional

from pydantic import BaseModel


class TxtVerification(BaseModel):
    name: str
    token: str


class ZoneResponse(BaseModel):
    id: str
    created: str
    modified: str
    legacy_dns_host: str
    legacy_ns: List[str]
    name: str
    ns: List[str]
    owner: str
    paused: bool
    permission: str
    project: str
    registrar: str
    status: str
    ttl: int
    verified: str
    records_count: int
    is_secondary_dns: bool
    txt_verification: TxtVerification


class RecordResponse(BaseModel):
    type: str
    id: str
    created: str
    modified: str
    zone_id: str
    name: str
    value: str
    ttl: int | None = None


class HCloudDNS:
    def __init__(self, token: str, api_domain="dns.hetzner.com"):
        self._token = token
        self.api_domain = api_domain
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={
                    "Auth-API-Token": self._token,
                    "Content-Type": "application/json; charset=utf-8",
                }
            )
        return self._session

    async def close(self):
        if self._session is not None:
            await self._session.close()
            await asyncio.sleep(0.250)

    async def get_all_zones(self, name: Optional[str] = None) -> List[ZoneResponse]:
        session = self._get_session()
        params = {}
        if name is not None:
            params["name"] = name
        async with session.get(
            f"https://{ self.api_domain }/api/v1/zones", params=params
        ) as resp:
            resp.raise_for_status()
            return [ZoneResponse(**zone) for zone in (await resp.json())["zones"]]

    async def get_zone(self, zone_id: str) -> ZoneResponse:
        session = self._get_session()
        async with session.get(
            f"https://{ self.api_domain }/api/v1/zones/{zone_id}"
        ) as resp:
            resp.raise_for_status()
            return ZoneResponse(**(await resp.json())["zone"])

    async def get_zone_by_name(self, name: str) -> ZoneResponse:
        zones = await self.get_all_zones(name)
        if len(zones) == 0:
            raise ValueError(f"Zone '{name}' not found")
        elif len(zones) > 1:
            raise ValueError(f"Multiple zones found for '{name}'")
        else:
            return zones[0]

    async def get_all_records(self, zone_id: str) -> List[RecordResponse]:
        session = self._get_session()
        async with session.get(
            f"https://{ self.api_domain }/api/v1/records", params={"zone_id": zone_id}
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return [RecordResponse(**record) for record in data["records"]]

    async def get_record(self, record_id: str) -> RecordResponse:
        session = self._get_session()
        async with session.get(
            f"https://{ self.api_domain }/api/v1/records/{record_id}"
        ) as resp:
            resp.raise_for_status()
            return RecordResponse(**(await resp.json())["record"])

    async def get_records_by_name(
        self, zone_id: str, name: str
    ) -> List[RecordResponse]:
        records = await self.get_all_records(zone_id)
        return [record for record in records if record.name == name]

    async def delete_record(self, record_id: str) -> None:
        session = self._get_session()
        async with session.delete(
            f"https://{ self.api_domain }/api/v1/records/{record_id}"
        ) as resp:
            resp.raise_for_status()
            return None

    async def create_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: Optional[int] = None,
    ):
        session = self._get_session()
        data: Dict[str, str | int] = {
            "name": name,
            "type": record_type,
            "value": value,
            "zone_id": zone_id,
        }
        if ttl is not None:
            data["ttl"] = ttl
        async with session.post(
            f"https://{ self.api_domain }/api/v1/records", json=data
        ) as resp:
            resp.raise_for_status()
            return RecordResponse(**(await resp.json())["record"])
