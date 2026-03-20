# Docker Notes

This folder contains optional local infrastructure for Linien Multi-Device.

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
