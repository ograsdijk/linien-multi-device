from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Dict, List

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from linien_client.device import Device
from linien_common.influxdb import InfluxDBCredentials

from . import device_store, group_store
from .schemas import (
    DeviceIn,
    DeviceOut,
    DevicePatch,
    GroupIn,
    GroupOut,
    GroupPatch,
    InfluxCredentials,
    LoggingParamUpdate,
    LoggingStart,
    ParamUpdate,
    RangeSelection,
    StopTask,
)
from .session import DeviceSession
from .serializers import UNSERIALIZABLE, to_jsonable
from .stream import WebsocketManager

app = FastAPI(title="Linien Gateway")
manager = WebsocketManager()
_sessions: Dict[str, DeviceSession] = {}

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
            except Exception:
                pass


@app.on_event("startup")
async def on_startup() -> None:
    _remove_rotating_file_handlers("linien_client")
    _remove_rotating_file_handlers("linien_common")
    logging.getLogger("linien_client").setLevel(logging.WARNING)
    logging.getLogger("linien_common").setLevel(logging.WARNING)
    logging.getLogger("linien_client.deploy").setLevel(logging.WARNING)
    manager.set_loop(asyncio.get_running_loop())
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


def _find_repo_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "config.json").exists() and (parent / "linien-web").exists():
            return parent
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if (parent / "config.json").exists() and (parent / "linien-web").exists():
            return parent
    return None


def _resolve_web_dist_dir() -> Path:
    repo_root = _find_repo_root()
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


def _session_for_device(device: Device) -> DeviceSession:
    if device.key not in _sessions:
        _sessions[device.key] = DeviceSession(device, manager)
    else:
        _sessions[device.key].device = device
    return _sessions[device.key]


def _get_session(key: str) -> DeviceSession:
    device = device_store.get_device(key)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
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
    device = device_store.get_device(key)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(device, field, value)
    device_store.save_device(device)
    session = _session_for_device(device)
    session.device = device
    return DeviceOut(**device.__dict__)


@app.delete("/api/devices/{key}")
def delete_device(key: str) -> dict:
    device = device_store.get_device(key)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    session = _sessions.get(key)
    if session is not None:
        session.disconnect()
        del _sessions[key]
    device_store.remove_device(device)
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
    session.connect_async()
    return {"ok": True}


@app.post("/api/devices/{key}/disconnect")
def disconnect_device(key: str) -> dict:
    session = _get_session(key)
    session.disconnect()
    return {"ok": True}


@app.get("/api/devices/{key}/status")
def device_status(key: str) -> dict:
    session = _get_session(key)
    return session.status()


@app.get("/api/devices/{key}/params")
def device_params(key: str) -> list:
    session = _get_session(key)
    return session.param_metadata()


@app.patch("/api/devices/{key}/params/{name}")
def set_parameter(key: str, name: str, payload: ParamUpdate) -> dict:
    session = _get_session(key)
    try:
        session.set_param(name, payload.value, payload.write_registers)
    except AttributeError:
        raise HTTPException(status_code=404, detail="Parameter not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/write_registers")
def write_registers(key: str) -> dict:
    session = _get_session(key)
    try:
        session.write_registers()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/start_server")
def start_server(key: str) -> dict:
    session = _get_session(key)
    session.start_server()
    return {"ok": True}


@app.post("/api/devices/{key}/control/start_lock")
def start_lock(key: str) -> dict:
    session = _get_session(key)
    try:
        session.start_lock()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/start_sweep")
def start_sweep(key: str) -> dict:
    session = _get_session(key)
    try:
        session.start_sweep()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/start_autolock")
def start_autolock(key: str, payload: RangeSelection) -> dict:
    session = _get_session(key)
    try:
        session.start_autolock(payload.x0, payload.x1)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/start_optimization")
def start_optimization(key: str, payload: RangeSelection) -> dict:
    session = _get_session(key)
    try:
        session.start_optimization(payload.x0, payload.x1)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/start_pid_optimization")
def start_pid_optimization(key: str) -> dict:
    session = _get_session(key)
    try:
        session.start_pid_optimization()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/stop_lock")
def stop_lock(key: str) -> dict:
    session = _get_session(key)
    try:
        session.stop_lock()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/stop_task")
def stop_task(key: str, payload: StopTask) -> dict:
    session = _get_session(key)
    try:
        session.stop_task(payload.use_new_parameters)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/control/shutdown_server")
def shutdown_server(key: str) -> dict:
    session = _get_session(key)
    try:
        session.shutdown_server()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/logging/start")
def start_logging(key: str, payload: LoggingStart) -> dict:
    session = _get_session(key)
    try:
        session.logging_start(payload.interval)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/devices/{key}/logging/stop")
def stop_logging(key: str) -> dict:
    session = _get_session(key)
    try:
        session.logging_stop()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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


@app.websocket("/api/devices/{key}/stream")
async def stream_device(websocket: WebSocket, key: str) -> None:
    session = _get_session(key)
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

    snapshot = session.snapshot()
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
