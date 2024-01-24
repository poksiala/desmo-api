from pydantic import BaseModel, ValidationError, validator
from typing import Optional


class JailInfo(BaseModel):
    base: str
    name: str
    state: str
    ip: str
    host: str


class FullJailInfoResponse(JailInfo):
    packages: list[str] = []
    commands: list[str] = []
    dns: str


class CreateJailRequest(BaseModel):
    name: str
    base: str = "14.0-RELEASE-base"
    packages: list[str] = []
    commands: list[str] = []

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


class PrisonInfo(BaseModel):
    replicas: int
    base: str
    name: str


class PrisonInfoResponse(PrisonInfo):
    dns: str
    jails: list[JailInfo] = []


class UpdatePrisonRequest(BaseModel):
    replicas: Optional[int] = None
