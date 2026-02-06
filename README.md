# Linien Multi-Device

Monorepo with a FastAPI gateway and a React (Vite) web UI.

## Structure
- linien-gateway: FastAPI backend
- linien-web: React UI (Vite)

## Prerequisites
- Python 3.10+
- Node.js 18+

## Development

### Port configuration
Edit config.json at the repo root to change ports:

- apiHost: FastAPI bind host (use 0.0.0.0 to allow LAN access)
- apiPort: FastAPI port
- webDevPort: Vite dev server port

### Backend (FastAPI)
```powershell
python .\linien-gateway\run.py
```

### Frontend (Vite)
```powershell
cd linien-web
npm install
npm run dev
```

The dev server runs on http://localhost:5175 by default.
If you need the UI to talk to a gateway on another host while using the dev server,
set VITE_API_URL (for example, http://192.168.1.10:8000/api).

## Serve pre-built UI from FastAPI
Build the UI, then run the gateway. If a build exists in linien-web/dist, FastAPI will serve it on the same port as the API.

```powershell
cd linien-web
npm install
npm run build

cd ..\linien-gateway
python -m uvicorn app.main:app --reload
```

Open http://localhost:8000 to view the UI, and the API remains under /api.

## Docker packaging (example)
You can package the gateway + prebuilt UI in a single image using a multi-stage build.

1) Create a Dockerfile at the repo root with this content:

```dockerfile
FROM node:18-alpine AS web
WORKDIR /app/linien-web
COPY linien-web/package.json linien-web/package-lock.json ./
RUN npm ci
COPY linien-web/ ./
RUN npm run build

FROM python:3.11-slim AS app
WORKDIR /app
COPY linien-gateway/ ./linien-gateway/
COPY --from=web /app/linien-web/dist ./linien-web/dist
RUN pip install --no-cache-dir -e ./linien-gateway
EXPOSE 8000
CMD ["python", "linien-gateway/run.py"]
```

2) Build and run:

```powershell
docker build -t linien-multi-device .
docker run --rm -p 8000:8000 -v ${PWD}\config.json:/app/config.json:ro linien-multi-device
```

The UI will be served at http://localhost:8000/ and the API under /api.
