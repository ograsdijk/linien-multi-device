# Linien Multi-Device Docker Stack

Standalone Docker stack for the gateway + built web UI.
This stack is intentionally separate from `docker/postgres`.

## Files

- `docker-compose.yml`: service definition for `linien-multi-device`.
- `Dockerfile`: multi-stage build (Node build for web UI + Python runtime for gateway).
- `data/config.json`: mounted gateway config.
- `data/device_settings.json`: mounted persisted device-level UI settings.

## Start

```powershell
docker compose -f docker/linien-multi-device/docker-compose.yml up -d --build
```

## Stop

```powershell
docker compose -f docker/linien-multi-device/docker-compose.yml down
```

## Access

- UI + API: `http://localhost:8000`

## Persistence

- `./data/config.json` and `./data/device_settings.json` are bind-mounted.
- Named volume `linien_user_data` persists Linien user-data content (device list, groups, integration files under user-data path).

## Notes

- Postgres is not included in this compose file.
- To use Postgres lock logging, run `docker/postgres` separately (or point to any external Postgres instance).
