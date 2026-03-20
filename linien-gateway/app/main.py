from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any, List

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from linien_client.device import Device
from linien_common.influxdb import InfluxDBCredentials

from . import device_store, group_store
from .device_config_store import (
    CONFIG_AUTO_LOCK_SCAN,
    CONFIG_AUTO_RELOCK,
    CONFIG_LOCK_INDICATOR,
    DeviceConfigStore,
)
from .schemas import (
    AutoRelockConfig,
    AutoRelockEnabledUpdate,
    AutoRelockState,
    AutoLockScanResult,
    AutoLockScanSettings,
    DeviceIn,
    DeviceOut,
    DevicePatch,
    GroupIn,
    GroupOut,
    GroupPatch,
    InfluxCredentials,
    LogTailResponse,
    LoggingParamUpdate,
    LoggingStart,
    LockIndicatorConfig,
    ParamUpdate,
    PostgresManualLockConfig,
    PostgresManualLockState,
    RangeSelection,
    StopTask,
)
from .log_store import LogStore
from .manual_lock_postgres import LockResultPostgresService
from .path_utils import find_repo_root
from .session import DeviceSession
from .session_registry import SessionRegistry
from .serializers import UNSERIALIZABLE, to_jsonable
from .stream import WebsocketManager

app = FastAPI(title="Linien Gateway")
manager = WebsocketManager()
lock_result_postgres = LockResultPostgresService()
log_store = LogStore(max_entries=10_000, max_age_s=24.0 * 60.0 * 60.0)
device_config_store = DeviceConfigStore()
session_registry = SessionRegistry()
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _remove_rotating_file_handlers(logger_name: str) -> None:
    logger = logging.getLogger(logger_name)
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - best effort logger cleanup
                logging.getLogger(__name__).debug(
                    "Failed closing rotating handler for logger=%s",
                    logger_name,
                    exc_info=True,
                )


@app.on_event("startup")
async def on_startup() -> None:
    _remove_rotating_file_handlers("linien_client")
    _remove_rotating_file_handlers("linien_common")
    logging.getLogger("linien_client").setLevel(logging.WARNING)
    logging.getLogger("linien_common").setLevel(logging.WARNING)
    logging.getLogger("linien_client.deploy").setLevel(logging.WARNING)
    loop = asyncio.get_running_loop()
    manager.set_loop(loop)
    log_store.set_loop(loop)
    if hasattr(lock_result_postgres, "set_event_callback"):
        lock_result_postgres.set_event_callback(
            lambda level, source, code, message, details: _emit_log(
                level=level,
                source=source,
                code=code,
                message=message,
                device_key=None,
                details=details,
            )
        )
    logging.getLogger("linien_client.device").setLevel(logging.WARNING)
    logging.getLogger("linien_client.device").propagate = False
    logging.getLogger("linien_client.connection").setLevel(logging.WARNING)
    logging.getLogger("linien_client.connection").propagate = False
    logging.getLogger(__name__).info(
        "UI dist=%s exists=%s assets_exists=%s",
        WEB_DIST_DIR,
        WEB_DIST_DIR.exists(),
        ASSETS_DIR.exists(),
    )
    if hasattr(lock_result_postgres, "start"):
        lock_result_postgres.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if hasattr(lock_result_postgres, "stop"):
        lock_result_postgres.stop()


def _resolve_web_dist_dir() -> Path:
    repo_root = find_repo_root(Path(__file__))
    if repo_root is not None:
        return repo_root / "linien-web" / "dist"
    return Path(__file__).resolve().parents[2] / "linien-web" / "dist"


WEB_DIST_DIR = _resolve_web_dist_dir()
ASSETS_DIR = WEB_DIST_DIR / "assets"


def _ui_status_payload() -> dict:
    if ASSETS_DIR.exists():
        assets = sorted([item.name for item in ASSETS_DIR.iterdir() if item.is_file()])
    else:
        assets = []
    return {
        "dist_dir": str(WEB_DIST_DIR),
        "dist_exists": WEB_DIST_DIR.exists(),
        "index_exists": (WEB_DIST_DIR / "index.html").exists(),
        "assets_count": len(assets),
        "assets_sample": assets[:5],
    }


@app.get("/__ui/status")
def ui_status() -> dict:
    return _ui_status_payload()


@app.get("/api/ui/status")
def ui_status_api() -> dict:
    return _ui_status_payload()


def _emit_log(
    level: int,
    source: str,
    code: str,
    message: str,
    device_key: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    log_store.emit(
        level=level,
        source=source,
        code=code,
        message=message,
        device_key=device_key,
        details=details,
    )


def _publish_config_update(device_key: str, config_name: str, value: dict) -> None:
    manager.publish(
        device_key,
        {
            "type": "config_update",
            "config_name": config_name,
            "value": value,
        },
    )


def _persist_config_block(device: Device, config_name: str, value: dict) -> None:
    normalized = _normalize_config_payload(config_name, value)
    parameters = device.parameters if isinstance(device.parameters, dict) else {}
    parameters[config_name] = normalized
    device.parameters = parameters
    device_store.save_device(device)
    device_config_store.set_config(device.key, config_name, normalized)


def _persist_influx_logging_state(device: Device, value: dict) -> None:
    parameters = device.parameters if isinstance(device.parameters, dict) else {}
    parameters["influx_logging_state"] = {
        "enabled": bool(value.get("enabled", False))
    }
    device.parameters = parameters
    device_store.save_device(device)


def _publish_status_update(device_key: str, session: DeviceSession) -> None:
    manager.publish(
        device_key,
        {
            "type": "status",
            **session.status(),
        },
    )


def _seed_config_store_from_device(device: Device) -> None:
    parameters = device.parameters if isinstance(device.parameters, dict) else {}
    if not parameters:
        return
    existing = device_config_store.get_device_configs(device.key)
    for config_name in (
        CONFIG_AUTO_LOCK_SCAN,
        CONFIG_LOCK_INDICATOR,
        CONFIG_AUTO_RELOCK,
    ):
        if config_name in existing:
            continue
        raw = parameters.get(config_name)
        if isinstance(raw, dict):
            try:
                normalized = _normalize_config_payload(config_name, raw)
            except HTTPException:
                logger.warning(
                    "Skipping invalid persisted config block device=%s config=%s",
                    device.key,
                    config_name,
                )
                continue
            device_config_store.set_config(device.key, config_name, normalized)


def _normalize_config_payload(config_name: str, value: dict) -> dict:
    if config_name == CONFIG_LOCK_INDICATOR:
        return LockIndicatorConfig.model_validate(value).model_dump()
    if config_name == CONFIG_AUTO_LOCK_SCAN:
        return AutoLockScanSettings.model_validate(value).model_dump()
    if config_name == CONFIG_AUTO_RELOCK:
        return AutoRelockConfig.model_validate(value).model_dump()
    raise HTTPException(status_code=422, detail=f"Unknown config name: {config_name}")


def _get_device_or_404(key: str) -> Device:
    device = device_store.get_device(key)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


def _run_session_action(action) -> dict:
    try:
        action()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


def _session_for_device(device: Device) -> DeviceSession:
    _seed_config_store_from_device(device)
    if device_config_store.apply_configs_to_device(device):
        device_store.save_device(device)
    with session_registry.lock_for(device.key):
        session = session_registry.get_or_create(
            device.key,
            lambda: DeviceSession(
                device,
                manager,
                lock_result_postgres,
                log_event_callback=_emit_log,
            ),
        )
        session.device = device
        session.set_log_event_callback(_emit_log)
        session.sync_configs_from_device()
        return session


def _get_session(key: str) -> DeviceSession:
    device = _get_device_or_404(key)
    return _session_for_device(device)


@app.get("/api/devices", response_model=List[DeviceOut])
def list_devices() -> List[DeviceOut]:
    devices = device_store.list_devices()
    return [DeviceOut(**device.__dict__) for device in devices]


@app.post("/api/devices", response_model=DeviceOut)
def create_device(payload: DeviceIn) -> DeviceOut:
    data = payload.model_dump()
    if data.get("key") is None:
        data.pop("key", None)
    device = Device(**data)
    if device_store.get_device(device.key) is not None:
        raise HTTPException(status_code=409, detail="Device key already exists")
    device_store.save_device(device)
    group_store.add_device_to_auto_groups(device.key)
    return DeviceOut(**device.__dict__)


@app.patch("/api/devices/{key}", response_model=DeviceOut)
def update_device(key: str, payload: DevicePatch) -> DeviceOut:
    device = _get_device_or_404(key)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(device, field, value)
    device_store.save_device(device)
    session = _session_for_device(device)
    session.device = device
    return DeviceOut(**device.__dict__)


@app.delete("/api/devices/{key}")
def delete_device(key: str) -> dict:
    device = _get_device_or_404(key)
    with session_registry.lock_for(key):
        session = session_registry.remove(key)
        if session is not None:
            session.disconnect()
    device_store.remove_device(device)
    device_config_store.remove_device(key)
    group_store.remove_device_from_groups(key)
    return {"ok": True}


@app.get("/api/groups", response_model=List[GroupOut])
def list_groups() -> List[GroupOut]:
    devices = device_store.list_devices()
    groups = group_store.list_groups([device.key for device in devices])
    return [
        GroupOut(
            key=group.key,
            name=group.name,
            device_keys=group.device_keys,
            auto_include=group.auto_include,
        )
        for group in groups
    ]


@app.post("/api/groups", response_model=GroupOut)
def create_group(payload: GroupIn) -> GroupOut:
    devices = device_store.list_devices()
    valid_keys = {device.key for device in devices}
    device_keys = [key for key in payload.device_keys if key in valid_keys]
    group = group_store.create_group(
        payload.name, device_keys, auto_include=payload.auto_include
    )
    return GroupOut(
        key=group.key,
        name=group.name,
        device_keys=group.device_keys,
        auto_include=group.auto_include,
    )


@app.patch("/api/groups/{key}", response_model=GroupOut)
def update_group(key: str, payload: GroupPatch) -> GroupOut:
    devices = device_store.list_devices()
    valid_keys = {device.key for device in devices}
    data = payload.model_dump(exclude_unset=True)
    device_keys = data.get("device_keys")
    if device_keys is not None:
        data["device_keys"] = [item for item in device_keys if item in valid_keys]
    try:
        group = group_store.update_group(key, **data)
    except KeyError:
        raise HTTPException(status_code=404, detail="Group not found")
    return GroupOut(
        key=group.key,
        name=group.name,
        device_keys=group.device_keys,
        auto_include=group.auto_include,
    )


@app.delete("/api/groups/{key}")
def delete_group(key: str) -> dict:
    group_store.delete_group(key)
    return {"ok": True}


@app.post("/api/devices/{key}/connect")
def connect_device(key: str) -> dict:
    session = _get_session(key)
    with session_registry.lock_for(key):
        session.connect_async()
    return {"ok": True}


@app.post("/api/devices/{key}/disconnect")
def disconnect_device(key: str) -> dict:
    session = _get_session(key)
    with session_registry.lock_for(key):
        session.disconnect()
    return {"ok": True}


@app.get("/api/devices/{key}/status")
def device_status(key: str) -> dict:
    with session_registry.lock_for(key):
        session = _get_session(key)
        return session.status()


@app.get("/api/devices/{key}/params")
def device_params(key: str) -> list:
    with session_registry.lock_for(key):
        session = _get_session(key)
        return session.param_metadata()


@app.patch("/api/devices/{key}/params/{name}")
def set_parameter(key: str, name: str, payload: ParamUpdate) -> dict:
    session = _get_session(key)
    try:
        session.set_param(name, payload.value, payload.write_registers)
    except AttributeError:
        raise HTTPException(status_code=404, detail="Parameter not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/write_registers")
def write_registers(key: str) -> dict:
    session = _get_session(key)
    return _run_session_action(session.write_registers)


@app.post("/api/devices/{key}/control/start_server")
def start_server(key: str) -> dict:
    session = _get_session(key)
    with session_registry.lock_for(key):
        session.start_server()
    return {"ok": True}


@app.post("/api/devices/{key}/control/start_lock")
def start_lock(key: str) -> dict:
    session = _get_session(key)
    try:
        session.start_lock()
    except ValueError as exc:
        _emit_log(
            level=logging.ERROR,
            source="lock",
            code="manual_lock_failed",
            message="Manual lock failed.",
            device_key=key,
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        _emit_log(
            level=logging.ERROR,
            source="lock",
            code="manual_lock_failed",
            message="Manual lock failed.",
            device_key=key,
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=409, detail=str(exc))
    _emit_log(
        level=logging.INFO,
        source="lock",
        code="manual_lock_attempt",
        message="Manual lock command accepted.",
        device_key=key,
    )
    try:
        device = device_store.get_device(key)
        device_name = device.name if device is not None else key
        row = session.build_manual_lock_row(
            device_name=device_name,
            device_key=key,
            lock_source="manual_lock",
        )
        enqueued = lock_result_postgres.enqueue_lock_result(row)
        if not enqueued:
            details: dict[str, Any] = {"error": "enqueue_rejected"}
            if hasattr(lock_result_postgres, "get_state"):
                try:
                    state = lock_result_postgres.get_state()
                    status = state.get("status", {}) if isinstance(state, dict) else {}
                    if isinstance(status, dict):
                        details["last_error"] = status.get("last_error")
                        details["active"] = status.get("active")
                except Exception:
                    logger.debug("Failed reading postgres state after manual enqueue rejection", exc_info=True)
            _emit_log(
                level=logging.WARNING,
                source="postgres",
                code="lock_result_postgres_enqueue_rejected",
                message="Lock-result postgres enqueue rejected.",
                device_key=key,
                details=details,
            )
    except Exception as exc:  # noqa: BLE001 - best effort logging hook
        logger.warning(
            "Lock-result postgres enqueue failed device=%s",
            key,
            exc_info=True,
        )
        _emit_log(
            level=logging.WARNING,
            source="postgres",
            code="lock_result_postgres_enqueue_failed",
            message="Lock-result postgres enqueue failed.",
            device_key=key,
            details={"error": str(exc)},
        )
    return {"ok": True}


@app.get("/api/devices/{key}/lock-indicator")
def get_lock_indicator_config(key: str) -> dict:
    session = _get_session(key)
    return session.get_lock_indicator_config()


@app.put("/api/devices/{key}/lock-indicator")
def update_lock_indicator_config(key: str, payload: LockIndicatorConfig) -> dict:
    device = _get_device_or_404(key)
    session = _session_for_device(device)
    saved_config = session.update_lock_indicator_config(payload.model_dump())
    _persist_config_block(device, CONFIG_LOCK_INDICATOR, saved_config)
    _publish_config_update(device.key, CONFIG_LOCK_INDICATOR, saved_config)
    return saved_config


@app.post("/api/devices/{key}/control/start_sweep")
def start_sweep(key: str) -> dict:
    session = _get_session(key)
    return _run_session_action(session.start_sweep)


@app.post("/api/devices/{key}/control/start_autolock")
def start_autolock(key: str, payload: RangeSelection) -> dict:
    session = _get_session(key)
    return _run_session_action(lambda: session.start_autolock(payload.x0, payload.x1))


@app.post(
    "/api/devices/{key}/control/auto_lock_scan",
    response_model=AutoLockScanResult,
)
def auto_lock_scan(key: str, payload: AutoLockScanSettings) -> dict:
    device = _get_device_or_404(key)
    session = _session_for_device(device)
    settings_payload = session.update_auto_lock_scan_settings(payload.model_dump())
    _persist_config_block(device, CONFIG_AUTO_LOCK_SCAN, settings_payload)
    _publish_config_update(device.key, CONFIG_AUTO_LOCK_SCAN, settings_payload)
    _emit_log(
        level=logging.INFO,
        source="auto_lock_scan",
        code="auto_lock_scan_attempt",
        message="Auto-lock from scan requested.",
        device_key=key,
    )
    try:
        result = session.auto_lock_from_scan(settings_payload)
    except RuntimeError as exc:
        _emit_log(
            level=logging.ERROR,
            source="auto_lock_scan",
            code="auto_lock_scan_failed",
            message="Auto-lock from scan failed.",
            device_key=key,
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        _emit_log(
            level=logging.ERROR,
            source="auto_lock_scan",
            code="auto_lock_scan_failed",
            message="Auto-lock from scan failed.",
            device_key=key,
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=422, detail=str(exc))
    _emit_log(
        level=logging.INFO,
        source="auto_lock_scan",
        code="auto_lock_scan_started",
        message="Auto-lock from scan started.",
        device_key=key,
        details={
            "target_voltage": result.get("target_voltage"),
            "target_index": result.get("target_index"),
            "score": result.get("score"),
        },
    )
    try:
        device = device_store.get_device(key)
        device_name = device.name if device is not None else key
        row = session.build_manual_lock_row(
            device_name=device_name,
            device_key=key,
            lock_source="auto_lock_scan",
        )
        enqueued = lock_result_postgres.enqueue_lock_result(row)
        if not enqueued:
            details: dict[str, Any] = {"error": "enqueue_rejected"}
            if hasattr(lock_result_postgres, "get_state"):
                try:
                    state = lock_result_postgres.get_state()
                    status = state.get("status", {}) if isinstance(state, dict) else {}
                    if isinstance(status, dict):
                        details["last_error"] = status.get("last_error")
                        details["active"] = status.get("active")
                except Exception:
                    logger.debug("Failed reading postgres state after autolock enqueue rejection", exc_info=True)
            _emit_log(
                level=logging.WARNING,
                source="postgres",
                code="auto_lock_scan_postgres_enqueue_rejected",
                message="Auto-lock postgres enqueue rejected.",
                device_key=key,
                details=details,
            )
    except Exception:  # noqa: BLE001 - best effort logging hook
        logger.warning(
            "Auto-lock postgres enqueue failed device=%s",
            key,
            exc_info=True,
        )
        _emit_log(
            level=logging.WARNING,
            source="postgres",
            code="auto_lock_scan_postgres_enqueue_failed",
            message="Auto-lock postgres enqueue failed.",
            device_key=key,
            details={"error": str(exc)},
        )
    return result


@app.get("/api/devices/{key}/auto-lock-scan-settings", response_model=AutoLockScanSettings)
def get_auto_lock_scan_settings(key: str) -> dict:
    session = _get_session(key)
    return session.get_auto_lock_scan_settings()


@app.put("/api/devices/{key}/auto-lock-scan-settings", response_model=AutoLockScanSettings)
def update_auto_lock_scan_settings(key: str, payload: AutoLockScanSettings) -> dict:
    device = _get_device_or_404(key)
    session = _session_for_device(device)
    settings_payload = session.update_auto_lock_scan_settings(payload.model_dump())
    _persist_config_block(device, CONFIG_AUTO_LOCK_SCAN, settings_payload)
    _publish_config_update(device.key, CONFIG_AUTO_LOCK_SCAN, settings_payload)
    return settings_payload


@app.get("/api/devices/{key}/auto-relock", response_model=AutoRelockState)
def get_auto_relock_state(key: str) -> dict:
    session = _get_session(key)
    return session.get_auto_relock_state()


@app.put("/api/devices/{key}/auto-relock", response_model=AutoRelockState)
def update_auto_relock_state(key: str, payload: AutoRelockConfig) -> dict:
    device = _get_device_or_404(key)
    session = _session_for_device(device)
    next_state = session.update_auto_relock_config(payload.model_dump())
    config_payload = next_state.get("config", {})
    _persist_config_block(device, CONFIG_AUTO_RELOCK, config_payload)
    _publish_config_update(device.key, CONFIG_AUTO_RELOCK, config_payload)
    return next_state


@app.put("/api/devices/{key}/auto-relock/enabled", response_model=AutoRelockState)
def update_auto_relock_enabled(key: str, payload: AutoRelockEnabledUpdate) -> dict:
    device = _get_device_or_404(key)
    session = _session_for_device(device)
    next_state = session.set_auto_relock_enabled(payload.enabled)
    config_payload = next_state.get("config", {})
    _persist_config_block(device, CONFIG_AUTO_RELOCK, config_payload)
    _publish_config_update(device.key, CONFIG_AUTO_RELOCK, config_payload)
    return next_state


@app.post("/api/devices/{key}/control/start_optimization")
def start_optimization(key: str, payload: RangeSelection) -> dict:
    session = _get_session(key)
    return _run_session_action(lambda: session.start_optimization(payload.x0, payload.x1))


@app.post("/api/devices/{key}/control/start_pid_optimization")
def start_pid_optimization(key: str) -> dict:
    session = _get_session(key)
    return _run_session_action(session.start_pid_optimization)


@app.post("/api/devices/{key}/control/stop_lock")
def stop_lock(key: str) -> dict:
    session = _get_session(key)
    return _run_session_action(session.stop_lock)


@app.post("/api/devices/{key}/control/stop_task")
def stop_task(key: str, payload: StopTask) -> dict:
    session = _get_session(key)
    return _run_session_action(lambda: session.stop_task(payload.use_new_parameters))


@app.post("/api/devices/{key}/control/shutdown_server")
def shutdown_server(key: str) -> dict:
    session = _get_session(key)
    return _run_session_action(session.shutdown_server)


@app.post("/api/devices/{key}/logging/start")
def start_logging(key: str, payload: LoggingStart) -> dict:
    device = _get_device_or_404(key)
    session = _session_for_device(device)
    try:
        state = session.logging_start(payload.interval)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _persist_influx_logging_state(device, state)
    _publish_status_update(device.key, session)
    return {"ok": True}


@app.post("/api/devices/{key}/logging/stop")
def stop_logging(key: str) -> dict:
    device = _get_device_or_404(key)
    session = _session_for_device(device)
    try:
        state = session.logging_stop()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _persist_influx_logging_state(device, state)
    _publish_status_update(device.key, session)
    return {"ok": True}


@app.patch("/api/devices/{key}/logging/param/{name}")
def update_logging_param(key: str, name: str, payload: LoggingParamUpdate) -> dict:
    session = _get_session(key)
    try:
        session.logging_set_param(name, payload.enabled)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.get("/api/devices/{key}/logging/credentials")
def get_logging_credentials(key: str) -> dict:
    session = _get_session(key)
    try:
        credentials = session.logging_get_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "url": credentials.url,
        "org": credentials.org,
        "token": credentials.token,
        "bucket": credentials.bucket,
        "measurement": credentials.measurement,
    }


@app.put("/api/devices/{key}/logging/credentials")
def update_logging_credentials(key: str, payload: InfluxCredentials) -> dict:
    session = _get_session(key)
    try:
        success, message = session.logging_update_credentials(
            InfluxDBCredentials(**payload.model_dump())
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"success": success, "message": message}


@app.get("/api/postgres/manual-lock", response_model=PostgresManualLockState)
def get_postgres_manual_lock_state() -> dict:
    return lock_result_postgres.get_state()


@app.put("/api/postgres/manual-lock", response_model=PostgresManualLockState)
def update_postgres_manual_lock_state(payload: PostgresManualLockConfig) -> dict:
    return lock_result_postgres.update_config(payload.model_dump())


@app.post("/api/postgres/manual-lock/test")
def test_postgres_manual_lock_state() -> dict:
    ok, detail = lock_result_postgres.test_connection()
    return {
        "ok": ok,
        "detail": detail,
        "state": lock_result_postgres.get_state(),
    }


@app.get("/api/logs/tail", response_model=LogTailResponse)
def get_logs_tail(limit: int = 500) -> dict:
    safe_limit = max(1, min(int(limit), 5_000))
    return {"entries": log_store.tail(limit=safe_limit)}


@app.delete("/api/logs")
def clear_logs() -> dict:
    return {"ok": True, "cleared": log_store.clear()}


@app.websocket("/api/logs/stream")
async def stream_logs(websocket: WebSocket) -> None:
    await websocket.accept()
    q = log_store.subscribe(maxsize=500)
    try:
        while True:
            payload = await q.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    finally:
        log_store.unsubscribe(q)


@app.websocket("/api/devices/{key}/stream")
async def stream_device(websocket: WebSocket, key: str) -> None:
    with session_registry.lock_for(key):
        session = _get_session(key)
        snapshot = session.snapshot()
    max_fps = None
    raw_max = websocket.query_params.get('max_fps')
    if raw_max is not None:
        try:
            max_fps = float(raw_max)
            if max_fps <= 0:
                max_fps = None
        except ValueError:
            max_fps = None
    await manager.register(key, websocket, max_fps=max_fps)

    async def safe_send(payload: dict) -> bool:
        try:
            await websocket.send_json(payload)
            return True
        except WebSocketDisconnect:
            await manager.unregister(key, websocket)
            return False

    for name, value in snapshot.get("params", {}).items():
        encoded = to_jsonable(value)
        if encoded is UNSERIALIZABLE:
            continue
        if not await safe_send({"type": "param_update", "name": name, "value": encoded}):
            return
    if snapshot.get("plot_frame") is not None:
        if not await safe_send(snapshot["plot_frame"]):
            return
    if not await safe_send({"type": "status", **snapshot.get("status", {})}):
        return

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.unregister(key, websocket)


app.mount("/assets", StaticFiles(directory=ASSETS_DIR, check_dir=False), name="assets")


def _index_file_response():
    index_file = WEB_DIST_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return PlainTextResponse("UI not built", status_code=404)


@app.get("/", include_in_schema=False)
def serve_root():
    return _index_file_response()


@app.get("/{path:path}", include_in_schema=False)
def serve_spa(path: str):
    if path.startswith("api") or path.startswith("docs") or path == "openapi.json":
        raise HTTPException(status_code=404, detail="Not Found")
    return _index_file_response()


def main() -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
