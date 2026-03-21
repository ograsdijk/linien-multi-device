# Docker Notes

This folder contains optional local Docker stacks for Linien Multi-Device.

## Gateway + Web UI Stack

Compose file:

- `docker/linien-multi-device/docker-compose.yml`

Start:

```powershell
docker compose -f docker/linien-multi-device/docker-compose.yml up -d --build
```

Stop:

```powershell
docker compose -f docker/linien-multi-device/docker-compose.yml down
```

Service:

- Linien Multi-Device UI + API: `http://localhost:8000`

Details:

- See `docker/linien-multi-device/README.md`

## Postgres + pgAdmin Stack

Compose file:

- `docker/postgres/docker-compose.yml`

Start:

```powershell
docker compose -f docker/postgres/docker-compose.yml up -d
```

Stop:

```powershell
docker compose -f docker/postgres/docker-compose.yml down
```

Services:

- Postgres: `localhost:5432`
- pgAdmin: `http://localhost:5050`

Default credentials in this stack:

- Postgres database: `experiment_db`
- Postgres user: `admin`
- Postgres password: `adminpassword`
- pgAdmin login: `admin@example.com`
- pgAdmin password: `adminpassword`

Initialization SQL:

- `docker/postgres/postgres-init/01-init.sql`

This SQL creates/updates the `pdh_lock_results` table used by gateway lock logging.
