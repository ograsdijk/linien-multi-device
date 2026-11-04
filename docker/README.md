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

It is generated from the gateway's schema definition (do not edit it by hand) and
creates the `linien` schema with `linien.pdh_lock_results` (one row per lock-acquisition
attempt) and `linien.pdh_lock_schema` (its schema version). Docker runs init scripts
only on an empty data volume. On an existing volume the gateway creates the same schema
itself on first use, and refuses to write to a table of another version. See
[Lock-acquisition records](../README.md#lock-acquisition-records-optional-postgresql).

The gateway's default Postgres password matches this container (`adminpassword`). The
API never returns the stored password; leave the password field blank to keep it.
