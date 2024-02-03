from pydantic import BaseModel, validator, Field
from typing import Optional, List

MAX_REPLICAS = 3


class JailInfo(BaseModel):
    base: str
    name: str
    state: str
    ip: str
    host: str


class FullJailInfoResponse(JailInfo):
    packages: List[str] = []
    commands: List[str] = []
    dns: str


class CreateJailRequest(BaseModel):
    name: str
    base: str = "14.0-RELEASE-base"
    packages: List[str] = []
    commands: List[str] = []

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


class PrisonInfoResponse(PrisonInfo):
    dns: str
    jails: List[JailInfo] = []


class UpdatePrisonRequest(BaseModel):
    replicas: Optional[int] = None

    @validator("replicas")
    def replicas_validator(cls, v: Optional[int] = None):
        if v is not None and (v < 1 or v > MAX_REPLICAS):
            raise ValueError(f"replicas must be between 1 and {MAX_REPLICAS}")
        return v


class PrisonManifest(BaseModel):
    packages: List[str]
    build_commands: List[str] = Field(alias="build-commands")
    start_command: str = Field(alias="start-command")
