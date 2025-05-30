from typing import List, Optional

from pydantic import BaseModel, validator

from .enums import JailEvent, JailState

MAX_REPLICAS = 3


class JailEventQueueObject(BaseModel):
    name: str
    event: JailEvent


class JailInfo(BaseModel):
    base: str
    name: str
    state: JailState
    ip: str
    host: str
    image_digest: str | None


class FullJailInfoResponse(JailInfo):
    dns: str


class CreateJailRequest(BaseModel):
    name: str
    image_digest: str
    base: str = "14.1-RELEASE-base"

    @validator("name")
    def server_name_validator(cls, v: str):
        if len(v) < 4 or len(v) > 32:
            raise ValueError("server_name must be between 4 and 32 characters")
        if not v.isascii():
            raise ValueError("server_name must be ascii")
        if not v.isalnum():
            raise ValueError("server_name must be alphanumeric")
        if v[0].isdigit():
            raise ValueError("server_name must not start with a number")
        return v


class CreatePrisonRequest(CreateJailRequest):
    replicas: int = 1

    @validator("replicas")
    def replicas_validator(cls, v: int):
        if v < 1 or v > MAX_REPLICAS:
            raise ValueError(f"replicas must be between 1 and {MAX_REPLICAS}")
        return v


class PrisonInfo(BaseModel):
    replicas: int
    base: str
    name: str
    image_digest: str | None


class PrisonInfoResponse(PrisonInfo):
    dns: str
    headless_svc: str
    jails: List[JailInfo] = []


class UpdatePrisonRequest(BaseModel):
    replicas: Optional[int] = None

    @validator("replicas")
    def replicas_validator(cls, v: Optional[int] = None):
        if v is not None and (v < 0 or v > MAX_REPLICAS):
            raise ValueError(f"replicas must be between 1 and {MAX_REPLICAS}")
        return v


class UpdateJailRequest(BaseModel):
    state: JailState


class CreateJailEventRequest(BaseModel):
    event: JailEvent


class DesmofileResponse(BaseModel):
    content: str
