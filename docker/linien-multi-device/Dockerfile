FROM node:20-bookworm-slim AS web-build

WORKDIR /build/linien-web
COPY linien-web/package.json linien-web/package-lock.json ./
RUN npm ci
COPY linien-web/ ./
RUN npm run build

FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /opt/linien-multi-device

COPY linien-gateway/ /opt/linien-multi-device/linien-gateway/
COPY config.json /opt/linien-multi-device/config.json
COPY --from=web-build /build/linien-web/dist /opt/linien-multi-device/linien-web/dist

RUN pip install --upgrade pip \
    && pip install ./linien-gateway

WORKDIR /opt/linien-multi-device/linien-gateway

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
