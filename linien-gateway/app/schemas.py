from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class DeviceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Optional[str] = None
    name: str = ""
    host: str = ""
    port: int = 18862
    username: str = ""
    password: str = ""
    parameters: Dict[str, Any] = Field(default_factory=dict)


class DeviceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    name: str
    host: str
    port: int
    username: str
    password: str
    parameters: Dict[str, Any]


class DevicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None


class ParamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any
    write_registers: bool = True


class RangeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x0: int
    x1: int


class LoggingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval: float


class LoggingParamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class InfluxCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    org: str
    token: str
    bucket: str
    measurement: str


class StopTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    use_new_parameters: bool = False


class GroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    device_keys: list[str] = Field(default_factory=list)
    auto_include: bool = False


class GroupOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    name: str
    device_keys: list[str]
    auto_include: bool = False


class GroupPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    device_keys: Optional[list[str]] = None
    auto_include: Optional[bool] = None

