from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    x0: int = Field(ge=0, le=2047)
    x1: int = Field(ge=0, le=2047)


class AutoLockScanSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    half_range_v: float = Field(default=0.08, ge=0.0, le=2.0)
    crossing_max_v: float = Field(default=0.03, ge=0.0, le=2.0)
    error_min: float = Field(default=0.08, ge=0.0, le=4.0)
    symmetry_min: float = Field(default=0.2, ge=0.0, le=1.0)
    allow_single_side: bool = False
    single_error_min: float = Field(default=0.1, ge=0.0, le=4.0)
    smooth_window_pts: int = Field(default=5, ge=1, le=301)
    use_monitor: bool = False
    monitor_contrast_min_v: float = Field(default=0.03, ge=0.0, le=4.0)


class AutoLockScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_index: int
    target_voltage: float
    target_slope_rising: bool
    score: float
    center_abs_v: float
    left_excursion_v: float
    right_excursion_v: float
    pair_excursion_v: float
    symmetry: float
    monitor_contrast_v: Optional[float] = None
    detail: Optional[str] = None


class AutoRelockConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    trigger_hold_s: float = Field(default=0.8, ge=0.05, le=120.0)
    verify_hold_s: float = Field(default=1.2, ge=0.05, le=120.0)
    cooldown_s: float = Field(default=8.0, ge=0.0, le=600.0)
    unlocked_trace_timeout_s: float = Field(default=2.0, ge=0.1, le=120.0)
    max_attempts: int = Field(default=2, ge=1, le=50)


class AutoRelockStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    state: Literal["idle", "lost_pending", "waiting_unlocked_trace", "verifying", "cooldown"] = "idle"
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=2, ge=1)
    cooldown_remaining_s: float = Field(default=0.0, ge=0.0)
    last_trigger_at: Optional[float] = None
    last_attempt_at: Optional[float] = None
    last_success_at: Optional[float] = None
    last_failure_at: Optional[float] = None
    last_error: Optional[str] = None


class AutoRelockState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: AutoRelockConfig
    status: AutoRelockStatus


class AutoRelockEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class LockIndicatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    bad_hold_s: float = Field(default=1.0, ge=0.05, le=120.0)
    good_hold_s: float = Field(default=2.0, ge=0.05, le=120.0)
    use_control: bool = True
    control_stuck_delta_counts: int = Field(default=0, ge=0)
    control_stuck_time_s: float = Field(default=1.5, ge=0.05, le=120.0)
    control_rail_threshold_v: float = Field(default=0.9, ge=0.0, le=2.0)
    control_rail_hold_s: float = Field(default=1.0, ge=0.05, le=120.0)
    use_error: bool = True
    error_mean_abs_max_v: float = Field(default=0.2, ge=0.0, le=5.0)
    error_std_min_v: float = Field(default=0.001, ge=0.0, le=5.0)
    error_std_max_v: float = Field(default=0.8, ge=0.0, le=5.0)
    use_monitor: bool = False
    monitor_mode: Literal["locked_above", "locked_below"] = "locked_above"
    monitor_threshold_v: float = 0.0

    @model_validator(mode="after")
    def _validate_error_std_bounds(self):
        if self.error_std_max_v < self.error_std_min_v:
            raise ValueError("error_std_max_v must be >= error_std_min_v")
        return self


class LoggingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval: float = Field(ge=0.1, le=3600.0)


class LoggingParamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class LoggingParamsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    names: list[str] = Field(default_factory=list)


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


class PostgresManualLockConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = "experiment_db"
    user: str = "admin"
    password: str = "adminpassword"
    sslmode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    connect_timeout_s: float = Field(default=3.0, ge=0.1, le=120.0)


class PostgresManualLockStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool = False
    last_test_ok: Optional[bool] = None
    last_test_at: Optional[float] = None
    last_write_ok: Optional[bool] = None
    last_write_at: Optional[float] = None
    last_error: Optional[str] = None
    enqueued_count: int = 0
    write_ok_count: int = 0
    write_error_count: int = 0
    dropped_count: int = 0
    queue_size: int = 0


class PostgresManualLockState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: PostgresManualLockConfig
    status: PostgresManualLockStatus


class LogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    ts: float
    level: int = Field(ge=0)
    level_name: str
    device_key: Optional[str] = None
    source: str
    code: Optional[str] = None
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class LogTailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[LogEntry]

